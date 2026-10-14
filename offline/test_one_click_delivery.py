from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.test import TestCase, Client
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque.models import Funcionario, MovimentoFinanceiro
from locacoes.models import MovimentoEstoqueLocacao
from .tests import fixtures
from .browser_support import Chrome
from . import test_rental_sidebar_actions as actions
from . import tests_sale_sync_browser as sync_tests


class OneClickDeliveryAPITests(TestCase):
    def setUp(self):
        self.user, self.rental, self.task, _ = fixtures()
        self.employee = Funcionario.objects.create(nome='Francisco Miranda')
        self.url = reverse('locacoes:confirmar_entrega_operacional', args=[self.task.pk])

    def test_authenticated_delivery_audit_duplicate_and_no_fake_whatsapp(self):
        self.client.force_login(self.user)
        before = (list(MovimentoEstoqueLocacao.objects.values()), list(MovimentoFinanceiro.objects.values()))
        self.assertEqual(self.client.post(self.url, {'funcionario': self.employee.pk}).status_code, 200)
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual(self.rental.status, 'entregue')
        self.assertEqual(self.task.confirmado_por, self.user.username)
        self.assertIsNotNone(self.task.confirmado_em)
        event = self.rental.eventos.get(tipo='entrega_confirmada_operacional')
        for text in (self.employee.nome, self.user.username, str(self.user.pk)):
            self.assertIn(text, event.descricao)
        self.assertFalse(self.rental.eventos.filter(tipo='entrega_confirmada_checklist_enviado').exists())
        self.assertFalse(self.rental.conferencias_entrega.exists())
        self.assertEqual(self.rental.tarefas_operacionais.get(tipo='recolhimento').status, 'pendente')
        self.assertEqual(self.client.post(self.url, {'funcionario': self.employee.pk}).status_code, 409)
        self.assertEqual(self.rental.eventos.filter(tipo='entrega_confirmada_operacional').count(), 1)
        self.assertEqual((list(MovimentoEstoqueLocacao.objects.values()), list(MovimentoFinanceiro.objects.values())), before)

    def test_authentication_csrf_and_employee_validation(self):
        self.assertEqual(self.client.post(self.url, {'funcionario': self.employee.pk}).status_code, 401)
        secure = Client(enforce_csrf_checks=True); secure.force_login(self.user)
        self.assertEqual(secure.post(self.url, {'funcionario': self.employee.pk}).status_code, 403)
        self.client.force_login(self.user)
        for value in ('', 'bad', '999999'):
            self.assertEqual(self.client.post(self.url, {'funcionario': value}).status_code, 400)
        Funcionario.objects.filter(pk=self.employee.pk).update(ativo=False)
        self.assertEqual(self.client.post(self.url, {'funcionario': self.employee.pk}).status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'pendente')

    def test_partial_delivery_cannot_be_closed_by_simple_confirmation(self):
        self.client.force_login(self.user)
        self.task.status = 'parcial'; self.task.save(update_fields=['status'])
        response = self.client.post(self.url, {'funcionario': self.employee.pk})
        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual(self.task.status, 'parcial')
        self.assertEqual(self.rental.status, 'reservada')
        self.assertFalse(self.rental.eventos.filter(tipo='entrega_confirmada_operacional').exists())


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome unavailable')
class OneClickDeliveryBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = actions.RentalSidebarActionBrowserTests.setUp
    open_sales = actions.RentalSidebarActionBrowserTests.open_sales
    repository = actions.RentalSidebarActionBrowserTests.repository
    select_customer = actions.RentalSidebarActionBrowserTests.select_customer
    add_product = actions.RentalSidebarActionBrowserTests.add_product
    open_panel = actions.RentalSidebarActionBrowserTests.open_panel
    click = actions.RentalSidebarActionBrowserTests.click
    assert_open = actions.RentalSidebarActionBrowserTests.assert_open

    def test_real_network_failure_preserves_draft_and_does_not_post(self):
        with TemporaryDirectory(prefix='one-click-offline-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                saved = tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))')
                self.open_panel(tab)
                tab.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                self.click(tab, '.locacoes-rapidas-confirmar')
                tab.wait('!document.querySelector("[data-resolver-erro]").hidden')
                self.assertIn('conexão', tab.evaluate('document.querySelector("[data-resolver-erro]").textContent'))
                self.assertEqual(tab.evaluate('actionPosts.length'), 0)
                self.assertEqual(tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))'), saved)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'pendente')
            finally:
                chrome.stop()

    def test_stale_offline_state_does_not_block_valid_session_and_double_click(self):
        with TemporaryDirectory(prefix='one-click-delivery-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial'); self.add_product(tab, 'Produto Fracionado')
                saved = tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))')
                operations = tab.evaluate('await repo.all("operations")')
                origin = tab.evaluate('performance.timeOrigin')
                self.open_panel(tab)
                self.assertFalse(tab.evaluate('!!document.querySelector("[data-confirmar-envio-form]")'))
                self.assertEqual(tab.evaluate('document.querySelector(".locacoes-rapidas-confirmar").textContent.trim()'), 'Confirmar entrega')
                tab.evaluate('document.getElementById("offline-global").dataset.actor="";document.getElementById("offline-global").dataset.connection="offline";window.confirmButton=document.querySelector(".locacoes-rapidas-confirmar");confirmButton.click();confirmButton.click();true')
                tab.wait('!document.querySelector(".locacoes-rapidas-confirmar") && document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.includes("1")')
                self.assertEqual(tab.evaluate('actionPosts.length'), 1)
                self.assertEqual(tab.evaluate('performance.timeOrigin'), origin)
                self.assertEqual(tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))'), saved)
                self.assertEqual(tab.evaluate('await repo.all("operations")'), operations)
                self.assertEqual(tab.evaluate('document.querySelector("[data-locacoes-filtro=entrega]").textContent'), 'Entregas 0')
                self.assertEqual(tab.evaluate('document.querySelectorAll("[data-locacoes-card][data-tipo=entrega]").length'), 0)
                self.assertEqual(tab.evaluate('document.querySelectorAll("[data-locacoes-card][data-tipo=recolhimento]").length'), 1)
                self.assert_open(tab, 'btnLocacoesOperacionaisVenda', True)
                self.click(tab, '#btnLocacoesOperacionaisVenda'); self.assert_open(tab, 'btnLocacoesOperacionaisVenda', False)
                self.click(tab, '#btnAtalhosVenda'); self.assert_open(tab, 'btnAtalhosVenda', True)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada_sem_checklist')
                self.assertEqual(self.rental.eventos.filter(tipo='entrega_confirmada_operacional').count(), 1)
            finally:
                chrome.stop()
