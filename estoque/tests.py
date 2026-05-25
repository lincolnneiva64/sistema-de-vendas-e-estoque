import io
import tempfile
import types
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import FuncionarioForm, PixRecebidoForm
from .models import Cliente, ContaReceber, CreditoCliente, Funcionario, PixRecebido, RecebimentoContaReceber, Venda
from .utils_pix import analisar_comprovante_pix
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

    def _imagem_pix_teste(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (900, 1600), "white").save(buffer, format="PNG")
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
        self.assertContains(resposta_lista, "Pix pendente detalhe")
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

    def test_detalhe_pix_processar_ocr_com_erro_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix", side_effect=RuntimeError("timeout render")):
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
            self.assertIn("OCR nao concluido no Render", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("arquivo=comprovante_", "\n".join(logs.output))
            self.assertIn("timeout render", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

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

            with patch("estoque.views.analisar_comprovante_pix", return_value=dados_timeout):
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
            self.assertIn("OCR nao concluido no Render", pix.texto_ocr_bruto)
            self.assertNotIn("Tesseract process timeout", pix.texto_ocr_bruto)
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
            if recorte == "rapido_superior":
                return (
                    "21 ABR 2026 - 13:05:01\n"
                    "Valor R$ 172,00\n"
                    "Tipo de transferencia Pix\n"
                )
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
                comprovante=SimpleUploadedFile("comprovante-topo.png", self._imagem_pix_teste(), content_type="image/png"),
            )

            with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
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
            self.assertEqual(chamadas, ["rapido_superior"])
            self.assertEqual(configs, ["--oem 1 --psm 6"])
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("Valor R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("modo leve parou cedo recorte=rapido_superior", "\n".join(logs.output))
            self.assertIn("config=--oem 1 --psm 6", "\n".join(logs.output))
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_timeout_rapido_aproveita_segundo_recorte(self):
        chamadas = []
        timeouts = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            timeouts.append(kwargs.get("timeout"))
            if recorte == "rapido_superior":
                raise RuntimeError("Tesseract process timeout")
            if recorte == "rapido_meio_superior":
                return (
                    "21 ABR 2026 - 13:05:01\n"
                    "Valor R$ 172,00\n"
                    "Tipo de transferencia Pix\n"
                )
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
                comprovante=SimpleUploadedFile("comprovante-render.png", self._imagem_pix_teste(), content_type="image/png"),
            )

            with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
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
            self.assertEqual(chamadas, ["rapido_superior", "rapido_meio_superior"])
            self.assertEqual(timeouts, [3, 3])
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("Valor R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("recorte=rapido_superior excecao=RuntimeError: padrao: RuntimeError: Tesseract process timeout", "\n".join(logs.output))
            self.assertIn("modo leve parou cedo recorte=rapido_meio_superior", "\n".join(logs.output))

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
        self.assertNotContains(resposta, "Usar este Pix na baixa")

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
