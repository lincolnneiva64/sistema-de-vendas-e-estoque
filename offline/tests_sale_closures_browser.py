"""Administrative decisions against an isolated real server and private Chrome profiles."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .browser_support import Chrome
from .models import EncerramentoVendaOffline
from . import tests_sale_sync_browser as sync
from . import tests_sale_release_browser as release
from . import tests_sale_creation as creation


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome unavailable')
class SaleClosureBrowserTests(StaticLiveServerTestCase):
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
    original_conflict = release.SaleReleaseBrowserTests.original_conflict
    assert_original_preserved = release.SaleReleaseBrowserTests.assert_original_preserved
    effects = creation.SaleCreationTests.effects

    def module(self, tab):
        tab.evaluate('window.closures=await import("/offline/assets/2-8g-close/sales-closures.js");window.operation=await repo.get("operations",'+json.dumps(self.original['operation_id'])+');true')

    def close_dialog(self, tab):
        tab.evaluate('document.dispatchEvent(new CustomEvent("sales-close-operation",{detail:{operation_id:operation.operation_id}}));true')
        tab.wait('document.getElementById("sales-close-dialog")?.open')
        tab.evaluate('document.getElementById("sales-close-reason").value="Teste abandonado pelo operador";document.getElementById("sales-close-confirm").checked=true;document.querySelector("[data-close-submit]").click();true')

    def test_confirmed_closure_two_tabs_reload_reopen_history_stock_and_new_sale(self):
        with TemporaryDirectory(prefix='closure-tabs-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.original_conflict(chrome); self.module(tab)
                other = self.open_sales(chrome)
                before = self.effects()
                snapshots = tab.evaluate('await repo.all("snapshots")')
                self.assertTrue(tab.evaluate('window.stock=await import("/offline/assets/2-8g-close/sales-stock.js");try{stock.validateStock(await stock.readStock(repo,scope),scope,[{...operation.payload.itens[0],quantidade:"1"}]);false}catch(e){e.message.includes("insuficiente")}'))
                self.assertFalse(tab.evaluate('closures.closedLocally(await repo.all("metadata"),operation)'))
                self.close_dialog(tab)
                tab.wait('document.querySelector("#sales-close-dialog [role=status]").textContent.includes("Encerrada administrativamente")')
                for page in [tab, other]:
                    page.wait('!salesDraftUI.finalization && !salesDraftUI.blocked && !btnGravarVenda.disabled')
                    self.reload(page); self.repository(page)
                    page.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                self.assert_original_preserved(tab)
                self.assertEqual(self.effects(), before)
                self.assertEqual(EncerramentoVendaOffline.objects.count(), 1)
                self.module(tab)
                self.assertTrue(tab.evaluate('closures.closedLocally(await repo.all("metadata"),operation)'))
                self.assertEqual(tab.evaluate('await repo.all("snapshots")'),snapshots)
                self.assertTrue(tab.evaluate('window.stock=await import("/offline/assets/2-8g-close/sales-stock.js");stock.validateStock(await stock.readStock(repo,scope),scope,[{...operation.payload.itens[0],quantidade:"1"}]);true'))
                tab.call('Page.close'); other.call('Page.close')
                reopened = self.open_sales(chrome)
                reopened.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                self.select_customer(reopened, 'Segundo Cliente'); self.add_product(reopened, 'Segundo Produto', '1')
                self.assertNotEqual(reopened.evaluate('(await drafts.loadDraft(repo,scope)).draft.draft_id'),self.original['operation_id'])
                self.assertEqual(self.effects(), before)
            finally: chrome.stop()

    def test_lost_response_network_failure_absence_same_identity_and_f5_recovery(self):
        with TemporaryDirectory(prefix='closure-lost-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.original_conflict(chrome); self.module(tab); before = self.effects()
                tab.evaluate('window.fetch=async(u,o)=>{if(String(u).endsWith("/close/"))throw TypeError("offline before request");return realFetch(u,o)};window.failure="";try{await closures.closeOperation(repo,scope,operation,"Abandono",true)}catch(e){failure=e.message};window.intentBefore=await repo.get("metadata",closures.intentKey(operation));true')
                self.assertEqual(EncerramentoVendaOffline.objects.count(),0)
                self.assertTrue(tab.evaluate('(await closures.recoverClosure(repo,scope,operation)).unknown'))
                self.assertTrue(tab.evaluate('!!(await drafts.loadDraft(repo,scope)).finalization'))
                tab.evaluate('window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u).endsWith("/close/"))throw TypeError("response lost after commit");return r};try{await closures.closeOperation(repo,scope,operation,"Different ignored reason",true)}catch(e){failure=e.message};true')
                self.assertEqual(EncerramentoVendaOffline.objects.count(),1)
                self.assertEqual(str(EncerramentoVendaOffline.objects.get().closure_id),tab.evaluate('intentBefore.body.closure_id'))
                self.assertTrue(tab.evaluate('!!(await drafts.loadDraft(repo,scope)).finalization'))
                self.reload(tab); self.repository(tab); self.module(tab)
                self.assertTrue(tab.evaluate('!!salesDraftUI.finalization'))
                tab.evaluate('await closures.recoverClosure(repo,scope,operation);true')
                tab.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                tab.evaluate('await closures.recoverClosure(repo,scope,operation);true')
                self.assertEqual(EncerramentoVendaOffline.objects.count(),1)
                self.assert_original_preserved(tab); self.assertEqual(self.effects(),before)
            finally: chrome.stop()

    def test_local_commit_failure_is_recovered_after_f5(self):
        with TemporaryDirectory(prefix='closure-local-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.original_conflict(chrome); self.module(tab); before = self.effects()
                tab.evaluate('window.originalTransaction=core.Repository.prototype.transaction;window.failMirror=false;core.Repository.prototype.transaction=function(stores,write,work){if(failMirror&&write&&stores.includes("operations")&&stores.includes("metadata"))return Promise.reject(Error("local commit failed"));return originalTransaction.call(this,stores,write,work)};window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u).endsWith("/close/"))failMirror=true;return r};try{await closures.closeOperation(repo,scope,operation,"Abandono",true)}catch(e){};true')
                self.assertEqual(EncerramentoVendaOffline.objects.count(),1)
                tab.evaluate('failMirror=false;core.Repository.prototype.transaction=originalTransaction;window.fetch=realFetch;true')
                self.assertTrue(tab.evaluate('!!(await drafts.loadDraft(repo,scope)).finalization'))
                self.reload(tab);self.repository(tab);self.module(tab)
                tab.evaluate('await closures.recoverClosure(repo,scope,operation);true')
                tab.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                self.assert_original_preserved(tab); self.assertEqual(self.effects(),before)
            finally: chrome.stop()

    def test_real_later_draft_survives_closure_in_another_tab(self):
        with TemporaryDirectory(prefix='closure-later-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab=self.prepare(chrome)
                tab.evaluate('await salesDraftUI.startNextSale();true')
                self.select_customer(tab,'Segundo Cliente');self.add_product(tab,'Segundo Produto','1')
                later=tab.evaluate('await drafts.loadDraft(repo,scope)')
                from estoque.models import Produto
                Produto.objects.filter(pk=self.product.pk).update(quantidade=0)
                self.stable(tab);self.manual(tab)
                self.original=tab.evaluate('await repo.get("operations",operation.operation_id)')
                self.assertEqual(self.original['status'],'conflito')
                from .models import OperacaoSincronizacao
                self.server_original=OperacaoSincronizacao.objects.values().get(operation_id=self.original['operation_id'])
                self.history_original=tab.evaluate('await repo.get("history",operation.operation_id)')
                before=self.effects()
                other=self.open_sales(chrome);self.module(other)
                other.evaluate('await closures.closeOperation(repo,scope,operation,"Abandono da original",true);true')
                self.assertEqual(tab.evaluate('await drafts.loadDraft(repo,scope)'),later)
                self.reload(tab);self.repository(tab)
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'),later['draft'])
                self.assert_original_preserved(tab);self.assertEqual(self.effects(),before)
            finally:chrome.stop()

    def test_local_revision_unknown_and_invalid_ack_fail_closed(self):
        with TemporaryDirectory(prefix='closure-refusals-') as profile:
            chrome = Chrome(sync.CHROME, profile).start()
            try:
                tab = self.original_conflict(chrome); self.module(tab)
                self.assertTrue(tab.evaluate('try{await closures.closeOperation(repo,scope,operation,"Abandono",false);false}catch(e){true}'))
                tab.evaluate('window.child={...operation,operation_id:crypto.randomUUID(),status:"resultado_desconhecido",payload:{...operation.payload,revisao:{original_operation_id:operation.operation_id}}};await repo.put("operations",child);true')
                self.assertTrue(tab.evaluate('try{await closures.closeOperation(repo,scope,operation,"Abandono",true);false}catch(e){true}'))
                self.assertTrue(tab.evaluate('try{await closures.mirrorClosure(repo,scope,operation,{status:"encerrada_sem_venda"});false}catch(e){true}'))
                self.assertEqual(EncerramentoVendaOffline.objects.count(),0)
                self.assertTrue(tab.evaluate('!!(await drafts.loadDraft(repo,scope)).finalization'))
                self.assert_original_preserved(tab)
            finally: chrome.stop()

    def test_other_device_closure_recovered_by_manual_lookup_without_resending_sale(self):
        from .tests_sale_closures import closure_body
        from .services import COMMAND_KEYS
        from .models import OperacaoSincronizacao
        from estoque.models import Produto
        with TemporaryDirectory(prefix='closure-other-device-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                tab=self.prepare(chrome)
                command={k:tab.evaluate('operation')[k] for k in COMMAND_KEYS}
                Produto.objects.filter(pk=self.product.pk).update(quantidade=0)
                tab.evaluate('window.posts=0;window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u)==="/api/offline/observations/"){posts++;throw TypeError("conflict response lost")};return r};true')
                self.stable(tab);self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'),'resultado_desconhecido')
                row_before=OperacaoSincronizacao.objects.values().get()
                before=self.effects()
                response=self.client.post('/api/offline/operations/'+command['operation_id']+'/close/',
                    json.dumps(closure_body(command)),content_type='application/json')
                self.assertEqual(response.status_code,200,response.content)
                tab.evaluate('window.fetch=realFetch;true')
                self.stable(tab);self.manual(tab)
                tab.wait('!salesDraftUI.finalization && !salesDraftUI.blocked')
                self.assertEqual(tab.evaluate('posts'),1)
                self.assertEqual(tab.evaluate('document.getElementById("offline-modal-text").textContent'),'Sincronização concluída')
                self.assertEqual(OperacaoSincronizacao.objects.values().get(),row_before)
                self.assertEqual(self.effects(),before)
                self.assertEqual(EncerramentoVendaOffline.objects.count(),1)
            finally:chrome.stop()
