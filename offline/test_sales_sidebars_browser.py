"""Sidebar navigation must survive the real offline protection states."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from .browser_support import Chrome
from . import tests_sale_sync_browser as sync_tests
from . import tests_sales_drafts_browser as draft_tests
from .tests import fixtures


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome unavailable')
class SalesSidebarBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True

    def setUp(self):
        sync_tests.SaleSyncBrowserTests.setUp(self)
        _, self.rental, self.task, _ = fixtures()

    open_sales = sync_tests.SaleSyncBrowserTests.open_sales
    prepare = sync_tests.SaleSyncBrowserTests.prepare
    repository = sync_tests.SaleSyncBrowserTests.repository
    select_customer = sync_tests.SaleSyncBrowserTests.select_customer
    add_product = sync_tests.SaleSyncBrowserTests.add_product
    reload = draft_tests.SalesDraftBrowserTests.reload

    def click(self, tab, selector):
        tab.wait(f'!document.querySelector({selector!r}).closest("aside")?.getAnimations().some(a=>a.playState==="running")')
        tab.evaluate(f'if(!document.querySelector({selector!r}).matches("[data-sales-navigation-toggle]"))document.querySelector({selector!r}).scrollIntoView({{block:"nearest",inline:"nearest"}});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));true')
        point = tab.evaluate(f'''(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();
            return {{x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height}};}})()''')
        self.assertGreater(point['width'], 0, selector)
        self.assertGreater(point['height'], 0, selector)
        hit = tab.evaluate(f'window.sidebarHit=document.elementFromPoint({point["x"]},{point["y"]})?.outerHTML;!!document.elementFromPoint({point["x"]},{point["y"]})?.closest({selector!r})')
        self.assertTrue(hit, str(tab.evaluate('({hit:sidebarHit,width:innerWidth,point:' + str(point).replace("'", '"') + '})')))
        for kind in ('mousePressed', 'mouseReleased'):
            tab.call('Input.dispatchMouseEvent', {'type': kind, 'x': point['x'], 'y': point['y'], 'button': 'left', 'clickCount': 1})

    def assert_open(self, tab, button, opened):
        try:
            tab.wait(f'document.getElementById({button!r}).getAttribute("aria-expanded")==={str(opened).lower()!r}')
        except AssertionError:
            self.fail(str(tab.evaluate('({hit:sidebarHit,active:salesOffline.active,completed:salesOffline.completed,html:document.getElementById("btnAtalhosVenda").outerHTML,rect:document.getElementById("btnAtalhosVenda").getBoundingClientRect().toJSON()})')))
        self.assertEqual(tab.evaluate(f'document.getElementById({button!r}).parentElement.classList.contains("aberto")'), opened)
        if opened:
            # Verify the visible panel, not just an attribute on a hidden aside.
            tab.wait(f'(()=>{{const r=document.getElementById(document.getElementById({button!r}).getAttribute("aria-controls")).getBoundingClientRect();return r.width>0 && r.right<=innerWidth+1;}})()')
        else:
            tab.wait(f'(()=>{{const b=document.getElementById({button!r}),r=b.parentElement.getBoundingClientRect();return Math.abs(r.left-(document.documentElement.clientWidth-parseFloat(getComputedStyle(b.parentElement).right)-b.offsetWidth))<1;}})()')

    def exercise_sidebars(self, tab):
        buttons = ['btnAtalhosVenda', 'btnLocacoesOperacionaisVenda']
        tab.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
        for button in buttons:
            self.assert_open(tab, button, False)
        for button in buttons:
            self.assertFalse(tab.evaluate(f'document.getElementById({button!r}).disabled'), button)
            self.click(tab, '#' + button)
            self.assert_open(tab, button, True)
            self.click(tab, '#' + button)
            self.assert_open(tab, button, False)
            self.click(tab, '#' + button)
            tab.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
            self.assert_open(tab, button, False)
            self.click(tab, '#' + button)
            self.click(tab, '#clienteBusca')  # Also works when editing is protected.
            self.assert_open(tab, button, False)
        self.click(tab, '#' + buttons[0])
        self.click(tab, '#' + buttons[1])
        self.assert_open(tab, buttons[0], False)
        self.assert_open(tab, buttons[1], True)

    def test_identity_change_preserves_draft_and_navigation(self):
        with TemporaryDirectory(prefix='sales-sidebar-identity-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('window.savedKey=drafts.draftKey(scope);window.savedDraft=JSON.stringify(await repo.get("metadata",savedKey));await repo.put("metadata",{key:"sales-identity",actor_id:"99999",environment_id:scope.environment_id});window.dispatchEvent(new Event("focus"));true')
                tab.wait('document.getElementById("sales-offline-notice").textContent.includes("Identidade mudou") && clienteBusca.disabled')
                for width in (1280, 980, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    self.exercise_sidebars(tab)
                self.assertTrue(tab.evaluate('clienteBusca.disabled && btnGravarVenda.disabled'))
                self.assertTrue(tab.evaluate('savedDraft===JSON.stringify(await repo.get("metadata",savedKey))'))
            finally:
                chrome.stop()

    def test_online_sidebar_navigation_after_full_initialization(self):
        with TemporaryDirectory(prefix='sales-sidebar-online-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                for width in (1280, 980, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    self.exercise_sidebars(tab)
                    self.click(tab, '#locacoesOperacionaisVendaPainel .atalhos-venda-titulo')
                    self.assert_open(tab, 'btnLocacoesOperacionaisVenda', True)
                self.assertFalse(tab.evaluate('salesOffline.active || salesOffline.completed'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
            finally:
                chrome.stop()

    def test_pending_sale_and_offline_keep_navigation_but_block_mutations(self):
        with TemporaryDirectory(prefix='sales-sidebar-completed-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                tab.wait('salesOffline.completed && clienteBusca.disabled')
                tab.evaluate('window.savedOperations=JSON.stringify(await repo.all("operations"));true')
                self.exercise_sidebars(tab)
                self.assertTrue(tab.evaluate('btnGravarVenda.disabled && btnConfirmarFechamentoVenda.disabled'))
                # Reproduce the second symptom independently: removing disabled
                # does not fix a capture-phase guard intercepting the toggle.
                tab.evaluate('btnLocacoesOperacionaisVenda.disabled=false;btnLocacoesOperacionaisVenda.click();true')
                self.assert_open(tab, 'btnLocacoesOperacionaisVenda', False)
                tab.evaluate('window.fetch=(u,o)=>["/api/offline/health/","/api/offline/session/"].includes(String(u))?Promise.reject(TypeError("offline")):realFetch(u,o);await app.probe();offset+=5001;await app.probe();offset+=5001;await app.probe();await salesOffline.ready;true')
                tab.wait('salesOffline.active && !salesOffline.activating')
                self.exercise_sidebars(tab)
                self.assertTrue(tab.evaluate('document.querySelector("[data-resolver-form]").hidden'))
                self.click(tab, '.locacoes-rapidas-resolver')
                self.assertFalse(tab.evaluate('document.querySelector("[data-resolver-form]").hidden'))
                self.assertIn('conexão', tab.evaluate('document.querySelector("[data-resolver-erro]").textContent'))
                self.assertTrue(tab.evaluate('btnGravarVenda.disabled && btnConfirmarFechamentoVenda.disabled'))
                self.assertTrue(tab.evaluate('savedOperations===JSON.stringify(await repo.all("operations"))'))
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'pendente')
            finally:
                chrome.stop()

    def test_enabling_button_does_not_bypass_capture_guard(self):
        with TemporaryDirectory(prefix='sales-sidebar-capture-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                tab.evaluate('btnLocacoesOperacionaisVenda.disabled=false;btnLocacoesOperacionaisVenda.click();true')
                self.assertEqual(tab.evaluate('btnLocacoesOperacionaisVenda.getAttribute("aria-expanded")'), 'true')
                self.assertTrue(tab.evaluate('clienteBusca.disabled && btnGravarVenda.disabled'))
            finally:
                chrome.stop()
