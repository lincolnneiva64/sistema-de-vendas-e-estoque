"""Synthetic fixtures only; no production data or stock-writing service calls."""
import base64
import os
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.template import Context, Template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from PIL import Image, ImageChops, ImageDraw

from inventario.models import MovimentoObservado
from offline.browser_support import Chrome
from estoque.documentos_horarios import horario_documento, linhas_horarios
from estoque.models import (Cliente, Compra, Fornecedor, ItemCompra, ItemListaCompraFornecedor,
                            ItemVenda, ListaCompraFornecedor, MovimentacaoEstoqueManual, Produto, Venda)
from estoque import views

OUT = Path(os.environ.get("RASTREABILIDADE_VISUAL_ARTIFACTS", str(
    Path(__file__).resolve().parent.parent / "artifacts/rastreabilidade-horarios-continuacao-20261028")))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
INSTANTE = datetime(2026, 10, 25, 1, 2, 3, tzinfo=dt_timezone.utc)


class Fixture:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("auditoria-teste", "teste@example.test", "teste")
        self.client.force_login(self.user)
        self.produto = Produto.objects.create(nome="Império Lta 15/350Ml", quantidade=8,
            preco_compra=30, preco_vista=50, preco_prazo=55, unidade_venda_1="PCT")
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor fictício")
        self.cliente = Cliente.objects.create(nome="Cliente fictício")
        self.venda = Venda.objects.create(cliente=self.cliente, data_venda=date(2026, 10, 22), total=50)
        ItemVenda.objects.create(venda=self.venda, produto=self.produto, quantidade=1,
            unidade="PCT", preco_unitario=50, valor_total=50)
        self.compra = Compra.objects.create(fornecedor=self.fornecedor, data_compra=date(2026, 10, 22),
            total=50, estoque_entrada_realizada=True, estoque_entrada_realizada_em=INSTANTE)
        ItemCompra.objects.create(compra=self.compra, produto=self.produto, quantidade=1,
            unidade="PCT", preco_unitario=50, valor_total=50)
        self.lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,
            data_lista=date(2026, 10, 22), data_inicio_periodo=date(2026, 10, 1),
            data_fim_periodo=date(2026, 10, 22), total_lista=50)
        ItemListaCompraFornecedor.objects.create(lista=self.lista, produto=self.produto,
            quantidade_final=1, unidade="PCT", preco_unitario=50, total=50)
        for objeto in (self.venda, self.compra, self.lista):
            type(objeto).objects.filter(pk=objeto.pk).update(criado_em=INSTANTE, atualizado_em=INSTANTE)
            objeto.refresh_from_db()
        self.registro = MovimentacaoEstoqueManual.objects.create(produto=self.produto,
            tipo="ajuste", estoque_antes=8, estoque_depois=1, diferenca=-7,
            usuario=self.user, operador_nome="nome preservado", motivo="")
        MovimentacaoEstoqueManual.objects.filter(pk=self.registro.pk).update(criado_em=INSTANTE)

    def url_historico(self):
        return reverse("estoque:historico_estoque_manual", args=[self.produto.pk])

    def documentos(self):
        return [("venda_detalhe", self.venda.pk), ("compras_detalhe", self.compra.pk),
                ("compras_lista_fornecedor_ver", self.lista.pk),
                ("compras_lista_fornecedor_interno", self.lista.pk),
                ("compras_lista_fornecedor_detalhe", self.lista.pk)]


