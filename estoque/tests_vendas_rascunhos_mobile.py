"""Apresentacao isolada: nenhum Django, banco, armazenamento ou API de vendas."""
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from offline.browser_support import Chrome


ROOT = Path(__file__).resolve().parents[1]
BROWSER = os.environ.get("OFFLINE_TEST_CHROME", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
TEMPLATE = ROOT / "estoque/templates/estoque/vendas_layout_teste.html"
SCRIPT = ROOT / "static/estoque/vendas-rascunhos-mobile.js"


def function(source, name):
    start = source.index("function " + name + "(")
    first_line = source[start:].splitlines()[0]
    if first_line.endswith("}"):
        return first_line + "\n"
    return source[start:source.index("\n}\n", start) + 3]


@unittest.skipUnless(Path(BROWSER).is_file(), "Configure OFFLINE_TEST_CHROME para Chrome/Edge")
class RascunhosMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="vendas-rascunhos-ui-", ignore_cleanup_errors=True)
        cls.browser = Chrome(BROWSER, str(Path(cls.temp.name) / "profile"))
        original_popen = subprocess.Popen
        # Perfil descartavel e documentos locais, sem navegar no sistema operacional.
        with patch("offline.browser_support.subprocess.Popen", side_effect=lambda cmd, **kwargs:
                   original_popen([cmd[0], "--no-sandbox", *cmd[1:]], **kwargs)):
            cls.browser.start()

    @classmethod
    def tearDownClass(cls):
        cls.browser.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.tab = self.browser.tab()

    def tearDown(self):
        self.tab.call("Page.close")
        self.tab.socket.close()

    def evaluate(self, expression):
        return self.tab.evaluate(expression)

    def prepare(self, adapter="local", width=400, enhance=True, height=800):
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", TEMPLATE.read_text(encoding="utf-8"), re.S))
        if adapter == "local":
            source = (ROOT / "static/offline/local-sales.js").read_text(encoding="utf-8")
            constructor = source[source.index("const box ="):source.index("const channel =")]
            recovery = source[source.index("const recovery=document.createElement"):source.index("for (const saved of await repo.all")]
            setup = constructor + recovery + "box.append(recovery,recover);"
            setup += """
                const bridge={capture:()=>({itens:[]})};
                let record={},saveError=false,blocked=false,busy=false,awaitingReceipt=false;
                let savedVersion=0,draftVersion=0;
            """
            setup += function(source, "pending") + function(source, "freeze") + function(source, "render")
            setup = setup.replace("function freeze(value)", "const disabledBeforeFreeze=new Map();function freeze(value)")
            setup += """
                window.setState=(state)=>{
                    record={assembly:{itens:[]}};saveError=false;blocked=false;savedVersion=draftVersion=0;
                    if(state==='saving')draftVersion=1;
                    if(state==='pending')record.command={};
                    if(state==='conflict')record={command:{},result:{receipt:{status:'conflito',erro:'Conflito isolado'}}};
                    if(state==='error')saveError=true;
                    if(state==='confirmed')record.result={sucesso:true,receipt:{status:'confirmada'},visualizar_url:'/vendas/123/'};
                    render();
                };
                window.refs={box,message:status,query,replay,link,recovery,recover,next};
                setState('saved');
            """
        else:
            source = (ROOT / "static/offline/sales-draft-ui.js").read_text(encoding="utf-8")
            setup = "const bridge={eligible:true};"
            setup += source[source.index("const box ="):source.index("box.hidden = !bridge.eligible;") + len("box.hidden = !bridge.eligible;")]
            setup += """
                window.salesDraftUI={recoverOnline(){actionCount++},startNextSale(){actionCount++}};
                window.refs={box,message,officialLink,diagnostics,deviceSales,deviceSalesList,queueCount,recover,recovery,recoverySelect};
                message.textContent='Rascunho local salvo. Dados de referência a revalidar.';
            """
        setup += """
            window.actionCount=0;window.saveEvents=0;
            window.originalNodes=Array.from(box.querySelectorAll('*'));
            document.getElementById('layout-vendas').addEventListener('click',()=>saveEvents++);
            for(const button of box.querySelectorAll('button'))button.addEventListener('click',()=>actionCount++);
        """
        page = "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>" + css + "</style></head><body><div id='layout-vendas'><input id='probe'><button id='btnGravarVenda' type='button'>Gravar Venda</button></div><script>" + setup + "</script></body></html>"
        file = Path(self.temp.name) / (adapter + ".html")
        file.write_text(page, encoding="utf-8")
        self.tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width <= 980})
        self.tab.call("Page.navigate", {"url": file.as_uri()})
        self.tab.wait("document.readyState==='complete' && !!window.refs")
        if enhance:
            self.evaluate(SCRIPT.read_text(encoding="utf-8"))
            if width <= 980:
                self.tab.wait("!!document.getElementById('sales-draft-mobile-toggle')")

    def visible(self, expression):
        return self.evaluate("(()=>{const e=" + expression + ";return !!(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');})()")

    def settle(self):
        self.evaluate("(async()=>{await new Promise(requestAnimationFrame);return true;})()")

    def toggle(self):
        self.evaluate("document.getElementById('sales-draft-mobile-toggle').click()")
        self.settle()

    def test_closed_expanded_saved_saving_and_original_controls(self):
        for adapter in ("local", "authenticated"):
            with self.subTest(adapter=adapter):
                self.prepare(adapter)
                self.assertFalse(self.visible("refs.message"))
                self.assertEqual(self.evaluate("document.getElementById('sales-draft-mobile-toggle').getAttribute('aria-expanded')"), "false")
                self.toggle()
                self.assertTrue(self.visible("refs.message"))
                self.toggle()
                self.assertEqual(self.evaluate("actionCount+saveEvents"), 0)
                self.assertTrue(self.evaluate("originalNodes.every(node=>refs.box.contains(node))"))
                self.assertTrue(self.evaluate("Array.from(document.querySelectorAll('[id]')).every(e=>document.querySelectorAll('[id=\"'+e.id+'\"]').length===1)"))
                self.toggle()
                self.evaluate("refs.recover.hidden=false;refs.recover.click()")
                self.assertGreater(self.evaluate("actionCount"), 0)
        self.prepare()
        self.evaluate("setState('saving')")
        self.settle()
        self.assertFalse(self.visible("refs.message"))
        self.toggle()
        self.assertIn("Salvando", self.evaluate("refs.message.textContent"))
        self.toggle()
        self.evaluate("setState('confirmed')")
        self.settle()
        self.assertFalse(self.visible("refs.message"))
        self.toggle()
        self.assertTrue(self.visible("refs.link"))
        self.assertEqual(self.evaluate("refs.link.getAttribute('href')"), "/vendas/123/")

    def test_pending_conflict_error_and_unknown_messages_remain_visible(self):
        self.prepare()
        for state in ("pending", "conflict", "error"):
            with self.subTest(state=state):
                self.evaluate("setState(" + json.dumps(state) + ")")
                self.settle()
                self.assertTrue(self.visible("refs.message"))
                if state != "error":
                    self.assertTrue(self.visible("refs.query"))
                    self.assertTrue(self.visible("refs.replay"))
        self.evaluate("setState('saved');refs.message.textContent='Aviso novo que exige conferir os dados';")
        self.settle()
        self.assertTrue(self.visible("refs.message"))
        self.evaluate("setState('saved');refs.message.setAttribute('role','alert')")
        self.settle()
        self.assertTrue(self.visible("refs.message"))

    def test_authenticated_pending_diagnostics_and_confirmed_reference(self):
        self.prepare("authenticated")
        self.evaluate("""
            refs.queueCount.hidden=false;refs.queueCount.textContent='1 venda aguardando resolução';
            refs.recover.hidden=false;
            refs.diagnostics.innerHTML='<details class="offline-operation-details"><summary>Ver diagnóstico</summary>Conflito</details>';
        """)
        self.settle()
        for name in ("message", "queueCount", "recover", "diagnostics"):
            self.assertTrue(self.visible("refs." + name), name)
        self.evaluate("""
            refs.queueCount.hidden=true;refs.recover.hidden=true;refs.diagnostics.replaceChildren();
            refs.message.textContent='Venda #123 sincronizada. Referência abcdef12.';
            refs.officialLink.hidden=false;refs.officialLink.href='/vendas/123/';refs.officialLink.textContent='Ver venda #123';
        """)
        self.settle()
        self.assertFalse(self.visible("refs.message"))
        self.toggle()
        self.assertTrue(self.visible("refs.officialLink"))
        self.assertIn("abcdef12", self.evaluate("refs.message.textContent"))
        self.assertTrue(self.evaluate("refs.officialLink.getAttribute('href')==='/vendas/123/'"))
        self.assertFalse(self.visible("refs.recover"))  # O hidden do produtor continua soberano.

    def test_saved_recovery_indication_and_dynamic_insertions(self):
        self.prepare()
        self.evaluate("refs.recovery.add(new Option('Montagem isolada salva','isolada'))")
        self.settle()
        self.assertIn("há salvos", self.evaluate("document.getElementById('sales-draft-mobile-toggle').textContent"))
        self.toggle()
        self.assertTrue(self.visible("refs.recovery"))
        self.assertTrue(self.visible("refs.recover"))
        self.prepare("authenticated")
        self.evaluate("refs.recovery.hidden=false")
        self.settle()
        self.assertIn("há salvos", self.evaluate("document.getElementById('sales-draft-mobile-toggle').textContent"))
        self.evaluate("refs.box.hidden=true")
        self.settle()
        self.assertFalse(self.visible("refs.box"))
        self.evaluate("refs.box.hidden=false;const warning=document.createElement('p');warning.textContent='Aviso adicional';refs.box.append(warning)")
        self.settle()
        self.assertTrue(self.visible("refs.box.lastElementChild"))  # Conteudo novo nao e escondido por padrao.

    def test_async_creation_nested_alert_and_resize(self):
        self.prepare("authenticated", enhance=False)
        self.evaluate("refs.box.remove()")
        self.evaluate(SCRIPT.read_text(encoding="utf-8"))
        self.evaluate("document.getElementById('layout-vendas').prepend(refs.box)")
        self.tab.wait("!!document.getElementById('sales-draft-mobile-toggle')")
        self.evaluate("refs.deviceSales.hidden=false;refs.deviceSalesList.innerHTML='<p role=alert>Revisão obrigatória</p>'")
        self.settle()
        self.assertTrue(self.visible("refs.deviceSales"))
        self.tab.call("Emulation.setDeviceMetricsOverride", {"width": 1366, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        self.assertFalse(self.visible("document.getElementById('sales-draft-mobile-toggle')"))
        self.assertTrue(self.visible("refs.message"))
        self.tab.call("Emulation.setDeviceMetricsOverride", {"width": 400, "height": 800, "deviceScaleFactor": 1, "mobile": True})
        self.settle()
        self.assertEqual(self.evaluate("document.querySelectorAll('#sales-draft-mobile-toggle').length"), 1)
        self.assertTrue(self.evaluate("originalNodes.every(node=>refs.box.contains(node))"))

    def test_widths_keyboard_scroll_and_desktop(self):
        for width in (320, 360, 390, 400, 430):
            with self.subTest(width=width):
                self.prepare(width=width, height=350)
                self.assertLessEqual(self.evaluate("refs.box.getBoundingClientRect().height"), 40)
                self.assertLessEqual(self.evaluate("document.documentElement.scrollWidth"), width)
                self.toggle()
                self.evaluate("refs.recover.scrollIntoView({block:'center'})")
                self.assertTrue(self.visible("refs.recover"))
                self.assertLessEqual(self.evaluate("refs.recover.getBoundingClientRect().bottom"), 350)
                self.assertLessEqual(self.evaluate("document.documentElement.scrollWidth"), width)
        self.prepare()
        self.evaluate("document.getElementById('sales-draft-mobile-toggle').focus()")
        for kind in ("keyDown", "keyUp"):
            self.tab.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter", "text": "\r" if kind == "keyDown" else "", "windowsVirtualKeyCode": 13})
        self.assertEqual(self.evaluate("document.getElementById('sales-draft-mobile-toggle').getAttribute('aria-expanded')"), "true")
        self.assertEqual(self.evaluate("actionCount+saveEvents"), 0)
        self.prepare(width=1366, enhance=False)
        before = self.evaluate("refs.box.outerHTML")
        self.evaluate(SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(self.evaluate("refs.box.outerHTML"), before)
        self.assertTrue(self.visible("refs.message"))


if __name__ == "__main__":
    unittest.main()
