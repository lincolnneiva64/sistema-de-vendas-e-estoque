import os
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.db import transaction
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .models import RodadaContagem, EventoInventario
from .tests import produto

CHROME = os.environ.get("INVENTARIO_TEST_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def capturar(tab, nome):
    if os.environ.get("INVENTARIO_VISUAL_ARTIFACTS"):
        pasta = Path(os.environ["INVENTARIO_VISUAL_ARTIFACTS"])
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / nome).write_bytes(base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]))


@skipUnless(Path(CHROME).is_file(), "Chrome não disponível")
class InventarioBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-browser", "", "teste")
        self.produto = produto()
        self.funcionario = Funcionario.objects.create(nome="Contador celular")
        self.inventario = services.criar_inventario(self.usuario, "Browser", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def abrir(self, tab):
        tab.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
        tab.call("Page.navigate", {"url": self.live_server_url + reverse("inventario:convite", args=[self.token])})
        tab.wait("!!document.querySelector('form button')")
        tab.evaluate("document.querySelector('form button').click();true")
        try:
            tab.wait("!!document.querySelector('form.contagem') && window.inventarioContagemPronta === true")
        except AssertionError:
            self.fail(str(tab.evaluate("({url:location.href, body:document.body.innerText.slice(0,4000)})")))

    def test_link_limitado_nao_acessa_outros_endpoints_no_chrome(self):
        with TemporaryDirectory(prefix="inventario-chrome-isolamento-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                urls = ["/vendas/", "/verificar-produto/", "/api/offline/snapshot/comercial/",
                    reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk]),
                    reverse("estoque:conferencia_estoque_corrigir", args=[self.produto.pk]),
                    reverse("inventario:detalhe", args=[self.inventario.pk])]
                resultados = tab.evaluate("(async()=>{const urls=" + json.dumps(urls) + ";return await Promise.all(urls.map(async url=>{const r=await fetch(url);return {status:r.status,body:await r.text()}}))})()")
                self.assertTrue(all(r["status"] == 403 for r in resultados), resultados)
                resultado = tab.evaluate("(async()=>{const t=document.querySelector('[name=csrfmiddlewaretoken]').value;const r=await fetch(" + json.dumps(urls[4]) + ",{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':t,'X-Operador-Venda':'Operador'},body:JSON.stringify({operador:'Operador',novo_estoque:'99'})});return r.status})()")
                self.assertEqual(resultado, 403)
                self.produto.refresh_from_db()
                self.assertEqual(self.produto.quantidade, 10)
            finally:
                chrome.stop()

    def test_mobile_progresso_f5_perda_resposta_ok_e_reabertura(self):
        with TemporaryDirectory(prefix="inventario-chrome-mobile-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                self.assertFalse(tab.evaluate("/esperado|custo_base|sequencia_ok|preco_compra/.test(document.documentElement.outerHTML)"))
                tab.evaluate("document.querySelector('[name=embalagens]').value='3';document.querySelector('[name=avulsos]').value='6';document.querySelector('[value=salvar]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('Progresso salvo')")
                tab.call("Page.reload")
                tab.wait("document.querySelector('[name=embalagens]')?.value === '3' && window.inventarioContagemPronta === true")
                self.assertEqual(tab.evaluate("document.querySelector('[name=avulsos]').value"), "6")
                capturar(tab, "funcionario-inteiros.png")
                # Server commits the OK, but transport loses its response.
                tab.evaluate("window.realFetch=fetch;window.fetch=async(...args)=>{const response=await realFetch(...args);throw new TypeError('Resposta perdida após gravação')};document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('Sem confirmação')")
                self.rodada.refresh_from_db()
                self.assertIsNotNone(self.rodada.ok_em)
                self.assertEqual(str(self.rodada.contado_base), "3.500")
                tab.call("Page.reload")
                tab.wait("document.querySelector('form.contagem [name=versao]')?.value === '2' && document.querySelector('fieldset')?.disabled === false")
                self.assertEqual(EventoInventario.objects.filter(acao="produto_ok").count(), 1)
                services.recontar(self.usuario, self.rodada.pk, "Recontagem autorizada")
                tab.call("Page.reload")
                tab.wait("document.body.textContent.includes('Nenhum produto disponível')")
            finally:
                chrome.stop()

    def test_gestor_tela_revisao_e_link_limitado(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "9", "0", True)
        from estoque.views import _baixar_estoque_produto
        with transaction.atomic():
            _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
        client = Client()
        client.force_login(self.usuario)
        with TemporaryDirectory(prefix="inventario-chrome-gestor-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("inventario:detalhe", args=[self.inventario.pk])})
                tab.wait("document.body.textContent.includes('Esperado no OK')")
                tab.wait("window.inventarioRevisaoPronta === true")
                self.assertEqual(tab.evaluate("document.querySelectorAll('.resumo-linha').length"), 1)
                self.assertTrue(tab.evaluate("document.querySelector('.tabela-resumo').scrollWidth <= document.querySelector('.tabela-resumo').clientWidth"))
                self.assertFalse(tab.evaluate("document.querySelector('.detalhe-produto').open"))
                capturar(tab, "administrativo-resumo.png")
                tab.evaluate("document.querySelector('a[data-detalhe]').click();true")
                tab.wait("document.querySelector('.detalhe-produto').open")
                self.assertEqual(tab.evaluate("document.querySelectorAll('input[name=acao][value=recontar]').length"), 1)
                self.assertTrue(tab.evaluate("document.querySelector('.resumo-linha').textContent.includes('Revisão necessária')"))
                self.assertTrue(tab.evaluate("document.querySelector('.movimento-digital').textContent.includes('Venda')"))
                self.assertTrue(tab.evaluate("document.querySelector('.movimento-digital').textContent.includes('-12 UN')"))
                self.assertTrue(tab.evaluate("document.querySelector('.movimento-digital').textContent.includes('Aguardando comprovação física')"))
                tab.evaluate("document.querySelector('.movimento-digital').closest('details').open=true;true")
                capturar(tab, "administrativo-movimentos.png")
                self.assertTrue(tab.evaluate("document.body.textContent.includes('Aplicação de ajustes desabilitada')"))
                self.assertFalse(tab.evaluate("!!document.querySelector('[value=aprovar]')"))
                self.assertTrue(tab.evaluate("!!document.querySelector('a[href$=\"relatorio.csv\"]')"))
            finally:
                chrome.stop()

    def test_kg_e_sinalizacao_fisica_apos_ok_sem_editar_contagem(self):
        from .models import SinalizacaoFisica
        kg = produto(nome="Peso browser", unidade_compra="KG", vende_fracionado=False)
        inventario = services.criar_inventario(self.usuario, "KG browser", "manual", [kg.pk])
        rodada = inventario.itens.get().rodadas.get()
        acesso, self.token = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [rodada.pk])
        with TemporaryDirectory(prefix="inventario-chrome-kg-sinalizacao-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                tab.evaluate("document.querySelector('[name=embalagens]').value='12.375';document.querySelector('[value=salvar]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('Progresso salvo')")
                tab.call("Page.reload")
                tab.wait("document.querySelector('[name=embalagens]')?.value === '12.375' && window.inventarioContagemPronta === true")
                tab.evaluate("document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('form.contagem [name=versao]').value === '2'")
                tab.evaluate("const f=document.querySelector('form.sinalizacao');f.closest('details').open=true;f.elements.descricao.value='Entrada física ainda não lançada';f.querySelector('button').click();true")
                tab.wait("document.querySelector('.mensagem-sinalizacao').textContent.includes('Sinalização enviada')")
                rodada.refresh_from_db()
                self.assertEqual(str(rodada.contado_base), "12.375")
                self.assertEqual(SinalizacaoFisica.objects.filter(rodada=rodada).count(), 1)
                self.assertFalse(tab.evaluate("document.querySelector('fieldset').disabled"))
                self.assertFalse(tab.evaluate("/esperado|custo_base|sequencia_ok|preco_compra/.test(document.documentElement.outerHTML)"))
                capturar(tab, "funcionario-kg-sinalizacao.png")
                kg.refresh_from_db()
                self.assertEqual(kg.quantidade, 10)
            finally:
                chrome.stop()
