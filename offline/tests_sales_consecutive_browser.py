"""2.8F: independent offline assemblies, persistent queue and real server effects."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, Produto
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sale_sync_browser as sync
from . import tests_online_sales_browser as online


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome unavailable')
class ConsecutiveSalesBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync.SaleSyncBrowserTests.setUp
    open_sales = sync.SaleSyncBrowserTests.open_sales
    repository = sync.SaleSyncBrowserTests.repository
    select_customer = sync.SaleSyncBrowserTests.select_customer
    add_product = sync.SaleSyncBrowserTests.add_product
    prepare = sync.SaleSyncBrowserTests.prepare
    reload = sync.SaleSyncBrowserTests.reload
    stable = sync.SaleSyncBrowserTests.stable
    manual = sync.SaleSyncBrowserTests.manual
    start_manual = sync.SaleSyncBrowserTests.start_manual

    def disconnect(self, tab):
        tab.evaluate('window.app ??= await import([...document.scripts].find(s=>s.src.includes("/app.js")).src);await app.initialized;window.realFetch ??= fetch;window.sent ??= [];true')
        tab.evaluate('window.comm=await repo.communication(scope);window.nowBase=Date.now()-offset;offset=Math.max(offset,...[comm.last_success_at,comm.failed_at,comm.confirmed_at,comm.suspect_at].filter(Number.isFinite).map(t=>t-nowBase+5001));window.fetch=(u,o)=>String(u)==="/api/offline/health/"?Promise.reject(TypeError("offline")):realFetch(u,o);offset+=5001;await app.probe();offset+=5001;await app.probe();offset+=5001;await app.probe();await salesOffline.ready;true')
        tab.wait('salesOffline.active')

    def next_sale(self, tab):
        tab.wait('!document.getElementById("sales-new-offline-sale").hidden && !document.getElementById("sales-new-offline-sale").disabled')
        tab.evaluate('document.getElementById("sales-new-offline-sale").click();true')
        tab.wait('!salesDraftUI.finalization && !salesDraftUI.blocked && !clienteBusca.disabled && !btnGravarVenda.disabled')
        self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),0)

    def conclude(self, tab, product='Segundo Produto', quantity='1', cash=False):
        self.select_customer(tab,'Cliente Comercial')
        self.add_product(tab,product,quantity)
        payment = 'À vista' if cash else 'A prazo'
        tab.evaluate('tipoVenda.value='+json.dumps(payment)+';tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();window.beforeDraft=(await drafts.loadDraft(repo,scope)).draft;true')
        origin = '{caixa:(Number(beforeDraft.total_referencia)/2).toFixed(2),banco:(Number(beforeDraft.total_referencia)/2).toFixed(2)}' if cash else 'undefined'
        tab.evaluate('await salesDraftUI.finalizeOffline('+origin+');true')
        tab.wait('!!salesDraftUI.finalization')
        return tab.evaluate('await repo.get("operations",salesDraftUI.finalization.operation_id)')

    def reconnect(self, tab):
        tab.evaluate('window.comm=await repo.communication(scope);window.nowBase=Date.now()-offset;offset=Math.max(offset,...[comm.last_success_at,comm.failed_at,comm.confirmed_at,comm.suspect_at].filter(Number.isFinite).map(t=>t-nowBase+5001));window.fetch=async(u,o)=>{if(String(u)==="/api/offline/observations/")sent.push(JSON.parse(o.body));return realFetch(u,o)};offset+=5001;await app.probe();true')

    def test_abc_f5_real_offline_reopen_manual_and_unique_cash_effects(self):
        with TemporaryDirectory(prefix='consecutive-abc-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,payment='À vista',quantity='2')
                operations = [tab.evaluate('operation')]
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                self.disconnect(tab)
                tab.wait('!!navigator.serviceWorker.controller')
                worker = chrome.worker(); worker.call('Network.enable')
                conditions = dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0)
                worker.call('Network.emulateNetworkConditions',conditions)
                tab.call('Network.emulateNetworkConditions',conditions)
                self.next_sale(tab)
                operations.append(self.conclude(tab,quantity='1',cash=True))
                self.reload(tab); self.repository(tab)
                self.assertTrue(tab.evaluate('salesOffline.shell'))
                self.next_sale(tab)
                operations.append(self.conclude(tab,quantity='2',cash=True))
                self.assertEqual(len({op['operation_id'] for op in operations}),3)
                self.assertEqual([op['sequence'] for op in operations], [1,2,3])
                self.assertTrue(tab.evaluate('document.getElementById("sales-pending-count").textContent.startsWith("3 ")'))
                self.assertEqual(tab.evaluate('await repo.all("operations")'),sorted(operations,key=lambda op:op['operation_id']))
                self.assertEqual(Venda.objects.count(),0)
                self.assertEqual(MovimentoFinanceiro.objects.count(),0)
                snapshots = tab.evaluate('await repo.all("snapshots")')
            finally: chrome.stop()
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.open_sales(chrome)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),3)
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).finalization'))
                self.reconnect(tab)
                self.assertEqual(tab.evaluate('sent.length'),0)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                self.stable(tab,29)
                self.assertEqual(tab.evaluate('sent.length'),0)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                self.stable(tab,1); self.manual(tab)
                updated = tab.evaluate('await repo.all("operations")')
                self.assertTrue(all(op['status']=='confirmada' for op in updated))
                self.assertEqual([cmd['operation_id'] for cmd in tab.evaluate('sent')],[op['operation_id'] for op in operations])
                for original in operations:
                    current = next(op for op in updated if op['operation_id']==original['operation_id'])
                    self.assertEqual(current['payload'],original['payload'])
                    self.assertEqual(current['payload_hash'],original['payload_hash'])
                    self.assertEqual(current['attempts'],1)
                self.assertEqual(Venda.objects.count(),3)
                self.assertEqual(ItemVenda.objects.count(),3)
                self.assertEqual(ContaReceber.objects.count(),0)
                self.assertEqual(MovimentoFinanceiro.objects.count(),6)
                self.assertEqual(OperacaoSincronizacao.objects.count(),3)
                self.product.refresh_from_db(); self.second_product.refresh_from_db()
                self.assertEqual(self.product.quantidade,Decimal('0.5'))
                self.assertEqual(self.second_product.quantidade,Decimal('97'))
                self.assertEqual(tab.evaluate('(await repo.all("history")).length'),3)
                self.assertEqual(tab.evaluate('await repo.all("snapshots")'),snapshots)
                before = list(MovimentoFinanceiro.objects.order_by('pk').values())
                awaitable = 'await app.probe();await salesDraftUI.refreshCompletion();true'
                tab.evaluate(awaitable)
                self.assertEqual(tab.evaluate('sent.length'),3)
                self.assertEqual(list(MovimentoFinanceiro.objects.order_by('pk').values()),before)
            finally: chrome.stop()

    def test_conflict_b_preserves_a_c_and_full_history(self):
        with TemporaryDirectory(prefix='consecutive-conflict-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); first = tab.evaluate('operation')
                self.disconnect(tab); self.next_sale(tab)
                second = self.conclude(tab,quantity='100')
                self.next_sale(tab); third = self.conclude(tab,product='Produto Fracionado',quantity='0.5')
                Produto.objects.filter(pk=self.second_product.pk).update(quantidade=0)
                self.reconnect(tab); self.stable(tab); self.manual(tab)
                for operation,status in [(first,'confirmada'),(second,'conflito'),(third,'confirmada')]:
                    current = tab.evaluate('await repo.get("operations",'+json.dumps(operation['operation_id'])+')')
                    self.assertEqual(current['status'],status)
                    self.assertEqual(current['payload'],operation['payload'])
                    self.assertEqual(current['payload_hash'],operation['payload_hash'])
                    self.assertEqual(current['attempts'],1)
                self.assertEqual(Venda.objects.count(),2)
                self.assertEqual(ItemVenda.objects.count(),2)
                self.assertEqual(ContaReceber.objects.count(),2)
                self.assertEqual(MovimentoFinanceiro.objects.count(),0)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade,Decimal('1.5'))
                self.assertEqual(tab.evaluate('(await repo.all("history")).length'),3)
                self.assertEqual(OperacaoSincronizacao.objects.count(),3)
                self.assertEqual(tab.evaluate('sent.length'),3)
            finally: chrome.stop()

    def test_two_tabs_cas_no_double_conclusion_or_old_assembly_resurrection(self):
        with TemporaryDirectory(prefix='consecutive-tabs-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.disconnect(tab)
                other = self.open_sales(chrome); self.disconnect(other)
                marker = tab.evaluate('salesDraftUI.finalization')
                for page in [tab,other]:
                    page.evaluate('window.marker='+json.dumps(marker)+';true')
                # Two genuinely concurrent IDB attempts: only one marker release.
                tab.evaluate('window.releasePromise=drafts.startNextOfflineDraft(repo,scope,marker.operation_id,marker.revision).then(()=>"released",()=>"blocked");true')
                other.evaluate('window.releasePromise=drafts.startNextOfflineDraft(repo,scope,marker.operation_id,marker.revision).then(()=>"released",()=>"blocked");true')
                self.assertEqual(sorted([tab.evaluate('await releasePromise'),other.evaluate('await releasePromise')]),['blocked','released'])
                self.reload(tab); self.repository(tab); self.disconnect(tab)
                self.select_customer(tab,'Cliente Comercial'); self.add_product(tab,'Segundo Produto','1')
                tab.evaluate('window.savedDraft=(await drafts.loadDraft(repo,scope)).draft;true')
                saved = tab.evaluate('savedDraft')
                self.assertNotEqual(saved['draft_id'],marker['operation_id'])
                other.evaluate('window.savedDraft='+json.dumps(saved)+';true')
                # A stale tab cannot restore the old finalized draft or release again.
                self.assertTrue(other.evaluate('try{await drafts.saveDraft(repo,scope,savedDraft,marker.revision);false}catch(e){true}'))
                for page in [tab,other]:
                    page.evaluate('window.finishPromise=drafts.finalizeDraftOffline(repo,scope,{revision:savedDraft.revision,draft_id:savedDraft.draft_id}).then(r=>r.finalization.operation_id);true')
                self.assertEqual(tab.evaluate('await finishPromise'),other.evaluate('await finishPromise'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),2)
                self.assertEqual(Venda.objects.count(),0)
            finally: chrome.stop()

    def test_abort_local_release_and_finalization_preserve_all_data(self):
        with TemporaryDirectory(prefix='consecutive-abort-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.disconnect(tab)
                tab.evaluate('window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),meta:await repo.all("metadata")});window.baseTransaction=core.Repository.prototype.transaction;core.Repository.prototype.transaction=function(stores,write,work){return baseTransaction.call(this,stores,write,(tx,done)=>{work(tx,done);if(write&&stores.includes("operations")&&stores.includes("metadata"))tx.objectStore("metadata").get("device").onsuccess=()=>tx.abort()})};true')
                tab.evaluate('await salesDraftUI.startNextSale();true')
                self.assertTrue(tab.evaluate('!!salesDraftUI.finalization && before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),meta:await repo.all("metadata")})'))
                tab.evaluate('core.Repository.prototype.transaction=baseTransaction;await salesDraftUI.refreshCompletion();true')
                self.next_sale(tab)
                self.select_customer(tab,'Cliente Comercial'); self.add_product(tab,'Segundo Produto','1')
                tab.evaluate('window.savedDraft=(await drafts.loadDraft(repo,scope)).draft;window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),meta:await repo.all("metadata")});core.Repository.prototype.transaction=function(stores,write,work){return baseTransaction.call(this,stores,write,(tx,done)=>{work(tx,done);if(write&&stores.includes("operations")&&stores.includes("metadata"))tx.objectStore("metadata").get("device").onsuccess=()=>tx.abort()})};true')
                self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,scope,{revision:savedDraft.revision,draft_id:savedDraft.draft_id});false}catch(e){true}'))
                self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),meta:await repo.all("metadata")})'))
                tab.evaluate('core.Repository.prototype.transaction=baseTransaction;await salesDraftUI.finalizeOffline();true')
                self.assertEqual(tab.evaluate('salesDraftUI.finalization.operation_id'),tab.evaluate('savedDraft.draft_id'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),2)
                self.assertEqual(Venda.objects.count(),0)
            finally: chrome.stop()

    def test_new_sale_button_updates_two_tabs_and_preserves_newer_draft(self):
        with TemporaryDirectory(prefix='consecutive-ui-tabs-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.disconnect(tab)
                other = self.open_sales(chrome); self.disconnect(other)
                original = tab.evaluate('await repo.all("operations")')
                self.next_sale(tab)
                other.wait('!salesDraftUI.finalization && !salesDraftUI.blocked && !clienteBusca.disabled')
                self.assertEqual(other.evaluate('await repo.all("operations")'),original)
                self.select_customer(tab,'Cliente Comercial'); self.add_product(tab,'Segundo Produto','1')
                other.wait('salesDraftUI.blocked')
                # The stale tab can neither release again nor save over the new draft.
                saved = tab.evaluate('(await drafts.loadDraft(repo,scope)).draft')
                self.assertTrue(other.evaluate('try{await drafts.startNextOfflineDraft(repo,scope,'+json.dumps(original[0]['operation_id'])+',salesDraftUI.revision);false}catch(e){true}'))
                other.evaluate('await salesDraftUI.startNextSale();await salesDraftUI.flush();true')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'),saved)
                self.assertEqual(tab.evaluate('await repo.all("operations")'),original)
                self.assertEqual(Venda.objects.count(),0)
            finally: chrome.stop()

    def test_detached_unknown_blocks_new_assembly_without_discarding_it(self):
        with TemporaryDirectory(prefix='consecutive-unknown-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.disconnect(tab)
                original = tab.evaluate('operation')
                self.next_sale(tab)
                self.select_customer(tab,'Cliente Comercial'); self.add_product(tab,'Segundo Produto','1')
                saved = tab.evaluate('(await drafts.loadDraft(repo,scope)).draft')
                self.reconnect(tab); self.stable(tab)
                tab.evaluate('window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u)==="/api/offline/observations/"){sent.push(JSON.parse(o.body));throw TypeError("Lost after offline commit")};return r};true')
                self.manual(tab)
                self.assertEqual(Venda.objects.count(),1)
                self.assertEqual(tab.evaluate('sent.length'),1)
                self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'),'resultado_desconhecido')
                tab.evaluate('window.savedDraft=(await drafts.loadDraft(repo,scope)).draft;await salesDraftUI.refreshCompletion();window.before=JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),draft:(await drafts.loadDraft(repo,scope)).draft});true')
                self.assertTrue(tab.evaluate('clienteBusca.disabled && btnGravarVenda.disabled'))
                self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,scope,{revision:savedDraft.revision,draft_id:savedDraft.draft_id});false}catch(e){true}'))
                self.assertTrue(tab.evaluate('try{await drafts.saveDraft(repo,scope,savedDraft,savedDraft.revision);false}catch(e){true}'))
                self.assertTrue(tab.evaluate('try{await drafts.discardDraft(repo,scope,savedDraft.revision);false}catch(e){true}'))
                self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),history:await repo.all("history"),draft:(await drafts.loadDraft(repo,scope)).draft})'))
                self.reload(tab); self.repository(tab)
                self.assertTrue(tab.evaluate('clienteBusca.disabled && btnGravarVenda.disabled'))
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'),saved)
                self.assertEqual(Venda.objects.count(),1)
                # Reconcile the detached UUID manually, then resume the SAME draft.
                self.disconnect(tab); self.reconnect(tab); self.stable(tab); self.manual(tab)
                tab.wait('!clienteBusca.disabled && !btnGravarVenda.disabled')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'),saved)
                self.assertEqual(Venda.objects.count(),1)
                self.assertEqual(ItemVenda.objects.count(),1)
                self.assertEqual(ContaReceber.objects.count(),1)
                current = tab.evaluate('(await repo.all("operations"))[0]')
                self.assertEqual(current['operation_id'],original['operation_id'])
                self.assertEqual(current['payload_hash'],original['payload_hash'])
                self.assertEqual(current['attempts'],1)
                self.assertEqual(tab.evaluate('sent.length'),0)
                self.assertEqual(OperacaoSincronizacao.objects.count(),1)
            finally: chrome.stop()

    def test_online_lost_commit_cannot_use_new_offline_sale_action(self):
        with TemporaryDirectory(prefix='consecutive-online-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = online.prepare(self,chrome)
                tab.evaluate('window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u)==="/api/vendas/online/")throw TypeError("Lost after commit");return r};await salesDraftUI.submitOnline(undefined,decodeURIComponent(document.cookie.split("; ").find(c=>c.startsWith("csrftoken=")).split("=")[1]));true')
                marker = tab.evaluate('salesDraftUI.finalization')
                self.assertEqual(Venda.objects.count(),1)
                self.assertTrue(tab.evaluate('document.getElementById("sales-new-offline-sale").hidden'))
                self.assertTrue(tab.evaluate('try{await drafts.startNextOfflineDraft(repo,scope,salesDraftUI.finalization.operation_id,salesDraftUI.revision);false}catch(e){true}'))
                self.reload(tab); self.repository(tab)
                self.assertEqual(tab.evaluate('salesDraftUI.finalization.operation_id'),marker['operation_id'])
                self.assertTrue(tab.evaluate('document.getElementById("sales-new-offline-sale").hidden && btnGravarVenda.disabled'))
                tab.evaluate('await salesDraftUI.recoverOnline();true')
                tab.wait('!salesDraftUI.finalization && !btnGravarVenda.disabled')
                self.assertEqual(Venda.objects.count(),1)
                self.assertEqual(ItemVenda.objects.count(),1)
                self.assertEqual(ContaReceber.objects.count(),1)
                self.assertEqual(OperacaoSincronizacao.objects.count(),1)
            finally: chrome.stop()
