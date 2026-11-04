"""Mandatory identification for new receipts; legacy payments remain untouched."""
import base64
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import skipUnless
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque.models import ContaFinanceira, Funcionario, MovimentoFinanceiro
from locacoes.models import EventoLocacao, Locacao, MovimentoEstoqueLocacao, PagamentoLocacao
from locacoes import tests_formulario_erros as formulario
from locacoes import tests_saldos_consulta as saldos
from offline.browser_support import Chrome


MESSAGE = Locacao.RESPONSAVEL_RECEBIMENTO_OBRIGATORIO


class ResponsavelRecebimentoTests(saldos.DadosSaldos, TestCase):
    def snapshot(self):
        return {model: list(model.objects.order_by('pk').values()) for model in
            (Locacao, PagamentoLocacao, EventoLocacao, MovimentoFinanceiro, MovimentoEstoqueLocacao, ContaFinanceira)}

    def test_no_automatic_selection_and_only_active_cashiers_are_listed(self):
        rental = self.criar()
        inactive = Funcionario.objects.create(nome='Inativo fictício', ativo=False, pode_operar_caixa=True)
        no_cash = Funcionario.objects.create(nome='Sem caixa fictício', ativo=True, pode_operar_sistema=True)
        for url in (self.payment_url(rental), reverse('locacoes:registrar_pagamento', args=[rental.pk])):
            response = self.client.get(url)
            field = response.context['form'].fields['responsavel']
            self.assertTrue(field.required)
            self.assertIsNone(response.context['form']['responsavel'].value())
            self.assertEqual(list(field.queryset.values_list('pk', flat=True)), [self.operador.pk])
            self.assertNotIn(inactive.pk, field.queryset.values_list('pk', flat=True))
            self.assertNotIn(no_cash.pk, field.queryset.values_list('pk', flat=True))

    def test_missing_responsible_is_rejected_on_both_http_endpoints_without_writes(self):
        rental = self.criar()
        for modal in (False, True):
            url = self.payment_url(rental) if modal else reverse('locacoes:registrar_pagamento', args=[rental.pk])
            data = self.payload(rental, 30)
            data.pop('responsavel')
            before = self.snapshot()
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 422 if modal else 200)
            self.assertEqual(list(response.context['form'].errors['responsavel']), [MESSAGE])
            self.assertContains(response, MESSAGE, status_code=422 if modal else 200)
            self.assertEqual(response.context['operacao_token'], data['operacao_token'])
            self.assertEqual(self.snapshot(), before)

    def test_inactive_unauthorized_and_forged_responsible_are_rejected(self):
        rental = self.criar()
        inactive = Funcionario.objects.create(nome='Inativo fictício', ativo=False)
        no_cash = Funcionario.objects.create(nome='Não habilitado fictício', ativo=True, pode_operar_sistema=True)
        for modal in (False, True):
            url = self.payment_url(rental) if modal else reverse('locacoes:registrar_pagamento', args=[rental.pk])
            for value in (str(inactive.pk), str(no_cash.pk), '99999999', self.operador.nome, ''):
                with self.subTest(modal=modal, responsible=value):
                    data = {**self.payload(rental, 30), 'responsavel': value}
                    before = self.snapshot()
                    response = self.client.post(url, data)
                    self.assertEqual(response.status_code, 422 if modal else 200)
                    self.assertIn('responsavel', response.context['form'].errors)
                    self.assertEqual(self.snapshot(), before)

    def test_partial_settlement_all_forms_of_payment_and_same_operation_retry(self):
        for modal in (False, True):
            for method in dict(PagamentoLocacao.FORMA_CHOICES):
                with self.subTest(modal=modal, method=method):
                    rental = self.criar()
                    url = self.payment_url(rental) if modal else reverse('locacoes:registrar_pagamento', args=[rental.pk])
                    for amount, balance in ((30, 70), (70, 0)):
                        data = {**self.payload(rental, amount), 'forma_pagamento': method}
                        first = self.client.post(url, data)
                        repeated = self.client.post(url, data)
                        self.assertEqual(first.status_code, 200 if modal else 302)
                        self.assertEqual(first.json()['pagamento'] if modal else first.url,
                                         repeated.json()['pagamento'] if modal else repeated.url)
                        rental.refresh_from_db()
                        self.assertEqual(rental.saldo_devedor, balance)
                    self.assertEqual(rental.pagamentos.count(), 2)
                    self.assertEqual(rental.status, 'reservada')
                    for payment in rental.pagamentos.select_related('movimento_financeiro'):
                        self.assertEqual(payment.responsavel, self.operador.nome)
                        self.assertEqual(payment.movimento_financeiro.operador, self.operador.nome)
                    self.assertEqual(list(rental.eventos.filter(tipo='pagamento').values_list('responsavel', flat=True)),
                                     [self.operador.nome, self.operador.nome])

    def test_service_and_direct_model_creation_refuse_new_unnamed_receipts(self):
        rental = self.criar()
        for value in ('', '   ', None):
            before = self.snapshot()
            with self.assertRaisesMessage(ValidationError, MESSAGE):
                rental.registrar_pagamento(Decimal('30'), 'pix', responsavel=value)
            self.assertEqual(self.snapshot(), before)
        with self.assertRaisesMessage(ValidationError, MESSAGE):
            PagamentoLocacao.objects.create(locacao=rental, valor=30, forma_pagamento='pix', responsavel='')
        self.assertFalse(PagamentoLocacao.objects.exists())

    def test_existing_unnamed_payment_is_not_changed_and_replay_does_not_duplicate(self):
        rental = self.criar()
        # A historical model reproduces a row created before this validation,
        # without disabling or bypassing the new service in the application.
        historical = MigrationLoader(None).project_state([('locacoes', '0018_comprovante_cliente')]).apps.get_model('locacoes', 'PagamentoLocacao')
        token = uuid4()
        legacy = historical.objects.create(locacao_id=rental.pk, valor=Decimal('10'), forma_pagamento='pix',
            responsavel='', recibo_token=token)
        rental.refresh_from_db()
        rental.atualizar_financeiro()
        before = self.snapshot()
        payment = PagamentoLocacao.objects.get(pk=legacy.pk)
        payment.full_clean()
        replay = rental.registrar_pagamento(Decimal('10'), 'pix', responsavel='', recibo_token=token)
        self.assertEqual(replay.pk, payment.pk)
        self.assertEqual(self.snapshot(), before)
        data = self.payload(rental, 30)
        self.assertEqual(self.client.post(self.payment_url(rental), data).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.responsavel, '')

    def nova_data(self, **changes):
        helper = formulario.FormularioErrosTests()
        helper.setUp()
        return helper.dados_nova_locacao(endereco_entrega='', **changes)

    def test_initial_deposit_requires_selected_cashier_before_creating_reservation(self):
        data = self.nova_data(sinal_valor='5.00', sinal_forma_pagamento='pix')
        before = self.snapshot()
        response = self.client.post(reverse('locacoes:nova'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['locacao_form'].errors['sinal_responsavel']), [MESSAGE])
        self.assertEqual(before, self.snapshot())
        response = self.client.post(reverse('locacoes:nova'), {**data, 'sinal_responsavel': str(self.operador.pk)})
        self.assertEqual(response.status_code, 302)
        payment = PagamentoLocacao.objects.get()
        self.assertEqual(payment.responsavel, self.operador.nome)

    def test_reservation_without_deposit_does_not_require_receipt_responsible(self):
        response = self.client.post(reverse('locacoes:nova'), self.nova_data())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PagamentoLocacao.objects.exists())


