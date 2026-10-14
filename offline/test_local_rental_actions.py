"""Local template configuration, real login and stale HTML identity regressions."""
import ast
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.utils import timezone

from .browser_support import Chrome
from . import test_rental_sidebar_actions as actions
from . import tests_sale_sync_browser as sync_tests


def manual_templates():
    tree = ast.parse((settings.BASE_DIR / 'sistema/settings_locacoes_manual.py').read_text(encoding='utf-8'))
    return ast.literal_eval(next(node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'TEMPLATES' for target in node.targets)))


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome unavailable')
class LocalRentalActionTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = actions.RentalSidebarActionBrowserTests.setUp
    click = actions.RentalSidebarActionBrowserTests.click
    assert_open = actions.RentalSidebarActionBrowserTests.assert_open
    open_panel = actions.RentalSidebarActionBrowserTests.open_panel
    repository = actions.RentalSidebarActionBrowserTests.repository
    select_customer = actions.RentalSidebarActionBrowserTests.select_customer
    add_product = actions.RentalSidebarActionBrowserTests.add_product

    def login(self, tab):
        tab.call('Page.navigate', {'url': self.live_server_url + '/offline/login/?next=/vendas/'})
        tab.wait('!!document.querySelector("input[name=password]")')
        tab.evaluate('document.querySelector("input[name=username]").value="commercial-reader";document.querySelector("input[name=password]").value="test-only";true')
        self.click(tab, 'form button')
        tab.wait('location.pathname==="/vendas/" && !!document.getElementById("offline-global") && document.readyState==="complete"')
        self.assertTrue(tab.evaluate('(await (await fetch("/api/offline/session/",{cache:"no-store"})).json()).authenticated'))

    @override_settings(TEMPLATES=manual_templates())
    def test_manual_configuration_real_login_has_environment_identity(self):
        with TemporaryDirectory(prefix='rental-manual-login-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.login(tab)
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.environment'), settings.OFFLINE_ENVIRONMENT_ID)
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.actor'), str(self.user.pk))
                tab.wait('!!window.salesDraftUI?.ready')
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector(".locacoes-rapidas-confirmar") && document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.includes("1")')
                self.assertEqual(tab.evaluate('location.pathname'), '/vendas/')
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada_sem_checklist')
            finally:
                chrome.stop()

    def test_authenticated_session_overrides_stale_anonymous_html_safely(self):
        with TemporaryDirectory(prefix='rental-stale-login-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('!!window.salesDraftUI?.ready')
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.actor'), '')
                self.login(chrome.tab())  # Actual login in another tab; original HTML remains stale.
                tab.call('Page.bringToFront')
                tab.wait('document.getElementById("offline-global").dataset.connection==="online"')
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector(".locacoes-rapidas-confirmar") && document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.includes("1")')
                self.assertEqual(tab.evaluate('location.pathname'), '/vendas/')
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada_sem_checklist')
                self.assertEqual(self.task.confirmado_por, self.user.username)
            finally:
                chrome.stop()

    def test_regularization_backdated_delivery_without_reason_and_layout(self):
        self.task.tentativa_por = 'Roseli da Costa Gama'
        self.task.save(update_fields=['tentativa_por'])
        with TemporaryDirectory(prefix='rental-backdated-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.login(tab)
                tab.wait('!!window.salesDraftUI?.ready')
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                self.repository(tab)
                saved_draft = tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))')
                self.open_panel(tab)
                self.click(tab, '.locacoes-rapidas-resolver')
                self.assertEqual(tab.evaluate('document.querySelector("[data-resolver-form] [name=responsavel]").value'), 'Roseli da Costa Gama')
                names = tab.evaluate('[...document.querySelector("[data-resolver-form] [name=responsavel]").options].map(o=>o.value).filter(Boolean)')
                self.assertEqual(names, ['Francisco Miranda', 'Lincoln Albuquerque Neiva', 'Roseli da Costa Gama'])
                tab.evaluate('document.querySelector("[data-resolver-form] [name=resultado]").value="entrega_equipe";document.querySelector("[data-resolver-form] [name=resultado]").dispatchEvent(new Event("change",{bubbles:true}));true')
                self.assertFalse(tab.evaluate('document.querySelector("[data-resolver-form] [name=motivo]").required'))
                date_value = (timezone.localdate() - timedelta(days=2)).isoformat()
                tab.evaluate(f'document.querySelector("[data-resolver-form] [name=data_efetiva]").value="{date_value}";true')
                for width in (1280, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    tab.evaluate('document.querySelector("[data-resolver-form]").scrollIntoView({block:"nearest"});await new Promise(r=>requestAnimationFrame(r));true')
                    self.assertTrue(tab.evaluate('(()=>{const f=document.querySelector("[data-resolver-form]");return [...f.querySelectorAll(".locacoes-operacionais-campo")].filter(c=>!c.hidden).every(c=>{const label=c.querySelector("label").getBoundingClientRect(),control=c.querySelector("input,select,textarea").getBoundingClientRect();return label.bottom<=control.top+1 && control.left>=0 && control.right<=innerWidth;});})()'))
                previous = tab.evaluate('performance.timeOrigin')
                self.click(tab, '[data-resolver-form] button[type=submit]')
                tab.wait(f'performance.timeOrigin!=={previous} && !!window.salesDraftUI?.ready')
                self.repository(tab)
                self.assertEqual(tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))'), saved_draft)
                self.task.refresh_from_db(); self.rental.refresh_from_db()
                self.assertEqual(self.task.resolvida_por, self.user.username)
                self.assertEqual(self.task.status, 'resolvida_admin')
                event = self.rental.eventos.get(tipo='tarefa_resolvida_admin')
                for text in (date_value, 'Roseli da Costa Gama', self.user.username):
                    self.assertIn(text, event.descricao)
                self.assertFalse(self.rental.conferencias_entrega.exists())
                self.assertEqual(self.rental.tarefas_operacionais.get(tipo='recolhimento').status, 'pendente')
            finally:
                chrome.stop()
