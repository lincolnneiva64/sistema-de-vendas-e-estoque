"""Additional PostgreSQL-only HTTP race and rollback validation; fixtures only."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from estoque.models import Venda, ItemVenda, MovimentoFinanceiro, EventoVenda
from .models import OperacaoSincronizacao
from .services import command_hash, process_operation
from . import tests_local_sales as base


@skipUnless(connection.vendor == 'postgresql', 'Requires disposable local PostgreSQL')
@override_settings(LOCAL_SALES_LOOPBACK=True)
class LocalSaleHTTPPostgreSQLTests(TransactionTestCase):
    setUp = base.LocalSaleTests.setUp
    build = base.LocalSaleTests.build
    send = base.LocalSaleTests.send
    lookup = base.LocalSaleTests.lookup
    effects = base.LocalSaleTests.effects

    def cash(self, command=None):
        command = copy.deepcopy(command or self.command)
        data = command['payload']['dados'] if command['type'] == 'venda_local' else command['payload']
        data['tipo_pagamento'] = 'A vista'
        data['origem_recebimento'] = {'caixa':'8', 'banco':'12'}
        return command

    def races(self, commands, modes=None):
        modes = modes or ['local'] * len(commands)
        barrier = Barrier(len(commands))
        authenticated = Client(HTTP_HOST='127.0.0.1')
        authenticated.cookies = copy.deepcopy(self.client.cookies)
        authenticated.force_login(self.user)
        original = process_operation
        def at_service(*args, **kwargs):
            barrier.wait(timeout=15)
            return original(*args, **kwargs)
        def request(command, mode):
            close_old_connections()
            try:
                browser = Client(enforce_csrf_checks=True, HTTP_HOST='127.0.0.1')
                browser.cookies = copy.deepcopy((self.client if mode == 'local' else authenticated).cookies)
                response = browser.post('/api/vendas/online/' if mode == 'local' else '/api/offline/observations/',
                    json.dumps({**command, 'payload_hash':command_hash(command)}), content_type='application/json',
                    HTTP_X_CSRFTOKEN=self.context['csrf'], **({'HTTP_X_LOCAL_SALES':'1'} if mode == 'local' else {}))
                return response.status_code, response.json()
            finally:
                close_old_connections()
        # Both HTTP requests must pass validation/authorization before either executes.
        with patch('offline.services.process_operation', side_effect=at_service), patch('offline.views.process_operation', side_effect=at_service):
            with ThreadPoolExecutor(max_workers=len(commands)) as pool:
                futures = [pool.submit(request, command, mode) for command, mode in zip(commands, modes)]
                return [future.result(timeout=45) for future in futures]

    def assert_one_cash_sale(self, stock):
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(EventoVenda.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        self.assertEqual(sum(MovimentoFinanceiro.objects.values_list('valor', flat=True)), Decimal('20'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal(stock))

    def test_two_http_requests_same_uuid_cash_have_one_set_of_effects(self):
        command = self.cash()
        results = self.races([command, copy.deepcopy(command)])
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(results[0][1]['receipt'], results[1][1]['receipt'])
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        self.assert_one_cash_sale('8')
        before = self.effects()
        for _ in range(3):
            self.assertEqual(self.send(command).json()['receipt'], results[0][1]['receipt'])
        self.assertEqual(self.effects(), before)

    def test_two_http_requests_same_uuid_different_content_keep_winner(self):
        changed = copy.deepcopy(self.command)
        changed['payload']['dados']['itens'][0]['quantidade'] = '3'
        results = self.races([self.command, changed])
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        winner = next(body['receipt'] for status, body in results if status == 200)
        self.assertEqual(OperacaoSincronizacao.objects.get().resultado, winner)
        before = self.effects()
        self.send(self.command); self.send(changed)
        self.assertEqual(self.effects(), before)

    def test_response_lost_after_commit_replay_and_lookup_do_not_repeat_effects(self):
        command = self.cash()
        with patch('offline.local_sales.sale_response', side_effect=ConnectionAbortedError('fixture: lost response after commit')):
            with self.assertRaises(ConnectionAbortedError):
                self.send(command)
        self.assert_one_cash_sale('8')
        receipt = OperacaoSincronizacao.objects.get().resultado
        before = self.effects()
        for _ in range(3):
            self.assertEqual(self.send(command).json()['receipt'], receipt)
            self.assertEqual(self.lookup(command).json()['receipt'], receipt)
        self.assertEqual(self.effects(), before)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)

    def last_stock(self, mixed):
        self.product.quantidade = Decimal('2'); self.product.save()
        first = self.cash()
        second = self.cash(self.old_command) if mixed else self.cash(self.build())
        results = self.races([first, second], ['local', 'offline' if mixed else 'local'])
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        self.assert_one_cash_sale('0')
        self.assertEqual(OperacaoSincronizacao.objects.count(), 2)
        self.assertEqual(OperacaoSincronizacao.objects.filter(status='confirmada').count(), 1)
        self.assertEqual(OperacaoSincronizacao.objects.filter(status='conflito').count(), 1)
        if mixed:
            self.assertEqual(OperacaoSincronizacao.objects.filter(origin='authenticated', actor=self.user, local_owner='').count(), 1)
            self.assertEqual(OperacaoSincronizacao.objects.filter(origin='local', actor__isnull=True).count(), 1)
        records = list(OperacaoSincronizacao.objects.order_by('pk').values())
        before = self.effects()
        self.send(first)
        if mixed:
            browser = Client(HTTP_HOST='127.0.0.1'); browser.force_login(self.user)
            browser.post('/api/offline/observations/', json.dumps({**second, 'payload_hash':command_hash(second)}), content_type='application/json')
        else:
            self.send(second)
        self.assertEqual(self.effects(), before)
        self.assertEqual(list(OperacaoSincronizacao.objects.order_by('pk').values()), records)

    def test_two_local_requests_compete_for_last_stock(self):
        self.last_stock(False)

    def test_local_and_old_offline_requests_compete_for_last_stock(self):
        self.last_stock(True)

    def test_failure_after_commercial_writes_rolls_back_receipt_stock_and_finance(self):
        from estoque.services.vendas import criar_ou_atualizar_venda
        command = self.cash()
        before = self.effects()
        def fail_after_effects(*args, **kwargs):
            criar_ou_atualizar_venda(*args, **kwargs)
            raise RuntimeError('fixture: fail after writes, before receipt')
        with patch('offline.local_sales.criar_ou_atualizar_venda', side_effect=fail_after_effects):
            with self.assertRaises(RuntimeError):
                self.send(command)
        self.assertEqual(self.effects(), before)
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        self.assertTrue(self.send(command).json()['sucesso'])
        self.assert_one_cash_sale('8')
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)

    def test_existing_authenticated_receipt_cannot_be_adopted_or_exposed_locally(self):
        process_operation(self.old_command, self.user)
        old = OperacaoSincronizacao.objects.values().get()
        before = self.effects()
        collision = copy.deepcopy(self.command)
        collision['operation_id'] = collision['aggregate_id'] = self.old_command['operation_id']
        self.assertEqual(self.send(collision).status_code, 409)
        self.assertEqual(self.lookup(self.old_command).status_code, 409)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(), old)
        self.assertEqual(self.effects(), before)
        self.assertTrue(self.send().json()['sucesso'])
        self.assertEqual(OperacaoSincronizacao.objects.filter(pk=old['id']).values().get(), old)
        local = OperacaoSincronizacao.objects.get(origin='local')
        self.assertIsNone(local.actor_id)
        self.assertEqual(local.declared_operator, self.operator.nome)
        browser = Client(HTTP_HOST='127.0.0.1'); browser.force_login(self.user)
        self.assertEqual(browser.get('/api/vendas/online/'+str(local.operation_id)+'/', {
            'type':'criar_venda', 'actor_id':str(self.user.pk), 'environment_id':local.environment_id,
            'device_id':str(local.device_id), 'hash':local.payload_hash}).status_code, 409)
