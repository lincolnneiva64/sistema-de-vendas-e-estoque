"""SC/BD cadastro confirmado pelo proprietario; apenas registros ficticios isolados."""
import copy
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase

from estoque.models import Produto, Venda, ItemVenda, MovimentoFinanceiro, ContaReceber
from estoque.views import _quantidade_estoque_para_unidade_base
from .models import OperacaoSincronizacao, RevisaoVendaOffline
from .services import command_hash
from .tests_sale_creation import fixtures
from . import tests_sale_creation as creation
from .tests_sale_revisions import replacement
from . import tests_sale_sync_browser as sync
from . import tests_sales_connectivity_browser as connectivity
from .browser_support import Chrome
from .commercial import build_commercial_snapshot


def configure_gelo(product):
    # Units/factor/stock supplied from the current catalogue by the owner.
    # Prices below are synthetic test values, not production prices.
    product.nome = 'Gelo Escama teste'
    product.unidade_compra = product.unidade_venda_1 = 'SC'
    product.unidade_venda_2 = 'BD'
    product.fator_conversao = Decimal('8')
    product.vende_fracionado = True
    product.quantidade = Decimal('2.875')
    product.preco_compra = Decimal('8')
    product.preco_compra_fracionado = Decimal('1')
    product.preco_vista = product.preco_prazo = Decimal('16')
    product.preco_vista_fracionado = Decimal('2')
    product.save()


