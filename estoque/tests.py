import io
import os
import tempfile
import types
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import FuncionarioForm, PixRecebidoForm
from .models import Cliente, ContaReceber, CreditoCliente, Funcionario, PixRecebido, RecebimentoContaReceber, Venda
from .utils_pix import analisar_comprovante_pix, analisar_comprovante_pix_google_vision, _preparar_recortes_ocr
from . import views


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

    def test_central_pix_pix_novo_aparece_com_linha_destacada(self):
        PixRecebido.objects.create(
            nome_pagador="Pix novo visual",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(reverse("estoque:central_pix"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix novo visual")
        self.assertContains(resposta, "pix-row-novo")
        self.assertNotContains(resposta, "pix-novo-badge")
        self.assertNotContains(resposta, ">Novo<")
        pix = PixRecebido.objects.get(nome_pagador="Pix novo visual")
        self.assertIsNone(pix.visualizado_em)

    def test_central_pix_detalhe_marca_pix_como_visualizado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix abre detalhe",
            valor="35.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Pix abre detalhe")
        self.assertNotContains(resposta_lista, '<tr class="pix-row pix-row-novo">', html=True)

    def test_central_pix_marcar_visualizado_nao_altera_status_financeiro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix status preservado",
            valor="45.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_bloqueia_salvar_sem_cliente_confirmado(self):
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        comprovante = SimpleUploadedFile(
            "mercado-pago.jpg",
            b"comprovante pix mercado pago",
            content_type="image/jpeg",
        )

        resposta = self.client.post(url, data={
            "cliente": "",
            "nome_pagador": "",
            "valor": "645.00",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "observacao": "",
            "status": PixRecebido.STATUS_PENDENTE,
            "comprovante": comprovante,
        }, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de salvar o Pix.")
        self.assertContains(resposta, "Confirme um cliente antes de salvar o Pix.")
        self.assertContains(resposta, 'placeholder="Sem cliente identificado"')
        self.assertEqual(PixRecebido.objects.count(), 0)

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

    def test_pagina_envio_comprovante_pix_inclui_manifest_pwa(self):
        resposta = self.client.get(reverse("estoque:central_pix_enviar_comprovante"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'rel="manifest"')
        self.assertContains(resposta, 'href="/static/manifest.json"')
        self.assertContains(resposta, 'name="theme-color" content="#16a34a"')

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_atalho_inteligente_pix_carrega_urls_configuradas(self):
        resposta = self.client.get(reverse("estoque:pix_enviar_inteligente"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Abrindo Enviar Pix")
        self.assertContains(resposta, "Tentando conexao local pelo Wi-Fi")
        self.assertContains(resposta, "http://10.0.0.154:8000/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "fetch(localUrl")
        self.assertContains(resposta, "window.location.replace(onlineUrl)")

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_local(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_online(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    @override_settings(ALLOWED_HOSTS=["example.com"])
    def test_pagina_envio_comprovante_pix_mostra_ambiente_nao_identificado(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="example.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "AMBIENTE NÃO IDENTIFICADO")

    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_local(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_online(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    def test_rota_antiga_envio_pix_post_sem_arquivo_mantem_selo_local(self):
        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"enviado_por": "Lincoln"},
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")
        self.assertContains(resposta, "Escolha uma imagem ou arquivo de comprovante Pix.")

    def test_rota_antiga_envio_pix_sucesso_mantem_selo_online(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        self.assertContains(resposta, "ONLINE / Render")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")

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

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        pix = PixRecebido.objects.get()
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
        self.assertContains(resposta, "Comprovante enviado")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(resposta, "Confira depois no computador antes de baixar qualquer conta.")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix_enviar_comprovante")}"')
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta, "Enviar outro comprovante")
        self.assertIsNone(pix.cliente)
        self.assertIsNone(pix.cliente_sugerido)
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(pix.enviado_por_nome, "Lincoln")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)
        self.assertTrue(pix.comprovante)
        self.assertContains(resposta, "OCR pendente / Conferencia pendente")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Ler comprovante (OCR)")

    def test_enviar_comprovante_pix_erro_storage_nao_derruba_pagina(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.PixRecebido.objects.create", side_effect=RuntimeError("falha storage")):
            with self.assertLogs("estoque.views", level="ERROR") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_enviar_comprovante"),
                    {"comprovante": arquivo, "enviado_por": "Lincoln"},
                    secure=True,
                )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta,
            "Erro ao salvar comprovante Pix. Verifique a configuração do armazenamento online.",
        )
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertIn("diagnostico_storage", "\n".join(logs.output))
        self.assertEqual(PixRecebido.objects.count(), 0)

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
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

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
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
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
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(
            resposta,
            "Confira depois no computador antes de baixar qualquer conta.",
        )
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")

    def test_enviar_comprovante_pix_com_erro_ocr_salva_diagnostico_sem_quebrar(self):
        arquivo = SimpleUploadedFile(
            "comprovante-render.jpg",
            b"imagem recebida pelo celular",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", side_effect=RuntimeError("tesseract nao encontrado")):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Roseli"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "OCR nao executado automaticamente no envio mobile")

    def test_analisar_comprovante_pix_com_ocr_vazio_salva_diagnostico(self):
        arquivo = SimpleUploadedFile(
            "comprovante-vazio.jpg",
            b"imagem sem texto",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", return_value=""):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

    def _imagem_pix_teste(self, tamanho=(900, 1600)):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", tamanho, "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_ocr_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in (80, 150, 220, 290, 360):
            desenho.rectangle((60, y, 430, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_nubank_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y, largura in zip((70, 135, 195, 260, 340), (80, 260, 320, 360, 340)):
            desenho.rectangle((70, y, 70 + largura, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_texto_denso_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (700, 1500), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in range(60, 1280, 28):
            desenho.rectangle((70, y, 610, y + 5), fill="black")
            desenho.rectangle((70, y + 10, 420, y + 14), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _modulo_pytesseract_fake(self, respostas_por_recorte):
        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            resposta = respostas_por_recorte.get(recorte, "")
            if callable(resposta):
                return resposta(imagem, kwargs)
            if isinstance(resposta, Exception):
                raise resposta
            return resposta

        return types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

    def _mock_google_vision_texto(self, texto):
        class ImagemFake:
            def __init__(self, content):
                self.content = content

        cliente = types.SimpleNamespace(
            document_text_detection=lambda image: types.SimpleNamespace(
                full_text_annotation=types.SimpleNamespace(text=texto),
                text_annotations=[],
                error=types.SimpleNamespace(message=""),
            )
        )
        vision = types.SimpleNamespace(Image=ImagemFake)
        return cliente, vision

    def test_analisar_comprovante_pix_google_vision_banpara_extrai_dados(self):
        texto = (
            "BANCO DO ESTADO DO PARÁ S.A. - BANPARÁ\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operação: 05/05/2026 16:45:50\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Instituição: NU PAGAMENTOS - IP\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
        )
        arquivo = SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["instituicao_pix"], "Banpará")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_valor_data(self):
        texto = "NU\nComprovante de transferência\n21 ABR 2026 - 13:05:01\nValor R$ 172,00\n"
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_pagador_origem_por_contexto(self):
        texto = (
            "NU\n"
            "Comprovante de transferencia\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Destino\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Juliana Cotrim Cardoso\n"
            "CPF ***.123.456-**\n"
            "Instituicao NU PAGAMENTOS - IP\n"
        )
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Juliana Cotrim Cardoso")
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_mercado_pago_extrai_pagador_de(self):
        texto = (
            "Comprovante de Pix\n"
            "23/maio/2026 as 18:55:32\n"
            "R$ 645\n"
            "De\n"
            "Joao de Almeida E Silva\n"
            "CPF: ***.105.902-**\n"
            "Mercado Pago\n"
            "Para\n"
            "Lincoln Albuquerque Neiva\n"
        )
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida E Silva")
        self.assertEqual(dados["valor"], "645.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-23T18:55")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_debug_texto_bruto_env(self):
        texto = "Comprovante de Pix\nDe\nJoao de Almeida E Silva\nPara\nLincoln Albuquerque Neiva\n"
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_OCR_DEBUG_TEXT": "True"}), patch(
            "estoque.utils_pix._criar_cliente_google_vision",
            return_value=self._mock_google_vision_texto(texto),
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            analisar_comprovante_pix_google_vision(arquivo)

        saida = "\n".join(logs.output)
        self.assertIn("[PIX OCR][Google Vision texto bruto]", saida)
        self.assertIn("--- INICIO GOOGLE VISION OCR ---", saida)
        self.assertIn("Joao de Almeida E Silva", saida)
        self.assertIn("--- FIM GOOGLE VISION OCR ---", saida)

    def test_analisar_comprovante_pix_google_vision_extrai_de_para_em_texto_achatado(self):
        casos = [
            (
                "pagbank.jpg",
                "Comprovante de envio de Pix 18/05/2026 as 07:08:52 Valor da transferencia R$ 5,00 De ROSELI DA COSTA GAMA CPF ***.115.912-** Instituicao PagBank Para Lincoln Albuquerque Neiva",
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "itau.jpg",
                "Comprovante de devolucao de Pix R$ 5,00 Realizado em 19/05/2026 as 17:18:36 De EUCLIDES CARNEIRO NEIVA NETO CPF/CNPJ: 787.484.883-72 Instituicao: ITAU UNIBANCO S.A Para Lincoln Albuquerque Neiva",
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
        ]

        for nome_arquivo, texto, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, b"imagem", content_type="image/jpeg")
                with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
                    dados = analisar_comprovante_pix_google_vision(arquivo)

                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_pix_google_vision_habilitado_le_variavel_de_ambiente_fora_dos_testes(self):
        with patch.dict(os.environ, {"PIX_USAR_GOOGLE_VISION": "True"}), patch("sys.argv", ["manage.py", "runserver"]):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_pix_google_vision_habilitado_usa_credencial_quando_flag_nao_definida(self):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "google-vision.json"}, clear=True), patch(
            "sys.argv",
            ["manage.py", "runserver"],
        ):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_analisar_comprovante_pix_google_vision_falha_sem_500(self):
        arquivo = SimpleUploadedFile("erro.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", side_effect=RuntimeError("sem credencial")):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("[Google Vision OCR erro]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_parcial_nao_derruba_extracao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-parcial.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 156,50\n16/05/2026 17:51\nNubank\n",
            "pagador": RuntimeError("timeout no recorte"),
            "inteira": "Origem\nNome: Joelson Ferreira dos Santos\nDestino\nNome: Lincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[OCR avisos]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_fallback_imagem_inteira_continua_funcionando(self):
        arquivo = SimpleUploadedFile(
            "comprovante-fallback.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": RuntimeError("falhou topo"),
            "pagador": RuntimeError("falhou pagador"),
            "inteira": (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Maria Fallback Silva\n"
                "Valor R$ 88,00\n"
                "16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Maria Fallback Silva")
        self.assertEqual(dados["valor"], "88.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["instituicao_pix"], "Banco do Brasil")

    def test_analisar_comprovante_pix_tenta_eng_quando_por_traineddata_falha(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-por.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )

        def texto_com_fallback(_imagem, kwargs):
            if kwargs.get("lang") == "por":
                raise RuntimeError("Error opening data file C:\\Program Files\\Tesseract-OCR/tessdata/por.traineddata")
            if kwargs.get("lang") == "eng":
                return "Pix enviado\nR$ 5,00\nData do pagamento\nSabado, 16/05/2026\nHorario\n23h41\n"
            return ""

        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": texto_com_fallback,
            "pagador": "",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertIn("recorte=topo tentativa OCR falhou=por:", "\n".join(logs.output))
        self.assertIn("recorte=topo idioma OCR usado=eng", "\n".join(logs.output))

    def test_analisar_comprovante_pix_recortes_nao_usam_recebedor_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-recebedor.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Valor R$ 600,00\n16/05/2026 16:33\nPara\nLincoln Albuquerque Neiva\n",
            "pagador": "@ De\nIvanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nMercado Pago\n",
            "inteira": "Para\nLA Neiva\nNu Pagamentos S.A.\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertNotIn(dados["pagador"], ["Lincoln Albuquerque Neiva", "LA Neiva"])
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_sem_banco_pagador_retorna_parcial_sem_erro(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-pagador.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 10,00\n21/05/2026 09:10\n",
            "pagador": "",
            "inteira": "Destino\nLincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertEqual(dados["valor"], "10.00")

    def test_analisar_comprovante_pix_todos_recortes_falham_sem_excecao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-falha.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        erro = RuntimeError("timeout")
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": erro,
            "pagador": erro,
            "inteira": erro,
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "")
        self.assertIn("preencha manualmente", dados["mensagem"].lower())

    def test_analisar_comprovante_pix_render_nao_usa_imagem_inteira_em_imagem_grande(self):
        arquivo = SimpleUploadedFile(
            "banpara-grande.png",
            self._imagem_pix_teste(tamanho=(900, 1800)),
            content_type="image/png",
        )
        recortes_chamados = []

        def falhar_recorte(imagem, _kwargs):
            recortes_chamados.append(imagem.info.get("ocr_recorte", "inteira"))
            raise RuntimeError("timeout render")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "faixa_valor_principal": falhar_recorte,
            "faixa_valor_alternativa": falhar_recorte,
            "faixa_data_principal": falhar_recorte,
            "faixa_data_alternativa": falhar_recorte,
            "rapido_superior": falhar_recorte,
            "rapido_meio_superior": falhar_recorte,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertNotIn("inteira", recortes_chamados)
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_render_bloqueia_imagem_densa_antes_do_tesseract(self):
        arquivo = SimpleUploadedFile(
            "banpara-denso.png",
            self._imagem_texto_denso_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
            True,
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("OCR bloqueado por seguran", dados["texto_ocr_bruto"])
        self.assertIn("OCR bloqueado por seguranca no Render antes do Tesseract", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_bloqueado_volta_sem_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara-denso.png", self._imagem_texto_denso_teste(), content_type="image/png"),
            )
            pytesseract_fake = self._modulo_pytesseract_fake({
                "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
            })

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
                True,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("OCR bloqueado por seguran", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_analisar_comprovante_pix_render_timeouts_sucessivos_retornam_falha_segura(self):
        arquivo = SimpleUploadedFile(
            "banpara-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def timeout(_imagem, _kwargs):
            raise RuntimeError("Tesseract process timeout")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "linha_01": timeout,
            "linha_02": timeout,
            "linha_03": timeout,
            "linha_04": timeout,
            "linha_05": timeout,
            "rapido_superior": timeout,
            "rapido_meio_superior": timeout,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_inter_enviado_preenche_valor_data_sem_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "ID da transacao\n"
                "E00416968202605170241GgMBKIN9FUL\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertNotIn("Lincoln Albuquerque Neiva", dados["pagador"])

    def test_analisar_comprovante_pix_inter_nao_usa_instituicao_do_recebedor_como_banco(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-recebedor-nubank.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Inter\n"
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "Quem pagou\n"
                "Nome\n"
                "RONISE DO SOCORRO DOS SANTOS FERREIRA\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RONISE DO SOCORRO DOS SANTOS FERREIRA")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "Inter")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")

    def test_analisar_comprovante_pix_inter_nao_usa_la_neiva_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-la-neiva.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Pix enviado\nRS 5,00\nData do pagamento Sabado, 16/05/2026 Horario 23h41\n",
            "pagador": "Quem recebeu\nLA Neiva\nInstituicao do recebedor\nNu Pagamentos\n",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")

    def test_enviar_mesmo_comprovante_pix_duas_vezes_nao_processa_duplicidade_no_mobile(self):
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
        self.assertContains(segunda, "Comprovante enviado com sucesso para a Central de Pix.")
        pix_original, pix_duplicado = PixRecebido.objects.order_by("id")
        self.assertNotEqual(pix_original.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(pix_duplicado.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(pix_duplicado.pix_original)
        self.assertIsNone(pix_duplicado.cliente)

    def test_enviar_comprovante_mobile_nao_roda_duplicidade_por_ocr(self):
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
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(novo_pix.pix_original)
        self.assertIn("OCR pendente", novo_pix.observacao)

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
        self.assertIsNone(novo_pix.cliente_sugerido)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
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
        self.assertContains(resposta_detalhe, "Possivel Pix duplicado")
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
        self.assertContains(resposta_lista, "Data do Pix")
        self.assertContains(resposta_lista, "Registrado em")
        self.assertNotContains(resposta_lista, "<th class=\"pix-col-observacao\">Observacao</th>")
        self.assertNotContains(resposta_lista, "Texto tecnico do OCR")
        self.assertContains(resposta_lista, "Pix pendente detalhe")
        self.assertContains(resposta_lista, 'class="pix-pagador" title="Pix pendente detalhe"')
        self.assertContains(resposta_lista, 'class="pix-sem-cliente">Sem cliente</span>')
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

    def test_central_pix_ver_detalhe_retorna_para_central_mesmo_com_next_externo(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix com retorno central",
            valor="18.00",
            instituicao_pix="PagBank",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        contas_url = reverse("estoque:contas_receber")
        caminho_lista = f"{central_url}?next={contas_url}&q=pagbank"

        resposta_lista = self.client.get(caminho_lista, secure=True)

        detalhe_path = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_lista, f"{detalhe_path}?next=")
        self.assertContains(resposta_lista, "central-pix")
        self.assertContains(resposta_lista, "q%3Dpagbank")

    def test_receber_cliente_voltar_ao_pix_preserva_next_original_do_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente retorno pix", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente retorno pix",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            f"?{urlencode({'next': central_url})}"
        )
        receber_url = (
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}"
            f"?{urlencode({'pix_recebido': pix.id, 'next': detalhe_url})}"
        )

        resposta = self.client.get(receber_url, secure=True)

        self.assertContains(resposta, f'href="{detalhe_url}"')
        self.assertNotContains(resposta, f"{detalhe_url}?")
        self.assertNotContains(resposta, f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido=")

    def test_central_pix_busca_filtra_pix_registrados(self):
        cliente = Cliente.objects.create(nome="Jo\u00e3o De Almeida E Silva", ativo=True)
        pix_joao = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Joao de Almeida e Silva",
            valor="42.50",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 23, 18, 55)),
        )
        PixRecebido.objects.create(
            nome_pagador="Maria Ignorada",
            valor="12.00",
            instituicao_pix="Nubank",
            status=PixRecebido.STATUS_IGNORADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 22, 8, 30)),
        )

        for termo in ("joao", "almeida", "mercado", "pendente", "23/05/2026", "42,50"):
            resposta = self.client.get(f"{reverse('estoque:central_pix')}?q={termo}", secure=True)
            self.assertEqual(resposta.status_code, 200)
            self.assertContains(resposta, "Joao de Almeida e Silva")
            self.assertContains(resposta, "Mercado Pago")
            self.assertNotContains(resposta, "Maria Ignorada")
            self.assertContains(
                resposta,
                f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix_joao.id})}?next=',
            )
            self.assertContains(resposta, "q%3D")

        resposta_ignorado = self.client.get(f"{reverse('estoque:central_pix')}?q=ignorado", secure=True)
        self.assertContains(resposta_ignorado, "Maria Ignorada")
        self.assertNotContains(resposta_ignorado, "Joao de Almeida e Silva")
        self.assertContains(resposta_ignorado, "Limpar")

    def test_central_pix_filtro_por_data(self):
        PixRecebido.objects.create(
            nome_pagador="Pix dentro da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            nome_pagador="Pix fora da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 5, 10, 0)),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?data_inicio=2026-05-04&data_fim=2026-05-04",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix dentro da data")
        self.assertNotContains(resposta, "Pix fora da data")

    def test_central_pix_filtro_por_status(self):
        PixRecebido.objects.create(
            nome_pagador="Pix status pendente",
            valor="30.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        PixRecebido.objects.create(
            nome_pagador="Pix status baixado",
            valor="30.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?status={PixRecebido.STATUS_PENDENTE}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix status pendente")
        self.assertNotContains(resposta, "Pix status baixado")

    def test_central_pix_combina_texto_cliente_data_e_status(self):
        cliente_roseli = Cliente.objects.create(nome="Roseli Cliente", ativo=True)
        cliente_maria = Cliente.objects.create(nome="Maria Cliente", ativo=True)
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo certo",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo baixado",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_maria,
            nome_pagador="Pix combo maria",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )

        resposta = self.client.get(
            (
                f"{reverse('estoque:central_pix')}?q=Mercado&cliente=Roseli"
                f"&data_inicio=2026-05-04&data_fim=2026-05-04&status={PixRecebido.STATUS_PENDENTE}"
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix combo certo")
        self.assertNotContains(resposta, "Pix combo baixado")
        self.assertNotContains(resposta, "Pix combo maria")
        self.assertContains(resposta, "status%3Dpendente")

    def test_analisar_comprovante_pix_usa_google_vision_quando_habilitado(self):
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida e Silva",
            "valor": "42.50",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nJoao de Almeida e Silva\nR$ 42,50",
            "mensagem": "Dados lidos pelo Google Vision. Confira antes de salvar.",
        }
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with override_settings(PIX_USAR_GOOGLE_VISION=True):
            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision) as vision_mock:
                with patch("estoque.views.analisar_comprovante_pix") as local_mock:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_analisar_comprovante"),
                        {"comprovante": arquivo},
                        secure=True,
                    )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida e Silva")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        vision_mock.assert_called_once()
        local_mock.assert_not_called()
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nao_usa_ocr_local_sem_fallback_explicito(self):
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.views.analisar_comprovante_pix") as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertIn("Leitura automatica nao realizada", dados["mensagem"])
        self.assertIn("[Google Vision indisponivel]", dados["texto_ocr_bruto"])
        self.assertIn("fallback local desativado", "\n".join(logs.output))
        local_mock.assert_not_called()

    def test_analisar_comprovante_pix_usa_ocr_local_quando_fallback_explicito(self):
        dados_local = {
            "ok": True,
            "pagador": "Pagador Local",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "PagBank",
            "texto_ocr_bruto": "[OCR linhas detectadas]\nPagador Local",
            "mensagem": "OCR parcial concluido. Confira os dados antes de salvar.",
        }
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
            "estoque.views.analisar_comprovante_pix",
            return_value=dados_local,
        ) as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Pagador Local")
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("OCR local fallback permitido", "\n".join(logs.output))
        local_mock.assert_called_once()

    def test_detalhe_pix_processar_ocr_agora_atualiza_dados_do_comprovante(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain"),
            )

            resposta = self.client.post(
                reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Cicero Cristiano Silva Souza")
            self.assertEqual(str(pix.valor), "20.00")
            self.assertEqual(pix.instituicao_pix, "Banco do Brasil")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertIn("Comprovante Pix", pix.texto_ocr_bruto)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "Texto OCR bruto")
            self.assertContains(resposta, "Reler comprovante (OCR)")

    def test_detalhe_pix_processar_google_vision_preenche_sem_criar_baixa(self):
        cliente = Cliente.objects.create(nome="Rubem Arruda de Souza", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "RUBEM ARRUDA DE SOUZA",
            "valor": "847.70",
            "data_pagamento": "2026-05-05T16:45",
            "instituicao_pix": "Banpará",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Dados de Origem\n"
                "Titular: RUBEM ARRUDA DE SOUZA\n"
                "Valor: 847,70\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "RUBEM ARRUDA DE SOUZA")
            self.assertEqual(str(pix.valor), "847.70")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-05-05T16:45")
            self.assertEqual(pix.instituicao_pix, "Banpará")
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_confirma_cliente_exato_normalizado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida E Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Mercado Pago\n"
                "@ De\n"
                "Joao de Almeida E Silva\n"
                "Valor: R$ 5,00\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente, cliente)
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_cliente_inexistente_continua_sem_confirmacao(self):
        dados_vision = {
            "ok": True,
            "pagador": "Cliente Inexistente Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nCliente Inexistente Silva\nValor: R$ 5,00\n",
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_central_pix_cliente_confirmado_autocomplete_lista_ao_focar(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        Cliente.objects.create(nome="Maria Cliente Ativa", ativo=True)
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        nomes = [cliente["nome"] for cliente in resposta.json()["clientes"]]
        self.assertIn("João De Almeida E Silva", nomes)
        self.assertIn("Maria Cliente Ativa", nomes)
        self.assertNotIn("Cliente Inativo", nomes)
        self.assertLessEqual(len(nomes), 12)

    def test_central_pix_detalhe_carrega_clientes_para_autocomplete_local(self):
        cliente = Cliente.objects.create(
            nome="Jo\u00e3o De Almeida E Silva",
            apelido_nome_conhecido="Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)
        pix = PixRecebido.objects.create(
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "pixClientesAutocompleteData")
        clientes = resposta.context["clientes_pix_autocomplete"]
        item = next(cliente_local for cliente_local in clientes if cliente_local["id"] == cliente.id)
        self.assertEqual(item["nome"], "Jo\u00e3o De Almeida E Silva")
        self.assertIn("joao", item["busca"])
        self.assertIn("almeida", item["busca"])
        self.assertIn("12345678900", item["busca"])
        self.assertIn("85999990000", item["busca"])
        self.assertNotIn("Cliente Inativo", [cliente_local["nome"] for cliente_local in clientes])

    def test_central_pix_cliente_confirmado_autocomplete_busca_normalizada(self):
        cliente = Cliente.objects.create(
            nome="João De Almeida E Silva",
            apelido_nome_conhecido="Joao Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )

        for termo in ("alm", "joao", "123456", "999990000"):
            resposta = self.client.get(
                f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q={termo}",
                secure=True,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(resposta.status_code, 200)
            ids = [item["id"] for item in resposta.json()["clientes"]]
            self.assertIn(cliente.id, ids)

    def test_central_pix_cliente_confirmado_autocomplete_sem_resultado_nao_confirma_cliente(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q=clienteinexistente",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["clientes"], [])
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_central_pix_pendente_pode_remover_cliente_confirmado_sem_baixa(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        conteudo = b"comprovante pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                cliente=cliente,
                cliente_sugerido=cliente,
                nome_pagador="Joao de Almeida E Silva",
                valor=Decimal("5.00"),
                data_pagamento=timezone.now(),
                instituicao_pix="Mercado Pago",
                status=PixRecebido.STATUS_PENDENTE,
                texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", conteudo, content_type="image/jpeg"),
            )
            comprovante_nome = pix.comprovante.name

            resposta = self.client.post(
                reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(str(pix.valor), "5.00")
            self.assertEqual(pix.instituicao_pix, "Mercado Pago")
            self.assertEqual(pix.comprovante.name, comprovante_nome)
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
            self.assertEqual(CreditoCliente.objects.count(), 0)
            self.assertContains(resposta, "Cliente confirmado removido")

    def test_central_pix_baixado_nao_remove_cliente_confirmado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Este Pix ja foi baixado/inativado")

    def test_receber_cliente_com_pix_sem_contas_abertas_oferece_saida_segura(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Conta", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Sem Conta",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="[Google Vision OCR]\nCliente Sem Conta",
        )

        resposta = self.client.get(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este cliente nao tem contas abertas")
        self.assertContains(resposta, "Voltar ao Pix")
        self.assertContains(resposta, "Trocar cliente")
        self.assertContains(resposta, "foco_cliente=1")
        self.assertContains(resposta, "rc-btn-pix-voltar")
        self.assertContains(resposta, "rc-btn-pix-trocar")
        self.assertContains(resposta, "rc-btn-pix-remover")
        self.assertContains(resposta, "Remover cliente confirmado e manter pendente")
        self.assertContains(
            resposta,
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
        )
        conteudo = resposta.content.decode()
        self.assertLess(
            conteudo.rindex("Saldo atual em aberto"),
            conteudo.rindex("Este cliente nao tem contas abertas"),
        )
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_detalhe_pix_com_foco_cliente_foca_sem_abrir_autocomplete(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Cliente Teste",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta_sem_foco = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        resposta_com_foco = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?foco_cliente=1",
            secure=True,
        )

        self.assertEqual(resposta_sem_foco.status_code, 200)
        self.assertEqual(resposta_com_foco.status_code, 200)
        self.assertContains(resposta_sem_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "clienteConfirmadoBusca.select();")
        self.assertContains(resposta_com_foco, 'addEventListener("click"')
        self.assertContains(resposta_com_foco, 'addEventListener("input"')
        conteudo = resposta_com_foco.content.decode()
        bloco_foco = conteudo.split("if (focarClienteConfirmado && clienteConfirmadoBusca)", 1)[1]
        bloco_foco = bloco_foco.split('document.querySelectorAll("form")', 1)[0]
        self.assertNotIn("buscarClientes", bloco_foco)
        self.assertNotIn('addEventListener("focus"', conteudo)

    def test_detalhe_pix_com_cliente_confirmado_destaca_sem_focar_digitacao(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Confirmado", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Pix Confirmado",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "const focarClienteConfirmado = false;")
        self.assertContains(resposta, "pix-client-confirmed-visible")
        self.assertContains(resposta, "Cliente encontrado com seguranca")

    def test_detalhe_pix_com_dados_lidos_sem_cliente_orienta_vincular_pagador(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Lisandra De Oliveira Da Silva",
            valor=Decimal("548.30"),
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 14, 9, 15)),
            instituicao_pix="Stone",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este Pix j\u00e1 tem dados lidos")
        self.assertContains(resposta, "Vincule o pagador a um cliente para usar este Pix na baixa")
        self.assertContains(resposta, "const focarClienteConfirmado = true;")
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_processar_ocr_com_erro_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=RuntimeError("timeout render"),
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("RuntimeError", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("arquivo=comprovante_", "\n".join(logs.output))
            self.assertIn("timeout render", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_excecao_inesperada_nao_retorna_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=ValueError("falha banpara"),
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("ValueError", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_detalhe_pix_processar_ocr_com_timeout_retornado_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )
            dados_timeout = {
                "ok": False,
                "pagador": "",
                "valor": "",
                "data_pagamento": "",
                "instituicao_pix": "",
                "texto_ocr_bruto": "ERRO OCR: RuntimeError: Tesseract process timeout",
            }

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_timeout,
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("Tesseract process timeout", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_aproveita_texto_parcial_com_valor_e_data(self):
        conteudo = (
            "ERRO OCR: timeout parcial no Render\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Tipo de transferencia Pix\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-parcial.txt", conteudo, content_type="text/plain"),
            )

            with self.assertLogs("estoque.views", level="INFO") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(pix.nome_pagador, "")
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("Valor R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertContains(resposta, "Reler comprovante (OCR)")
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_para_apos_topo_com_valor_e_data(self):
        chamadas = []
        configs = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            configs.append(kwargs.get("config"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-topo.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(
                configs,
                [
                    "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789R$r$.,,",
                    "--oem 1 --psm 7",
                ],
            )
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))
            self.assertIn("config=--oem 1 --psm 7", "\n".join(logs.output))
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_faixas_evita_bloco_grande_com_timeout(self):
        chamadas = []
        timeouts = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            timeouts.append(kwargs.get("timeout"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            if recorte in {"rapido_superior", "rapido_meio_superior"}:
                raise RuntimeError("Tesseract process timeout")
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-render.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(timeouts, [4, 4])
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("recorte=faixa_valor_principal", "\n".join(logs.output))
            self.assertIn("recorte=faixa_data_principal", "\n".join(logs.output))
            self.assertIn("texto=R$ 172,00", "\n".join(logs.output))
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))

    def test_analisar_comprovante_pix_nubank_faixas_calculadas_na_imagem_700(self):
        with patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            _original, _reduzido, recortes = _preparar_recortes_ocr(self._imagem_pix_teste((485, 1600)))

        por_nome = {nome: (imagem, caixa) for nome, imagem, caixa in recortes}
        imagem_valor, caixa_valor = por_nome["faixa_valor_principal"]
        imagem_data, caixa_data = por_nome["faixa_data_principal"]

        self.assertEqual(imagem_valor.info["ocr_base_nome"], "nubank_700")
        self.assertEqual(imagem_valor.info["ocr_base_tamanho"][0], 700)
        self.assertEqual(imagem_valor.info["ocr_caixa_percentual"], (0.35, 0.38, 1.00, 0.47))
        self.assertEqual(imagem_data.info["ocr_caixa_percentual"], (0.05, 0.25, 0.95, 0.33))
        self.assertLess(caixa_data[1], int(imagem_data.info["ocr_base_tamanho"][1] * 0.34))
        self.assertLess(caixa_valor[1], int(imagem_valor.info["ocr_base_tamanho"][1] * 0.48))
        self.assertEqual(imagem_valor.info["ocr_tamanho_depois"][0], imagem_valor.info["ocr_tamanho_antes"][0] * 3)

    def test_analisar_comprovante_pix_ocr_por_linhas_extrai_valor_e_data(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas.png",
            self._imagem_linhas_nubank_teste(),
            content_type="image/png",
        )
        timeouts = {}

        respostas = {
            "linha_01": "NU",
            "linha_02": "Comprovante de",
            "linha_03": "transferencia",
            "linha_04": "21 ABR 2026 - 13:05:01",
            "linha_05": "Valor R$ 172,00",
        }

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte")
            timeouts[recorte] = kwargs.get("timeout")
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(timeouts["linha_04"], 4)
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("04 candidata_data: 21 ABR 2026 - 13:05:01", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_por_linhas_continua_ate_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-santander.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )
        chamadas = []
        respostas = {
            "linha_01": "RS 500,00",
            "linha_02": "18/05/2026 as 17:45:00",
            "linha_03": "Dados do pagador",
            "linha_04": "De",
            "linha_05": "lvanildo Ferraz Patricio Junior",
        }

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            chamadas.append(recorte)
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "500.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-18T17:45")
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertIn("linha_05", chamadas)
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_ocr_por_linhas_timeout_continua(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            if recorte == "linha_01":
                raise RuntimeError("Tesseract process timeout")
            if recorte == "linha_02":
                return "21 ABR 2026 - 13:05:01"
            if recorte == "linha_04":
                return "R$ 172,00"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")

    def test_analisar_comprovante_pix_ids_de_transacao_nao_viram_valor(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "ID da transacao 14605308174802dd\n"
            "E182361202604211305\n"
            "NU PAGAMENTOS - IP\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-id.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["valor"], "")
        self.assertEqual(dados["data_pagamento"], "")

    def test_detalhe_pix_processar_ocr_salva_debug_recortes_com_variavel(self):
        chamadas = []

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, DEBUG=False):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-debug.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {
                "PIX_OCR_DEBUG_CROPS": "True",
                "PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True",
            }), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIn("[OCR debug recortes]", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_valor_principal.jpg", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_data_principal.jpg", pix.texto_ocr_bruto)
            self.assertEqual(str(pix.valor), "172.00")

    def test_detalhe_pix_processar_ocr_sem_data_preserva_data_existente(self):
        data_original = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        dados_sem_data = {
            "ok": True,
            "pagador": "",
            "valor": "172.00",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "[OCR faixa_valor_principal]\nR$ 172,00",
        }

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                data_pagamento=data_original,
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-sem-data.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_sem_data,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(pix.data_pagamento, data_original)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")

    def test_detalhe_pix_usa_rota_propria_do_comprovante(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix_original = PixRecebido.objects.create(
                nome_pagador="Pix original com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("pix-original.jpg", b"imagem original", content_type="image/jpeg"),
            )
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
                pix_original=pix_original,
                comprovante=SimpleUploadedFile("pix-render.jpg", b"imagem pix", content_type="image/jpeg"),
            )
            comprovante_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id})
            comprovante_original_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix_original.id})

            resposta = self.client.get(
                reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertContains(resposta, f'href="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_original_url}"')
            self.assertNotContains(resposta, "/media/")

    def test_rota_comprovante_pix_retorna_arquivo_salvo(self):
        conteudo = b"imagem pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com comprovante",
                valor="20.00",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("comprovante-render.jpg", conteudo, content_type="image/jpeg"),
            )

            resposta = self.client.get(
                reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/jpeg")
            self.assertIn('inline; filename="comprovante-render', resposta["Content-Disposition"])
            self.assertEqual(b"".join(resposta.streaming_content), conteudo)

    def test_abrir_comprovante_pix_usa_fallback_media_root_para_arquivo_antigo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            caminho_antigo = Path(media_root) / "pix" / "comprovantes" / "antigo.txt"
            caminho_antigo.parent.mkdir(parents=True, exist_ok=True)
            caminho_antigo.write_bytes(b"arquivo antigo")
            pix = PixRecebido(valor=Decimal("0.00"), data_pagamento=timezone.now())
            pix.comprovante.name = "pix/comprovantes/antigo.txt"

            with patch.object(pix.comprovante, "open", side_effect=FileNotFoundError):
                arquivo = views._abrir_comprovante_pix(pix)

            try:
                self.assertEqual(arquivo.read(), b"arquivo antigo")
            finally:
                arquivo.close()

    def test_rota_comprovante_pix_retorna_404_sem_comprovante(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix sem comprovante",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)

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

    def test_ignorar_pix_preserva_dados_conferidos(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Ignorado", ativo=True)
        pix_original = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45))
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            pix_original=pix_original,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=data_pagamento,
            instituicao_pix="Mercado Pago",
            observacao="Conferido antes de ignorar.",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "ignorar",
                "valor": "",
                "nome_pagador": "",
                "instituicao_pix": "",
                "cliente": "",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)
        self.assertEqual(str(pix.valor), "500.00")
        self.assertEqual(pix.data_pagamento, data_pagamento)
        self.assertEqual(pix.instituicao_pix, "Mercado Pago")
        self.assertEqual(pix.nome_pagador, "Ivanildo Ferraz Patricio Junior")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.pix_original, pix_original)
        self.assertIn("Conferido antes de ignorar.", pix.observacao)
        self.assertIn("Pix ignorado sem baixa pelo operador", pix.observacao)

    def _criar_conta_receber_pix(self, cliente, valor="100.00"):
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=valor,
        )
        return ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=valor,
            valor_em_aberto=valor,
            status=ContaReceber.STATUS_ABERTA,
        )

    def test_baixa_com_pix_atual_bloqueia_quando_parecido_ja_foi_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Duplicado", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix_atual.id}",
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_manual_identifica_duplicado_ja_baixado_e_bloqueia_botao(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Jr",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_baixado)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Use apenas para conferencia")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "Abrir Pix parecido")
        self.assertNotContains(resposta, "Abrir imagem do Pix parecido")
        self.assertEqual(resposta.content.count(b">Ignorar Pix sem baixa</button>"), 1)

    def test_central_pix_prioriza_duplicado_baixado_sobre_pendente(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Junior",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        pix_novo = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(pix_novo.pix_original, pix_baixado)
        self.assertEqual(pix_novo.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")

    def test_reler_ocr_no_detalhe_nao_envia_next_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix OCR detalhe",
            valor="10.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={reverse('estoque:central_pix')}",
            secure=True,
        )

        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})
        self.assertContains(resposta, f'action="{action}"')
        self.assertNotContains(resposta, f'action="{action}?next=')

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": True,
            "pagador": "Pix OCR detalhe",
            "valor": "10.00",
            "data_pagamento": "2026-05-18T17:45",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "Comprovante Pix",
        }):
            resposta_ocr = self.client.post(action, secure=True, follow=True)

        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_ocr, "Comprovante original")
        self.assertContains(resposta_ocr, f'href="{detalhe_url}"')

    def test_reler_ocr_preserva_bloqueio_quando_duplicado_ja_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente OCR Duplicado", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )
        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "Comprovante Pix sem leitura completa",
        }):
            resposta = self.client.post(action, secure=True, follow=True)

        pix.refresh_from_db()
        self.assertEqual(pix.pix_original, pix_baixado)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertContains(resposta, "Comprovante original")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")

    def test_detalhe_pix_sem_valor_nao_mostra_acao_de_baixa(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente Pix Sem Valor",
            valor="0.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertContains(resposta, "Confira/digite o valor do Pix")
        self.assertContains(resposta, "Usar este Pix na baixa")

    def test_detalhe_pix_usar_na_baixa_salva_dados_e_abre_recebimento_com_data_do_pix(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Conferido", ativo=True)
        self._criar_conta_receber_pix(cliente, "172.00")
        data_pix = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        pix = PixRecebido.objects.create(
            nome_pagador="Nome OCR antigo",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 25, 17, 10)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador conferido",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.nome_pagador, "Pagador conferido")
        self.assertEqual(str(pix.valor), "172.00")
        self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
        self.assertEqual(pix.instituicao_pix, "Nubank")
        self.assertContains(resposta, 'name="valor" type="text" inputmode="decimal" autocomplete="off" value="172,00"')
        self.assertContains(resposta, 'name="data_recebimento" type="date" value="2026-04-21"')
        self.assertContains(resposta, '<option value="PIX" selected>PIX</option>', html=True)
        self.assertContains(resposta, "nenhuma baixa foi feita ainda")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertNotEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(timezone.localtime(pix.data_pagamento), data_pix)

    def test_detalhe_pix_usar_na_baixa_sem_cliente_nao_avanca(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pagador sem cliente",
            valor="172.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": "",
                "nome_pagador": "Pagador sem cliente",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de usar este Pix na baixa.")
        self.assertContains(resposta, "baixaSemClienteConfirmado")
        self.assertContains(resposta, "event.preventDefault();")
        self.assertContains(resposta, "pix-client-attention")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_usar_na_baixa_sem_valor_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem valor",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem valor",
                "valor": "",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe o valor do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_usar_na_baixa_sem_data_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Data Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem data",
            valor="172.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem data",
                "valor": "172,00",
                "data_pagamento": "",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe a data do pagamento do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_baixa_bloqueia_pix_igual_a_baixado_mesmo_sem_vinculo_original(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Vinculo", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "96.30")
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:conta_receber_receber", kwargs={"pk": conta.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "observacao": "",
                "destino_diferenca": "troco",
                "usar_credito": "0",
                "credito_utilizado": "0,00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Ja existe Pix igual baixado")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_baixa_marca_pix_baixado_quando_id_vem_na_url(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "500.00")
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            {
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "500,00",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertIn(f"conta(s): {conta.id}", pix.observacao)
        self.assertIn("valor: R$ 500,00", pix.observacao)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_atual_marca_parecido_pendente_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Atual", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_parecido.pix_original, pix_atual)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_parecido_marca_pix_atual_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Parecido", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_parecido.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_atual.pix_original, pix_parecido)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

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

    def test_analisar_comprovante_pix_nubank_origem_nome_nao_usa_titulo_data(self):
        conteudo = (
            "Comprovante de transferência\n"
            "04 MAI 2026 - 10:49:17\n"
            "Valor R$ 400,00\n"
            "Tipo de transferência Pix\n"
            "Destino\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Origem\n"
            "Nome\n"
            "José Carlos da Silva Nascimento\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "CPF\n"
            "***.915.482-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "José Carlos da Silva Nascimento")
        self.assertNotIn("transferência", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "400.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-04T10:49")
        self.assertRegex(dados["instituicao_pix"], r"Nubank|NU PAGAMENTOS")

    def test_analisar_comprovante_pix_ton_stone_usa_dados_de_origem(self):
        conteudo = (
            "Comprovante de transfer\u00eancia\n"
            "14/05/2026 09:15\n"
            "Valor R$ 548,30\n"
            "Tipo Pix | Transfer\u00eancia\n"
            "\n"
            "DADOS DE DESTINO\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF ***.319.532-**\n"
            "Institui\u00e7\u00e3o\n"
            "NU PAGAMENTOS S.A. - INSTITUI\u00c7\u00c3O DE PAGAMENTO\n"
            "\n"
            "DADOS DE ORIGEM\n"
            "Nome\n"
            "Lisandra De Oliveira Da Silva\n"
            "CPF ***.157.092-**\n"
            "Institui\u00e7\u00e3o\n"
            "STONE INSTITUI\u00c7\u00c3O DE PAGAMENTO S.A.\n"
            "Ag\u00eancia 0001\n"
            "Conta 24279683-7\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("ton-stone.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "548.30")
        self.assertEqual(dados["data_pagamento"], "2026-05-14T09:15")
        self.assertEqual(dados["pagador"], "Lisandra De Oliveira Da Silva")
        self.assertEqual(dados["instituicao_pix"], "Stone")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotIn("DESTINO", dados["pagador"].upper())

    def test_analisar_comprovante_pix_caixa_tem_quem_vai_enviar_nao_usa_data(self):
        conteudo = (
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "Valor\n"
            "R$ 826,62\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "Dados da transa\u00e7\u00e3o\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertNotIn("maio de 2026", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertRegex(dados["instituicao_pix"], r"Caixa|Caixa Econ")

    def test_analisar_comprovante_pix_nubank_monta_pagador_origem_nome_quebrado(self):
        conteudo = (
            "Destino\n"
            "\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "\n"
            "Instituigaéo = NU PAGAMENTOS - IP\n"
            "\n"
            "Chave Pix +5591984111011\n"
            "\n"
            "Origem\n"
            "\n"
            "Maria Antonia Alves de\n"
            "\n"
            "Nome\n"
            "Paiva\n"
            "\n"
            "Instituigdo = NU PAGAMENTOS - IP\n"
            "\n"
            "CPF s+1.252.002-++\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["pagador"], "Maria Antonia Alves de Paiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

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

    def test_analisar_comprovante_pix_pagador_por_contexto_sem_usar_recebedor(self):
        casos = [
            (
                "mercado-pago-de.txt",
                "Mercado Pago\nComprovante de Pix\n17/05/2026 11:09\nR$ 100\nDe Ivanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nPara Lincoln Albuquerque Neiva\n",
                "Ivanildo Ferraz Patricio Junior",
            ),
            (
                "quem-pagou.txt",
                "Comprovante Pix\nQuem pagou\nNome RONISE DO SOCORRO DOS SANTOS FERREIRA\nQuem recebeu Lincoln Albuquerque Neiva\nValor R$ 76,00\n27 ABR 2026 - 08:04:01\n",
                "RONISE DO SOCORRO DOS SANTOS FERREIRA",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador_esperado in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador_esperado)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_extrai_pagador_de_em_comprovantes_reais(self):
        casos = [
            (
                "pagbank-roceli.txt",
                (
                    "Comprovante de envio de Pix\n"
                    "18/05/2026 \u00e0s 07:08:52\n"
                    "Valor da transferencia R$ 5,00\n"
                    "De\n"
                    "ROSELI DA COSTA GAMA\n"
                    "CPF ***.115.912-**\n"
                    "Instituicao\n"
                    "PagBank (PagSeguro Internet Instituicao de Pagamento S.A.)\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "mercado-pago-joao.txt",
                (
                    "Comprovante de Pix\n"
                    "23/maio/2026 \u00e0s 18:55:32\n"
                    "R$ 645\n"
                    "De\n"
                    "Joao de Almeida E Silva\n"
                    "CPF: ***.105.902-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Joao de Almeida E Silva",
                "645.00",
                "2026-05-23T18:55",
                "Mercado Pago",
            ),
            (
                "itau-devolucao-euclides.txt",
                (
                    "Comprovante de devolucao de Pix\n"
                    "R$ 5,00\n"
                    "Realizado em 19/05/2026 \u00e0s 17:18:36\n"
                    "De\n"
                    "EUCLIDES CARNEIRO NEIVA NETO\n"
                    "CPF/CNPJ: 787.484.883-72\n"
                    "Instituicao: ITAU UNIBANCO S.A\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
            (
                "mercado-pago-ivanildo.txt",
                (
                    "Comprovante de Pix\n"
                    "18/maio/2026 \u00e0s 17:45:00\n"
                    "R$ 500\n"
                    "De\n"
                    "Ivanildo Ferraz Patricio Junior\n"
                    "CPF: ***.188.882-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Ivanildo Ferraz Patricio Junior",
                "500.00",
                "2026-05-18T17:45",
                "Mercado Pago",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

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
        cliente = Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 51))
        pix_parecido = PixRecebido.objects.create(
            nome_pagador="Joelson Ferreira dos Santos",
            valor="156.50",
            data_pagamento=data_pagamento,
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(reverse("estoque:central_pix"), data={
            "cliente": cliente.id,
            "nome_pagador": "  joelson ferreira dos santos ",
            "valor": "156.50",
            "data_pagamento": "2026-05-16T17:52",
            "observacao": "Tentativa duplicada",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(PixRecebido.objects.count(), 2)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_parecido)
        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": novo_pix.id})
        self.assertTrue(any(url.startswith(detalhe_url) for url, _status in resposta.redirect_chain))
        mensagem = f"Pix salvo como possivel duplicado. Confira a comparacao com o Pix #{pix_parecido.id}."
        self.assertContains(resposta, mensagem, count=1)
        self.assertContains(resposta, f"Pix parecido #{pix_parecido.id}")
