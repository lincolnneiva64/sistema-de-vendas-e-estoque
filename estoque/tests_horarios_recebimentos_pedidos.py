"""Synthetic receipts and orders only, in the guarded disposable test database."""
import base64
import copy
import re
from datetime import date, datetime, timedelta, timezone as tz
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlencode, unquote

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from PIL import Image, ImageChops, ImageDraw

from offline.browser_support import Chrome
from estoque import views
from estoque.models import (Cliente, Produto, ContaReceber, OperacaoRecebimentoCliente,
    RecebimentoContaReceber, FechamentoRotaRecebimento, Pedido, ItemPedido,
    MovimentoFinanceiro, ContaFinanceira, CreditoCliente)
from estoque.horarios_recebimentos import horario_legivel, campos_comprovante

OUT = Path(__file__).resolve().parent.parent / "artifacts/horarios-recebimentos-pedidos-20261028"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
INSTANTE = datetime(2026, 10, 25, 1, 2, 3, tzinfo=tz.utc)
REGISTRO = "24/10/2026 às 22:02:03"
COMERCIAL = date(2026, 10, 22)


class Fixture:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("horarios-financeiros", "teste@example.test", "teste")
        self.client.force_login(self.user)
        self.cliente = Cliente.objects.create(nome="Cliente fictício de horários", bairro="Rota Centro", whatsapp="91999999999")
        self.produto = Produto.objects.create(nome="Produto fictício de pedido", quantidade=10,
            preco_compra=10, preco_vista=50, preco_prazo=55, unidade_venda_1="UN")
        self.conta = ContaReceber.objects.create(cliente=self.cliente, data_emissao=COMERCIAL,
            data_vencimento=date(2026, 11, 22), valor_original=100, valor_em_aberto=50, status="parcial")
        self.op = OperacaoRecebimentoCliente.objects.create(cliente=self.cliente,
            cliente_nome_snapshot=self.cliente.nome, valor_recebido=50, valor_aplicado=50,
            saldo_anterior=100, saldo_atual=50, data_recebimento=COMERCIAL,
            forma_pagamento="Dinheiro", rota_snapshot="Rota Centro", criado_por=self.user,
            comprovante_dados={"cliente_nome": self.cliente.nome, "data_recebimento": "22/10/2026",
                "valor_pago": "50.00", "saldo_anterior": "100.00", "saldo_atual": "50.00",
                "forma_pagamento": "Dinheiro", "contas": [{"conta_id": self.conta.pk,
                    "valor_aplicado": "50.00", "saldo_restante": "50.00", "quitada": False}],
                "contas_abertas": [{"conta_id": self.conta.pk, "saldo_atual": "50.00"}]})
        self.recebimento = RecebimentoContaReceber.objects.create(conta=self.conta, operacao=self.op,
            data_recebimento=COMERCIAL, valor=50, forma_pagamento="Dinheiro")
        self.fechamento = FechamentoRotaRecebimento.objects.create(rota="Rota Centro", data_referencia=COMERCIAL,
            status="finalizado", total_sistema=50, total_conferido=50, diferenca=0, usuario=self.user, criado_por=self.user)
        self.pedido = Pedido.objects.create(cliente=self.cliente, data_pedido=COMERCIAL, total=100, operador="Fictício")
        ItemPedido.objects.create(pedido=self.pedido, produto=self.produto, quantidade=2, unidade="UN",
                                 preco_unitario=50, valor_total=100)
        for obj in [self.conta, self.op, self.pedido]:
            type(obj).objects.filter(pk=obj.pk).update(criado_em=INSTANTE, atualizado_em=INSTANTE + timedelta(hours=1))
            obj.refresh_from_db()
        RecebimentoContaReceber.objects.filter(pk=self.recebimento.pk).update(criado_em=INSTANTE)
        FechamentoRotaRecebimento.objects.filter(pk=self.fechamento.pk).update(created_at=INSTANTE,
            updated_at=INSTANTE + timedelta(hours=1))
        self.fechamento.refresh_from_db()

    def paginas(self):
        params = "?" + urlencode({"data": COMERCIAL.isoformat(), "rota": "Rota Centro"})
        return [
            ("receber_cliente", reverse("estoque:receber_cliente", args=[self.cliente.pk])),
            ("conta_receber_receber", reverse("estoque:conta_receber_receber", args=[self.conta.pk])),
            ("contas_receber", reverse("estoque:contas_receber")),
            ("receber_cliente_confirmado", reverse("estoque:receber_cliente_confirmado", args=[self.cliente.pk, self.op.pk])),
            ("receber_cliente_recebimentos_dia", reverse("estoque:receber_cliente_recebimentos_dia") + params),
            ("receber_cliente_recebimentos_rota", reverse("estoque:receber_cliente_recebimentos_rota") + params),
            ("conferencia_recebimentos_rota", reverse("estoque:conferencia_recebimentos_rota") + params),
            ("recibos_pendentes", reverse("estoque:recebimentos_recibos_pendentes") + "?periodo=todos"),
            ("corrigir_recebimento", reverse("estoque:receber_cliente_corrigir_recebimento", args=[self.op.pk])),
            ("pedidos", reverse("estoque:pedidos")),
            ("pedido_detalhe", reverse("estoque:pedido_detalhe", args=[self.pedido.pk])),
        ]

    def retrato(self):
        return [list(model.objects.order_by("pk").values()) for model in [Produto, ContaReceber,
            OperacaoRecebimentoCliente, RecebimentoContaReceber, FechamentoRotaRecebimento,
            MovimentoFinanceiro, CreditoCliente, Pedido, ItemPedido]]


