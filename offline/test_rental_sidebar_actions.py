"""Real browser and endpoint regressions for the two rental sidebar actions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.urls import reverse

from .browser_support import Chrome
from . import test_sales_sidebars_browser as sidebar_tests
from . import tests_sale_sync_browser as sync_tests
from .tests import fixtures


class RentalSidebarAuthenticationTests(TestCase):
    def test_anonymous_posts_cannot_change_rental_or_task(self):
        _, rental, task, _ = fixtures()
        before = (task.status, rental.status, rental.eventos.count())
        for name, data in (
            ('resolver_tarefa_operacional', {'resultado': 'tarefa_engano', 'motivo': 'Teste'}),
            ('confirmar_envio_checklist_entrega', {'modalidade': 'equipe', 'confirmacao': 'materiais_entregues_checklist_enviado'}),
        ):
            with self.subTest(name=name):
                response = self.client.post(reverse('locacoes:' + name, args=[task.pk]), data)
                self.assertEqual(response.status_code, 401)
                self.assertIn('Autentique', response.json()['erro'])
                task.refresh_from_db(); rental.refresh_from_db()
                self.assertEqual((task.status, rental.status, rental.eventos.count()), before)
                self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('locacoes:' + name, args=[task.pk]), data).status_code, 403)


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome unavailable')
class RentalSidebarActionBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sidebar_tests.SalesSidebarBrowserTests.setUp
    open_sales = sidebar_tests.SalesSidebarBrowserTests.open_sales
    prepare = sidebar_tests.SalesSidebarBrowserTests.prepare
    repository = sidebar_tests.SalesSidebarBrowserTests.repository
    select_customer = sidebar_tests.SalesSidebarBrowserTests.select_customer
    add_product = sidebar_tests.SalesSidebarBrowserTests.add_product
    click = sidebar_tests.SalesSidebarBrowserTests.click
    assert_open = sidebar_tests.SalesSidebarBrowserTests.assert_open

    def open_panel(self, tab):
        tab.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
        self.click(tab, '#btnLocacoesOperacionaisVenda')
        self.assert_open(tab, 'btnLocacoesOperacionaisVenda', True)
        tab.evaluate('window.actionPosts=[];window.actionFetch=fetch;window.fetch=(u,o)=>{if(o?.method==="POST"&&String(u).includes("/locacoes/"))actionPosts.push(String(u));return actionFetch(u,o)};true')

    def resolve_fields(self, tab):
        tab.evaluate('document.querySelector("[data-resolver-form] [name=resultado]").value="tarefa_engano";document.querySelector("[data-resolver-form] [name=resultado]").dispatchEvent(new Event("change",{bubbles:true}));document.querySelector("[data-resolver-form] [name=motivo]").value="Tarefa criada por engano no teste";true')

    def test_resolve_works_with_pending_sale_and_keeps_local_operation(self):
        with TemporaryDirectory(prefix='rental-action-resolve-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                saved = tab.evaluate('await repo.all("operations")')
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-resolver')
                self.assertFalse(tab.evaluate('document.querySelector("[data-resolver-form]").hidden'))
                self.resolve_fields(tab)
                previous = tab.evaluate('performance.timeOrigin')
                self.click(tab, '[data-resolver-form] button[type=submit]')
                tab.wait(f'performance.timeOrigin!=={previous} && !!window.salesDraftUI?.ready')
                self.repository(tab)
                self.assertEqual(tab.evaluate('await repo.all("operations")'), saved)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'resolvida_admin')
                self.assertEqual(self.task.resolvida_por, self.user.username)
                self.assertEqual(self.rental.eventos.filter(tipo='tarefa_resolvida_admin').count(), 1)
            finally:
                chrome.stop()

    def test_confirm_one_click_with_pending_sale_keeps_local_operation(self):
        with TemporaryDirectory(prefix='rental-action-confirm-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                saved = tab.evaluate('await repo.all("operations")')
                self.open_panel(tab)
                previous = tab.evaluate('performance.timeOrigin')
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector(".locacoes-rapidas-confirmar") && document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.includes("1")')
                self.assertEqual(tab.evaluate('performance.timeOrigin'), previous)
                self.assertEqual(tab.evaluate('actionPosts.length'), 1)
                self.assertEqual(tab.evaluate('await repo.all("operations")'), saved)
                self.task.refresh_from_db(); self.rental.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada_sem_checklist')
                self.assertEqual(self.rental.status, 'entregue')
                self.assertEqual(self.task.confirmado_por, self.user.username)
                self.assertFalse(self.rental.conferencias_entrega.exists())
                self.assertEqual(self.rental.tarefas_operacionais.get(tipo='recolhimento').status, 'pendente')
            finally:
                chrome.stop()

    def test_identity_lock_and_expired_session_show_feedback_without_saving(self):
        with TemporaryDirectory(prefix='rental-action-auth-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('window.draftKey=drafts.draftKey(scope);window.savedDraft=JSON.stringify(await repo.get("metadata",draftKey));await repo.put("metadata",{key:"sales-identity",actor_id:"99999",environment_id:scope.environment_id});window.dispatchEvent(new Event("focus"));true')
                tab.wait('clienteBusca.disabled')
                self.open_panel(tab)
                tab.call('Network.deleteCookies', {'name': 'sessionid', 'url': self.live_server_url})
                for button, form, fill in (
                    ('.locacoes-rapidas-resolver', '[data-resolver-form]', self.resolve_fields),
                ):
                    self.click(tab, button)
                    self.assertFalse(tab.evaluate(f'document.querySelector({form!r}).hidden'))
                    fill(tab)
                    self.click(tab, form + ' button[type=submit]')
                    tab.wait('!document.querySelector("[data-resolver-erro]").hidden && document.querySelector("[data-resolver-erro]").textContent.includes("Autentique")')
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector("[data-resolver-erro]").hidden && document.querySelector("[data-resolver-erro]").textContent.includes("Autentique")')
                self.task.refresh_from_db(); self.rental.refresh_from_db()
                self.assertEqual(self.task.status, 'pendente')
                self.assertEqual(self.rental.status, 'reservada')
                self.assertTrue(tab.evaluate('savedDraft===JSON.stringify(await repo.get("metadata",draftKey))'))
            finally:
                chrome.stop()

    def test_offline_actions_show_forms_and_message_without_posting(self):
        with TemporaryDirectory(prefix='rental-action-offline-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                tab.evaluate('window.fetch=(u,o)=>["/api/offline/health/","/api/offline/session/"].includes(String(u))?Promise.reject(TypeError("offline")):realFetch(u,o);await app.probe();offset+=5001;await app.probe();offset+=5001;await app.probe();await salesOffline.ready;true')
                tab.wait('salesOffline.active && !salesOffline.activating')
                self.open_panel(tab)
                for button, form, fill in (
                    ('.locacoes-rapidas-resolver', '[data-resolver-form]', self.resolve_fields),
                ):
                    self.click(tab, button)
                    self.assertFalse(tab.evaluate(f'document.querySelector({form!r}).hidden'))
                    fill(tab)
                    self.click(tab, form + ' button[type=submit]')
                    self.assertIn('conexão', tab.evaluate('document.querySelector("[data-resolver-erro]").textContent'))
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector("[data-resolver-erro]").hidden')
                self.assertEqual(tab.evaluate('actionPosts.length'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'pendente')
            finally:
                chrome.stop()
