"""Visual-only confirmation: real preview endpoint, isolated SQLite and private Chrome."""
import base64
import json
import tempfile
from pathlib import Path
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from offline.browser_support import Chrome
from offline import test_sales_sidebars_browser as sidebars
from . import tests_precos_vinculados_interfaces as fixtures
from .models import Produto, GrupoProdutoVinculado, MembroGrupoProduto, AlteracaoPrecoVinculado, Compra, MovimentoFinanceiro

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@skipUnless(Path(CHROME).is_file(), "Chrome unavailable")
class ModalPrecosVisualTests(StaticLiveServerTestCase):
    host = "127.0.0.1"
    click = sidebars.SalesSidebarBrowserTests.click

    def setUp(self):
        fixtures.InterfacesPrecosVinculadosTests.setUp(self)
        Produto.objects.filter(pk=self.produtos[0].pk).update(nome="Produto iniciador com nome longo para conferir no celular")

    def snapshot(self):
        return [list(model.objects.order_by("pk").values()) for model in
                (Produto, GrupoProdutoVinculado, MembroGrupoProduto, AlteracaoPrecoVinculado, Compra, MovimentoFinanceiro)]

    def open_modal(self, tab, novos, comprados=None, fonte=None):
        tab.evaluate("window.modalResult=null;window.modalFailure=null;confirmarPrecosSeletivos(" +
                     json.dumps(self.url) + "," + str(fonte if fonte is not None else self.produtos[0].pk) + "," + json.dumps(novos) +
                     ("," + json.dumps(comprados) if comprados is not None else "") +
                     ").then(value=>window.modalResult=value).catch(error=>window.modalFailure=error.code);true")
        tab.wait('!!document.querySelector("dialog.ps-confirm[open]")')

    def test_visual_choices_prices_cancel_and_confirmation_all_widths(self):
        before = self.snapshot()
        output = Path(tempfile.mkdtemp(prefix="linked-prices-visual-"))
        for width in (320, 390, 1280):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix="linked-prices-chrome-") as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = chrome.tab()
                    tab.call("Emulation.setDeviceMetricsOverride", {"width":width,"height":900,"deviceScaleFactor":1,"mobile":width<600})
                    tab.call("Network.setCookie", {"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url})
                    tab.call("Page.navigate", {"url":self.live_server_url+reverse("estoque:produto_editar",args=[self.produtos[0].pk])})
                    tab.wait('typeof confirmarPrecosSeletivos==="function"')
                    # Change cash and fractional prices; preserve the same installment price.
                    self.open_modal(tab, {"preco_vista":"40.00","preco_prazo":"39.00","preco_vista_fracionado":"8.00","preco_prazo_fracionado":"7.50"})
                    self.assertIn("system-ui",tab.evaluate('getComputedStyle(document.querySelector(".ps-confirm-title")).fontFamily'))
                    self.assertTrue(tab.evaluate('document.querySelector(".ps-confirm-body").scrollTop===0'))
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card").length'),3)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-choice input:checked:disabled").length'),1)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-choice input:not(:disabled):not(:checked)").length'),2)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card.is-required .is-changed").length'),2)
                    self.assertEqual(tab.evaluate('Array.from(document.querySelectorAll(".ps-confirm-price:not(.is-changed) .ps-confirm-difference")).every(n=>n.textContent==="Sem altera\\u00e7\\u00e3o")'),True)
                    self.assertTrue(tab.evaluate('(()=>{const d=document.querySelector(".ps-confirm"),f=d.querySelector("footer"),b=d.querySelector(".ps-confirm-primary");const r=d.getBoundingClientRect(),fr=f.getBoundingClientRect();return d.scrollWidth<=d.clientWidth && document.documentElement.scrollWidth<=innerWidth && r.left>=0 && r.right<=innerWidth && fr.bottom<=innerHeight && fr.top>=0 && b.getBoundingClientRect().height>=44;})()'))
                    optional = ".ps-confirm-choice input:not(:disabled)"
                    self.click(tab, optional)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card .is-changed").length'),4)
                    self.click(tab, optional)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card .is-changed").length'),2)
                    tab.evaluate('document.querySelector(".ps-confirm-title").focus({preventScroll:true});document.querySelector(".ps-confirm-body").scrollTop=0;true')
                    screenshot = tab.call("Page.captureScreenshot", {"format":"png"})
                    path = output/("modal-"+str(width)+".png")
                    path.write_bytes(base64.b64decode(screenshot["data"]))
                    print("MODAL_CAPTURE="+str(path),flush=True)
                    self.click(tab, ".ps-confirm-button:not(.ps-confirm-primary)")
                    tab.wait('window.modalFailure==="PRECOS_SELETIVOS_CANCELADOS"')
                    self.assertEqual(self.snapshot(),before)
                    self.open_modal(tab, {"preco_vista":"40.00"})
                    self.click(tab, optional)
                    self.click(tab, ".ps-confirm-primary")
                    tab.wait("!!window.modalResult")
                    self.assertEqual(set(tab.evaluate("modalResult.ids")),
                                     {str(self.produtos[0].pk),str(self.produtos[1].pk)})
                    self.assertEqual(self.snapshot(),before)
                    # Purchase participants retain their mandatory checked state.
                    ids = [self.produtos[0].pk,self.produtos[1].pk]
                    self.open_modal(tab, {"preco_vista":"36.00","preco_prazo":"42.00"}, ids)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-choice input:checked:disabled").length'),2)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card .is-changed").length'),2)
                    self.click(tab, ".ps-confirm-primary")
                    tab.wait("!!window.modalResult")
                    self.assertEqual(set(tab.evaluate("modalResult.ids")),set(map(str,ids)))
                    self.assertIn("versao",tab.evaluate("modalResult"))
                    self.assertIn("assinatura",tab.evaluate("modalResult"))
                    self.assertEqual(self.snapshot(),before)
                    self.open_modal(tab, {"preco_vista":"36.00","preco_prazo":"39.00"})
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-price.is-changed").length'),0)
                    tab.call("Input.dispatchKeyEvent", {"type":"keyDown","key":"Escape","code":"Escape","windowsVirtualKeyCode":27})
                    tab.wait('window.modalFailure==="PRECOS_SELETIVOS_CANCELADOS"')
                    self.assertEqual(self.snapshot(),before)
                finally:
                    chrome.stop()

    def test_initiator_first_and_optional_order_and_ids_preserved(self):
        before = self.snapshot()
        fonte = self.produtos[-1].pk
        original = self.client.get(self.url).json()["precos_vinculados"]
        original_ids = [str(p["id"]) for p in original["produtos"]]
        expected = [str(fonte)] + [pk for pk in original_ids if pk != str(fonte)]
        for width in (320, 390, 1280):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix="linked-prices-order-") as profile:
                chrome = Chrome(CHROME,profile).start()
                try:
                    tab = chrome.tab()
                    tab.call("Emulation.setDeviceMetricsOverride",{"width":width,"height":900,"deviceScaleFactor":1,"mobile":width<600})
                    tab.call("Network.setCookie",{"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url})
                    tab.call("Page.navigate",{"url":self.live_server_url+reverse("estoque:produto_editar",args=[fonte])})
                    tab.wait('typeof confirmarPrecosSeletivos==="function"')
                    self.open_modal(tab,{"preco_vista":"40.00","preco_prazo":"39.00","preco_vista_fracionado":"8.00"},fonte=fonte)
                    order = 'Array.from(document.querySelectorAll(".ps-confirm-card [data-destinatario]")).map(b=>b.value)'
                    self.assertEqual(tab.evaluate(order),expected)
                    self.assertTrue(tab.evaluate('document.querySelector(".ps-confirm-card input").checked && document.querySelector(".ps-confirm-card input").disabled'))
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".ps-confirm-card:first-child .is-changed").length'),2)
                    optional = '.ps-confirm-choice input[value="'+expected[1]+'"]'
                    for checked in (True,False,True):
                        self.click(tab,optional)
                        self.assertEqual(tab.evaluate(order),expected)
                        self.assertEqual(tab.evaluate(f'document.querySelector({optional!r}).checked'),checked)
                        self.assertEqual(tab.evaluate(f'document.querySelector({optional!r}).closest(".ps-confirm-card").querySelectorAll(".is-changed").length'),2 if checked else 0)
                        self.assertEqual(tab.evaluate('document.querySelector(".ps-confirm-card:first-child .ps-confirm-name").textContent'),self.produtos[-1].nome)
                    self.click(tab,".ps-confirm-primary")
                    tab.wait("!!window.modalResult")
                    result = tab.evaluate("modalResult")
                    self.assertEqual(set(result["ids"]),{str(fonte),expected[1]})
                    self.assertEqual(result["versao"],original["versao_grupo"])
                    self.assertEqual(result["assinatura"],original["assinatura_precos"])
                    self.assertEqual(self.snapshot(),before)
                finally:
                    chrome.stop()
