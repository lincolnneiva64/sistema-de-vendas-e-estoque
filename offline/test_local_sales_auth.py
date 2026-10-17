"""Authentication recovery without changing ownership of local sales data."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase

from estoque.models import Venda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sales_drafts_browser as draft_tests
CHROME = draft_tests.CHROME
from .tests_commercial import commercial_fixtures


class LocalSalesAuthTests(TestCase):
    def test_anonymous_sales_stays_direct_and_explicit_login_returns_to_sales(self):
        user = get_user_model().objects.create_user('local-auth', password='test-only')
        response = self.client.get('/vendas/?cliente_id=123')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        response = self.client.post('/offline/login/?next=/vendas/%3Fcliente_id%3D123', {'username': user.username, 'password': 'test-only', 'next': '/vendas/?cliente_id=123'})
        self.assertRedirects(response, '/vendas/?cliente_id=123', fetch_redirect_response=False)
        data = self.client.get('/api/offline/session/').json()
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['actor_id'], str(user.pk))
        self.assertFalse(data['can_prepare'])
        self.assertEqual(self.client.get('/api/offline/snapshot/comercial/').status_code, 403)

    def test_session_and_shell_keep_distinct_access_rules(self):
        response = self.client.get('/api/offline/session/')
        self.assertEqual(response.json(), {'authenticated': False, 'actor_id': '', 'can_prepare': False, 'username': ''})
        self.assertIn('no-store', response['Cache-Control'])
        response = self.client.get('/offline/vendas-shell/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'shell: true')
        self.assertContains(response, 'data-actor=""')
        self.assertEqual(self.client.get('/api/offline/snapshot/comercial/').status_code, 401)


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class LocalSalesAuthBrowserTests(StaticLiveServerTestCase):
    open_sales = draft_tests.SalesDraftBrowserTests.open_sales
    reload = draft_tests.SalesDraftBrowserTests.reload
    select_customer = draft_tests.SalesDraftBrowserTests.select_customer
    add_product = draft_tests.SalesDraftBrowserTests.add_product
    repository = draft_tests.SalesDraftBrowserTests.repository

    def setUp(self):
        self.user, self.customer, self.product, self.operator, *_ = commercial_fixtures()
        self.user.set_password('test-only')
        self.user.save(update_fields=['password'])
        self.client.force_login(self.user)

    def local_state(self, tab):
        self.repository(tab)
        return tab.evaluate('JSON.stringify({metadata:(await repo.all("metadata")).filter(r=>r.key==="sales-identity"||r.key==="device"||r.key.startsWith("rascunho_venda:")),operations:await repo.all("operations"),snapshots:await repo.all("snapshots")})')

    def expire(self, tab):
        # Expire only the disposable database session, not browser storage.
        Session.objects.filter(session_key=self.client.cookies['sessionid'].value).delete()
        tab.evaluate('dispatchEvent(new Event("online"));true')
        tab.wait('salesOffline.authenticationRequired === true && !!document.getElementById("sales-session-login")')

    def test_chrome_anonymous_login_and_authenticated_reload(self):
        with TemporaryDirectory(prefix='local-sales-login-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.navigate', {'url': self.live_server_url+'/vendas/?cliente_id='+str(self.customer.pk)})
                tab.wait('location.pathname === "/vendas/" && window.salesDraftUI?.ready')
                self.assertFalse(tab.evaluate('clienteBusca.disabled'))
                tab.call('Page.navigate', {'url': self.live_server_url+'/offline/login/?next=/vendas/%3Fcliente_id%3D'+str(self.customer.pk)})
                tab.wait('!!document.querySelector("[name=username]")')
                tab.evaluate('document.querySelector("[name=username]").value='+json.dumps(self.user.username)+';document.querySelector("[name=password]").value="test-only";document.querySelector("button").click();true')
                tab.wait('location.pathname === "/vendas/" && window.salesDraftUI?.ready')
                self.assertEqual(tab.evaluate('new URLSearchParams(location.search).get("cliente_id")'), str(self.customer.pk))
                self.assertFalse(tab.evaluate('clienteBusca.disabled'))
                self.reload(tab)
                self.assertFalse(tab.evaluate('clienteBusca.disabled'))
                self.assertTrue(tab.evaluate('(await (await fetch("/api/offline/session/")).json()).authenticated'))
            finally:
                chrome.stop()

    def test_expiry_multiple_tabs_relogin_and_f5_preserve_draft_and_queue(self):
        with TemporaryDirectory(prefix='local-sales-expiry-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                self.repository(tab)
                # A queued record and recovery marker must remain byte-identical.
                tab.evaluate('await repo.put("operations",{operation_id:"auth-preservation-sentinel",actor_id:scope.actor_id,environment_id:scope.environment_id,type:"auth-test-sentinel",status:"confirmada"});localStorage.setItem("auth-preservation-sentinel","pending-confirmation");true')
                second = self.open_sales(chrome)
                # Production keeps a separate draft for each tab.
                self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.select_customer(second, 'Cliente Comercial')
                self.add_product(second, 'Produto Fracionado', '1')
                before = self.local_state(tab)
                self.expire(tab)
                second.wait('salesOffline.authenticationRequired === true')
                for page in [tab, second]:
                    self.assertTrue(page.evaluate('clienteBusca.disabled'))
                    self.assertEqual(page.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                    self.assertNotIn('Identidade mudou', page.evaluate('document.getElementById("sales-offline-notice").textContent'))
                    self.assertFalse(page.evaluate('await salesDraftUI.beforeSubmit()'))
                self.assertEqual(self.local_state(tab), before)
                self.assertEqual(tab.evaluate('localStorage.getItem("auth-preservation-sentinel")'), 'pending-confirmation')
                # Authenticate through the real login page in a separate tab.
                login = chrome.tab()
                login.call('Page.navigate', {'url': self.live_server_url+'/offline/login/?next=/vendas/'})
                login.wait('!!document.querySelector("[name=username]")')
                login.evaluate('document.querySelector("[name=username]").value='+json.dumps(self.user.username)+';document.querySelector("[name=password]").value="test-only";document.querySelector("button").click();true')
                login.wait('location.pathname === "/vendas/" && window.salesDraftUI?.ready')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertFalse(tab.evaluate('clienteBusca.disabled'))
                self.assertEqual(self.local_state(tab), before)
                self.assertEqual(Venda.objects.count(), 0)
                self.assertEqual(ContaReceber.objects.count(), 0)
                self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                chrome.stop()

    def test_different_user_blocks_old_tab_without_reassigning_draft(self):
        other = get_user_model().objects.create_user('other-auth-test', password='test-only')
        other_client = Client(); other_client.force_login(other)
        with TemporaryDirectory(prefix='local-sales-identity-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                self.repository(tab)
                before = tab.evaluate('JSON.stringify((await repo.all("metadata")).filter(r=>r.key.startsWith("rascunho_venda:")))')
                second = chrome.tab(); second.call('Network.enable')
                second.call('Network.setCookie', {'name':'sessionid','value':other_client.cookies['sessionid'].value,'url':self.live_server_url})
                second.call('Page.navigate', {'url': self.live_server_url+'/vendas/'})
                second.wait('window.salesDraftUI?.ready')
                tab.wait('salesDraftUI.blocked && clienteBusca.disabled')
                self.assertIn('Identidade mudou',tab.evaluate('document.getElementById("sales-offline-notice").textContent'))
                self.assertEqual(tab.evaluate('JSON.stringify((await repo.all("metadata")).filter(r=>r.key.startsWith("rascunho_venda:")))'), before)
                self.assertFalse(second.evaluate('(await (await fetch("/api/offline/session/")).json()).can_prepare'))
                self.assertFalse(second.evaluate('clienteBusca.disabled'))
            finally:
                chrome.stop()

    def test_prepared_shell_keeps_draft_after_session_expires(self):
        with TemporaryDirectory(prefix='local-sales-shell-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('document.querySelector(".offline-commercial button").click();true')
                tab.wait('document.querySelector(".offline-commercial [role=status]").textContent.includes("Catalogo salvo") || document.querySelector(".offline-commercial [role=status]").textContent.includes("Catálogo salvo")')
                before = self.local_state(tab)
                self.expire(tab)
                tab.call('Page.navigate', {'url': self.live_server_url+'/offline/vendas-shell/'})
                tab.wait('window.salesDraftUI?.ready && salesOffline.active && salesOffline.available')
                self.assertTrue(tab.evaluate('salesOffline.shell'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertEqual(self.local_state(tab), before)
                self.assertFalse(tab.evaluate('(await (await fetch("/api/offline/session/")).json()).authenticated'))
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                chrome.stop()
