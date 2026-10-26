"""Quantidade insuficiente: rascunho, confirmação explícita e persistência."""
import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from estoque.models import Produto, Venda, ItemVenda, Compra, ContaPagar, MovimentoFinanceiro
from estoque.tests_separacao_peso_fracionado import PesoFixtures
from estoque.views import _preparar_item_checklist_separacao
from offline.browser_support import Chrome, DevTools

OUT = Path(__file__).resolve().parents[1] / "amostras/separacao-insuficiente"


class InsuficienteBackendTests(PesoFixtures, TestCase):
    def test_passos_inteiros_e_fracoes_legitimas(self):
        for unidade, passo in (("UN", "1.000"), ("PC", "1.000"), ("KG", "0.001"), ("MIL", "0.001"), ("CX", "0.500")):
            sep, item, _ = self.create("4", unidade)
            self.assertEqual(_preparar_item_checklist_separacao(item).quantidade_passo_data, passo)
        sep, item, linha = self.create("4", "CX")
        p = linha.produto
        p.vende_fracionado = True
        p.fator_conversao = 4
        p.unidade_venda_2 = "UN"
        p.save()
        item = sep.itens.select_related("item_venda__produto").get()
        self.assertEqual(_preparar_item_checklist_separacao(item).quantidade_passo_data, "0.250")

    def test_confirma_falta_sem_alterar_nota_e_preserva_apos_reabertura(self):
        for unidade, valor in (("UN", "3"), ("CX", "2.5"), ("MIL", "1.125")):
            sep, item, linha = self.create("4", unidade)
            antes = list(ItemVenda.objects.filter(pk=linha.pk).values())
            resposta = self.save(sep, item, status="quantidade_insuficiente", value=valor)
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta.json()["separacao_status"], "com_pendencia")
            self.assertEqual(self.client.get(reverse("estoque:separacao_venda_detalhe", args=[sep.pk])).status_code, 200)
            item.refresh_from_db()
            self.assertEqual(item.status, "quantidade_insuficiente")
            self.assertEqual(item.quantidade_separada, Decimal(valor))
            self.assertEqual(item.quantidade_solicitada, Decimal("4"))
            self.assertEqual(list(ItemVenda.objects.filter(pk=linha.pk).values()), antes)

    def test_valor_inicial_solicitado_nao_pode_ser_gravado_como_falta(self):
        sep, item, _ = self.create("4", "UN")
        for valor in ("4", "0", "3.25", "5"):
            self.assertEqual(self.save(sep, item, status="quantidade_insuficiente", value=valor).status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.status, "pendente")
        self.assertIsNone(item.quantidade_separada)


