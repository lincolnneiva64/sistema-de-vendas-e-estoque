import io
from contextlib import redirect_stdout

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import FuncionarioForm, PixRecebidoForm
from .models import Cliente, ContaReceber, CreditoCliente, Funcionario, PixRecebido, RecebimentoContaReceber, Venda


class FuncionarioTests(TestCase):
    def test_exige_whatsapp_para_receber_checklist(self):
        funcionario = Funcionario(
            nome="Joao Entregador",
            pode_receber_checklist=True,
        )

        with self.assertRaises(ValidationError):
            funcionario.full_clean()

    def test_habilitados_para_checklist_filtra_ativos_e_marcados(self):
        habilitado = Funcionario.objects.create(
            nome="Maria Entregadora",
            telefone_whatsapp="85999990000",
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            telefone_whatsapp="85999991111",
            ativo=False,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Checklist",
            telefone_whatsapp="85999992222",
            pode_receber_checklist=False,
        )

        self.assertEqual(list(Funcionario.habilitados_para_checklist()), [habilitado])

    def test_form_valida_whatsapp_quando_checklist_marcado(self):
        form = FuncionarioForm(data={
            "nome": "Carlos Entregador",
            "telefone_whatsapp": "",
            "pode_receber_checklist": "on",
            "ativo": "on",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("telefone_whatsapp", form.errors)

    def test_view_cria_busca_edita_e_alterna_status(self):
        url = reverse("estoque:funcionarios")

        resposta_criar = self.client.post(url, data={
            "nome": "Ana Entregadora",
            "telefone_whatsapp": "85999990000",
            "pode_receber_checklist": "on",
            "ativo": "on",
            "observacoes": "Rota centro",
        }, secure=True)
        self.assertEqual(resposta_criar.status_code, 302)

        funcionario = Funcionario.objects.get(nome="Ana Entregadora")
        self.assertTrue(funcionario.ativo)
        self.assertTrue(funcionario.pode_receber_checklist)
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85999990000")

        resposta_busca = self.client.get(url, {"q": "99999"}, secure=True)
        self.assertContains(resposta_busca, "Ana Entregadora")

        resposta_editar = self.client.post(url, data={
            "funcionario_id": funcionario.id,
            "nome": "Ana Silva",
            "telefone_whatsapp": "85988887777",
            "pode_receber_checklist": "on",
            "ativo": "on",
        }, secure=True)
        self.assertEqual(resposta_editar.status_code, 302)
        funcionario.refresh_from_db()
        self.assertEqual(funcionario.nome, "Ana Silva")
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85988887777")

        resposta_status = self.client.post(url, data={
            "acao": "alternar_status",
            "funcionario_id": funcionario.id,
            "ativo": "0",
        }, secure=True)
        self.assertEqual(resposta_status.status_code, 302)
        funcionario.refresh_from_db()
        self.assertFalse(funcionario.ativo)
        self.assertFalse(funcionario.pode_receber_checklist)


class PixRecebidoTests(TestCase):
    def test_form_permite_cliente_opcional_e_status_pendente(self):
        form = PixRecebidoForm(data={
            "cliente": "",
            "nome_pagador": "Maria Silva",
            "valor": "50.00",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "observacao": "Pix recebido no caixa",
            "status": PixRecebido.STATUS_PENDENTE,
        })

        self.assertTrue(form.is_valid(), form.errors)
        pix = form.save()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)

    def test_central_pix_registra_lista_e_nao_altera_fluxo_financeiro(self):
        cliente = Cliente.objects.create(nome="Cliente Pix", ativo=True)
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }

        resposta_get = self.client.get(url, secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, "Central de Pix")

        resposta_post = self.client.post(url, data={
            "cliente": cliente.id,
            "nome_pagador": "Filho do cliente",
            "valor": "123.45",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "instituicao_pix": "PicPay",
            "observacao": "Conferir manualmente depois",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta_post.status_code, 200)
        self.assertContains(resposta_post, "Filho do cliente")
        self.assertContains(resposta_post, "PicPay")
        pix = PixRecebido.objects.get(nome_pagador="Filho do cliente")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(pix.instituicao_pix, "PicPay")

        contagens_depois = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        self.assertEqual(contagens_depois, contagens_antes)

    def test_central_pix_usa_next_interno_e_ignora_next_externo(self):
        url = reverse("estoque:central_pix")

        resposta_interna = self.client.get(f"{url}?next=/vendas/", secure=True)
        self.assertContains(resposta_interna, 'href="/vendas/"')

        resposta_externa = self.client.get(f"{url}?next=https://example.com/", secure=True)
        self.assertContains(resposta_externa, f'href="{reverse("estoque:contas_receber")}"')
        self.assertNotContains(resposta_externa, "example.com")

    def test_botoes_central_pix_alertam_somente_pix_em_atencao(self):
        cliente = Cliente.objects.create(nome="Cliente Alerta Pix", ativo=True)

        resposta_sem_pix = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertNotContains(resposta_sem_pix, "(pendente)")

        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pix baixado",
            valor="10.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        resposta_baixado = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertNotContains(resposta_baixado, "(pendente)")

        PixRecebido.objects.create(
            nome_pagador="Pix pendente",
            valor="20.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resposta_contas = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertContains(resposta_contas, "Central de Pix (pendente)")
        self.assertContains(resposta_contas, "contas-btn-pix-alerta")

        resposta_vendas = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertContains(resposta_vendas, "Central de Pix (pendente)")
        self.assertContains(resposta_vendas, "pix-alerta")

        PixRecebido.objects.filter(status=PixRecebido.STATUS_PENDENTE).update(
            status=PixRecebido.STATUS_BAIXADO
        )
        PixRecebido.objects.create(
            nome_pagador="Pix nao identificado",
            valor="30.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )
        resposta_receber = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )
        self.assertContains(resposta_receber, "Central de Pix (pendente)")
        self.assertContains(resposta_receber, "rc-btn-pix-alerta")

    def test_analisar_comprovante_pix_preenche_dados_sem_confirmar_cliente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Destino\n"
            "Nome: Loja Exemplo\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Cicero Cristiano Silva Souza")
        self.assertEqual(dados["valor"], "20.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(dados["mensagem_cliente"], "")
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_enviar_comprovante_pix_cria_registro_pendente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        arquivo = SimpleUploadedFile(
            "comprovante.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cicero Cristiano Silva Souza\n"
                "Valor R$ 20,00\n"
                "Data 16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo, "enviado_por": "Lincoln"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertIn(detalhe_url, resposta.redirect_chain[-1][0])
        self.assertIn("next=%2Fcentral-pix%2Fenviar-comprovante%2F", resposta.redirect_chain[-1][0])
        self.assertContains(resposta, "Comprovante recebido e salvo na Central de Pix.")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix_enviar_comprovante")}"')
        self.assertContains(resposta, "Enviar outro comprovante")
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.nome_pagador, "Cicero Cristiano Silva Souza")
        self.assertEqual(pix.enviado_por_nome, "Lincoln")
        self.assertEqual(str(pix.valor), "20.00")
        self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-05-16T17:30")
        self.assertEqual(pix.instituicao_pix, "Banco do Brasil")
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
        self.assertIn("Comprovante Pix", pix.texto_ocr_bruto)
        self.assertTrue(pix.comprovante)

    def test_enviar_comprovante_pix_sem_cliente_sugerido_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-cliente.txt",
            (
                "Mercado Pago\n"
                "Comprovante de Pix\n"
                "16/maio/2026 as 16:33:29\n"
                "R$ 600\n"
                "@ De\n"
                "Ivanildo Ferraz Patricio Junior\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.nome_pagador, "Ivanildo Ferraz Patricio Junior")
        self.assertEqual(str(pix.valor), "600.00")
        self.assertEqual(pix.instituicao_pix, "Mercado Pago")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_enviar_comprovante_pix_com_outro_salva_nome_informado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-outro.txt",
            (
                "Comprovante Pix\n"
                "Nome: Cliente Sem Cadastro\n"
                "Valor R$ 42,00\n"
                "Data 16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {
                "comprovante": arquivo,
                "enviado_por": "Outro",
                "enviado_por_outro": "  Ana Caixa  ",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertIn(detalhe_url, resposta.redirect_chain[-1][0])
        self.assertIn("next=%2Fcentral-pix%2Fenviar-comprovante%2F", resposta.redirect_chain[-1][0])
        self.assertEqual(pix.enviado_por_nome, "Ana Caixa")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Ana Caixa")
        self.assertContains(resposta, "Enviar outro comprovante")

    def test_enviar_comprovante_pix_sem_ocr_completo_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-ocr.txt",
            b"texto sem dados de pix",
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo, "enviado_por": "Roseli"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante recebido e salvo na Central de Pix.")
        self.assertContains(
            resposta,
            "Comprovante recebido na Central de Pix. A leitura automática não identificou todos os dados. Confira depois no computador.",
        )
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")

    def test_enviar_mesmo_comprovante_pix_duas_vezes_marca_segundo_como_possivel_duplicado(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria Duplicada Silva\n"
            "Valor R$ 88,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
            "ID da transacao\n"
            "E00000000202605161730ABCDEF123456789\n"
        ).encode("utf-8")
        url = reverse("estoque:central_pix_enviar_comprovante")

        primeira = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-1.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )
        segunda = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-2.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertContains(segunda, "Possível Pix duplicado encontrado. Confira antes de baixar.")
        pix_original, pix_duplicado = PixRecebido.objects.order_by("id")
        self.assertNotEqual(pix_original.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(pix_duplicado.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(pix_duplicado.pix_original, pix_original)
        self.assertIsNone(pix_duplicado.cliente)

    def test_enviar_comprovante_com_mesmo_valor_data_pagador_banco_marca_possivel_duplicado(self):
        PixRecebido.objects.create(
            nome_pagador="Jose Parecido da Silva",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "provavel-duplicado.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "Pagador: José Parecido da Silva\n"
                "Valor R$ 55,00\n"
                "16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertIsNotNone(novo_pix.pix_original)
        self.assertIn("Possivel Pix duplicado", novo_pix.observacao)

    def test_enviar_comprovante_pix_diferente_continua_pendente_normal(self):
        cliente = Cliente.objects.create(nome="Cliente Diferente Pix", ativo=True)
        PixRecebido.objects.create(
            nome_pagador="Outro Pagador",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "pix-diferente.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cliente Diferente Pix\n"
                "Valor R$ 75,00\n"
                "Data 16/05/2026 18:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.cliente_sugerido, cliente)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_PENDENTE)
        self.assertIsNone(novo_pix.pix_original)

    def test_status_possivel_duplicado_aparece_na_central_e_no_detalhe(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
            observacao="Possivel Pix duplicado do registro #1.",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Possivel duplicado")
        self.assertContains(resposta_lista, "possivel_duplicado")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Possível Pix duplicado encontrado. Confira antes de baixar.")
        self.assertContains(resposta_detalhe, f"Ver Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Comparação com Pix parecido")
        self.assertContains(resposta_detalhe, f"Pix atual #{duplicado.id}")
        self.assertContains(resposta_detalhe, f"Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Compare os dados antes de decidir se este envio é duplicado.")

    def test_enviado_por_aparece_na_central_detalhe_e_comparacao(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original enviado",
            enviado_por_nome="Lincoln",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado enviado",
            enviado_por_nome="Roseli",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Enviado por")
        self.assertContains(resposta_lista, "Lincoln")
        self.assertContains(resposta_lista, "Roseli")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Enviado por")
        self.assertContains(resposta_detalhe, "Lincoln")
        self.assertContains(resposta_detalhe, "Roseli")

    def test_detalhe_pix_volta_para_resumo_de_envio_quando_recebe_next_seguro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix do resumo",
            valor="40.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resumo_url = f"{reverse('estoque:central_pix_enviar_comprovante')}?pix_recebido={pix.id}"
        detalhe_url = f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={resumo_url}"

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{resumo_url}"')

    def test_detalhe_pix_ignora_next_externo_e_volta_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix next externo",
            valor="15.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            "?next=https://example.com/roubo"
        )

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertNotContains(resposta, "example.com")

    def test_central_pix_lista_link_de_detalhe_do_pix_pendente(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix pendente detalhe",
            valor="35.50",
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="Texto tecnico do OCR",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Pix pendente detalhe")
        self.assertContains(resposta_lista, "Ações")
        self.assertContains(resposta_lista, "Ver detalhe")
        self.assertContains(
            resposta_lista,
            f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})}?next={reverse("estoque:central_pix")}',
        )

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta_detalhe, "Enviar outro comprovante")
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Texto tecnico do OCR")

    def test_detalhe_pix_pendente_permite_marcar_como_ignorado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix para ignorar",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {"acao": "ignorar"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)

    def test_analisar_comprovante_pix_nubank_usa_nome_da_origem(self):
        conteudo = (
            "Comprovante Pix\n"
            "Destino\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nubank_com_destino_banco_inter_nao_usa_regra_inter(self):
        conteudo = (
            "nu,\n"
            "\n"
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituigdo BANCO INTER\n"
            "\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "Nu Pagamentos S.A.\n"
            "nubank.com.br\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")
        saida = io.StringIO()

        with redirect_stdout(saida):
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotIn("[PIX OCR][Banco Inter][BRUTO]", saida.getvalue())
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_mercado_pago_usa_de_como_pagador(self):
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF: ***.319.532-**\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T16:33")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_retorna_nome_arquivo_e_nao_reaproveita_upload_anterior(self):
        primeiro = SimpleUploadedFile(
            "mercado_pago_100.txt",
            (
                "Comprovante de Pix\n"
                "17/05/2026 11:09\n"
                "Valor R$ 100\n"
                "@ De\n"
                "Cliente Mercado Pago\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        segundo = SimpleUploadedFile(
            "comprovante_picpay_50.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "21/set/2025 - 13:20:41\n"
                "Valor\n"
                "R$ 50,00\n"
                "Pagador: ISA ALVES DE SOUZA\n"
                "Instituicao: PicPay\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        url = reverse("estoque:central_pix_analisar_comprovante")

        resposta_primeira = self.client.post(url, {"comprovante": primeiro}, secure=True)
        self.assertEqual(resposta_primeira.status_code, 200)
        dados_primeiro = resposta_primeira.json()
        self.assertEqual(dados_primeiro["valor"], "100.00")
        self.assertEqual(dados_primeiro["data_pagamento"], "2026-05-17T11:09")

        resposta_segunda = self.client.post(url, {"comprovante": segundo}, secure=True)
        self.assertEqual(resposta_segunda.status_code, 200)
        dados_segundo = resposta_segunda.json()
        self.assertEqual(dados_segundo["nome_arquivo"], "comprovante_picpay_50.txt")
        self.assertEqual(dados_segundo["valor"], "50.00")
        self.assertEqual(dados_segundo["data_pagamento"], "2025-09-21T13:20")
        self.assertEqual(dados_segundo["instituicao_pix"], "PicPay")
        self.assertEqual(dados_segundo["pagador"], "ISA ALVES DE SOUZA")
        self.assertIn("PicPay", dados_segundo["debug_texto_ocr"])
        self.assertNotIn("Mercado Pago", dados_segundo["debug_texto_ocr"])
        self.assertNotEqual(dados_segundo["valor"], dados_primeiro["valor"])
        self.assertNotEqual(dados_segundo["data_pagamento"], dados_primeiro["data_pagamento"])

    def test_analisar_comprovante_pix_picpay_data_mes_abreviado_e_instituicao(self):
        datas_ocr = [
            "21/set/2025 - 13:20:41",
            "21 / set / 2025 - 13:20:41",
            "21/set/2025 13:20",
            "21/set/2025 - 13:20",
            "21/set/2025 - 13.20.41",
            "21/set/2025 13h20",
            "21/set/2025 às 13h20",
            "21/set/2025 as 13h20",
            "21/set./2025 - 13:20:41",
            "21 / set. / 2025 - 13:20:41",
            "21/5et/2O25 - l3:2O:41",
            "21/sct/2025 - 13.2O.41",
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for indice, data_ocr in enumerate(datas_ocr):
            with self.subTest(data_ocr=data_ocr):
                conteudo = (
                    "PicPay\n"
                    "Comprovante de Pix\n"
                    f"{data_ocr}\n"
                    "Valor\n"
                    "R$ 50,00\n"
                    "De\n"
                    "ISA ALVES DE SOUZA\n"
                    "PIC PAY\n"
                ).encode("utf-8")
                arquivo = SimpleUploadedFile(f"picpay-{indice}.txt", conteudo, content_type="text/plain")

                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["valor"], "50.00")
                self.assertEqual(dados["data_pagamento"], "2025-09-21T13:20")
                self.assertEqual(dados["pagador"], "ISA ALVES DE SOUZA")
                self.assertEqual(dados["instituicao_pix"], "PicPay")
                self.assertIn("Data enviada ao frontend: 2025-09-21T13:20", dados["debug_data_pagamento"])

    def test_analisar_comprovante_pix_lida_com_datas_e_valores_comuns_sem_fallback_atual(self):
        casos = [
            (
                "mercado_maio_as.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 as 8h42\nValor\nR$ 50,00\nPagador: Maria Silva\n",
                "50.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "mercado_maio_acento.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 às 8h42\nR$ 100\nPagador: Jose Silva\n",
                "100.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "nubank_mai.txt",
                "Nu Pagamentos\nComprovante Pix\n05 MAI 2026 - 17:36\nValor R$ 50,00\nOrigem\nNome: Ana Souza\n",
                "50.00",
                "2026-05-05T17:36",
                "Nubank",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome, conteudo, valor_esperado, data_esperada, instituicao_esperada in casos:
            with self.subTest(nome=nome):
                arquivo = SimpleUploadedFile(nome, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)
                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["nome_arquivo"], nome)
                self.assertEqual(dados["valor"], valor_esperado)
                self.assertEqual(dados["data_pagamento"], data_esperada)
                self.assertEqual(dados["instituicao_pix"], instituicao_esperada)

    def test_analisar_comprovante_pix_banco_inter_prioriza_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Dados do pagador\n"
            "Nome: Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banco_inter_nao_usa_recebedor_como_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_normaliza_horario_com_o_no_lugar_de_zero(self):
        conteudo = (
            "Domingo, 17/05/2026\n"
            "O8h09\n"
            "Valor R$ 5,00\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_inter_whatsapp_ocr_real_extrai_pagador(self):
        conteudo = (
            "Sobre a transagao\n"
            "Data da transacao\n"
            "Horario\n"
            "\n"
            "Identificador\n"
            "\n"
            "ID da transacao\n"
            "\n"
            "sinter\n"
            "\n"
            "Pix enviado\n"
            "R$ 5,00\n"
            "\n"
            "Domingo, 17/05/2026\n"
            "\n"
            "O8h09\n"
            "\n"
            "\u00a300416968202605171109huk671IHk1t\n"
            "\n"
            "Quem recebeu\n"
            "\n"
            "Nome\n"
            "\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Chave Pix\n"
            "\n"
            "Quem pagou\n"
            "Nome\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Lincoin Albuquerque Neiva\n"
            "\n"
            "*#*,319.532-\u00ab*\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "\n"
            "+5591984111011\n"
            "\n"
            "FRANCISCO NETO DA SILVA MIRANDA\n"
            "\n"
            "***,467.952-**\n"
            "\n"
            "BANCO INTER\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "FRANCISCO NETO DA SILVA MIRANDA")
        self.assertNotEqual(dados["pagador"], "Lincoin Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "NU PAGAMENTOS - IP")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_instituicao_do_cabecalho(self):
        conteudo = (
            "BANCO DO ESTADO DO PAR\u00c1 S.A. - BANPAR\u00c1\n"
            "COMPROVANTE DE PIX\n"
            "Data da Opera\u00e7\u00e3o: 03/02/2026 17:59:09\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Institui\u00e7\u00e3o: NU PAGAMENTOS - IP\n"
            "Valor: 1.449,08\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-banpara.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["instituicao_pix"], "Banpar\u00e1")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "1449.08")
        self.assertEqual(dados["data_pagamento"], "2026-02-03T17:59")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_caixa_tem_usa_quem_vai_enviar(self):
        cliente = Cliente.objects.create(nome="ELIANA NAZARE DA SILVA FERREIRA", ativo=True)
        conteudo = (
            "CAIXA Tem\n"
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "\n"
            "Valor\n"
            "R$ 826,62\n"
            "\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque\n"
            "Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA\n"
            "SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "\n"
            "Dados da transa\u00e7\u00e3o\n"
            "ID\n"
            "E003603052026051711183ffbb3dd95\n"
            "NSU\n"
            "64892124094\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-caixa-tem.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertEqual(dados["instituicao_pix"], "Caixa Econ\u00f4mica Federal")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_usa_titular_da_origem(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao: 17/05/2026 01:54:04\n"
            "Codigo da Sessao: 123456\n"
            "\n"
            "Dados de Origem\n"
            "Titular: LINCOLN ALBUQUERQUE NEIVA\n"
            "Agencia: 0057\n"
            "Conta: 000341187-7\n"
            "Tipo de Conta: CC\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao: NU PAGAMENTOS - IP\n"
            "Titular: RECEBEDOR ERRADO\n"
            "CPF: ***.319.532-**\n"
            "Agencia: 0001\n"
            "Conta: 123\n"
            "Tipo de Conta: Conta de Pagamento\n"
            "\n"
            "ID Transacao: E123\n"
            "Tipo de Pagamento: Chave\n"
            "Finalidade: Compra/Transferencia\n"
            "Valor: 4,40\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "LINCOLN ALBUQUERQUE NEIVA")
        self.assertNotEqual(dados["pagador"], "RECEBEDOR ERRADO")
        self.assertEqual(dados["valor"], "4.40")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T01:54")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_origem_e_data_operacao(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao:                  05/05/2026 16:45:50\n"
            "Codigo da Sessao:                  APP00570000023744PP639135962448580000\n"
            "\n"
            "Dados de Origem\n"
            "Titular:                           RUBEM ARRUDA DE SOUZA\n"
            "Agencia:                           0057\n"
            "Conta:                             000002374-4\n"
            "Tipo de Conta:                     PP\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao:                       NU PAGAMENTOS - IP\n"
            "Titular:                           Lincoln Albuquerque Neiva\n"
            "CPF:                               ***.319.532-**\n"
            "Agencia:                           0001\n"
            "Conta:                             088228354-6\n"
            "Tipo de Conta:                     Conta de Pagamento\n"
            "\n"
            "ID Transacao:                      E04913711202605051945RF704X8VJHS\n"
            "Tipo de Pagamento:                 Chave\n"
            "Finalidade:                        Compra/Transferencia\n"
            "Valor:                             847,70\n"
            "Autenticacao:                      209002000000000002633000000.16\n"
            "                                   00530000087366805058084946550.44\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_data_operacao_quebrada(self):
        conteudo = (
            "BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operacao:\n"
            "05/05/2026\n"
            "16:45:50\n"
            "Codigo da Sessao: APP00570000023744PP639135962448580000\n"
            "Dados de Origem\n"
            "Titular:\n"
            "RUBEM ARRUDA DE SOUZA\n"
            "Agencia: 0057\n"
            "Conta: 000002374-4\n"
            "Tipo de Conta: PP\n"
            "Dados do Recebedor\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")

    def test_analisar_comprovante_pix_banco_inter_caso_real_recebido(self):
        conteudo = (
            "Inter\n"
            "Comprovante de Pix recebido\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "Beneficiario\n"
            "Lincoln Albuquerque Neiva\n"
            "Chave Pix do recebedor\n"
            "Instituicao do recebedor\n"
            "Banco Inter\n"
            "De\n"
            "Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banco_inter_bloqueia_nome_recebedor_conhecido(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "De\n"
            "Lincoln Albuquerque\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")

    def test_analisar_comprovante_pix_banco_inter_reconstroi_destino_nome_quebrado(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituicao BANCO INTER\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_sugere_cliente_com_jr_e_nome_intermediario(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["cliente_sugerido_nome"], "Ivanildo Patricio Jr")
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nome_generico_nao_sugere_cliente(self):
        Cliente.objects.create(nome="Maria Silva", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Maria")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "baixa")

    def test_analisar_comprovante_pix_nao_sugere_cliente_quando_ambiguo(self):
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "ambigua")
        self.assertIn("mais de um cliente parecido", dados["mensagem_cliente"])

    def test_analisar_comprovante_pix_nao_usa_instituicao_como_pagador(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Valor R$ 156,50\n"
            "16/05/2026 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")

    def test_analisar_comprovante_pix_falha_sem_bloquear_preenchimento_manual(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"texto sem dados de pix", content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertIn("Preencha manualmente", dados["mensagem"])
        self.assertIsNone(dados["cliente_sugerido_id"])

    def test_central_pix_bloqueia_duplicado_pendente_evidente(self):
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 51))
        PixRecebido.objects.create(
            nome_pagador="Joelson Ferreira dos Santos",
            valor="156.50",
            data_pagamento=data_pagamento,
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(reverse("estoque:central_pix"), data={
            "cliente": "",
            "nome_pagador": "  joelson ferreira dos santos ",
            "valor": "156.50",
            "data_pagamento": "2026-05-16T17:52",
            "observacao": "Tentativa duplicada",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix duplicado nao foi salvo")
        self.assertEqual(PixRecebido.objects.count(), 1)
