"""Real browser + real isolated sale records; discard responses only after commit."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sales_drafts_browser as draft_tests
CHROME = draft_tests.CHROME


def prepare(test, chrome, payment='A prazo'):
    tab = test.open_sales(chrome)
    test.select_customer(tab, 'Cliente Comercial')
    test.add_product(tab, 'Produto Fracionado', '1')
    test.repository(tab)
    tab.evaluate('tipoVenda.value='+json.dumps(payment)+';tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')
    return tab


def submit(tab):
    tab.evaluate('btnGravarVenda.click();true')


def check_loss(test, reopen=False, payment='A prazo'):
    with TemporaryDirectory(prefix='online-sale-loss-') as profile:
        chrome = Chrome(CHROME, profile).start()
        try:
            tab = prepare(test, chrome, payment)
            # Invoke the same public submission method used by the sale button;
            # cash origin is explicit rather than faking an official receipt.
            tab.evaluate('window.realFetch=fetch;window.onlinePosts=0;window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==="/api/vendas/online/"){onlinePosts++;throw new TypeError("Resposta perdida depois do commit")};return response};window.done=salesDraftUI.submitOnline('+('{caixa:"10",banco:"14"}' if payment == 'À vista' else 'undefined')+',decodeURIComponent(document.cookie.split("; ").find(c=>c.startsWith("csrftoken=")).split("=")[1]));true')
            tab.wait('document.querySelector("#sales-draft-status [role=alert]")?.textContent.includes("desconhecido")')
            operation = tab.evaluate('(await repo.all("operations"))[0]')
            test.assertEqual(operation['status'], 'resultado_desconhecido')
            test.assertEqual(Venda.objects.count(), 1)
            test.assertEqual(ItemVenda.objects.count(), 1)
            test.product.refresh_from_db()
            test.assertEqual(test.product.quantidade, Decimal('1.5'))
            models = [Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto]
            before = {m.__name__:list(m.objects.order_by('pk').values()) for m in models}
            test.assertTrue(tab.evaluate('btnGravarVenda.disabled'))
            tab.evaluate('btnGravarVenda.click();true')
            test.assertEqual(tab.evaluate('onlinePosts'), 1)
            if reopen:
                chrome.stop(); chrome = Chrome(CHROME, profile).start()
                tab = test.open_sales(chrome)
            else:
                test.reload(tab)
            test.repository(tab)
            test.assertEqual(tab.evaluate('(await repo.all("operations"))[0].payload_hash'), operation['payload_hash'])
            test.assertTrue(tab.evaluate('btnGravarVenda.disabled'))
            tab.wait('!document.getElementById("sales-online-recover").hidden && !document.getElementById("sales-online-recover").disabled')
            tab.evaluate('document.getElementById("sales-online-recover").click();true')
            try:
                tab.wait('!salesDraftUI.finalization && !document.getElementById("sales-official-sale-link").hidden')
            except AssertionError:
                raise AssertionError(tab.evaluate('JSON.stringify({message:document.querySelector("#sales-draft-status span").textContent,button:document.getElementById("sales-online-recover").outerHTML,operations:await repo.all("operations")})'))
            test.assertEqual(tab.evaluate('(await repo.all("operations"))[0].server_result.record_id'), Venda.objects.get().pk)
            test.assertEqual(before, {m.__name__:list(m.objects.order_by('pk').values()) for m in models})
            test.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        finally:
            chrome.stop()


def check_cleanup_failure(test):
    with TemporaryDirectory(prefix='online-cleanup-') as profile:
        chrome = Chrome(CHROME, profile).start()
        try:
            tab = prepare(test, chrome)
            tab.evaluate('window.realFetch=fetch;window.cleanupFailed=0;window.baseTransaction=core.Repository.prototype.transaction;window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==="/api/vendas/online/")core.Repository.prototype.transaction=function(stores,write,work){if(write&&stores.includes("metadata")&&stores.includes("operations")){cleanupFailed++;return Promise.reject(Error("cleanup failure"))};return baseTransaction.call(this,stores,write,work)};return response};btnGravarVenda.click();true')
            tab.wait('cleanupFailed>0 && (await repo.all("operations"))[0].status==="confirmada"')
            test.assertEqual(Venda.objects.count(), 1)
            test.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'confirmada')
            tab.evaluate('core.Repository.prototype.transaction=baseTransaction;true')
            test.reload(tab); test.repository(tab)
            tab.wait('!salesDraftUI.finalization')
            test.assertEqual(Venda.objects.count(), 1)
            test.assertEqual(ItemVenda.objects.count(), 1)
            test.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
        finally: chrome.stop()


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class OnlineSaleBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = draft_tests.SalesDraftBrowserTests.setUp
    open_sales = draft_tests.SalesDraftBrowserTests.open_sales
    select_customer = draft_tests.SalesDraftBrowserTests.select_customer
    add_product = draft_tests.SalesDraftBrowserTests.add_product
    repository = draft_tests.SalesDraftBrowserTests.repository
    reload = draft_tests.SalesDraftBrowserTests.reload

    def test_normal_online_button_real_records(self):
        with TemporaryDirectory(prefix='online-normal-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = prepare(self, chrome)
                submit(tab)
                tab.wait('!salesDraftUI.finalization && document.querySelectorAll("#tabelaProdutos tr").length===0 && !document.getElementById("sales-official-sale-link").hidden')
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(ContaReceber.objects.count(), 1)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade, Decimal('1.5'))
            finally: chrome.stop()

    def test_lost_commit_f5_and_official_recovery(self):
        check_loss(self)

    def test_lost_commit_browser_reopen(self):
        check_loss(self, reopen=True)

    def test_lost_commit_cash_unique_financial(self):
        check_loss(self, payment='À vista')
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)

    def test_lost_commit_consumption_unique_expense(self):
        check_loss(self, payment='consumo_proprio')
        self.assertEqual(DespesaDiaria.objects.count(), 1)

    def test_absent_lookup_only_manual_stable_retry_same_command(self):
        from . import tests_sale_sync_browser as sync_tests
        with TemporaryDirectory(prefix='online-absent-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = sync_tests.SaleSyncBrowserTests.open_sales(self, chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado', '1')
                tab.evaluate('tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();window.onlinePosts=0;window.fetch=async(url,options)=>{if(String(url)==="/api/vendas/online/"){onlinePosts++;throw TypeError("request did not arrive")};if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};btnGravarVenda.click();true')
                tab.wait('(document.querySelector("#sales-draft-status span").textContent.includes("desconhecido"))')
                original = tab.evaluate('(await repo.all("operations"))[0]')
                tab.evaluate('await salesDraftUI.recoverOnline();true')
                self.assertIn('ainda nao encontrada', tab.evaluate('document.querySelector("#sales-draft-status span").textContent'))
                self.assertEqual(Venda.objects.count(), 0)
                self.assertEqual(tab.evaluate('onlinePosts'), 1)
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                sync_tests.SaleSyncBrowserTests.stable(self, tab)
                sync_tests.SaleSyncBrowserTests.start_manual(self, tab)
                tab.wait('(await repo.all("operations"))[0].status==="confirmada"')
                wire = tab.evaluate('sent[0]')
                self.assertEqual(wire, {key:value for key,value in original.items() if key in sync_tests.COMMAND_KEYS or key=='payload_hash'})
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(ContaReceber.objects.count(), 1)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade, Decimal('1.5'))
            finally: chrome.stop()

    def test_local_failure_prevents_post(self):
        with TemporaryDirectory(prefix='online-idb-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = prepare(self, chrome)
                tab.evaluate('window.realFetch=fetch;window.posts=0;window.originalTransaction=core.Repository.prototype.transaction;core.Repository.prototype.transaction=function(stores,write,work){if(write&&stores.includes("operations"))return Promise.reject(Error("local commit failed"));return originalTransaction.call(this,stores,write,work)};window.fetch=(url,options)=>{if(String(url)==="/api/vendas/online/")posts++;return realFetch(url,options)};btnGravarVenda.click();true')
                tab.wait('document.getElementById("btnVendaErroOk").offsetParent!==null')
                self.assertEqual(tab.evaluate('posts'), 0)
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(Venda.objects.count(), 0)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally: chrome.stop()

    def test_f5_while_response_in_flight_recovers_committed_sale(self):
        with TemporaryDirectory(prefix='online-inflight-f5-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = prepare(self, chrome)
                tab.evaluate('window.realFetch=fetch;window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==="/api/vendas/online/"){window.committed=true;return new Promise(()=>{})}return response};btnGravarVenda.click();true')
                tab.wait('window.committed===true')
                operation = tab.evaluate('(await repo.all("operations"))[0]')
                self.assertEqual(operation['status'], 'enviando')
                self.assertEqual(Venda.objects.count(), 1)
                self.reload(tab); self.repository(tab)
                tab.wait('!document.getElementById("sales-online-recover").hidden && !document.getElementById("sales-online-recover").disabled')
                tab.evaluate('document.getElementById("sales-online-recover").click();true')
                tab.wait('!salesDraftUI.finalization')
                recovered = tab.evaluate('(await repo.all("operations"))[0]')
                self.assertEqual(recovered['operation_id'], operation['operation_id'])
                self.assertEqual(recovered['payload_hash'], operation['payload_hash'])
                self.assertEqual(recovered['attempts'], 1)
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(ContaReceber.objects.count(), 1)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade, Decimal('1.5'))
            finally: chrome.stop()

    def test_query_confirmation_then_late_post_does_not_clear_new_assembly(self):
        with TemporaryDirectory(prefix='online-late-response-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = prepare(self, chrome)
                tab.evaluate('window.realFetch=fetch;window.baseSubmit=salesDraftUI.submitOnline;window.postDone=false;salesDraftUI.submitOnline=async(...args)=>{try{return await baseSubmit.apply(salesDraftUI,args)}finally{postDone=true}};window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==="/api/vendas/online/"){window.committed=true;return new Promise(resolve=>window.deliverResponse=()=>resolve(response))}return response};btnGravarVenda.click();true')
                tab.wait('window.committed===true')
                tab.wait('!document.getElementById("sales-online-recover").disabled')
                tab.evaluate('document.getElementById("sales-online-recover").click();true')
                tab.wait('!salesDraftUI.finalization')
                self.select_customer(tab, 'Segundo Cliente')
                self.add_product(tab, 'Segundo Produto', '1')
                before = tab.evaluate('(await drafts.loadDraft(repo,scope)).draft')
                tab.evaluate('deliverResponse();true')
                tab.wait('postDone===true')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'), before)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(ContaReceber.objects.count(), 1)
            finally: chrome.stop()

    def test_two_tabs_and_offline_transition_during_real_send(self):
        with TemporaryDirectory(prefix='online-two-tabs-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = prepare(self, chrome)
                second = self.open_sales(chrome)
                # Finish the second tab's startup recovery lock before deliberately
                # starting the first POST. Otherwise the safe ifAvailable guard
                # can legitimately refuse the send while startup is still running.
                second.evaluate('await (await import([...document.scripts].find(s=>s.src.includes("/app.js")).src)).initialized;true')
                tab.evaluate('window.realFetch=fetch;window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==="/api/vendas/online/"){window.committed=true;return new Promise((resolve,reject)=>window.loseResponse=()=>reject(TypeError("offline after commit")))}return response};btnGravarVenda.click();true')
                tab.wait('window.committed===true')
                second.wait('!!salesDraftUI.finalization')
                self.assertTrue(second.evaluate('btnGravarVenda.disabled'))
                second.evaluate('btnGravarVenda.click();true')
                tab.call('Network.emulateNetworkConditions', dict(offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))
                tab.evaluate('loseResponse();true')
                tab.wait('document.getElementById("sales-online-recover").hidden===false')
                tab.call('Network.emulateNetworkConditions', dict(offline=False, latency=0, downloadThroughput=0, uploadThroughput=0))
                tab.wait('!document.getElementById("sales-online-recover").hidden && !document.getElementById("sales-online-recover").disabled')
                tab.evaluate('document.getElementById("sales-online-recover").click();true')
                tab.wait('!salesDraftUI.finalization')
                second.wait('!salesDraftUI.finalization')
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(ContaReceber.objects.count(), 1)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade, Decimal('1.5'))
            finally: chrome.stop()
