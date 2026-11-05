"""Current balances and the existing payment pipeline, disposable data only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente, FechamentoRotaRecebimento, Funcionario, MovimentoFinanceiro, OperacaoRecebimentoCliente
from locacoes.models import ConfiguracaoLocacao, EventoLocacao, FaixaPrecoLocacao, Locacao, PagamentoLocacao
from locacoes.saldos_consulta import contexto_saldos, indicadores_saldos
from offline.browser_support import Chrome


class DadosSaldos:
    def setUp(self):
        ConfiguracaoLocacao.obter()
        self.faixa, _ = FaixaPrecoLocacao.objects.get_or_create(codigo='centro_perto',
            defaults={'nome': 'Faixa fictícia', 'preco_jogo_diaria': 10})
        self.user = get_user_model().objects.create_superuser('resumo-teste', password='somente-teste')
        self.client.force_login(self.user)
        self.operador = Funcionario.objects.create(nome='Operador fictício', ativo=True, pode_operar_caixa=True)
        self.url = reverse('locacoes:lista')

    def criar(self, nome='Cliente fictício', status='reservada', total=100):
        today = timezone.localdate()
        return Locacao.objects.create(tipo_pessoa='avulsa', pessoa_avulsa_nome=nome,
            faixa_preco=self.faixa, data_entrega=today, data_evento=today, horario_entrega=time(9), horario_evento=time(18),
            data_prevista_devolucao=today + timedelta(days=1), status=status,
            total=total, saldo_devedor=total)

    def payment_url(self, rental):
        return reverse('locacoes:registrar_pagamento', args=[rental.pk]) + '?consulta=1'

    def payload(self, rental, value):
        page = self.client.get(self.payment_url(rental))
        self.assertEqual(page.status_code, 200)
        return {'valor': str(value), 'forma_pagamento': 'pix', 'responsavel': str(self.operador.pk), 'operacao_token': page.context['operacao_token']}


class SaldosConsultaTests(DadosSaldos, TestCase):
    def test_approved_titles_order_and_period_amount_unchanged(self):
        rental = self.criar(status='entregue')
        rental.registrar_pagamento(Decimal('20'), 'pix', responsavel="Operador ficticio")
        page = self.client.get(self.url, {'saldo_categoria': 'andamento'})
        self.assertEqual([item['rotulo'] for item in page.context['indicadores_saldos']],
            ['Entregues a receber', 'Em atraso', 'Reservas futuras', 'Total em aberto'])
        self.assertContains(page, 'Saldo a receber no período')
        self.assertContains(page, '(Recolhidas, mas não quitadas)')
        self.assertNotContains(page, 'Em andamento')
        self.assertEqual(page.context['resumo_financeiro']['saldo'], 80)

    def test_modal_initial_time_is_belem_and_manual_time_preserves_server_audit(self):
        rental = self.criar()
        fixed = datetime.fromisoformat('2026-11-04T02:15:37+00:00')
        with timezone.override('Asia/Tokyo'):
            with patch('locacoes.views.timezone.now', return_value=fixed):
                response = self.client.get(self.payment_url(rental))
            self.assertEqual(response.context['form'].initial['data_hora'], datetime(2026, 11, 3, 23, 15))
            self.assertContains(response, '2026-11-03T23:15')
            self.assertEqual(timezone.get_current_timezone_name(), 'Asia/Tokyo')
            data = {'valor': '10', 'forma_pagamento': 'pix', 'operacao_token': response.context['operacao_token'],
                    'data_hora': '2026-11-02T10:25', 'responsavel': str(self.operador.pk)}
            before = timezone.now()
            result = self.client.post(self.payment_url(rental), data)
            self.assertEqual(result.status_code, 200)
            payment = PagamentoLocacao.objects.get(pk=result.json()['pagamento'])
            self.assertEqual(payment.data_hora, datetime.fromisoformat('2026-11-02T13:25:00+00:00'))
            self.assertGreaterEqual(payment.criado_em, before)
            self.assertGreaterEqual(EventoLocacao.objects.get(tipo='pagamento').criado_em, before)
            repeated = self.client.post(self.payment_url(rental), data)
            self.assertEqual(repeated.json()['pagamento'], payment.pk)
            self.assertEqual(PagamentoLocacao.objects.count(), 1)

    def test_history_is_paginated_read_only_and_keeps_other_rentals_private(self):
        rental = self.criar()
        for _ in range(12):
            rental.registrar_pagamento(Decimal('1'), 'pix', responsavel='Conferente fictício')
        other = self.criar('Outro cliente fictício')
        other_payment = other.registrar_pagamento(Decimal('2'), 'dinheiro', responsavel="Operador ficticio")
        before = {model: list(model.objects.order_by('pk').values()) for model in
                  (Locacao, PagamentoLocacao, EventoLocacao, MovimentoFinanceiro)}
        history_url = self.payment_url(rental) + '&historico=1'
        response = self.client.get(history_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pagina_pagamentos'].paginator.count, 12)
        self.assertEqual(len(response.context['pagina_pagamentos']), 10)
        self.assertContains(response, 'Registro do pagamento no servidor')
        self.assertContains(response, 'Pagamento — horário informado')
        self.assertContains(response, 'data-editar-pagamento=')
        self.assertNotContains(response, f'data-historico-pagamento="{other_payment.pk}"')
        self.assertNotContains(response, 'operacao_token')
        self.assertEqual(len(self.client.get(history_url + '&pagina=2').context['pagina_pagamentos']), 2)
        self.assertEqual(before, {model: list(model.objects.order_by('pk').values()) for model in before})

    def test_categories_signals_partial_return_cancelled_and_paid(self):
        future = self.criar('Reserva com sinal')
        future.registrar_pagamento(Decimal('30'), 'pix', responsavel="Operador ficticio")
        Locacao.objects.filter(pk=future.pk).update(data_vencimento_saldo=date(2020, 1, 1))
        delivered = self.criar('Entregue', 'entregue', 200)
        delivered.registrar_pagamento(Decimal('50'), 'dinheiro', responsavel="Operador ficticio")
        late = self.criar('Recolhimento completo', 'devolvida', 300)
        broken = self.criar('Completo com avaria', 'devolvida_com_avaria', 40)
        partial = self.criar('Recolhimento parcial', 'pendente_devolucao', 80)
        in_transit = self.criar('Entrega ainda não confirmada', 'saiu_para_entrega', 20)
        paid = self.criar('Quitada', 'entregue', 100)
        paid.registrar_pagamento(Decimal('100'), 'pix', responsavel="Operador ficticio")
        cancelled = self.criar('Cancelada', 'cancelada', 500)
        values = {item['chave']: item['valor'] for item in indicadores_saldos()}
        self.assertEqual(values, {'reservas': Decimal('90'), 'andamento': Decimal('230'),
            'atrasadas': Decimal('340'), 'total': Decimal('660')})
        self.assertEqual(sum(values[key] for key in ('reservas', 'andamento', 'atrasadas')), values['total'])
        seen = []
        for category, expected in [('reservas', {future.pk, in_transit.pk}), ('andamento', {delivered.pk, partial.pk}),
                                   ('atrasadas', {late.pk, broken.pk})]:
            ids = {r.pk for r in contexto_saldos(category)['pagina_saldos']}
            self.assertEqual(ids, expected)
            seen.extend(ids)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertNotIn(paid.pk, seen)
        self.assertNotIn(cancelled.pk, seen)

    def test_current_month_default_all_history_option_and_old_contract_payment(self):
        old = self.criar('Contrato antigo', 'devolvida', 200)
        Locacao.objects.filter(pk=old.pk).update(criado_em=timezone.now()-timedelta(days=400))
        old.registrar_pagamento(Decimal('25'), 'pix', responsavel="Operador ficticio")
        page = self.client.get(self.url)
        self.assertEqual(page.context['periodo_selecionado'], 'mes')
        self.assertEqual(page.context['resumo_inicio'], timezone.localdate().replace(day=1).isoformat())
        self.assertEqual(page.context['resumo_financeiro']['quantidade'], 0)
        self.assertEqual(page.context['resumo_financeiro']['recebido'], 25)
        self.assertEqual(page.context['indicadores_saldos'][-1]['valor'], 175)
        history = self.client.get(self.url, {'resumo_periodo': 'historico'})
        self.assertEqual(history.context['resumo_financeiro']['quantidade'], 1)
        for params in ({'resumo_inicio': '2020-01-01', 'resumo_fim': '2020-01-31'},
                       {'cliente': 'Inexistente', 'status': 'cancelada'}, {'resumo_inicio': 'invalida'}):
            page = self.client.get(self.url, params)
            self.assertEqual(page.context['indicadores_saldos'][-1]['valor'], 175)

    def test_queries_are_bounded_and_reads_do_not_write(self):
        for number in range(25):
            self.criar(str(number))
        with self.assertNumQueries(3):
            context = contexto_saldos('total')
            rows = list(context['pagina_saldos'])
            [r.nome_contratante for r in rows]
        self.assertEqual(len(rows), 20)
        self.assertEqual(context['indicadores_saldos'][-1]['quantidade'], 25)
        before = list(Locacao.objects.values())
        with CaptureQueriesContext(connections['default']) as queries:
            self.client.get(self.url, {'fragmento_saldos': '1', 'saldo_categoria': 'reservas'})
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'CREATE')) for q in queries))
        self.assertEqual(before, list(Locacao.objects.values()))

    def test_partial_payment_settlement_and_advance_use_same_pipeline(self):
        rental = self.criar('Reserva antecipada')
        original_status = rental.status
        data = self.payload(rental, 30)
        response = self.client.post(self.payment_url(rental), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        first = response.json()['pagamento']
        duplicate = self.client.post(self.payment_url(rental), data)
        self.assertEqual(duplicate.json()['pagamento'], first)
        rental.refresh_from_db()
        self.assertEqual((rental.total_pago, rental.saldo_devedor, rental.status), (30, 70, original_status))
        self.assertEqual(PagamentoLocacao.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem='locacao').count(), 1)
        self.assertEqual(EventoLocacao.objects.filter(tipo='pagamento').count(), 1)
        refresh = self.client.get(self.url, {'fragmento_financeiro': '1'}).json()
        self.assertIn('70,00', refresh['saldos'])
        self.assertIn('30,00', refresh['periodo'])
        data = self.payload(rental, 70)
        response = self.client.post(self.payment_url(rental), data)
        repeated = self.client.post(self.payment_url(rental), data)
        self.assertEqual(response.json()['pagamento'], repeated.json()['pagamento'])
        rental.refresh_from_db()
        self.assertEqual((rental.total_pago, rental.saldo_devedor, rental.status), (100, 0, original_status))
        self.assertEqual(indicadores_saldos()[-1]['valor'], 0)
        self.assertEqual(PagamentoLocacao.objects.count(), 2)

    def test_invalid_token_cancelled_overpayment_and_changed_duplicate(self):
        rental = self.criar()
        data = self.payload(rental, 10)
        token = data['operacao_token']
        for wrong in ('', 'adulterado'):
            response = self.client.post(self.payment_url(rental), {**data, 'operacao_token': wrong})
            self.assertEqual(response.status_code, 422)
        self.assertFalse(PagamentoLocacao.objects.exists())
        response = self.client.post(self.payment_url(rental), {**data, 'valor': '101'})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, token, status_code=422)
        self.assertEqual(self.client.post(self.payment_url(rental), data).status_code, 200)
        self.assertEqual(self.client.post(self.payment_url(rental), {**data, 'valor': '11'}).status_code, 422)
        self.assertEqual(PagamentoLocacao.objects.count(), 1)
        cancelled = self.criar(status='cancelada')
        self.assertEqual(self.client.get(self.payment_url(cancelled)).status_code, 403)
        self.assertEqual(self.client.post(self.payment_url(cancelled), data).status_code, 403)

    def test_token_bound_to_user_and_rental_and_csrf(self):
        rental = self.criar()
        data = self.payload(rental, 10)
        other = self.criar()
        self.assertEqual(self.client.post(self.payment_url(other), data).status_code, 422)
        another = get_user_model().objects.create_superuser('outro-gestor', password='somente-teste')
        self.client.force_login(another)
        self.assertEqual(self.client.post(self.payment_url(rental), data).status_code, 422)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.user)
        self.assertEqual(guarded.post(self.payment_url(rental), data).status_code, 403)
        self.assertFalse(PagamentoLocacao.objects.exists())

    def test_modal_preserves_existing_access_policy(self):
        middleware = list(settings.MIDDLEWARE)
        if 'sistema.acesso.AcessoComercialMiddleware' not in middleware:
            middleware.append('sistema.acesso.AcessoComercialMiddleware')
        with override_settings(MIDDLEWARE=middleware):
            rental = self.criar()
            anonymous = Client()
            self.assertNotEqual(anonymous.get(self.url).status_code, 200)
            self.assertNotEqual(anonymous.post(self.payment_url(rental)).status_code, 200)
            reader = get_user_model().objects.create_user('sem-recebimento')
            anonymous.force_login(reader)
            self.assertEqual(anonymous.get(self.payment_url(rental)).status_code, 403)


class RecebimentoConcorrenteTests(DadosSaldos, TransactionTestCase):
    def test_same_operation_two_simultaneous_posts_only_one_payment(self):
        rental = self.criar()
        data = self.payload(rental, 100)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.user)
        def pay(client):
            close_old_connections()
            try:
                response = client.post(self.payment_url(rental), data)
                return response.status_code, response.json()['pagamento']
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pay, clients))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 200)
        self.assertEqual(PagamentoLocacao.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem='locacao').count(), 1)


class CorrecaoExistenteTests(DadosSaldos, TestCase):
    """Validate the existing unified service, never present it as a direct-payment editor."""
    def unified(self):
        from estoque.views import _criar_operacao_recebimento_cliente
        rental = self.criar('Cliente da correção existente')
        customer = Cliente.objects.create(nome='Cliente unificado fictício', bairro='Rota fictícia', ativo=True)
        rental.cliente = customer
        rental.tipo_pessoa = 'cliente'
        rental.save(update_fields=['cliente', 'tipo_pessoa'])
        result = _criar_operacao_recebimento_cliente(customer, Decimal('30'), timezone.localdate(), 'PIX',
            dividas_selecionadas=[f'locacao:{rental.pk}'], usuario=self.user)
        operation = result['operacao']
        url = reverse('estoque:receber_cliente_corrigir_recebimento', args=[operation.pk])
        data = {'valor': '20,00', 'data_recebimento': operation.data_recebimento.isoformat(),
                'forma_pagamento': 'PIX', 'motivo': 'Correção fictícia autorizada'}
        return rental, operation, url, data

    def test_unified_authorized_correction_preserves_audit_and_updates_categories(self):
        rental, operation, url, data = self.unified()
        self.assertEqual(self.client.post(url, data).status_code, 302)
        rental.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual((rental.total_pago, rental.saldo_devedor), (20, 80))
        self.assertTrue(operation.comprovante_dados['corrigida'])
        corrected = OperacaoRecebimentoCliente.objects.get(pk=operation.comprovante_dados['corrigida_por_operacao_id'])
        self.assertEqual(corrected.comprovante_dados['correcao_de_operacao_id'], operation.pk)
        self.assertEqual(corrected.comprovante_dados['motivo_correcao'], data['motivo'])
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 2)
        self.assertEqual(PagamentoLocacao.objects.filter(locacao=rental).count(), 1)
        self.client.post(url, data)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 2)
        response = self.client.get(self.url, {'fragmento_saldos': '1', 'saldo_categoria': 'reservas'}).json()
        self.assertIn('80,00', response['html'])

    def test_unified_closed_origin_and_destination_cash_routes_block_correction(self):
        rental, operation, url, data = self.unified()
        for day in (operation.data_recebimento, operation.data_recebimento + timedelta(days=1)):
            closure = FechamentoRotaRecebimento.objects.create(rota=operation.rota_snapshot or 'Rota fictícia',
                data_referencia=day, status='finalizado', total_sistema=30, total_conferido=30)
            try:
                response = self.client.post(url, {**data, 'data_recebimento': (operation.data_recebimento + timedelta(days=1)).isoformat()})
                self.assertEqual(response.status_code, 200)
                rental.refresh_from_db()
                self.assertEqual((rental.total_pago, rental.saldo_devedor), (30, 70))
                self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 1)
                self.assertFalse(MovimentoFinanceiro.objects.filter(origem='recebimento_cliente_estorno').exists())
            finally:
                closure.delete()

    def test_direct_payments_have_no_unified_correction_capability(self):
        rental = self.criar()
        payment = rental.registrar_pagamento(Decimal('10'), 'pix', responsavel="Operador ficticio")
        self.assertIsNone(payment.operacao_recebimento_cliente_id)
        response = self.client.get(self.payment_url(rental) + '&historico=1')
        self.assertNotContains(response, 'corrigir-recebimento')
        self.assertContains(response, 'data-editar-pagamento=')

    def test_correction_permission_is_still_restricted(self):
        _, _, url, data = self.unified()
        middleware = list(settings.MIDDLEWARE)
        if 'sistema.acesso.AcessoComercialMiddleware' not in middleware:
            middleware.append('sistema.acesso.AcessoComercialMiddleware')
        with override_settings(MIDDLEWARE=middleware):
            reader = get_user_model().objects.create_user('sem-correcao')
            self.client.force_login(reader)
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 1)


CHROME = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')


@skipUnless(CHROME.exists(), 'Chrome necessário para validar a interface')
class SaldosBrowserTests(DadosSaldos, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def test_desktop_mobile_receive_partial_settle_and_refresh(self):
        rental = self.criar('Cliente fictício do modal')
        self.criar('Recolhida com dívida', 'devolvida', 150)
        with TemporaryDirectory(prefix='resumo-saldos-chrome-') as profile:
            chrome = Chrome(str(CHROME), profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("saldos-consulta")')
                evidence = settings.BASE_DIR / 'tmp_whatsapp_visual_resumo_financeiro'
                evidence.mkdir(exist_ok=True)
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': width < 600})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertEqual(tab.evaluate('Array.from(document.querySelectorAll(".saldo-titulo")).map(el=>el.firstChild.textContent.trim())'),
                        ['Entregues a receber', 'Em atraso', 'Reservas futuras', 'Total em aberto'])
                    metrics = tab.evaluate('Array.from(document.querySelectorAll(".saldos-indicadores button")).map(el=>({top:el.getBoundingClientRect().top,title:el.querySelector(".saldo-titulo").getBoundingClientRect().top,value:el.querySelector(":scope>strong").getBoundingClientRect().top,count:el.querySelector(":scope>small").getBoundingClientRect().top}))')
                    for left, right in zip(metrics, metrics[1:]):
                        if abs(left['top'] - right['top']) < 1:
                            for key in ('title', 'value', 'count'):
                                self.assertLessEqual(abs(left[key] - right[key]), 1)
                    tab.evaluate('document.getElementById("saldos-consulta").scrollIntoView({block:"start"});true')
                    shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                    (evidence / f'consulta-{width}.png').write_bytes(base64.b64decode(shot['data']))
                tab.evaluate('document.querySelector("[data-saldo-categoria=reservas]").click();true')
                tab.wait('document.getElementById("saldos-consulta").dataset.categoria === "reservas"')
                for value, balance in [(30, 70), (70, 0)]:
                    tab.evaluate('document.querySelector("[data-receber-locacao]").click();true')
                    tab.wait('!!document.querySelector("#form-recebimento-resumo [name=valor]")')
                    self.assertTrue(tab.evaluate('document.activeElement.name === "valor"'))
                    self.assertTrue(tab.evaluate('!!document.querySelector("#form-recebimento-resumo [name=data_hora]").value'))
                    button_style = tab.evaluate('(()=>{const s=getComputedStyle(document.querySelector("[data-receber-locacao]"));return {background:s.backgroundColor,color:s.color}})()')
                    self.assertEqual(button_style, {'background': 'rgb(21, 128, 61)', 'color': 'rgb(255, 255, 255)'})
                    self.assertTrue(tab.evaluate('document.getElementById("modal-recebimento").scrollWidth <= document.getElementById("modal-recebimento").clientWidth'))
                    if balance == 0:
                        tab.evaluate('document.getElementById("historico-recebimentos").open=true;true')
                        tab.wait('!!document.querySelector("[data-historico-pagamento]")')
                        self.assertIn('30,00', tab.evaluate('document.querySelector(".historico-pagamentos").textContent'))
                        self.assertIn('Registro do pagamento no servidor', tab.evaluate('document.querySelector(".historico-pagamentos").textContent'))
                    shot = tab.call('Page.captureScreenshot', {'format': 'png'})
                    (evidence / ('modal-historico-320.png' if balance == 0 else 'modal-320.png')).write_bytes(base64.b64decode(shot['data']))
                    tab.evaluate(f'document.querySelector("#form-recebimento-resumo [name=valor]").value="{value}";document.querySelector("#form-recebimento-resumo [name=forma_pagamento]").value="pix";document.querySelector("#form-recebimento-resumo [name=responsavel]").value="{self.operador.pk}";document.getElementById("form-recebimento-resumo").requestSubmit();true')
                    tab.wait('document.getElementById("status-saldos").textContent.includes("Saldos atualizados")')
                    rental.refresh_from_db()
                    self.assertEqual(rental.saldo_devedor, balance)
                    self.assertEqual(tab.evaluate('location.pathname'), self.url)
                    self.assertFalse(tab.evaluate('document.getElementById("modal-recebimento").open'))
                    if balance:
                        self.assertIn('70,00', tab.evaluate('document.getElementById("saldos-consulta").textContent'))
                self.assertFalse(tab.evaluate('!!document.querySelector("[data-saldo-locacao=\\"' + str(rental.pk) + '\\"]")'))
                self.assertEqual(PagamentoLocacao.objects.count(), 2)
            finally:
                chrome.stop()
