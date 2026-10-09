"""2.8E: release actual concluded assemblies only after official revision receipts."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, Produto
from .models import OperacaoSincronizacao, RevisaoVendaOffline
from .services import COMMAND_KEYS
from .browser_support import Chrome
from . import tests_sale_sync_browser as sync
from . import tests_sale_revisions_browser as revisions


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome unavailable')
class SaleReleaseBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync.SaleSyncBrowserTests.setUp
    open_sales = sync.SaleSyncBrowserTests.open_sales
    repository = sync.SaleSyncBrowserTests.repository
    select_customer = sync.SaleSyncBrowserTests.select_customer
    add_product = sync.SaleSyncBrowserTests.add_product
    prepare = sync.SaleSyncBrowserTests.prepare
    stable = sync.SaleSyncBrowserTests.stable
    start_manual = sync.SaleSyncBrowserTests.start_manual
    manual = sync.SaleSyncBrowserTests.manual
    reload = sync.SaleSyncBrowserTests.reload
    quantity = revisions.SaleRevisionBrowserTests.quantity
    conclude = revisions.SaleRevisionBrowserTests.conclude

    def revision_tab(self, chrome, identifier=None):
        tab = revisions.SaleRevisionBrowserTests.revision_tab(self,chrome,identifier)
        # The preceding real upload advanced the test clock in another tab.
        # Keep the same timeline; never change the application's stability policy.
        tab.evaluate('window.releaseCommunication=await repo.communication(scope);window.releaseWall=Date.now()-offset;offset=Math.max(offset,(releaseCommunication.last_success_at||releaseWall)-releaseWall+5001);await app.probe();true')
        return tab

    def original_conflict(self, chrome):
        tab = self.prepare(chrome, quantity='2')
        # The online editor refuses quantities above its current stock. Conclude
        # a valid local draft, then simulate stock consumed before its upload.
        Produto.objects.filter(pk=self.product.pk).update(quantidade=Decimal('0'))
        self.stable(tab); self.manual(tab)
        Produto.objects.filter(pk=self.product.pk).update(quantidade=Decimal('2.5'))
        self.original = tab.evaluate('await repo.get("operations",operation.operation_id)')
        self.assertEqual(self.original['status'], 'conflito')
        self.command = {k:self.original[k] for k in COMMAND_KEYS}
        self.server_original = OperacaoSincronizacao.objects.values().get(operation_id=self.original['operation_id'])
        self.history_original = tab.evaluate('await repo.get("history",operation.operation_id)')
        return tab

    def assert_original_preserved(self, tab):
        self.assertEqual(tab.evaluate('await repo.get("operations",'+json.dumps(self.original['operation_id'])+')'), self.original)
        self.assertEqual(tab.evaluate('await repo.get("history",'+json.dumps(self.original['operation_id'])+')'), self.history_original)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.original['operation_id']), self.server_original)

    def assert_official(self):
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(ContaReceber.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('1.5'))
        return Venda.objects.get().pk

    def test_no_revision_and_all_nonterminal_replacements_block_release(self):
        with TemporaryDirectory(prefix='release-blocked-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                normal = self.original_conflict(chrome)
                normal.evaluate('window.marker=(await drafts.loadDraft(repo,scope)).finalization;true')
                self.assertTrue(normal.evaluate('try{await drafts.releaseConfirmedDraft(repo,scope,marker.operation_id);false}catch(e){true}'))
                tab = self.revision_tab(chrome); self.quantity(tab,'1000'); child = self.conclude(tab)
                for status in ['pendente','enviando','resultado_desconhecido','erro','conflito']:
                    tab.evaluate('window.child='+json.dumps(child)+';child.status='+json.dumps(status)+';await repo.put("operations",child);window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")});true')
                    self.assertTrue(tab.evaluate('try{await drafts.releaseConfirmedDraft(repo,scope,original.operation_id);false}catch(e){true}'))
                    self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")})'))
                    normal.evaluate('await salesDraftUI.refreshCompletion();true')
                    self.assertTrue(normal.evaluate('!!salesDraftUI.finalization && btnGravarVenda.disabled'))
                self.assert_original_preserved(tab)
                self.assertEqual(Venda.objects.count(), 0)
            finally: chrome.stop()

    def test_confirmed_revision_releases_two_tabs_f5_and_new_assembly(self):
        with TemporaryDirectory(prefix='release-confirmed-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                normal = self.original_conflict(chrome)
                tab = self.revision_tab(chrome); self.quantity(tab,'1'); child = self.conclude(tab)
                normal.call('Page.close')
                self.stable(tab); self.manual(tab)
                sale_id = self.assert_official()
                tab.evaluate('window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")});true')
                first, second = self.open_sales(chrome), self.open_sales(chrome)
                for page in [first,second]:
                    page.wait('!salesDraftUI.finalization && !salesDraftUI.blocked && !btnGravarVenda.disabled')
                    page.wait('document.getElementById("sales-official-sale-link").getAttribute("href")==="/vendas/'+str(sale_id)+'/"')
                    self.reload(page)
                    page.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")})'))
                self.assert_original_preserved(tab)
                self.select_customer(first,'Segundo Cliente'); self.add_product(first,'Segundo Produto','1')
                self.repository(first)
                self.assertIsNotNone(first.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(RevisaoVendaOffline.objects.count(), 1)
                self.assertEqual(tab.evaluate('requests.filter(r=>r.method==="POST"&&r.url==="/api/offline/observations/").length'), 1)
            finally: chrome.stop()

    def test_chain_unknown_f5_reconciliation_reopen_and_history(self):
        with TemporaryDirectory(prefix='release-chain-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                normal = self.original_conflict(chrome)
                tab = self.revision_tab(chrome); self.quantity(tab,'1000'); child = self.conclude(tab)
                self.stable(tab); self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status'),'conflito')
                third = self.revision_tab(chrome,child['operation_id']); self.quantity(third,'1'); grandchild = self.conclude(third)
                third.evaluate('window.realFetch=fetch;window.posts=0;window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u)==="/api/offline/observations/"){posts++;throw TypeError("response lost after commit")};return r};true')
                self.stable(third); self.manual(third)
                sale_id = self.assert_official()
                self.assertEqual(third.evaluate('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status'),'resultado_desconhecido')
                self.reload(normal); self.repository(normal)
                normal.wait('!!salesDraftUI.finalization')
                self.assertTrue(normal.evaluate('try{await drafts.releaseConfirmedDraft(repo,scope,salesDraftUI.finalization.operation_id);false}catch(e){true}'))
                normal.call('Page.close')
                third.evaluate('window.fetch=realFetch;true'); self.stable(third); self.manual(third)
                self.assertEqual(third.evaluate('posts'),1)
                third.evaluate('window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")});true')
                self.assert_original_preserved(third)
                before = third.evaluate('before')
                chrome.stop(); chrome = Chrome(sync.CHROME,profile).start()
                restored = self.open_sales(chrome); self.repository(restored)
                restored.wait('!salesDraftUI.finalization && !salesDraftUI.blocked && !btnGravarVenda.disabled')
                restored.wait('document.getElementById("sales-official-sale-link").getAttribute("href")==="/vendas/'+str(sale_id)+'/"')
                self.assertEqual(restored.evaluate('JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")})'),before)
                self.assert_original_preserved(restored)
                self.assertEqual(RevisaoVendaOffline.objects.count(),2)
                self.assertEqual(OperacaoSincronizacao.objects.count(),3)
                self.assertNotEqual(child['operation_id'],grandchild['operation_id'])
                self.assert_official()
            finally: chrome.stop()

    def test_corrupt_receipt_hash_scope_or_link_cannot_release(self):
        with TemporaryDirectory(prefix='release-invalid-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                normal = self.original_conflict(chrome)
                tab = self.revision_tab(chrome); self.quantity(tab,'1'); child = self.conclude(tab)
                normal.call('Page.close')
                self.stable(tab); self.manual(tab); self.assert_official()
                tab.evaluate('window.revisions=await import("/offline/assets/2-8f/sales-revisions.js");await revisions.lookupOriginal(repo,original,scope);window.good=await repo.get("operations",salesRevisionUI.record.replacement_operation_id);window.observationKey=revisions.resolutionObservationKey(scope,original.operation_id);window.observed=await repo.get("metadata",observationKey);true')
                mutations = [
                    'bad.server_result.record_id=0',
                    'bad.server_result.hash="0".repeat(64)',
                    'bad.payload.itens[0].quantidade="999"',
                    'bad.actor_id="999999"',
                    'bad.environment_id="another-environment"',
                    'bad.payload.revisao.original_hash="0".repeat(64)',
                    'bad.payload.revisao.relacao="unrelated"',
                    'bad.status="resultado_desconhecido"',
                ]
                for mutation in mutations:
                    tab.evaluate('window.bad=structuredClone(good);'+mutation+';await repo.put("operations",bad);window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")});true')
                    self.assertTrue(tab.evaluate('try{await drafts.releaseConfirmedDraft(repo,scope,original.operation_id);false}catch(e){true}'),mutation)
                    self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")})'))
                    self.assertTrue(tab.evaluate('!!(await drafts.loadDraft(repo,scope)).finalization'))
                tab.evaluate('await repo.put("operations",good);await repo.put("metadata",observed);window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")});await drafts.releaseConfirmedDraft(repo,scope,original.operation_id);true')
                self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history")})'))
                self.assert_original_preserved(tab)
                self.assert_official()
            finally: chrome.stop()
