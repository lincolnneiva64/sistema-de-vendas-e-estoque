"""Synthetic cash sale: existing conflict revision UI, no production operation."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone

from estoque.models import Venda, ItemVenda, MovimentoFinanceiro, ContaReceber
from .browser_support import Chrome
from .models import OperacaoSincronizacao, RevisaoVendaOffline
from .services import command_hash, process_operation
from .tests_gelo_units import configure_gelo
from . import tests_sale_creation as creation
from . import tests_sale_revisions_browser as revision_tests
from . import tests_sale_sync_browser as sync


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome unavailable')
class GeloRecoveryBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync.SaleSyncBrowserTests.setUp
    open_sales = sync.SaleSyncBrowserTests.open_sales
    repository = sync.SaleSyncBrowserTests.repository
    select_customer = sync.SaleSyncBrowserTests.select_customer
    stable = sync.SaleSyncBrowserTests.stable
    start_manual = sync.SaleSyncBrowserTests.start_manual
    manual = sync.SaleSyncBrowserTests.manual
    revision_tab = revision_tests.SaleRevisionBrowserTests.revision_tab
    conclude = revision_tests.SaleRevisionBrowserTests.conclude
    unchanged = revision_tests.SaleRevisionBrowserTests.unchanged

    def test_cash_original_preserved_revision_lost_response_and_single_financial(self):
        configure_gelo(self.product)
        self.product.preco_vista = self.product.preco_prazo = Decimal('40')
        self.product.save()
        with TemporaryDirectory(prefix='gelo-revision-browser-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                normal = self.open_sales(chrome)
                self.select_customer(normal, 'Cliente Comercial')
                sync.SaleSyncBrowserTests.add_product(self, normal, 'Gelo Escama teste', '2.5')
                normal.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.normal = normal.evaluate('(await drafts.loadDraft(repo,scope)).draft')
                self.scope = normal.evaluate('scope')
                identifier = str(uuid4())
                self.command = {
                    'operation_id': identifier, 'aggregate_id': identifier,
                    **{k: self.scope[k] for k in ('actor_id', 'device_id', 'environment_id')},
                    'type': 'criar_venda', 'schema_version': 1, 'sequence': 1,
                    'created_at': timezone.now().isoformat(),
                    'payload': {'schema_version': 1, 'cliente_id': str(self.customer.pk),
                        'data_venda': '2026-10-18', 'data_vencimento': self.normal['data_vencimento'],
                        'operador': self.operator.nome, 'tipo_pagamento': 'A vista',
                        'origem_recebimento': {'caixa': '100', 'banco': '0'},
                        'itens': [{'produto_id': str(self.product.pk), 'quantidade': '2.5',
                            'unidade': 'SC', 'preco_unitario': '40'}]},
                }
                with patch('estoque.views._quantidade_valida_para_produto_unidade', return_value=False):
                    receipt, status = process_operation(self.command, self.user)
                self.assertEqual(status, 409)
                self.before_server = OperacaoSincronizacao.objects.values().get(operation_id=identifier)
                self.original = {**self.command, 'payload_hash': command_hash(self.command),
                    'status': 'conflito', 'attempts': 1, 'last_error': receipt['erro'], 'server_result': receipt}
                self.original_history = {**self.original, 'synchronized_at': self.command['created_at']}
                normal.evaluate('await repo.put("operations",'+json.dumps(self.original)+');'
                    'await repo.put("history",'+json.dumps(self.original_history)+');'
                    'await repo.put("metadata",{key:"device",id:scope.device_id,sequence:1});true')
                tab = self.revision_tab(chrome)
                self.assertEqual(tab.evaluate('salesRevisionUI.record.payload'), self.command['payload'])
                self.assertEqual(tab.evaluate('requests.filter(r=>r.method!=="GET").length'), 0)
                child = self.conclude(tab)
                self.assertEqual(child['payload']['itens'], self.command['payload']['itens'])
                self.assertEqual(child['payload']['origem_recebimento'], self.command['payload']['origem_recebimento'])
                self.assertEqual(Venda.objects.count(), 0)
                self.unchanged(tab)
                tab.evaluate('window.transport=fetch;window.posts=0;window.fetch=async(u,o)=>{const response=await transport(u,o);if(String(u)==="/api/offline/observations/"){posts++;throw TypeError("lost response")};return response};true')
                self.stable(tab)
                self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status'), 'resultado_desconhecido')
                self.assertEqual(Venda.objects.get().total, Decimal('100'))
                self.assertEqual(ItemVenda.objects.get().quantidade, Decimal('2.5'))
                self.assertEqual(ItemVenda.objects.get().unidade, 'SC')
                self.assertEqual(MovimentoFinanceiro.objects.get().valor, Decimal('100'))
                self.assertFalse(ContaReceber.objects.exists())
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade, Decimal('0.375'))
                effects = creation.SaleCreationTests.effects(self)
                tab.evaluate('window.fetch=transport;true')
                self.stable(tab)
                self.manual(tab)
                tab.wait('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status==="confirmada"')
                self.assertEqual(tab.evaluate('posts'), 1)
                self.assertEqual(RevisaoVendaOffline.objects.count(), 1)
                self.assertEqual(creation.SaleCreationTests.effects(self), effects)
                self.unchanged(tab)
            finally:
                chrome.stop()

    def test_online_sale_button_accepts_2500_sacks(self):
        configure_gelo(self.product)
        self.product.preco_vista = self.product.preco_prazo = Decimal('40')
        self.product.save()
        with TemporaryDirectory(prefix='gelo-online-browser-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                sync.SaleSyncBrowserTests.add_product(self, tab, 'Gelo Escama teste', '2.5')
                tab.evaluate('btnGravarVenda.click();true')
                tab.wait('!salesDraftUI.finalization && !document.getElementById("sales-official-sale-link").hidden')
                self.assertEqual(Venda.objects.get().total, Decimal('100'))
                self.assertEqual(ItemVenda.objects.get().unidade, 'SC')
                self.assertEqual(ContaReceber.objects.get().valor_em_aberto, Decimal('100'))
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade, Decimal('0.375'))
            finally:
                chrome.stop()
