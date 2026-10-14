"""Anonymous dashboard navigation and real session recovery on loopback HTTP."""
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest import skipUnless
from urllib.request import urlopen

from django.contrib.sessions.models import Session
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.utils import timezone

from .browser_support import Chrome, DevTools
from . import test_one_click_delivery as delivery
from . import test_local_rental_actions as local
from . import tests_sale_sync_browser as sync_tests


class DeliveryIdentityTests(TestCase):
    def test_dashboard_and_sales_do_not_create_authenticated_session(self):
        client = Client()
        self.assertEqual(client.get('/').status_code, 200)
        self.assertEqual(client.get('/vendas/').status_code, 200)
        response = client.get('/api/offline/session/')
        self.assertFalse(response.json()['authenticated'])
        self.assertNotIn('sessionid', client.cookies)
        self.assertIn('no-store', response.headers['Cache-Control'])


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome unavailable')
class DeliveryLoginBrowserTests(StaticLiveServerTestCase):
    host = '127.0.0.1'
    reset_sequences = True
    setUp = delivery.OneClickDeliveryBrowserTests.setUp
    click = delivery.OneClickDeliveryBrowserTests.click
    assert_open = delivery.OneClickDeliveryBrowserTests.assert_open
    open_panel = delivery.OneClickDeliveryBrowserTests.open_panel
    repository = delivery.OneClickDeliveryBrowserTests.repository
    select_customer = delivery.OneClickDeliveryBrowserTests.select_customer
    add_product = delivery.OneClickDeliveryBrowserTests.add_product
    login = local.LocalRentalActionTests.login

    def open_login_link(self, chrome, tab):
        href = tab.evaluate('document.querySelector("[data-operacional-login]").href')
        self.assertTrue(href.startswith(self.live_server_url + '/offline/login/?next='))
        self.click(tab, '[data-operacional-login]')
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with urlopen(f'http://127.0.0.1:{chrome.port}/json/list', timeout=2) as response:
                target = next((target for target in json.load(response) if target['type'] == 'page'
                    and target['url'].startswith(self.live_server_url + '/offline/login/')), None)
            if target:
                login_tab = DevTools(target['webSocketDebuggerUrl'])
                login_tab.call('Page.enable'); login_tab.call('Page.bringToFront')
                try:
                    login_tab.wait('!!document.querySelector("input[name=password]")')
                except RuntimeError as error:
                    if 'Cannot find default execution context' not in str(error):
                        raise
                    login_tab.socket.close()
                    sleep(.05)
                    continue
                login_tab.evaluate('document.querySelector("input[name=username]").value="commercial-reader";document.querySelector("input[name=password]").value="test-only";true')
                self.click(login_tab, 'form button')
                login_tab.wait('location.pathname==="/vendas/" && !!window.salesDraftUI?.ready')
                return login_tab
            sleep(.05)
        self.fail('The existing login page did not open')

    def confirm_and_check(self, tab):
        tab.call('Page.bringToFront')
        self.click(tab, '.locacoes-rapidas-confirmar')
        tab.wait('!document.querySelector(".locacoes-rapidas-confirmar") && document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.includes("1")')
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual(self.rental.status, 'entregue')
        self.assertEqual(self.task.confirmado_por, self.user.username)
        self.assertEqual(self.rental.tarefas_operacionais.get(tipo='recolhimento').status, 'pendente')

    def test_anonymous_dashboard_then_sales_offers_working_login(self):
        with TemporaryDirectory(prefix='delivery-dashboard-login-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait('document.readyState==="complete"')
                self.assertFalse(tab.evaluate('(await (await fetch("/api/offline/session/",{cache:"no-store"})).json()).authenticated'))
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('!!window.salesDraftUI?.ready')
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector("[data-resolver-erro]").hidden')
                self.assertTrue(tab.evaluate('!!document.querySelector("[data-operacional-login]")'))
                self.assertEqual(tab.evaluate('actionPosts.length'), 0)
                self.task.refresh_from_db(); self.assertEqual(self.task.status, 'pendente')
                login_tab = self.open_login_link(chrome, tab)
                cookies = login_tab.call('Network.getCookies', {'urls': [self.live_server_url]})['cookies']
                session = next(cookie for cookie in cookies if cookie['name'] == 'sessionid')
                self.assertEqual(session['domain'], '127.0.0.1')
                self.assertEqual(session['path'], '/')
                self.assertFalse(session['secure']); self.assertTrue(session['httpOnly'])
                self.assertTrue(any(cookie['name'] == 'csrftoken' for cookie in cookies))
                self.confirm_and_check(tab)
            finally:
                chrome.stop()

    def test_expired_database_session_keeps_cookie_and_draft_then_recovers(self):
        with TemporaryDirectory(prefix='delivery-expired-login-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                self.login(tab)
                # The draft UI claims a tab identity asynchronously after real login.
                tab.wait('window.salesDraftUI?.ready === true && !!window.salesDraftUI.scope')
                self.repository(tab)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('window.savedKey=drafts.draftKey(scope);true')
                saved = tab.evaluate('await repo.get("metadata",savedKey)')
                cookie = next(cookie for cookie in tab.call('Network.getCookies', {'urls': [self.live_server_url]})['cookies'] if cookie['name'] == 'sessionid')
                self.assertEqual(Session.objects.filter(session_key=cookie['value']).update(expire_date=timezone.now()-timedelta(seconds=1)), 1)
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!!document.querySelector("[data-operacional-login]")')
                self.assertFalse(tab.evaluate('(await (await fetch("/api/offline/session/",{cache:"no-store"})).json()).authenticated'))
                self.assertEqual(tab.evaluate('await repo.get("metadata",savedKey)'), saved)
                login_tab = self.open_login_link(chrome, tab)
                self.repository(login_tab)
                # Upstream isolates drafts per tab. Login must not transfer the
                # original tab's assembly automatically to the new login tab.
                self.assertNotEqual(login_tab.evaluate('drafts.draftKey(scope)'), tab.evaluate('savedKey'))
                self.assertIsNone(login_tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertEqual(tab.evaluate('await repo.get("metadata",savedKey)'), saved)
                self.confirm_and_check(tab)
                self.assertEqual(tab.evaluate('await repo.get("metadata",savedKey)'), saved)
            finally:
                chrome.stop()

    def test_valid_session_ignores_cached_identity_and_offline_prepare_permission(self):
        self.user.user_permissions.remove(*self.user.user_permissions.filter(codename='registrar_observacao'))
        with TemporaryDirectory(prefix='delivery-session-cache-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                self.login(tab)
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;const cache=await caches.open("test-stale-identity");await cache.put("/api/offline/session/",new Response(JSON.stringify({authenticated:false,can_prepare:false,username:""}),{headers:{"Content-Type":"application/json"}}));true')
                tab.wait('!!navigator.serviceWorker.controller')
                self.assertTrue(tab.evaluate('(await (await fetch("/api/offline/session/",{cache:"no-store"})).json()).authenticated'))
                self.assertFalse(tab.evaluate('(await (await fetch("/api/offline/session/",{cache:"no-store"})).json()).can_prepare'))
                self.open_panel(tab)
                self.confirm_and_check(tab)
                self.assertFalse(tab.evaluate('!!document.querySelector("[data-operacional-login]")'))
            finally:
                chrome.stop()
