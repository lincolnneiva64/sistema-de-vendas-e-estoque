"""Direct-payment corrections: disposable PostgreSQL only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from estoque.documentos_horarios import FUSO_DOCUMENTOS
from estoque.models import ContaFinanceira, FechamentoRotaRecebimento, Funcionario, MovimentoFinanceiro
from .models import CorrecaoPagamentoLocacao, EventoLocacao, Locacao, PagamentoLocacao
from .payment_corrections import correct_direct_payment, correction_receipt_url, version
from .tests_saldos_consulta import DadosSaldos


class CorrectionData(DadosSaldos):
    def setUp(self):
        super().setUp()
        self.rental = self.criar('Cliente da correção', 'entregue', 100)
        self.payment = self.rental.registrar_pagamento(Decimal('100'), 'pix', responsavel=self.operador.nome)
        self.rental.refresh_from_db()
        for form in ('pix', 'dinheiro', 'cartao'):
            PagamentoLocacao.conta_financeira_para_forma(form)
        self.edit_url = reverse('locacoes:corrigir_pagamento_direto', args=[self.rental.pk, self.payment.pk])

    def values(self, **extra):
        values = dict(payment_id=self.payment.pk, rental_id=self.rental.pk, actor=self.user,
                      operation_id=uuid4(), expected_version=version(self.payment), valor=Decimal('70'),
                      data_hora=self.payment.data_hora, forma_pagamento='pix', responsavel_id=self.operador.pk,
                      observacao='Conferido', motivo='Valor informado incorretamente')
        values.update(extra)
        return values

    def state(self):
        return {m.__name__: list(m.objects.order_by('pk').values()) for m in
                (Locacao, PagamentoLocacao, MovimentoFinanceiro, EventoLocacao, CorrecaoPagamentoLocacao)}

    def post_values(self, **extra):
        page = self.client.get(self.edit_url)
        self.assertEqual(page.status_code, 200)
        values = dict(valor='70', forma_pagamento='pix', responsavel=str(self.operador.pk),
                      data_hora=timezone.localtime(self.payment.data_hora, FUSO_DOCUMENTOS).strftime('%Y-%m-%dT%H:%M'),
                      observacao='Conferido', motivo='Correção autorizada', operacao_token=page.context['operacao_token'])
        values.update(extra)
        return values


class DirectPaymentCorrectionTests(CorrectionData, TestCase):
    def test_audit_table_has_the_approved_unique_constraints(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, CorrecaoPagamentoLocacao._meta.db_table)
        unique = {tuple(info['columns']) for info in constraints.values() if info['unique']}
        for fields in (('operacao_uuid',), ('pagamento_id', 'versao_anterior'), ('estorno_id',), ('movimento_novo_id',)):
            self.assertIn(fields, unique)

    def test_value_change_reopens_balance_preserves_original_entry_and_audits(self):
        original = MovimentoFinanceiro.objects.get(pk=self.payment.movimento_financeiro_id)
        original_fields = {f.attname: getattr(original, f.attname) for f in original._meta.concrete_fields}
        created_at, token = self.payment.criado_em, self.payment.recibo_token
        correction = correct_direct_payment(**self.values())
        self.payment.refresh_from_db(); self.rental.refresh_from_db(); original.refresh_from_db()
        self.assertEqual(self.payment.valor, 70)
        self.assertEqual(self.rental.saldo_devedor, 30)
        self.assertEqual(self.rental.status_financeiro, 'parcial')
        self.assertEqual(PagamentoLocacao.objects.count(), 1)
        self.assertEqual(self.payment.criado_em, created_at)
        self.assertEqual(self.payment.recibo_token, token)
        self.assertEqual(original_fields, {f.attname: getattr(original, f.attname) for f in original._meta.concrete_fields})
        self.assertEqual(correction.antes['pagamento']['valor'], '100.00')
        self.assertEqual(correction.depois['pagamento']['valor'], '70.00')
        self.assertEqual(correction.usuario_id, self.user.pk)
        self.assertEqual(correction.estorno.valor, 100)
        self.assertEqual(correction.movimento_novo.valor, 70)
        self.assertTrue(correction.criado_em)
        self.assertEqual(self.rental.eventos.filter(tipo='pagamento_corrigido').count(), 1)
        from .saldos_consulta import indicadores_saldos
        indicators = {i['chave']: i['valor'] for i in indicadores_saldos()}
        self.assertEqual(indicators['andamento'], 30)
        self.assertEqual(indicators['total'], 30)

    def test_date_form_employee_update_and_net_account_balances(self):
        employee = Funcionario.objects.create(nome='Outra operadora', ativo=True, pode_operar_caixa=True)
        date = self.payment.data_hora - timedelta(days=2)
        correction = correct_direct_payment(**self.values(forma_pagamento='dinheiro', data_hora=date, responsavel_id=employee.pk))
        self.assertEqual(correction.movimento_novo.conta.tipo, 'caixa')
        self.assertEqual(correction.movimento_novo.data, timezone.localtime(date, FUSO_DOCUMENTOS).date())
        self.assertEqual(correction.movimento_novo.operador, employee.nome)
        original_net = sum((m.valor if m.tipo == 'entrada' else -m.valor)
                           for m in MovimentoFinanceiro.objects.filter(conta=correction.movimento_original.conta))
        self.assertEqual(original_net, 0)
        self.assertEqual(MovimentoFinanceiro.objects.filter(conta=correction.movimento_novo.conta).count(), 1)
        from .consulta import resumo_financeiro_locacoes
        day = timezone.localtime(date, FUSO_DOCUMENTOS).date()
        self.assertEqual(resumo_financeiro_locacoes(inicio=day, fim=day)['recebido'], 70)

    def test_both_dates_any_closure_status_and_route_are_blocked(self):
        new_date = self.payment.data_hora - timedelta(days=2)
        for day in {timezone.localtime(self.payment.data_hora, FUSO_DOCUMENTOS).date(),
                    timezone.localtime(new_date, FUSO_DOCUMENTOS).date()}:
            for status in ('aberto', 'conferido', 'finalizado'):
                closure = FechamentoRotaRecebimento.objects.create(rota='Outra rota, sem inferência histórica',
                                                                 data_referencia=day, status=status)
                before = self.state()
                with self.assertRaisesMessage(ValidationError, 'Existe fechamento'):
                    correct_direct_payment(**self.values(data_hora=new_date))
                self.assertEqual(self.state(), before)
                closure.delete()

    def test_invalid_values_reason_actor_and_employee_leave_no_changes(self):
        regular = get_user_model().objects.create_user('sem-correcao', is_staff=True)
        for values, error in (({'valor': Decimal('101')}, ValidationError), ({'valor': Decimal('0')}, ValidationError),
                              ({'valor': Decimal('1.111')}, ValidationError), ({'motivo': ''}, ValidationError),
                              ({'actor': regular}, PermissionDenied)):
            before = self.state()
            with self.assertRaises(error):
                correct_direct_payment(**self.values(**values))
            self.assertEqual(self.state(), before)
        for changes in ({'ativo': False}, {'ativo': True, 'pode_operar_caixa': False}):
            Funcionario.objects.filter(pk=self.operador.pk).update(**changes)
            before = self.state()
            with self.assertRaisesMessage(ValidationError, 'funcionário ativo'):
                correct_direct_payment(**self.values())
            self.assertEqual(self.state(), before)

    def test_missing_inactive_or_mismatched_financial_context_is_blocked(self):
        movement = self.payment.movimento_financeiro
        for change in ({'valor': Decimal('99')}, {'origem': 'manual'}, {'operador': 'Divergente'}):
            with transaction.atomic():
                MovimentoFinanceiro.objects.filter(pk=movement.pk).update(**change)
                self.payment.refresh_from_db()
                before = self.state()
                with self.assertRaises(ValidationError):
                    correct_direct_payment(**self.values())
                self.assertEqual(self.state(), before)
                transaction.set_rollback(True)
        ContaFinanceira.objects.filter(pk=movement.conta_id).update(ativo=False)
        self.payment.refresh_from_db()
        before = self.state()
        with self.assertRaises(ValidationError):
            correct_direct_payment(**self.values())
        self.assertEqual(self.state(), before)

    def test_idempotency_and_old_form_rejection(self):
        values = self.values()
        correction = correct_direct_payment(**values)
        before = self.state()
        self.assertEqual(correct_direct_payment(**values).pk, correction.pk)
        with self.assertRaisesMessage(ValidationError, 'outros dados'):
            correct_direct_payment(**{**values, 'valor': Decimal('60')})
        with self.assertRaisesMessage(ValidationError, 'foi alterado'):
            correct_direct_payment(**{**values, 'operation_id': uuid4()})
        self.assertEqual(self.state(), before)

    def test_rollback_after_reversal_and_new_entry(self):
        before = self.state()
        for manager in (CorrecaoPagamentoLocacao.objects, EventoLocacao.objects):
            with patch.object(manager, 'create', side_effect=RuntimeError('Falha isolada')):
                with self.assertRaises(RuntimeError):
                    correct_direct_payment(**self.values())
            self.assertEqual(self.state(), before)

    def test_normal_receiving_still_settles_the_reopened_balance(self):
        correct_direct_payment(**self.values())
        values = self.payload(self.rental, 30)
        response = self.client.post(self.payment_url(self.rental), values)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(self.payment_url(self.rental), values).json(), response.json())
        self.rental.refresh_from_db()
        self.assertEqual(self.rental.saldo_devedor, 0)
        self.assertEqual(self.rental.total_pago, 100)
        self.assertEqual(self.rental.pagamentos.count(), 2)

    def test_receipts_original_and_each_version_are_preserved(self):
        self.payment.confirmar_recibo_enviado('Responsável original')
        self.payment.refresh_from_db()
        correction = correct_direct_payment(**self.values())
        original_url = reverse('locacoes:recibo_pagamento', args=[self.payment.pk])
        old_html = self.client.get(original_url).content
        corrected_html = self.client.get(correction_receipt_url(correction)).content
        self.assertEqual(old_html.decode(), correction.recibo_anterior_html)
        self.assertIn(b'100.00', old_html)  # Original WhatsApp message keeps its original amount.
        self.assertIn(b'70.00', corrected_html)
        self.assertNotIn('Confirmação de envio do recibo', corrected_html.decode())
        self.payment.refresh_from_db()
        second = correct_direct_payment(**self.values(valor=Decimal('60')))
        self.assertEqual(second.recibo_anterior_html, correction.recibo_corrigido_html)
        self.assertEqual(self.client.get(original_url).content, old_html)
        self.assertEqual(self.client.get(correction_receipt_url(correction)).content, corrected_html)
        self.assertNotEqual(self.client.get(correction_receipt_url(second)).content, corrected_html)
        self.assertEqual(self.client.get(original_url + '?versao=' + str(uuid4())).status_code, 404)
        self.assertEqual(self.payment.correcoes.count(), 2)
        with self.assertRaises(ValidationError):
            correction.save()
        with self.assertRaises(ValidationError):
            correction.delete()

    def test_verified_destination_is_kept_if_another_account_becomes_available(self):
        from .payment_corrections import freeze_receipt
        destination = PagamentoLocacao.conta_financeira_para_forma('dinheiro')
        alternative = ContaFinanceira.objects.create(pk=0, nome='Caixa em espécie', tipo='caixa', ativo=False)

        def change_available_account(*args, **kwargs):
            ContaFinanceira.objects.filter(pk=alternative.pk).update(ativo=True)
            return freeze_receipt(*args, **kwargs)

        with patch('locacoes.payment_corrections.freeze_receipt', side_effect=change_available_account):
            correction = correct_direct_payment(**self.values(forma_pagamento='dinheiro'))
        self.assertEqual(correction.movimento_novo.conta_id, destination.pk)

    def test_modal_post_csrf_permissions_required_reason_and_scoped_token(self):
        data = self.post_values()
        form = self.client.get(self.edit_url)
        self.assertContains(form, 'Editar recebimento')
        self.assertContains(form, 'Salvar correção')
        self.assertContains(form, 'id="form-recebimento-resumo"')
        before = self.state()
        denied = self.client.post(self.edit_url, {**data, 'motivo': ''})
        self.assertEqual(denied.status_code, 422)
        self.assertContains(denied, 'Informe o motivo da correção.', status_code=422)
        self.assertEqual(self.state(), before)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.edit_url, data).status_code, 403)
        self.assertEqual(Client().post(self.edit_url, data).status_code, 401)
        user = get_user_model().objects.create_user('sem-permissao', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.edit_url).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.edit_url, {**data, 'operacao_token': 'adulterado'}).status_code, 400)
        self.assertEqual(self.state(), before)
        response = self.client.post(self.edit_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(self.edit_url, data).json(), response.json())
        history = self.client.get(self.payment_url(self.rental) + '&historico=1')
        self.assertContains(history, 'Correção registrada')
        self.assertContains(history, 'Correção autorizada')

    def test_footer_edit_action_and_recent_payment_selector_preserve_history_and_data(self):
        self.rental.total = 200
        self.rental.save(update_fields=['total'])
        older = self.rental.registrar_pagamento(15, 'dinheiro',
            data_hora=self.payment.data_hora - timedelta(days=2), responsavel=self.operador.nome)
        middle = self.rental.registrar_pagamento(10, 'cartao',
            data_hora=self.payment.data_hora - timedelta(days=1), responsavel=self.operador.nome)
        before = self.state()
        form = self.client.get(self.payment_url(self.rental))
        self.assertContains(form, 'Editar pagamento')
        self.assertNotContains(form, 'modos-recebimento')
        self.assertContains(form, 'Voltar ao novo recebimento')
        html = form.content.decode()
        self.assertGreater(html.index('class="acoes-recebimento"'), html.index('id="form-recebimento-resumo"'))
        selection = self.client.get(self.payment_url(self.rental) + '&historico=1&selecao=1')
        self.assertEqual([p.pk for p in selection.context['pagina_pagamentos']],
                         [self.payment.pk, middle.pk, older.pk])
        self.assertContains(selection, 'data-pagamento-opcao=', count=3)
        self.assertContains(selection, 'data-editar-pagamento=', count=3)
        self.assertContains(selection, 'Data do pagamento:')
        self.assertContains(selection, self.operador.nome)
        self.assertContains(selection, 'Cartao')
        self.assertContains(selection, 'Ver recibo')
        history = self.client.get(self.payment_url(self.rental) + '&historico=1')
        self.assertContains(history, 'Registro do pagamento no servidor')
        self.assertEqual(self.state(), before)

    def test_unified_payment_is_not_individually_editable(self):
        from estoque.models import Cliente, OperacaoRecebimentoCliente
        customer = Cliente.objects.create(nome='Cliente unificado')
        operation = OperacaoRecebimentoCliente.objects.create(cliente=customer, cliente_nome_snapshot=customer.nome,
            valor_recebido=100, valor_aplicado=100, saldo_anterior=100, saldo_atual=0,
            data_recebimento=timezone.localdate(), forma_pagamento='Pix')
        PagamentoLocacao.objects.filter(pk=self.payment.pk).update(operacao_recebimento_cliente=operation)
        self.payment.refresh_from_db()
        before = self.state()
        self.assertEqual(self.client.get(self.edit_url).status_code, 403)
        with self.assertRaisesMessage(ValidationError, 'Recebimento unificado'):
            correct_direct_payment(**self.values())
        self.assertEqual(self.state(), before)
        history = self.client.get(self.payment_url(self.rental) + '&historico=1')
        self.assertContains(history, 'Não é permitida edição individual')
        self.assertNotContains(history, 'data-editar-pagamento=')
        selection = self.client.get(self.payment_url(self.rental) + '&historico=1&selecao=1')
        self.assertContains(selection, 'Não é permitida edição individual')
        self.assertNotContains(selection, 'data-editar-pagamento=')


class DirectPaymentCorrectionConcurrencyTests(CorrectionData, TransactionTestCase):
    def worker(self, values, barrier):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return correct_direct_payment(**values).pk
            except ValidationError:
                return 'recusada'
        finally:
            close_old_connections()

    def test_same_uuid_is_applied_once_across_connections(self):
        values = self.values(); barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.worker(values, barrier), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(CorrecaoPagamentoLocacao.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 3)

    def test_competing_versions_accept_only_one(self):
        values = self.values(); barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.worker, {**values, 'operation_id': uuid4()}, barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results.count('recusada'), 1)
        self.assertEqual(CorrecaoPagamentoLocacao.objects.count(), 1)

    def test_closure_committing_concurrently_is_seen_before_correction(self):
        values = self.values(); started = Event()
        def correct():
            close_old_connections()
            try:
                started.set()
                try:
                    return correct_direct_payment(**values).pk
                except ValidationError:
                    return 'recusada'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                FechamentoRotaRecebimento.objects.create(rota='Conferência concorrente',
                    data_referencia=timezone.localtime(self.payment.data_hora, FUSO_DOCUMENTOS).date())
                result = pool.submit(correct)
                self.assertTrue(started.wait(10))
            self.assertEqual(result.result(timeout=15), 'recusada')
        self.assertFalse(CorrecaoPagamentoLocacao.objects.exists())

    def test_closure_summary_waits_for_committing_correction(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from estoque.views import conferencia_recebimentos_rota
        from .payment_corrections import freeze_receipt
        paused, release, closing_started, summarized = Event(), Event(), Event(), Event()
        observed = []

        def freeze(payment, notice, corrected=False):
            if corrected:
                paused.set()
                if not release.wait(10):
                    raise RuntimeError('Teste de concorrência não liberou a correção.')
            return freeze_receipt(payment, notice, corrected=corrected)

        def summary(*args):
            observed.append(PagamentoLocacao.objects.get(pk=self.payment.pk).valor)
            summarized.set()
            return {'itens': []}

        def close():
            close_old_connections()
            try:
                request = RequestFactory().post('/conferencia/?rota=Teste', {})
                request.user = self.user
                request.session = {}
                request._messages = FallbackStorage(request)
                closing_started.set()
                return conferencia_recebimentos_rota(request).status_code
            finally:
                close_old_connections()

        def correct():
            close_old_connections()
            try:
                return correct_direct_payment(**values).pk
            finally:
                close_old_connections()

        values = self.values()
        with patch('locacoes.payment_corrections.freeze_receipt', side_effect=freeze), \
                patch('estoque.views._resumo_conferencia_recebimentos_rota', side_effect=summary), \
                ThreadPoolExecutor(max_workers=2) as pool:
            correcting = pool.submit(correct)
            try:
                self.assertTrue(paused.wait(10))
                closing = pool.submit(close)
                self.assertTrue(closing_started.wait(10))
                self.assertFalse(summarized.wait(0.2))
            finally:
                release.set()
            correcting.result(timeout=15)
            self.assertEqual(closing.result(timeout=15), 302)
        self.assertEqual(observed, [Decimal('70')])


CHROME = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')


@skipUnless(CHROME.exists(), 'Chrome necessário para a validação visual')
class DirectPaymentCorrectionBrowserTests(CorrectionData, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def test_footer_alignment_and_empty_payment_list_returns_without_closing(self):
        from offline.browser_support import Chrome
        empty = self.criar('Cliente fictício sem pagamentos', 'entregue', 100)
        before = self.state()
        with TemporaryDirectory(prefix='locacao-rodape-browser-') as profile:
            chrome = Chrome(str(CHROME), profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': settings.SESSION_COOKIE_NAME,
                    'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("saldos-consulta")')
                tab.evaluate('document.querySelector("[data-saldo-categoria=andamento]").click();true')
                tab.wait('!!document.querySelector(".saldos-lista [data-receber-locacao]")')
                tab.evaluate(f'''document.querySelector('[data-saldo-locacao="{empty.pk}"] [data-receber-locacao]').click();true''')
                tab.wait('!!document.getElementById("form-recebimento-resumo")')
                self.assertEqual(tab.evaluate('document.getElementById("titulo-recebimento").textContent'), 'Receber / Quitar')
                evidence = settings.BASE_DIR / 'diagnostics' / 'correcao-pagamento'
                evidence.mkdir(parents=True, exist_ok=True)
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000,
                             'deviceScaleFactor': 1, 'mobile': width < 600})
                    tab.evaluate('document.querySelector(".acoes-recebimento").scrollIntoView({block:"center"});true')
                    positions = tab.evaluate('Array.from(document.querySelectorAll(".acoes-recebimento button")).map(b=>({x:b.getBoundingClientRect().x,y:b.getBoundingClientRect().y,right:b.getBoundingClientRect().right}))')
                    self.assertEqual(len(positions), 3)
                    self.assertLess(positions[0]['x'], positions[1]['x'])
                    self.assertLess(positions[1]['x'], positions[2]['x'])
                    for position in positions[1:]:
                        self.assertLess(abs(position['y'] - positions[0]['y']), 2)
                    row_right = tab.evaluate('document.querySelector(".acoes-recebimento").getBoundingClientRect().right')
                    self.assertLessEqual(positions[-1]['right'], row_right + 1)
                    self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").scrollWidth<=document.getElementById("modal-recebimento").clientWidth'))
                    self.assertTrue(tab.evaluate('Array.from(document.querySelectorAll(".acoes-recebimento button")).every(b=>b.scrollWidth<=b.clientWidth)'))
                    shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                    (evidence / f'rodape-editar-{width}.png').write_bytes(base64.b64decode(shot['data']))
                tab.evaluate('document.querySelector("[name=valor]").value="7";window.originalToken=document.querySelector("[name=operacao_token]").value;document.querySelector(".acoes-recebimento [data-modo-recebimento=corrigir]").click();true')
                tab.wait('document.querySelector("[data-selecao-pagamentos]").textContent.includes("Nenhum pagamento registrado")')
                self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").open'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("[data-pagamento-opcao]").length'), 0)
                tab.evaluate('document.querySelector("[data-selecao-pagamentos] [data-modo-recebimento=novo]").click();true')
                self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").open'))
                self.assertFalse(tab.evaluate('document.getElementById("form-recebimento-resumo").hidden'))
                self.assertEqual(tab.evaluate('document.querySelector("[name=valor]").value'), '7')
                self.assertTrue(tab.evaluate('document.querySelector("[name=operacao_token]").value===window.originalToken'))
                self.assertEqual(self.state(), before)
            finally:
                chrome.stop()

    def test_modes_multiple_payments_errors_and_scroll_only_after_correction(self):
        from offline.browser_support import Chrome
        self.rental.total = 200
        self.rental.save(update_fields=['total'])
        older = self.rental.registrar_pagamento(15, 'dinheiro',
            data_hora=self.payment.data_hora - timedelta(days=2), responsavel=self.operador.nome)
        middle = self.rental.registrar_pagamento(10, 'cartao',
            data_hora=self.payment.data_hora - timedelta(days=1), responsavel=self.operador.nome)
        with TemporaryDirectory(prefix='locacao-modos-browser-') as profile:
            chrome = Chrome(str(CHROME), profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': settings.SESSION_COOKIE_NAME,
                    'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("saldos-consulta")')
                tab.evaluate('document.querySelector("[data-saldo-categoria=andamento]").click();true')
                tab.wait('!!document.querySelector(".saldos-lista [data-receber-locacao]")')
                tab.evaluate('const spacer=document.createElement("div");spacer.style.height="2400px";document.body.appendChild(spacer);window.scrollTo(0,1600);document.querySelector(".saldos-lista [data-receber-locacao]").click();true')
                tab.wait('!!document.getElementById("form-recebimento-resumo")')
                self.assertEqual(tab.evaluate('document.querySelector(".acoes-recebimento [data-modo-recebimento]").textContent'), 'Editar pagamento')
                self.assertEqual(tab.evaluate('document.querySelectorAll(".modos-recebimento").length'), 0)
                tab.evaluate('document.querySelector("[name=valor]").value="7";window.originalToken=document.querySelector("[name=operacao_token]").value;document.querySelector("[data-modo-recebimento=corrigir]").click();true')
                tab.wait('document.querySelectorAll("[data-pagamento-opcao]").length===3')
                self.assertEqual(tab.evaluate('Array.from(document.querySelectorAll("[data-pagamento-opcao]")).map(p=>Number(p.dataset.pagamentoOpcao))'),
                                 [self.payment.pk, middle.pk, older.pk])
                self.assertTrue(tab.evaluate('document.getElementById("form-recebimento-resumo").hidden'))
                evidence = settings.BASE_DIR / 'diagnostics' / 'correcao-pagamento'
                evidence.mkdir(parents=True, exist_ok=True)
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000,
                             'deviceScaleFactor': 1, 'mobile': width < 600})
                    self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").scrollWidth<=document.getElementById("modal-recebimento").clientWidth'))
                    self.assertTrue(tab.evaluate('document.querySelector("[data-selecao-pagamentos] .voltar-recebimento").getBoundingClientRect().top>0'))
                    shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                    (evidence / f'selecionar-pagamento-{width}.png').write_bytes(base64.b64decode(shot['data']))
                tab.evaluate(f'''document.querySelector('[data-pagamento-opcao="{older.pk}"] [data-editar-pagamento]').click();true''')
                tab.wait('!!document.querySelector("[data-correcao-pagamento]") && document.querySelector("[name=valor]").value==="15.00"')
                tab.evaluate('document.querySelector("[data-voltar-formulario] [data-modo-recebimento=novo]").click();true')
                self.assertEqual(tab.evaluate('document.querySelector("[name=valor]").value'), '7')
                self.assertTrue(tab.evaluate('document.querySelector("[name=operacao_token]").value===window.originalToken'))
                self.assertFalse(tab.evaluate('document.getElementById("form-recebimento-resumo").hidden'))
                tab.evaluate('document.querySelector("[data-modo-recebimento=corrigir]").click();true')
                tab.evaluate(f'''document.querySelector('[data-pagamento-opcao="{self.payment.pk}"] [data-editar-pagamento]').click();true''')
                tab.wait('!!document.querySelector("[data-correcao-pagamento]") && document.querySelector("[name=valor]").value==="100.00"')
                self.assertEqual(tab.evaluate('document.querySelectorAll("dialog[open]").length'), 1)
                before_y = tab.evaluate('window.scrollY')
                tab.evaluate('document.querySelector("#form-recebimento-resumo").noValidate=true;document.querySelector("[name=valor]").value="60";document.querySelector("[name=motivo]").value="";document.querySelector("#form-recebimento-resumo [type=submit]").click();true')
                tab.wait('document.getElementById("status-recebimento").textContent.includes("Revise a correção")')
                self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").open'))
                self.assertEqual(tab.evaluate('window.scrollY'), before_y)
                self.assertFalse(CorrecaoPagamentoLocacao.objects.exists())
                tab.evaluate('document.querySelector("[name=motivo]").value="Correção visual autorizada";document.querySelector("#form-recebimento-resumo [type=submit]").click();true')
                tab.wait('!document.getElementById("modal-recebimento").open && document.getElementById("status-saldos").textContent.includes("Correção confirmada")')
                tab.wait('Math.abs(document.getElementById("saldos-consulta").getBoundingClientRect().top-12)<4')
                self.assertGreater(tab.evaluate('window.scrollY'), 0)
                self.assertEqual(tab.evaluate('document.getElementById("saldos-consulta").dataset.categoria'), 'andamento')
                self.assertTrue(tab.evaluate('!!document.querySelector("#saldos-consulta #status-saldos")'))
                self.assertTrue(tab.evaluate('document.querySelector("[data-saldo-categoria=andamento]").getAttribute("aria-pressed")==="true"'))
                self.rental.refresh_from_db()
                self.assertEqual(self.rental.saldo_devedor, 115)
                self.assertEqual(CorrecaoPagamentoLocacao.objects.count(), 1)
                shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                (evidence / 'correcao-retorno-saldos-320.png').write_bytes(base64.b64decode(shot['data']))
            finally:
                chrome.stop()

    def test_same_modal_desktop_and_mobile_edit_and_refresh(self):
        from offline.browser_support import Chrome
        # Leave a balance so the existing receiving action is available.
        correction = correct_direct_payment(**self.values(valor=Decimal('80')))
        with TemporaryDirectory(prefix='locacao-correcao-browser-') as profile:
            chrome = Chrome(str(CHROME), profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': settings.SESSION_COOKIE_NAME,
                    'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("saldos-consulta")')
                tab.evaluate('document.querySelector("[data-saldo-categoria=andamento]").click();true')
                tab.wait('!!document.querySelector("[data-receber-locacao]")')
                tab.evaluate('document.querySelector("[data-receber-locacao]").click();true')
                tab.wait('!!document.getElementById("historico-recebimentos")')
                tab.evaluate('document.getElementById("historico-recebimentos").open=true;true')
                tab.wait('!!document.querySelector("[data-editar-pagamento]")')
                tab.evaluate('document.querySelector("[data-editar-pagamento]").click();true')
                tab.wait('!!document.querySelector("[data-correcao-pagamento]")')
                self.assertEqual(tab.evaluate('document.querySelectorAll("dialog[open]").length'), 1)
                self.assertEqual(tab.evaluate('document.getElementById("titulo-recebimento").textContent'), 'Editar recebimento')
                evidence = settings.BASE_DIR / 'diagnostics' / 'correcao-pagamento'
                evidence.mkdir(parents=True, exist_ok=True)
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000,
                             'deviceScaleFactor': 1, 'mobile': width < 600})
                    self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").scrollWidth <= document.getElementById("modal-recebimento").clientWidth'))
                    shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                    (evidence / f'editar-{width}.png').write_bytes(base64.b64decode(shot['data']))
                tab.evaluate('document.querySelector("[name=valor]").value="60"; document.querySelector("[name=motivo]").value="Conferência visual fictícia"; document.querySelector("#form-recebimento-resumo [type=submit]").click();true')
                tab.wait('!document.getElementById("modal-recebimento").open && document.getElementById("status-saldos").textContent.includes("Correção confirmada")')
                self.rental.refresh_from_db()
                self.assertEqual(self.rental.saldo_devedor, 40)
                self.assertEqual(CorrecaoPagamentoLocacao.objects.count(), 2)
            finally:
                chrome.stop()