@skipUnless(saldos.CHROME.exists(), 'Chrome necessário')
class ResponsavelMobileTests(saldos.DadosSaldos, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def test_required_message_server_rejection_and_mobile_payment(self):
        rental = self.criar('Recebimento fictício com responsável')
        with TemporaryDirectory(prefix='responsavel-caixa-mobile-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(str(saldos.CHROME), profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': True})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("saldos-consulta")')
                tab.evaluate('document.querySelector("[data-saldo-categoria=reservas]").click();true')
                tab.wait('!!document.querySelector("[data-receber-locacao]")')
                tab.evaluate('document.querySelector("[data-receber-locacao]").click();true')
                tab.wait('!!document.getElementById("form-recebimento-resumo")')
                self.assertEqual(tab.evaluate('document.querySelector("#form-recebimento-resumo [name=responsavel]").value'), '')
                tab.evaluate('document.querySelector("#form-recebimento-resumo [name=valor]").value="30";document.getElementById("form-recebimento-resumo").requestSubmit();true')
                self.assertEqual(tab.evaluate('document.querySelector("#form-recebimento-resumo [name=responsavel]").validationMessage'), MESSAGE)
                self.assertFalse(PagamentoLocacao.objects.exists())
                # Bypass native validation deliberately: the server must still reject.
                tab.evaluate('const select=document.querySelector("#form-recebimento-resumo [name=responsavel]");select.required=false;select.setCustomValidity("");document.getElementById("form-recebimento-resumo").requestSubmit();true')
                tab.wait('!!document.querySelector("#form-recebimento-resumo .errorlist")')
                self.assertIn(MESSAGE, tab.evaluate('document.querySelector("#form-recebimento-resumo .errorlist").textContent'))
                self.assertEqual(tab.evaluate('document.activeElement.name'), 'responsavel')
                self.assertFalse(PagamentoLocacao.objects.exists())
                self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").scrollWidth <= document.getElementById("modal-recebimento").clientWidth'))
                evidence = settings.BASE_DIR / 'tmp_whatsapp_visual_resumo_financeiro'
                evidence.mkdir(exist_ok=True)
                image = tab.call('Page.captureScreenshot', {'format': 'png'})
                (evidence / 'responsavel-obrigatorio-390.png').write_bytes(base64.b64decode(image['data']))
                tab.evaluate(f'document.querySelector("#form-recebimento-resumo [name=responsavel]").value="{self.operador.pk}";document.querySelector("#form-recebimento-resumo [name=responsavel]").dispatchEvent(new Event("change",{{bubbles:true}}));document.getElementById("form-recebimento-resumo").requestSubmit();document.getElementById("form-recebimento-resumo").requestSubmit();true')
                tab.wait('document.getElementById("status-saldos").textContent.includes("Saldos atualizados")')
                self.assertEqual(PagamentoLocacao.objects.count(), 1)
                payment = PagamentoLocacao.objects.get()
                self.assertEqual((payment.valor, payment.responsavel), (30, self.operador.nome))
                rental.refresh_from_db()
                self.assertEqual(rental.saldo_devedor, 70)
            finally:
                chrome.stop()