class HorariosApresentacaoTests(Fixture, TestCase):
    def test_fuso_explicito_independe_do_fuso_ativo_e_muda_data(self):
        with timezone.override("Asia/Tokyo"):
            self.assertEqual(horario_documento(INSTANTE), "24/10/2026 22:02:03")

    def test_legado_sem_horario_nao_inventa_meia_noite(self):
        for valor in (None, date(2026, 10, 22), datetime(2026, 10, 22)):
            self.assertEqual(horario_documento(valor), "Horário não registrado")
        html = Template("{% load horarios_documentos %}{% rodape_horarios documento %}").render(
            Context({"documento": SimpleNamespace()}))
        self.assertIn("Horário não registrado", html)
        self.assertNotIn("00:00:00", html)

    def test_rotulos_distinguem_atualizacao_entrada_e_ocorrencia(self):
        texto = "\n".join(linhas_horarios(self.compra))
        self.assertIn("Entrada registrada no sistema: 24/10/2026 às 22:02:03", texto)
        self.assertIn("Última atualização", texto)
        self.assertNotIn("Última edição", texto)
        self.assertIn("Horário de Belém (PA)", texto)

    def test_rodape_negrito_sem_horario_fixo_e_atualizacao_generica(self):
        modelo = Template("{% load horarios_documentos %}{% rodape_horarios documento %}")
        html = modelo.render(Context({"documento": self.venda}))
        self.assertIn('<strong style="font-weight:700;color:#1e293b">24/10/2026 às 22:02:03</strong>', html)
        self.assertIn("font-size:12px", html)
        self.assertIn("color:#334155", html)
        self.assertIn("sem comprovar edição comercial", html)
        outro = SimpleNamespace(criado_em=datetime(2026, 10, 28, 20, 4, 5, tzinfo=dt_timezone.utc))
        html_outro = modelo.render(Context({"documento": outro}))
        self.assertIn("28/10/2026 às 17:04:05", html_outro)
        self.assertNotIn("24/10/2026", html_outro)
        self.assertNotIn("Última atualização:", html_outro)

    def test_exportadores_data_em_negrito_e_contraste_escuro(self):
        chamadas = []
        original = ImageDraw.ImageDraw.text
        def observar(draw, xy, texto, *args, **kwargs):
            chamadas.append((texto, kwargs.get("font"), kwargs.get("fill")))
            return original(draw, xy, texto, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", observar):
            views._gerar_paginas_nota_whatsapp(self.venda)
            views._gerar_lista_fornecedor_whatsapp_imagem(self.lista)
            views._gerar_lista_fornecedor_interno_imagem(self.lista)
        datas = [(fonte, cor) for texto, fonte, cor in chamadas if texto == "24/10/2026 às 22:02:03"]
        self.assertEqual(len(datas), 6)
        for fonte, cor in datas:
            self.assertEqual(cor, "#1e293b")
            self.assertEqual(fonte.size, 24)
            self.assertIn("bold", fonte.getname()[1].lower())

    def test_documentos_html_mantem_data_comercial_e_rodape(self):
        for nome, pk in self.documentos():
            with self.subTest(nome=nome):
                resposta = self.client.get(reverse("estoque:" + nome, args=[pk]))
                self.assertIn("Registro no sistema: 24/10/2026 às 22:02:03", strip_tags(resposta.content.decode()))
                self.assertContains(resposta, "America/Belem")
                self.assertContains(resposta, "22/10/2026")
                self.assertNotContains(resposta, "Última edição:")
        # Main note and existing print copy each need their own footer.
        resposta = self.client.get(reverse("estoque:venda_detalhe", args=[self.venda.pk]))
        self.assertEqual(resposta.content.decode().count('class="documento-horarios"'), 2)

    def test_historico_identifica_valores_autoria_e_limitacao_unidade(self):
        resposta = self.client.get(self.url_historico())
        for texto in ["24/10/2026 às 22:02:03", "auditoria-teste", "nome preservado",
                      "Motivo não informado", "Império Lta 15/350Ml", "PCT",
                      "unidade histórica congelada", "Estoque anterior", "Estoque posterior", "-7"]:
            self.assertContains(resposta, texto)

    def test_historico_paginado_estavel_sem_consulta_por_registro(self):
        MovimentacaoEstoqueManual.objects.bulk_create([
            MovimentacaoEstoqueManual(produto=self.produto, tipo="ajuste", estoque_antes=1,
                estoque_depois=Decimal("1.5"), diferenca=Decimal("0.5"), usuario=self.user,
                motivo=f"Registro {i}") for i in range(26)])
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(self.url_historico())
        self.assertEqual(len(resposta.context["pagina"]), 25)
        self.assertContains(resposta, "Página 1 de 2")
        self.assertContains(resposta, "+0,5")
        usuarios = [q for q in consultas if 'FROM "auth_user"' in q["sql"]]
        self.assertLessEqual(len(usuarios), 1)
        pagina2 = self.client.get(self.url_historico(), {"pagina": 2})
        self.assertEqual(len(pagina2.context["pagina"]), 2)
        for valor in ("invalida", "-1", "999999"):
            self.assertEqual(self.client.get(self.url_historico(), {"pagina": valor}).status_code, 200)

    def test_historico_usuario_removido_e_escape_de_texto(self):
        self.registro.usuario = None
        self.registro.motivo = '<script>alert("x")</script>'
        self.registro.save(update_fields=["usuario", "motivo"])
        resposta = self.client.get(self.url_historico())
        self.assertContains(resposta, "Não identificado")
        self.assertContains(resposta, "nome preservado")
        self.assertContains(resposta, "&lt;script&gt;")
        self.assertNotContains(resposta, '<script>alert("x")</script>')

    def test_referencias_665_667_668_em_registros_inteiramente_sinteticos(self):
        itaipava = Produto.objects.create(nome="Itaipava 12/269Ml", quantidade=0,
            preco_compra=20, preco_vista=30, preco_prazo=35, unidade_venda_1="PCT")
        for pk, produto, antes, depois, hora in [
            (665, itaipava, "0", "5", "09:46:02"),
            (667, self.produto, "8", "1", "17:04:51"),
            (668, self.produto, "1", "1.5", "17:05:00"),
        ]:
            registro = MovimentacaoEstoqueManual.objects.create(pk=pk, produto=produto,
                tipo="ajuste", estoque_antes=Decimal(antes), estoque_depois=Decimal(depois),
                diferenca=Decimal(depois) - Decimal(antes), usuario=self.user, operador_nome="lincoln")
            instante = datetime.fromisoformat(f"2026-10-24T{hora}-03:00")
            MovimentacaoEstoqueManual.objects.filter(pk=registro.pk).update(criado_em=instante)
            resposta = self.client.get(reverse("estoque:historico_estoque_manual", args=[produto.pk]))
            self.assertContains(resposta, f"#{pk}")
            self.assertContains(resposta, f"24/10/2026 às {hora}")
            self.assertContains(resposta, "Motivo não informado")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("8"))

    def test_historico_aceita_autorizacao_temporaria_sem_login_comercial(self):
        self.client.logout()
        self.client.post(reverse("estoque:conferencia_estoque_autorizar"),
            {"username": "auditoria-teste", "password": "teste"})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(self.url_historico()).status_code, 200)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.url_historico()).status_code, 403)

    def test_historico_preserva_permissoes_e_cegueira(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url_historico()).status_code, 403)
        usuario = get_user_model().objects.create_user("sem-permissao")
        self.client.force_login(usuario)
        self.assertEqual(self.client.get(self.url_historico()).status_code, 403)
        usuario.user_permissions.add(Permission.objects.get(codename="view_produto", content_type__app_label="estoque"))
        self.assertEqual(self.client.get(self.url_historico()).status_code, 200)
        self.assertEqual(self.client.post(self.url_historico()).status_code, 405)
        # A counting credential alone must never grant commercial stock access.
        self.client.logout()
        from django.contrib.sessions.backends.db import SessionStore
        sessao = SessionStore()
        sessao["inventario_acesso"] = "acesso-cego"
        sessao.save()
        self.client.cookies["inventario_sessionid"] = sessao.session_key
        self.assertEqual(self.client.get(self.url_historico()).status_code, 403)

    def test_consulta_nao_altera_saldos_historicos_ou_movimentos(self):
        antes = (list(Produto.objects.values()), list(MovimentacaoEstoqueManual.objects.values()),
                 list(MovimentoObservado.objects.values()))
        self.client.get(self.url_historico())
        for nome, pk in self.documentos():
            self.client.get(reverse("estoque:" + nome, args=[pk]))
        depois = (list(Produto.objects.values()), list(MovimentacaoEstoqueManual.objects.values()),
                  list(MovimentoObservado.objects.values()))
        self.assertEqual(antes, depois)

    def test_imagens_listas_estendem_canvas_sem_cobrir_conteudo(self):
        for funcao in (views._gerar_lista_fornecedor_whatsapp_imagem,
                       views._gerar_lista_fornecedor_interno_imagem):
            with self.subTest(funcao=funcao.__name__):
                with patch.object(views, "acrescentar_rodape_imagem", side_effect=lambda imagem, *_: imagem):
                    original = Image.open(funcao(self.lista)).convert("RGB")
                saida = Image.open(funcao(self.lista)).convert("RGB")
                self.assertGreater(saida.height, original.height)
                self.assertIsNone(ImageChops.difference(original, saida.crop((0, 0, original.width, original.height))).getbbox())
                self.assertIsNotNone(ImageChops.difference(saida.crop((0, original.height, saida.width, saida.height)),
                    Image.new("RGB", (saida.width, saida.height - original.height), "white")).getbbox())

    def test_nota_imagem_pdf_multipagina_preserva_itens_e_rodape(self):
        OUT.mkdir(parents=True, exist_ok=True)
        for i in range(22):
            ItemVenda.objects.create(venda=self.venda, produto=self.produto, quantidade=Decimal("1.175"),
                unidade="PCT", preco_unitario=50, valor_total=Decimal("58.75"))
        textos = []
        original = ImageDraw.ImageDraw.text
        def observar(draw, xy, texto, *args, **kwargs):
            textos.append((xy, texto))
            return original(draw, xy, texto, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", observar):
            paginas = views._gerar_paginas_nota_whatsapp(self.venda)
        self.assertGreater(len(paginas), 1)
        self.assertEqual(sum(texto == self.produto.nome for _, texto in textos), 23)
        self.assertTrue(any(texto == "Registro no sistema: " for _, texto in textos))
        self.assertTrue(any(texto == "24/10/2026 às 22:02:03" for _, texto in textos))
        for (x, y), texto in textos:
            if "Registro no sistema:" in texto:
                self.assertLess(y + 24, 1600)
        pdf = views._gerar_nota_whatsapp_pdf(self.venda).getvalue()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(f"/Count {len(paginas)}".encode(), pdf)
        (OUT / "nota-multipagina.pdf").write_bytes(pdf)
        imagem = views._gerar_nota_whatsapp_imagem(self.venda)
        (OUT / "nota-multipagina.png").write_bytes(imagem.getvalue())
        for nome, funcao in [("lista-fornecedor", views._gerar_lista_fornecedor_whatsapp_imagem),
                             ("lista-interna", views._gerar_lista_fornecedor_interno_imagem)]:
            (OUT / (nome + ".png")).write_bytes(funcao(self.lista).getvalue())


@skipUnless(Path(CHROME).is_file(), "Chrome indisponível")
class HorariosBrowserTests(Fixture, StaticLiveServerTestCase):
    def test_documentos_historico_mobile_desktop_e_impressao(self):
        OUT.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="horarios-chrome-") as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value,
                                               "url": self.live_server_url})
                # A different browser zone must not change server-rendered timestamps.
                tab.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Tokyo"})
                for width in (1366, 390, 320):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900,
                        "deviceScaleFactor": 1, "mobile": width < 768})
                    for nome, pk in self.documentos() + [("historico_estoque_manual", self.produto.pk)]:
                        tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:" + nome, args=[pk])})
                        esperado = "24/10/2026 às 22:02:03"
                        tab.wait(f'document.readyState === "complete" && document.body.textContent.includes("{esperado}")')
                        seletor = ".ajustes-registro" if nome == "historico_estoque_manual" else ".documento-horarios"
                        dados = tab.evaluate(f'''[...document.querySelectorAll('{seletor}')].filter(e=>e.getBoundingClientRect().width>0).map(e=>({{left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,width:e.clientWidth,scroll:e.scrollWidth}}))''')
                        self.assertTrue(dados, (width, nome))
                        if nome != "historico_estoque_manual":
                            estilo = tab.evaluate('''(()=>{const el=document.querySelector('.documento-horarios');const strong=el.querySelector('strong');return {size:parseFloat(getComputedStyle(el).fontSize),color:getComputedStyle(el).color,weight:parseInt(getComputedStyle(strong).fontWeight)}})()''')
                            self.assertGreaterEqual(estilo["size"], 12)
                            self.assertGreaterEqual(estilo["weight"], 700)
                            self.assertEqual(estilo["color"], "rgb(51, 65, 85)")
                        for dado in dados:
                            self.assertGreaterEqual(dado["left"], -1)
                            self.assertLessEqual(dado["right"], width + 1, (width, nome, dado))
                            self.assertLessEqual(dado["scroll"], dado["width"] + 1)
                        captura = tab.call("Page.captureScreenshot", {"format": "png"})
                        (OUT / f"{nome}-{width}.png").write_bytes(base64.b64decode(captura["data"]))
                        tab.call("Emulation.setEmulatedMedia", {"media": "print"})
                        self.assertTrue(tab.evaluate(f'''[...document.querySelectorAll('{seletor}')].some(e=>getComputedStyle(e).display!=='none')'''))
                        pdf = tab.call("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True})
                        self.assertTrue(base64.b64decode(pdf["data"]).startswith(b"%PDF"))
                        if width == 1366:
                            (OUT / (nome + "-impressao.pdf")).write_bytes(base64.b64decode(pdf["data"]))
                        tab.call("Emulation.setEmulatedMedia", {"media": "screen"})
                # Check the modal formatter in a browser with a non-local timezone.
                tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.auditErrors=[];addEventListener('error',e=>auditErrors.push(e.message));"})
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:vendas")})
                tab.wait('document.readyState === "complete" && !!window.salesDraftBridge')
                tab.evaluate('document.getElementById("produtoBusca").value="Império";document.getElementById("produtoBusca").dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('!!document.querySelector(".estoque-item-sugestao")')
                tab.evaluate('document.querySelector(".estoque-item-sugestao").click();true')
                tab.wait('document.getElementById("modalConferenciaEstoqueHistorico").textContent.includes("24/10/2026 às 22:02:03")')
                texto = tab.evaluate('document.getElementById("modalConferenciaEstoqueHistorico").textContent')
                self.assertIn("Motivo não informado", texto)
                self.assertIn("nome preservado", texto)
                self.assertIn("-7", texto)
                self.assertIn(f"/{self.produto.pk}/", tab.evaluate('document.getElementById("modalConferenciaEstoqueHistoricoLink").href'))
                self.assertEqual(tab.evaluate("window.auditErrors"), [])
                captura = tab.call("Page.captureScreenshot", {"format": "png"})
                (OUT / "modal-historico-320.png").write_bytes(base64.b64decode(captura["data"]))
            finally:
                chrome.stop()