class GeloUnitTests(TestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.command = fixtures()
        configure_gelo(self.product)
        self.client.force_login(self.user)

    def send(self, quantity, unit, *, online=False, command=None):
        command = copy.deepcopy(command or self.command)
        command['payload']['itens'][0].update(quantidade=quantity, unidade=unit, preco_unitario='20')
        url = '/api/vendas/online/' if online else '/api/offline/observations/'
        return self.client.post(url, json.dumps({**command, 'payload_hash': command_hash(command)}),
                                content_type='application/json')

    def test_online_offline_bucket_conversion_and_replay_are_identical(self):
        for online in (True, False):
            with self.subTest(online=online):
                self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                result = self.send('1', 'BD', online=online)
                self.assertEqual(result.status_code, 200, result.content)
                before = list(Produto.objects.values()), list(Venda.objects.values()), list(ItemVenda.objects.values())
                self.assertEqual(self.send('1', 'BD', online=online).json(), result.json())
                self.assertEqual(before, (list(Produto.objects.values()), list(Venda.objects.values()), list(ItemVenda.objects.values())))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('2.875') - Decimal('2') / self.product.fator_conversao)
        self.assertEqual(Venda.objects.count(), 2)
        for item in ItemVenda.objects.all():
            self.assertEqual(item.unidade, 'BD')
            self.assertEqual(item.quantidade, Decimal('1'))
            self.assertEqual(item.estoque_movimentado, Decimal('1') / self.product.fator_conversao)

    def test_incompatible_sack_fractions_remain_rejected_on_both_transports(self):
        for online in (True, False):
            for quantity in ('0.1', '0.3', '0.333'):
                with self.subTest(online=online, quantity=quantity):
                    self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                    result = self.send(quantity, 'SC', online=online)
                    self.assertEqual(result.status_code, 409, result.content)
                    self.assertIn('selecione BD', result.content.decode())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('2.875'))
        self.assertFalse(Venda.objects.exists())
        self.assertFalse(ItemVenda.objects.exists())

    def test_sack_fractions_matching_whole_buckets_are_valid_on_both_transports(self):
        for online in (True, False):
            for quantity in ('0.125', '0.25', '0.5', '2.5'):
                with self.subTest(online=online, quantity=quantity):
                    Produto.objects.filter(pk=self.product.pk).update(quantidade=Decimal('2.875'))
                    self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                    result = self.send(quantity, 'SC', online=online)
                    self.assertEqual(result.status_code, 200, result.content)
                    self.assertEqual(self.send(quantity, 'SC', online=online).json(), result.json())
                    self.product.refresh_from_db()
                    self.assertEqual(self.product.quantidade, Decimal('2.875') - Decimal(quantity))
        self.assertEqual(Venda.objects.count(), 8)

    def test_whole_sack_remains_valid(self):
        self.assertEqual(self.send('1', 'SC').status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('1.875'))

    def test_missing_or_invalid_conversion_never_infers_buckets(self):
        for fractional, factor in ((False, '8'), (True, '0')):
            with self.subTest(fractional=fractional, factor=factor):
                self.product.vende_fracionado = fractional
                self.product.fator_conversao = Decimal(factor)
                self.product.save()
                self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                self.assertEqual(self.send('1', 'BD').status_code, 409)
                self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                self.assertEqual(self.send('0.5', 'SC').status_code, 409)
        self.assertFalse(Venda.objects.exists())

    def test_conversion_uses_current_product_factor_not_a_constant(self):
        self.product.fator_conversao += Decimal('1')  # Deliberate synthetic catalogue change.
        quantity, unit = _quantidade_estoque_para_unidade_base(self.product, Decimal('1'), 'BD')
        self.assertEqual(quantity, (Decimal('1') / self.product.fator_conversao).quantize(Decimal('.001')))
        self.assertEqual(unit, 'SC')

    def test_snapshot_contains_current_secondary_unit_factor_and_stock(self):
        snapshot = build_commercial_snapshot(self.user, 'offline-isolated-tests', str(uuid4()))
        product = next(p for p in snapshot['produtos'] if p['id'] == str(self.product.pk))
        self.assertEqual((product['unidade_venda_1'], product['unidade_venda_2']), ('SC', 'BD'))
        self.assertTrue(product['vende_fracionado'])
        self.assertEqual(product['fator_conversao'], '8.00')
        self.assertEqual(product['estoque_referencia'], '2.875')
        self.assertEqual(Decimal(product['estoque_referencia']) * Decimal(product['fator_conversao']), Decimal('23'))

    def test_rejected_original_is_immutable_and_explicit_sack_revision_is_single(self):
        self.command['payload']['itens'][0].update(quantidade='2.5', unidade='SC', preco_unitario='20')
        # Persist the old version's business rejection; the fix may not rewrite it.
        with patch('estoque.views._quantidade_valida_para_produto_unidade', return_value=False):
            original = self.send('2.5', 'SC')
        self.assertEqual(original.status_code, 409)
        before = OperacaoSincronizacao.objects.values().get()
        corrected = replacement(self.command, quantity='2.5')
        result = self.send('2.5', 'SC', command=corrected)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(self.send('2.5', 'SC', command=corrected).json(), result.json())
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.command['operation_id']), before)
        self.assertEqual(RevisaoVendaOffline.objects.count(), 1)
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.get().unidade, 'SC')
        self.assertEqual(ItemVenda.objects.get().quantidade, Decimal('2.5'))
        self.assertEqual(self.send('2.5', 'SC').json(), original.json())

    def test_cash_2500_sacks_at_40_recovery_lost_response_and_competing_revision(self):
        # Reproduce the reported values with a random UUID and synthetic records.
        self.product.preco_vista = self.product.preco_prazo = Decimal('40')
        self.product.save()
        self.command['payload'].update(data_venda='2026-10-18', tipo_pagamento='A vista',
            origem_recebimento={'caixa': '100', 'banco': '0'})
        self.command['payload']['itens'][0].update(quantidade='2.5', unidade='SC', preco_unitario='40')

        def send(command, online=False):
            return self.client.post('/api/vendas/online/' if online else '/api/offline/observations/',
                json.dumps({**command, 'payload_hash': command_hash(command)}), content_type='application/json')

        with patch('estoque.views._quantidade_valida_para_produto_unidade', return_value=False):
            rejected = send(self.command)
        self.assertEqual(rejected.status_code, 409)
        original = OperacaoSincronizacao.objects.values().get()
        self.assertFalse(Venda.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('2.875'))
        corrected = replacement(self.command, quantity='2.5')
        result = send(corrected)
        self.assertEqual(result.status_code, 200, result.content)
        sale = Venda.objects.get()
        self.assertEqual(sale.data_venda.isoformat(), '2026-10-18')
        self.assertEqual(sale.total, Decimal('100'))
        self.assertEqual(ItemVenda.objects.get().estoque_movimentado, Decimal('2.5'))
        self.assertEqual(MovimentoFinanceiro.objects.get().valor, Decimal('100'))
        self.assertFalse(ContaReceber.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('0.375'))
        effects = creation.SaleCreationTests.effects(self)
        for _ in range(3):
            self.assertEqual(send(corrected).json(), result.json())
            # The online transport deliberately refuses conflict revisions.
            self.assertEqual(send(corrected, online=True).status_code, 400)
            self.assertEqual(send(self.command).json(), rejected.json())
        self.assertEqual(send(replacement(self.command, quantity='2.5')).status_code, 409)
        self.assertEqual(creation.SaleCreationTests.effects(self), effects)
        self.assertEqual(RevisaoVendaOffline.objects.count(), 1)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.command['operation_id']), original)


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome unavailable')
class GeloUnitBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync.SaleSyncBrowserTests.setUp
    open_sales = sync.SaleSyncBrowserTests.open_sales
    repository = sync.SaleSyncBrowserTests.repository
    select_customer = sync.SaleSyncBrowserTests.select_customer
    confirm_offline = connectivity.SalesConnectivityBrowserTests.confirm_offline
    fail_health = connectivity.SalesConnectivityBrowserTests.fail_health

    def test_same_sack_guard_and_bucket_selection_online_and_offline_preserve_pending(self):
        configure_gelo(self.product)
        with TemporaryDirectory(prefix='gelo-unit-isolated-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                tab.evaluate('operadorVenda.value="Operador Comercial";operadorVenda.dispatchEvent(new Event("change",{bubbles:true}));tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));produtoBusca.value="Gelo Escama teste";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('!!document.querySelector(".produto-sugestao-item")')
                tab.evaluate('document.querySelector(".produto-sugestao-item").click();quantidade.value="0.3";true')
                self.assertEqual(tab.evaluate('[...unidade.options].map(o=>o.value)'), ['SC', 'BD'])
                tab.evaluate('document.getElementById("btnAdicionarItemVenda").click();true')
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.assertTrue(tab.evaluate('document.body.innerText.includes("selecione BD")'), tab.evaluate('document.body.innerText.slice(-2000)'))
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                # Simulate the obsolete historical factor only in this disposable profile.
                # Even an old catalogue may not rewrite BD as a fraction of SC.
                tab.evaluate('const snapshot=(await repo.all("snapshots")).find(s=>s.tipo==="comercial_vendas");snapshot.produtos.find(p=>p.id==='+json.dumps(str(self.product.pk))+').fator_conversao="4.00";await repo.put("snapshots",snapshot);true')
                self.confirm_offline(tab)
                tab.evaluate('document.getElementById("btnAdicionarItemVenda").click();true')
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                tab.evaluate('unidade.value="BD";unidade.dispatchEvent(new Event("change"));quantidade.value="1";true')
                tab.evaluate('document.getElementById("btnAdicionarItemVenda").click();await salesDraftUI.flush();await salesDraftUI.finalizeOffline();true')
                tab.wait('!!salesDraftUI.finalization')
                original = tab.evaluate('JSON.stringify(await repo.all("operations"))')
                payload = json.loads(original)[0]['payload']['itens'][0]
                self.assertEqual((payload['quantidade'], payload['unidade']), ('1', 'BD'))
                tab.evaluate('window.fetch=realFetch;offset+=5001;await app.probe();await salesOffline.deactivate();true')
                self.assertEqual(tab.evaluate('JSON.stringify(await repo.all("operations"))'), original)
                self.assertEqual(tab.evaluate('sent'), [])
                self.assertFalse(Venda.objects.exists())
                self.assertFalse(OperacaoSincronizacao.objects.exists())
                # Explicit test-client submission in the disposable DB, never an automatic browser send.
                command = tab.evaluate('core.commandOf((await repo.all("operations"))[0])')
                response = self.client.post('/api/offline/observations/', json.dumps({**command, 'payload_hash':command_hash(command)}), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade, Decimal('2.750'))
                self.assertEqual(ItemVenda.objects.get().unidade, 'BD')
                self.assertEqual(ItemVenda.objects.get().estoque_movimentado, Decimal('0.125'))
            finally:
                chrome.stop()

    def test_legitimate_fraction_in_sacks_is_kept_as_sacks_online_then_offline(self):
        configure_gelo(self.product)
        with TemporaryDirectory(prefix='gelo-sack-isolated-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                sync.SaleSyncBrowserTests.add_product(self, tab, 'Gelo Escama teste', '2.5')
                self.assertEqual(tab.evaluate('salesDraftBridge.capture().itens[0].unidade'), 'SC')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.confirm_offline(tab)
                tab.evaluate('await salesDraftUI.flush();await salesDraftUI.finalizeOffline();true')
                tab.wait('!!salesDraftUI.finalization')
                command = tab.evaluate('core.commandOf((await repo.all("operations"))[0])')
                self.assertEqual((command['payload']['itens'][0]['quantidade'], command['payload']['itens'][0]['unidade']), ('2.5', 'SC'))
                self.assertEqual(tab.evaluate('sent'), [])
                response = self.client.post('/api/offline/observations/', json.dumps({**command, 'payload_hash':command_hash(command)}), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade, Decimal('0.375'))
                self.assertEqual(ItemVenda.objects.get().unidade, 'SC')
            finally:
                chrome.stop()