class HorariosTests(Fixture, TestCase):
    def test_paginas_registros_antigos_com_segundos_e_fuso_explicito(self):
        with timezone.override("Asia/Tokyo"):
            for nome, url in self.paginas():
                with self.subTest(pagina=nome):
                    resposta = self.client.get(url)
                    self.assertEqual(resposta.status_code, 200)
                    self.assertIn(REGISTRO, strip_tags(resposta.content.decode()))
                    self.assertRegex(resposta.content.decode(), r'<strong\b[^>]*>(?:<time[^>]*>)?' + re.escape(REGISTRO))

    def test_todas_consultas_preservam_valores_saldos_e_snapshots(self):
        antes = self.retrato()
        for nome, url in self.paginas():
            self.assertEqual(self.client.get(url).status_code, 200, nome)
        self.assertEqual(antes, self.retrato())

    def test_adapter_nao_regrava_snapshot_e_descarta_horario_falso_do_json(self):
        original = copy.deepcopy(self.op.comprovante_dados)
        original["_horarios_registro"] = [["Registro no sistema", "2000-01-01T00:00:00Z"]]
        OperacaoRecebimentoCliente.objects.filter(pk=self.op.pk).update(comprovante_dados=original)
        self.op.refresh_from_db()
        dados = views._dados_comprovante_operacao_recebimento(self.op)
        self.assertEqual(self.op.comprovante_dados, original)
        self.assertEqual(campos_comprovante(dados)[0][1], INSTANTE.isoformat())
        self.op.refresh_from_db()
        self.assertEqual(self.op.comprovante_dados, original)

    def test_data_comercial_registro_e_fechamento_sao_distintos(self):
        resposta = self.client.get(reverse("estoque:receber_cliente_confirmado", args=[self.cliente.pk, self.op.pk]))
        texto = strip_tags(resposta.content.decode())
        self.assertIn("Data informada: 22/10/2026", texto)
        self.assertIn("Registro no sistema: " + REGISTRO, texto)
        url = reverse("estoque:conferencia_recebimentos_rota") + "?" + urlencode({"rota": "Rota Centro", "data": COMERCIAL.isoformat()})
        texto = strip_tags(self.client.get(url).content.decode())
        self.assertIn("Registro do fechamento", texto)
        self.assertIn("não representa um horário físico independente", texto)
        self.assertNotIn("Finalizada em", texto)

    def test_envio_dispensa_e_idempotencia_sem_mudar_financeiro(self):
        financeiro_antes = (self.conta.valor_em_aberto, self.op.valor_recebido, MovimentoFinanceiro.objects.count())
        url = reverse("estoque:receber_cliente_confirmar_recibo", args=[self.cliente.pk, self.op.pk])
        primeira = self.client.post(url)
        self.assertEqual(primeira.status_code, 200)
        self.op.refresh_from_db()
        instante = self.op.recibo_confirmado_em
        segunda = self.client.post(url)
        self.assertEqual(segunda.json()["confirmado_em"], instante.isoformat())
        self.op.refresh_from_db()
        self.assertEqual(self.op.recibo_confirmado_em, instante)
        self.conta.refresh_from_db()
        self.assertEqual(financeiro_antes, (self.conta.valor_em_aberto, self.op.valor_recebido, MovimentoFinanceiro.objects.count()))
        dados = views._dados_comprovante_operacao_recebimento(self.op)
        self.assertIn(("Confirmação de envio do recibo", instante.isoformat()), campos_comprovante(dados))
        OperacaoRecebimentoCliente.objects.filter(pk=self.op.pk).update(status_recibo="dispensado",
            recibo_dispensado_em=INSTANTE, recibo_confirmado_em=None)
        self.op.refresh_from_db()
        self.assertIn(("Dispensa do recibo", INSTANTE.isoformat()), campos_comprovante(views._dados_comprovante_operacao_recebimento(self.op)))
        self.assertEqual(self.client.post(url).status_code, 409)

    def test_horarios_ausentes_nao_viram_meia_noite_ou_agora(self):
        for valor in [None, "", "data inválida", COMERCIAL, datetime(2026, 10, 22), "2026-10-22"]:
            self.assertEqual(horario_legivel(valor), "Horário não registrado")
        dados = views._dados_comprovante_operacao_recebimento(self.op)
        self.assertNotIn("Confirmação de envio do recibo", [rotulo for rotulo, _ in campos_comprovante(dados)])

    def test_whatsapp_texto_tem_valores_originais_e_horarios_confiaveis(self):
        antes = self.retrato()
        dados = views._dados_comprovante_operacao_recebimento(self.op)
        mensagem = views._montar_whatsapp_confirmacao_recebimento(self.cliente, dados)
        self.assertIn(REGISTRO, mensagem["mensagem"])
        self.assertIn("*" + REGISTRO + "*", mensagem["mensagem"])
        self.assertIn("Total pago: R$ 50,00", mensagem["mensagem"])
        self.assertIn(REGISTRO, unquote(mensagem["url"]))
        legado = views._montar_mensagem_curta_confirmacao_recebimento(dict(self.op.comprovante_dados))
        self.assertIn("Horário não registrado", legado)
        self.assertEqual(antes, self.retrato())

    def imagens(self):
        dados = views._dados_comprovante_operacao_recebimento(self.op)
        resumo = views._resumo_fechamento_rota(self.fechamento)
        conferencia = views._resumo_conferencia_recebimentos_rota("Rota Centro", COMERCIAL)
        return [("comprovante", views._gerar_comprovante_recebimento_imagem, (dados,)),
                ("card", views._gerar_recibo_recebimento_whatsapp_card, (dados,)),
                ("fechamento", views._gerar_conferencia_recebimentos_rota_imagem,
                 ("Rota Centro", COMERCIAL, conferencia, resumo, Decimal("0")))]

    def test_imagens_estendem_canvas_preservam_todos_os_pixels_dos_valores(self):
        OUT.mkdir(parents=True, exist_ok=True)
        antes = self.retrato()
        for nome, gerar, args in self.imagens():
            with self.subTest(imagem=nome):
                with patch.object(views, "imagem_com_horarios", side_effect=lambda imagem, *_: imagem):
                    original = Image.open(gerar(*args)).convert("RGB")
                imagem_bytes = gerar(*args).getvalue()
                imagem = Image.open(__import__("io").BytesIO(imagem_bytes)).convert("RGB")
                self.assertGreater(imagem.height, original.height)
                self.assertEqual(imagem.width, original.width)
                self.assertIsNone(ImageChops.difference(original, imagem.crop((0, 0, original.width, original.height))).getbbox())
                (OUT / (nome + ".png")).write_bytes(imagem_bytes)
        self.assertEqual(antes, self.retrato())

    def test_card_estreito_horarios_nao_cortam_nem_perdem_negrito(self):
        chamadas = []
        original = ImageDraw.ImageDraw.text
        def observar(draw, xy, texto, *args, **kwargs):
            chamadas.append((xy, texto, kwargs["font"], draw.textlength(texto, font=kwargs["font"])))
            return original(draw, xy, texto, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", observar):
            views._gerar_recibo_recebimento_whatsapp_card(views._dados_comprovante_operacao_recebimento(self.op))
        datas = [(xy, fonte, largura) for xy, texto, fonte, largura in chamadas if texto == REGISTRO]
        self.assertTrue(datas)
        for (x, _), fonte, largura in datas:
            self.assertLessEqual(x + largura, 480)
            self.assertIn("bold", fonte.getname()[1].lower())

    def test_acesso_anonimo_sem_permissao_e_cliente_errado_nao_expoem_dados(self):
        anonimo = Client()
        url = reverse("estoque:receber_cliente_confirmado", args=[self.cliente.pk, self.op.pk])
        self.assertIn(anonimo.get(url).status_code, (302, 401, 403))
        leitor = get_user_model().objects.create_user("sem-permissao-horarios", password="teste")
        anonimo.force_login(leitor)
        self.assertEqual(anonimo.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("estoque:receber_cliente_operacao_recibo_card_imagem", args=[99999, self.op.pk])).status_code, 404)

    def test_gravacao_recebimento_continua_distribuindo_sem_metadados_no_snapshot(self):
        resultado = views._criar_operacao_recebimento_cliente(self.cliente, Decimal("20"), COMERCIAL,
            "Dinheiro", usuario=self.user, hoje_referencia=COMERCIAL)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_em_aberto, Decimal("30"))
        nova = OperacaoRecebimentoCliente.objects.order_by("-id").first()
        self.assertEqual(nova.valor_aplicado, Decimal("20"))
        self.assertEqual(nova.recebimentos.get().valor, Decimal("20"))
        self.assertNotIn("_horarios_registro", nova.comprovante_dados)
        antes = self.retrato()
        views._dados_comprovante_operacao_recebimento(nova)
        self.assertEqual(antes, self.retrato())