class InsuficienteBrowserTests(PesoFixtures, StaticLiveServerTestCase):
    def test_fila_checklist_confirmacao_reabertura_desktop_mobile(self):
        OUT.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="insuficiente-browser-", ignore_cleanup_errors=True) as profile:
            browser = Chrome(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", profile).start()
            try:
                time.sleep(1)
                with urlopen(f"http://127.0.0.1:{browser.port}/json/list") as response:
                    targets = json.load(response)
                tab = DevTools(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"))
                tab.call("Page.enable")
                tab.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                for width in (1440, 390, 320):
                    for unidade, esperado in (("UN", "3"), ("CX", "3.5"), ("MIL", "3.999")):
                        sep, item, linha = self.create("4", unidade)
                        modelos = (Produto, Venda, ItemVenda, Compra, ContaPagar, MovimentoFinanceiro)
                        antes = [list(m.objects.order_by("pk").values()) for m in modelos]
                        tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
                        tab.call("Emulation.setTouchEmulationEnabled", {"enabled": width < 600})
                        url = self.live_server_url + reverse("estoque:separacao_venda_detalhe", args=[sep.pk])
                        tab.call("Page.navigate", {"url": url})
                        tab.wait('document.readyState==="complete" && !!document.querySelector("[data-sep-item]")')
                        self.assertFalse(tab.evaluate("!!document.querySelector('[data-sep-item].is-active')"))
                        tab.evaluate("window.card=document.querySelector('[data-sep-item]');card.querySelector('[data-reabrir-item]').click();window.q=card.querySelector('[data-qtd-input]');card.querySelector('[value=quantidade_insuficiente]').click();true")
                        self.assertEqual(tab.evaluate("q.value"), "4")
                        item.refresh_from_db()
                        self.assertIsNone(item.quantidade_separada)
                        # Confirmar o rascunho igual ao solicitado não pode reduzir/salvar silenciosamente.
                        tab.evaluate("card.querySelector('[data-insuficiente-confirmar]').click();true")
                        self.assertFalse(tab.evaluate("q.checkValidity()"))
                        tab.evaluate("card.querySelector('[data-step^=\"-\"]').click();true")
                        self.assertEqual(tab.evaluate("q.value"), esperado)
                        tab.evaluate("card.querySelector('[data-step^=\"-\"]').click();card.querySelector('[data-step]:not([data-step^=\"-\"])').click();true")
                        self.assertEqual(tab.evaluate("q.value"), esperado)
                        item.refresh_from_db()
                        self.assertIsNone(item.quantidade_separada)
                        self.assertEqual(item.status, "pendente")
                        self.assertFalse(tab.evaluate("card.classList.contains('is-done')"))
                        self.assertFalse(tab.evaluate("!!card.querySelector('[data-save-state].is-error')"))
                        # Entrada direta também permanece rascunho até a confirmação.
                        tab.evaluate("q.dispatchEvent(new Event('change',{bubbles:true}));true")
                        item.refresh_from_db()
                        self.assertIsNone(item.quantidade_separada)
                        if unidade == "UN":
                            shot = tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]
                            (OUT / f"editor-{width}.png").write_bytes(base64.b64decode(shot))
                        if unidade == "MIL":
                            tab.evaluate("q.focus();q.select();true")
                            tab.call("Input.insertText", {"text": esperado})
                            tab.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"})
                            tab.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
                        else:
                            tab.evaluate("document.documentElement.style.setProperty('scroll-behavior','auto','important');card.querySelector('[data-insuficiente-confirmar]').scrollIntoView({block:'center',behavior:'instant'});true")
                            point = tab.evaluate("(()=>{const r=card.querySelector('[data-insuficiente-confirmar]').getBoundingClientRect();return {x:r.x+r.width/2-visualViewport.offsetLeft,y:r.y+r.height/2-visualViewport.offsetTop}})()")
                            if width < 600:
                                tab.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
                                tab.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                            else:
                                for event in ("mousePressed", "mouseReleased"):
                                    tab.call("Input.dispatchMouseEvent", {"type": event, **point, "button": "left", "clickCount": 1})
                        tab.wait("card.dataset.confirmado==='true' && !card._sepSaving && !card.classList.contains('is-active')")
                        item.refresh_from_db(); sep.refresh_from_db()
                        self.assertEqual(item.quantidade_separada, Decimal(esperado))
                        self.assertEqual(item.quantidade_solicitada, Decimal("4"))
                        self.assertEqual(item.status, "quantidade_insuficiente")
                        self.assertEqual(sep.status, "com_pendencia")
                        self.assertIn("faltam", tab.evaluate("card.querySelector('[data-resultado-texto]').textContent"))
                        tab.evaluate("card.querySelector('[data-reabrir-item]').click();true")
                        self.assertEqual(tab.evaluate("q.value"), esperado)
                        self.assertLessEqual(tab.evaluate("document.documentElement.scrollWidth"), width)
                        tab.call("Page.navigate", {"url": url})
                        tab.wait('document.readyState==="complete" && !!document.querySelector("[data-sep-item]")')
                        tab.evaluate("window.card=document.querySelector('[data-sep-item]');window.q=card.querySelector('[data-qtd-input]');true")
                        self.assertIsNone(tab.evaluate("q"))
                        self.assertTrue(tab.evaluate("!!document.querySelector('[data-consulta-checklist]')"))
                        self.assertContains(self.client.get(reverse("estoque:separacao_venda_detalhe", args=[sep.pk])), f"Separado: {esperado}")
                        self.assertEqual(tab.evaluate("card.dataset.status"), "quantidade_insuficiente")
                        self.assertEqual(antes, [list(m.objects.order_by("pk").values()) for m in modelos])
                        tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:separacao_vendas_fila")})
                        tab.wait('document.readyState==="complete" && !!document.querySelector(".pend-list")')
                        self.assertIn("falta", tab.evaluate("document.body.textContent"))
            finally:
                browser.stop()
