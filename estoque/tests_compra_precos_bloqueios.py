"""Observe real waits on the disposable PostgreSQL, never on production."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from time import perf_counter, sleep
from unittest.mock import patch
import json

from django.db import connection, close_old_connections, transaction
from django.test import Client, RequestFactory, TransactionTestCase
from django.urls import reverse

from estoque import views
from estoque.models import (Compra, Produto, ItemCompra, ContaPagar, MovimentoFinanceiro,
    ControlePrecosVinculados, MembroGrupoProduto, AlteracaoPrecoVinculado,
    ContaFinanceira, CartaoCredito, LancamentoCartao)
from estoque.services import precos_compra
from estoque import tests_compra_precos_desempenho as desempenho
from inventario.models import MovimentoObservado


class CompraPrecosBloqueiosTests(TransactionTestCase):
    setUp = desempenho.CompraPrecosDesempenhoTests.setUp
    fixture = desempenho.CompraPrecosDesempenhoTests.fixture

    def assert_rollback(self, compra, products):
        compra.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertFalse(compra.estoque_entrada_realizada)
        self.assertEqual(compra.alteracoes_precos_vinculados, [])
        self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in products], quantidade=10,
            preco_compra=5, preco_vista=100).count(), len(products))
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(AlteracaoPrecoVinculado.objects.exists())
        self.assertFalse(compra.itens.exclude(alteracoes_precos={}).exists())

    def test_systemexit_depois_do_evento_vinculado_reverte_tudo(self):
        compra, products, payload = self.fixture(15, 'linked')
        request = RequestFactory().post('/', payload)
        request.user = self.user
        prices = views._produtos_preco_venda_atualizar_post(request)
        original = precos_compra._aplicar_precos_compra_bloqueada
        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if AlteracaoPrecoVinculado.objects.exists():
                raise SystemExit(1)
            return result
        with patch.object(precos_compra, '_aplicar_precos_compra_bloqueada', interrupt):
            with self.assertRaises(SystemExit):
                views._finalizar_compra_com_financeiro(compra,
                    atualizar_custo_produto_ids={p.pk for p in products}, atualizar_preco_venda_produtos=prices)
        self.assert_rollback(compra, products)

    def test_lote_nao_reutiliza_versao_antiga_depois_de_alterar_grupo(self):
        compra, products, payload = self.fixture(15, 'linked')
        itens = list(compra.itens.order_by('pk'))
        options = dict(operador=self.user, versao_esperada=0,
            selecionados={p.pk for p in products[:5]}, confirmar=True,
            assinatura_esperada=payload[f'assinatura_precos_produto_{products[0].pk}'])
        comandos = [dict(item=itens[0], atualizacoes={'preco_vista': Decimal('120')}, **options),
                    dict(item=itens[1], atualizacoes={'preco_prazo': Decimal('130')}, **options)]
        with self.assertRaisesMessage(ValueError, 'desatualizada'):
            precos_compra.aplicar_precos_compra_em_lote(compra, comandos)
        self.assert_rollback(compra, products)

    def test_lote_repetido_preserva_snapshot_anterior_e_autoria(self):
        compra, products, _ = self.fixture(15, 'sale')
        item = compra.itens.first()
        precos_compra.aplicar_precos_compra_em_lote(compra, [
            dict(item=item, atualizacoes={'preco_vista': Decimal('120.00')}),
            dict(item=item, atualizacoes={'preco_vista': Decimal('130.00')}),
        ])
        item.refresh_from_db()
        self.assertEqual(item.alteracoes_precos['preco_vista']['anterior'], '100.00')
        self.assertEqual(item.alteracoes_precos['preco_vista']['novo'], '130.00')
        p = Produto.objects.get(pk=products[0].pk)
        self.assertEqual(p.autoria_precos['preco_vista'], item.alteracoes_precos['preco_vista']['alteracao_id'])

    def test_15_itens_precos_e_pagamentos_avista_cartao(self):
        for payment in ('avista', 'cartao_credito'):
            with self.subTest(payment=payment):
                compra, products, payload = self.fixture(15, 'linked')
                payload['tipo_pagamento'] = payment
                if payment == 'avista':
                    ContaFinanceira.objects.create(nome='Caixa sintetico', tipo='caixa')
                    payload['origem_caixa'] = '2153,11'
                else:
                    card = CartaoCredito.objects.create(nome='Cartao sintetico', titular='Teste')
                    payload['cartao_id'] = str(card.pk)
                response = self.client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), payload)
                self.assertEqual(response.status_code, 302)
                compra.refresh_from_db()
                self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
                self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in products], quantidade=11,
                    preco_compra=10, preco_vista=120).count(), 15)
                self.assertEqual(len(compra.alteracoes_precos_vinculados), 3)
                self.assertFalse(ContaPagar.objects.filter(compra=compra).exists())
                if payment == 'avista':
                    self.assertEqual(MovimentoFinanceiro.objects.get(compra=compra).valor, Decimal('2153.11'))
                    self.assertFalse(LancamentoCartao.objects.filter(compra=compra).exists())
                else:
                    self.assertEqual(LancamentoCartao.objects.get(compra=compra).valor, Decimal('2153.11'))
                    self.assertFalse(MovimentoFinanceiro.objects.filter(compra=compra).exists())

    def test_falha_depois_do_financeiro_reverte_precos_vinculados_e_estoque(self):
        compra, products, payload = self.fixture(15, 'linked')
        original = views._criar_contas_pagar_compra
        def fail(purchase):
            original(purchase)
            raise RuntimeError('Falha financeira sintetica apos gravacao')
        with patch.object(views, '_criar_contas_pagar_compra', fail), self.assertLogs('estoque.views', level='ERROR'):
            response = self.client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), payload)
        self.assertEqual(response.status_code, 302)
        self.assert_rollback(compra, products)

    def test_dois_posts_15_itens_precos_vinculados_nao_duplicam(self):
        compra, products, payload = self.fixture(15, 'linked')
        ready = Event()
        def finalize():
            close_old_connections()
            try:
                client = Client()
                client.cookies = self.client.cookies.copy()
                ready.wait(timeout=5)
                return client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), payload).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(finalize) for _ in range(2)]
            ready.set()
            self.assertEqual([future.result(timeout=15) for future in futures], [302, 302])
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(MovimentoObservado.objects.count(), 15)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(), 3)
        self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in products], quantidade=11,
            preco_compra=10, preco_vista=120).count(), 15)

    def observe_wait(self, kind, relationship_only=False):
        compra, products, payload = self.fixture(15, 'linked')
        ControlePrecosVinculados.objects.get_or_create(pk=1)
        locked, release, connected = Event(), Event(), Event()
        pids, waits = {}, {}
        def holder():
            close_old_connections()
            try:
                with transaction.atomic():
                    pids['holder'] = connection.connection.get_backend_pid()
                    if kind == 'catalog':
                        ControlePrecosVinculados.objects.select_for_update().get(pk=1)
                    elif kind == 'product':
                        Produto.objects.select_for_update().get(pk=products[-1].pk)
                    elif kind == 'member':
                        MembroGrupoProduto.objects.select_for_update().get(produto=products[0])
                    elif kind == 'ddl':
                        with connection.cursor() as cursor:
                            cursor.execute('LOCK TABLE estoque_membrogrupoproduto IN ACCESS EXCLUSIVE MODE')
                    locked.set()
                    release.wait(timeout=10)
            finally:
                close_old_connections()
        def worker():
            close_old_connections()
            try:
                connection.ensure_connection()
                pids['worker'] = connection.connection.get_backend_pid()
                connected.set()
                start = perf_counter()
                if relationship_only:
                    produto = Produto.objects.get(pk=products[0].pk)
                    self.assertIsNotNone(getattr(produto, 'vinculo_grupo', None))
                else:
                    client = Client()
                    client.cookies = self.client.cookies.copy()
                    response = client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), payload)
                    self.assertEqual(response.status_code, 302)
                return perf_counter() - start
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            holding = pool.submit(holder)
            try:
                self.assertTrue(locked.wait(timeout=5))
                pending = pool.submit(worker)
                self.assertTrue(connected.wait(timeout=5))
                if kind == 'member':
                    elapsed = pending.result(timeout=3)
                    self.assertFalse(release.is_set())  # Plain SELECT completes while row lock remains held.
                else:
                    deadline = perf_counter() + 5
                    while perf_counter() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type, wait_event, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=%s', [pids['worker']])
                            row = cursor.fetchone()
                        if row and row[0] == 'Lock' and pids['holder'] in row[2]:
                            waits.update(event_type=row[0], event=row[1], blockers=row[2])
                            break
                        sleep(0.01)
                    self.assertEqual(waits.get('event_type'), 'Lock')
                    self.assertFalse(pending.done())
                    sleep(0.3)
            finally:
                release.set()
            holding.result(timeout=10)
            elapsed = pending.result(timeout=10)
        print('PRECO_LOCK_METRIC ' + json.dumps(dict(kind=kind, seconds=round(elapsed, 4), **waits)), flush=True)
        if not relationship_only:
            compra.refresh_from_db()
            self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
            self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
            self.assertEqual(MovimentoObservado.objects.count(), 15)
            self.client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), payload)
            self.assertEqual(MovimentoObservado.objects.count(), 15)

    def test_catalogo_global_espera_com_bloqueador_identificado(self):
        self.observe_wait('catalog')

    def test_produto_espera_com_bloqueador_identificado(self):
        self.observe_wait('product')

    def test_select_simples_do_relacionamento_nao_espera_row_lock(self):
        self.observe_wait('member', relationship_only=True)

    def test_select_simples_do_relacionamento_pode_esperar_ddl(self):
        self.observe_wait('ddl', relationship_only=True)