@skipUnless(Path(CHROME).is_file(), "Chrome indisponível")
class HorariosBrowserTests(Fixture, StaticLiveServerTestCase):
    def test_desktop_mobile_impressao_e_confirmacao_sem_enviar_whatsapp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="horarios-recebimentos-chrome-") as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Tokyo"})
                tab.call("Network.setBlockedURLs", {"urls": ["*wa.me/*", "*api.whatsapp.com/*", "*web.whatsapp.com/*"]})
                for largura in (1366, 390, 320):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 900, "deviceScaleFactor": 1, "mobile": largura < 768})
                    for nome, url in self.paginas():
                        tab.call("Page.navigate", {"url": self.live_server_url + url})
                        tab.wait('document.readyState === "complete" && document.body.textContent.includes("' + REGISTRO + '")')
                        dimensoes = tab.evaluate('''[...document.querySelectorAll('.horario-registro,.documento-horarios strong')].filter(e=>e.getBoundingClientRect().width>0).map(e=>({width:e.getBoundingClientRect().width,parent:e.parentElement.getBoundingClientRect().width,weight:getComputedStyle(e).fontWeight}))''')
                        self.assertTrue(dimensoes, (nome, largura))
                        for d in dimensoes:
                            self.assertGreaterEqual(int(d["weight"]), 700)
                            self.assertLessEqual(d["width"], max(d["parent"], 1) + 1, (nome, largura, d))
                        tab.evaluate('document.querySelector(".horario-registro,.documento-horarios strong").scrollIntoView({block:"center"});true')
                        (OUT / f"{nome}-{largura}.png").write_bytes(base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png"})["data"]))
                        if largura == 1366:
                            tab.call("Emulation.setEmulatedMedia", {"media": "print"})
                            pdf = tab.call("Page.printToPDF", {"printBackground": True})
                            (OUT / (nome + "-impressao.pdf")).write_bytes(base64.b64decode(pdf["data"]))
                            tab.call("Emulation.setEmulatedMedia", {"media": "screen"})
                url = reverse("estoque:receber_cliente_confirmado", args=[self.cliente.pk, self.op.pk])
                tab.call("Page.navigate", {"url": self.live_server_url + url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("btn-confirmar-recibo-enviado")')
                tab.evaluate('document.getElementById("btn-enviar-confirmacao-whatsapp").addEventListener("click",e=>e.preventDefault());document.getElementById("btn-enviar-confirmacao-whatsapp").click();true')
                tab.evaluate('document.getElementById("btn-confirmar-recibo-enviado").click();true')
                tab.wait('document.querySelector(".rcp-page").dataset.statusRecibo === "enviado"')
                self.op.refresh_from_db()
                self.assertEqual(tab.evaluate('document.getElementById("horarioConfirmacaoRecibo").textContent'), horario_legivel(self.op.recibo_confirmado_em))
            finally:
                chrome.stop()
