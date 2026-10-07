import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.contrib.auth import get_user_model
from django.test import override_settings

from estoque.models import Cliente, Produto, Venda, ItemVenda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from .tests_commercial import commercial_fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class SalesDraftBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    def setUp(self):
        self.user, self.customer, self.product, self.operator, *_ = commercial_fixtures()
        self.second_customer = Cliente.objects.create(nome='Segundo Cliente', prazo_padrao_dias=7)
        self.second_product = Produto.objects.create(nome='Segundo Produto', codigo='P-02', preco_compra=1,
            preco_vista=5, preco_prazo=6, unidade_compra='UN', quantidade=100)
        self.client = Client()
        self.client.force_login(self.user)

    def open_sales(self, chrome, offline=False):
        tab = chrome.tab()
        tab.call('Network.enable')
        tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
        if offline:
            tab.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
        tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
        tab.wait('window.salesDraftUI?.ready === true')
        return tab

    def reload(self, tab):
        previous = tab.evaluate('performance.timeOrigin')
        tab.call('Page.reload')
        for attempt in range(3):
            try:
                tab.wait('performance.timeOrigin !== ' + str(previous) + ' && window.salesDraftUI?.ready === true')
                return
            except RuntimeError as error:
                if 'navigated or closed' not in str(error) or attempt == 2:
                    raise

    def select_customer(self, tab, name):
        tab.evaluate('await (await import([...document.scripts].find(s=>s.src.includes("/offline/app.js")).src)).initialized;await salesOffline.ready;true')
        tab.evaluate('clienteBusca.value=' + repr(name) + ';clienteBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
        # Query and click in one browser task: an async autocomplete response can
        # replace the list between two separate CDP calls.
        tab.evaluate('window.customerRetryAt=performance.now();true')
        # Mobile preloading can clear suggestions after the first response.
        tab.wait('(()=>{const item=document.querySelector(".cliente-sugestao-item");if(item){item.click();return true;}if(performance.now()-customerRetryAt>500){customerRetryAt=performance.now();clienteBusca.dispatchEvent(new Event("input",{bubbles:true}));}return false})()')

    def add_product(self, tab, name, quantity='2'):
        tab.evaluate('operadorVenda.value="Operador Comercial";operadorVenda.dispatchEvent(new Event("change",{bubbles:true}));produtoBusca.value=' + repr(name) + ';produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
        tab.wait('!!document.querySelector(".produto-sugestao-item")')
        tab.evaluate('document.querySelector(".produto-sugestao-item").click();quantidade.value=' + repr(quantity) + ';document.getElementById("btnAdicionarItemVenda").click();true')
        tab.evaluate('await salesDraftUI.flush();true')
        tab.wait('document.querySelector("#sales-draft-status [role=status]").textContent.includes("salvo")')

    def repository(self, tab):
        tab.evaluate("window.core=await import('/static/offline/core.js');window.drafts=await import('/static/offline/sales-drafts.js');window.repo=new core.Repository(await core.openDB());window.scope=await drafts.draftScope(repo,await repo.get('metadata','sales-identity'));true")

    def test_reload_reopen_restart_offline_edit_discard_and_no_official_changes(self):
        models = [Produto, Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]
        before = {m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        with TemporaryDirectory(prefix='sales-drafts-persistence-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertEqual(tab.evaluate('clienteId.value'), str(self.customer.pk))
                self.assertEqual(tab.evaluate('tipoVenda.value'), 'A prazo')
                self.assertEqual(tab.evaluate('operadorVenda.value'), self.operator.nome)
                self.assertIn('restaurado', tab.evaluate('document.getElementById("sales-draft-status").textContent'))
                target_id = tab.call('Target.getTargetInfo')['targetInfo']['targetId']
                chrome.browser.call('Target.closeTarget', {'targetId':target_id})
                tab = self.open_sales(chrome)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                tab.wait('!document.querySelector(".offline-commercial button").disabled')
                tab.evaluate('document.querySelector(".offline-commercial button").click();true')
                tab.wait('!document.querySelector(".offline-commercial button").disabled && document.querySelector(".offline-commercial [role=status]").textContent.includes("Catálogo salvo")')
                chrome.stop()
                chrome = Chrome(CHROME, profile).start()
                tab = self.open_sales(chrome)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                tab.wait('!!navigator.serviceWorker.controller')
                worker = chrome.worker(); worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                tab.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                self.reload(tab)
                self.assertTrue(tab.evaluate('salesOffline.shell'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.add_product(tab, 'Segundo Produto', '3')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 2)
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();quantidade.value="4";unidade.value="UN";unidade.dispatchEvent(new Event("change",{bubbles:true}));document.getElementById("btnAdicionarItemVenda").click();true')
                self.select_customer(tab, 'Segundo Cliente')
                tab.evaluate('tipoVenda.value="consumo_proprio";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));vencimentoVenda.value="2026-10-20";vencimentoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')
                self.reload(tab)
                self.assertEqual(tab.evaluate('clienteId.value'), str(self.second_customer.pk))
                self.assertEqual(tab.evaluate('tipoVenda.value'), 'consumo_proprio')
                self.assertEqual(tab.evaluate('vencimentoVenda.value'), '2026-10-20')
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.quantidade'), '4')
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.unidade'), 'UN')
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.precoUnitario'), '3.00')
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();document.dispatchEvent(new KeyboardEvent("keydown",{key:"Delete",bubbles:true}));true')
                tab.wait('!!document.getElementById("btn-confirmar-excluir")')
                tab.evaluate('document.getElementById("btn-confirmar-excluir").click();true')
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===1')
                tab.evaluate('await salesDraftUI.flush();true')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                for width in [1280,390]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=850,deviceScaleFactor=1,mobile=width==390))
                    self.assertTrue(tab.evaluate('(()=>{const r=document.getElementById("sales-draft-status").getBoundingClientRect();return r.width>0 && r.right<=innerWidth+1})()'))
                self.repository(tab)
                record = tab.evaluate('(await drafts.loadDraft(repo,scope)).draft')
                self.assertEqual(record['schema_version'], 1)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), 0)
                self.assertTrue(tab.evaluate('!btnGravarVenda.disabled && btnGravarVenda.textContent==="Salvar venda offline"'))
                tab.evaluate('document.getElementById("sales-draft-discard").click();true')
                tab.wait('document.getElementById("vendaConfirmacaoOverlay").classList.contains("visivel")')
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                tab.evaluate('document.getElementById("btnCancelarConfirmacaoVenda").click();true')
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                tab.evaluate('document.getElementById("sales-draft-discard").click();true')
                tab.evaluate('document.getElementById("btnConfirmarConfirmacaoVenda").click();true')
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===0')
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.assertEqual(tab.evaluate('clienteId.value'), '')
            finally:
                chrome.stop()
        self.assertEqual({m.__name__:list(m.objects.order_by('pk').values()) for m in models}, before)

    def test_atomic_scope_projection_missing_catalog_and_stale_tab(self):
        with TemporaryDirectory(prefix='sales-drafts-safety-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                self.repository(tab)
                tab.evaluate('window.original=(await drafts.loadDraft(repo,scope));window.extra=structuredClone(original.draft);extra.cliente.cpf_cnpj="SECRET";extra.cliente.limite_credito="SECRET";extra.pix="SECRET";extra.itens[0].financial="SECRET";window.next=await drafts.saveDraft(repo,scope,extra,original.revision);true')
                self.assertFalse(tab.evaluate('JSON.stringify((await drafts.loadDraft(repo,scope)).draft).includes("SECRET")'))
                tab.evaluate('window.originalTransaction=repo.transaction;repo.transaction=function(stores,write,work){return originalTransaction.call(this,stores,write,(tx,done)=>{work(tx,done);if(write)tx.abort()})};true')
                self.assertTrue(tab.evaluate('try{await drafts.saveDraft(repo,scope,extra,next.revision);false}catch(e){true}'))
                tab.evaluate('repo.transaction=originalTransaction;true')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).revision'), tab.evaluate('next.revision'))
                for field in ['actor_id','environment_id','device_id']:
                    replacement = 'crypto.randomUUID()' if field=='device_id' else ('"99999"' if field=='actor_id' else '"another-environment"')
                    self.assertTrue(tab.evaluate('try{await drafts.loadDraft(repo,{...scope,' + field + ':' + replacement + '});false}catch(e){true}'))
                self.reload(tab)
                other = self.open_sales(chrome)
                other.evaluate('tipoVenda.value="À vista";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')
                tab.wait('salesDraftUI.blocked')
                tab.evaluate('tipoVenda.value="consumo_proprio";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')
                self.repository(other)
                self.assertEqual(other.evaluate('(await drafts.loadDraft(repo,scope)).draft.tipo_venda'), 'À vista')
                self.assertTrue(tab.evaluate('await salesDraftUI.beforeSubmit()===false'))
                self.reload(other)
                self.product.preco_vista = 99
                self.product.fator_conversao = 10
                self.product.save(update_fields=['preco_vista', 'fator_conversao'])
                self.reload(other)
                self.assertEqual(other.evaluate('document.querySelector("#tabelaProdutos tr").dataset.precoUnitario'), '24.00')
                self.assertEqual(other.evaluate('document.querySelector("#tabelaProdutos tr").dataset.draftFator'), '12.00')
                self.product.ativo = False; self.product.save(update_fields=['ativo'])
                self.reload(other)
                self.assertEqual(other.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.assertIn('fora do catálogo', other.evaluate('document.querySelector("#tabelaProdutos tr").textContent'))
                self.assertTrue(other.evaluate('await salesDraftUI.beforeSubmit()===false'))
            finally:
                chrome.stop()

    def test_identity_changes_and_local_failure_preserve_previous_record(self):
        other_user = get_user_model().objects.create_user(username='another-draft-user', password='test-only')
        other_user.user_permissions.set(self.user.user_permissions.all())
        other_client = Client(); other_client.force_login(other_user)
        with TemporaryDirectory(prefix='sales-drafts-identity-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                self.repository(tab)
                tab.evaluate('window.previous=(await drafts.loadDraft(repo,scope));window.deviceBefore=await repo.get("metadata","device");window.savedKey=drafts.draftKey(scope);window.transactionBefore=core.Repository.prototype.transaction;core.Repository.prototype.transaction=function(stores,write,work){if(write && stores.length===1 && stores[0]==="metadata")return Promise.reject(new DOMException("Quota simulada","QuotaExceededError"));return transactionBefore.call(this,stores,write,work)};document.querySelector("#tabelaProdutos tr").click();quantidade.value="1";document.getElementById("btnAdicionarItemVenda").click();await salesDraftUI.flush();true')
                self.assertIn('Quota simulada', tab.evaluate('document.getElementById("sales-draft-status").textContent'))
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft.itens[0].quantidade'), '2')
                # F5 restores the last valid committed assembly after the failed write.
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.quantidade'), '2')
                second = self.open_sales(chrome)
                second.call('Network.setCookie', {'name':'sessionid','value':other_client.cookies['sessionid'].value,'url':self.live_server_url})
                self.reload(second)
                self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===0')
                self.assertTrue(tab.evaluate('salesDraftUI.blocked'))
                second.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                with override_settings(OFFLINE_ENVIRONMENT_ID='different-environment'):
                    self.reload(second)
                    self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.reload(second)
                self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.repository(second)
                old_device = second.evaluate('await repo.get("metadata","device")')
                second.evaluate('await repo.put("metadata",{key:"device",id:crypto.randomUUID(),sequence:0});true')
                self.reload(second)
                self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.repository(second)
                second.evaluate('await repo.put("metadata",' + json.dumps(old_device) + ');true')
                self.reload(second)
                self.assertEqual(second.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                self.repository(second)
                second.evaluate('window.last=await drafts.loadDraft(repo,scope);window.payload=last.draft;await drafts.discardDraft(repo,scope,last.revision);true')
                self.assertTrue(second.evaluate('try{await drafts.saveDraft(repo,scope,payload,last.revision);false}catch(e){true}'))
                self.assertIsNone(second.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
            finally:
                chrome.stop()

    def test_online_failure_and_confirmed_success_cleanup(self):
        with TemporaryDirectory(prefix='sales-drafts-submit-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado', '1')
                self.repository(tab)
                # Hold the response: the draft must remain while official save is pending.
                tab.evaluate('window.realFetch=fetch;window.fetch=(url,options)=>String(url).includes("/vendas/gravar/") ? new Promise(resolve=>window.releaseSale=resolve) : realFetch(url,options);btnGravarVenda.click();true')
                tab.wait('typeof window.releaseSale === "function"')
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                tab.evaluate('releaseSale(new Response(JSON.stringify({sucesso:false,mensagem:"Erro controlado"}),{status:400,headers:{"Content-Type":"application/json"}}));true')
                tab.wait('!btnGravarVenda.disabled')
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                tab.evaluate('document.getElementById("btnVendaErroOk").click();window.fetch=realFetch;btnGravarVenda.click();true')
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===0')
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), 0)
            finally:
                chrome.stop()

    def test_confirmed_sale_survives_idb_cleanup_failure_without_resubmit(self):
        with TemporaryDirectory(prefix='sales-drafts-confirmed-failure-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado', '1')
                self.repository(tab)
                tab.evaluate('window.savedDraft=(await drafts.loadDraft(repo,scope)).draft;window.sequenceBefore=(await repo.get("metadata","device")).sequence;window.realFetch=fetch;window.posts=0;window.originalTransaction=core.Repository.prototype.transaction;window.originalSuccess=salesDraftUI.confirmedSuccess;salesDraftUI.confirmedSuccess=async()=>{await originalSuccess();throw new Error("Falha local inesperada após confirmação")};window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url).includes("/vendas/gravar/")){posts++;core.Repository.prototype.transaction=function(stores,write,work){if(write&&stores.length===1&&stores[0]==="metadata")return Promise.reject(new DOMException("Falha IndexedDB na limpeza","QuotaExceededError"));return originalTransaction.call(this,stores,write,work)}}return response};btnGravarVenda.click();true')
                tab.wait('posts===1 && document.querySelectorAll("#tabelaProdutos tr").length===0')
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertTrue(tab.evaluate('btnGravarVenda.disabled'))
                self.assertFalse(tab.evaluate('document.getElementById("vendaErroOverlay").classList.contains("visivel")'))
                self.assertIn('confirmada', tab.evaluate('document.getElementById("sales-draft-status").textContent'))
                self.assertTrue(tab.evaluate('!!document.getElementById("vendaGravadaBloco").textContent'))
                # Residual bytes remain in IDB, but are fenced by the independent confirmation marker.
                self.assertIsNotNone(tab.evaluate('await repo.get("metadata",drafts.draftKey(scope))'))
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).confirmation.estado'), 'concluido')
                self.assertTrue(tab.evaluate('try{await drafts.saveDraft(repo,scope,savedDraft,savedDraft.revision);false}catch(e){true}'))
                tab.evaluate('btnGravarVenda.click();document.getElementById("btnConfirmarFechamentoVenda").click();true')
                self.assertEqual(tab.evaluate('posts'), 1)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore'))
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.assertEqual(tab.evaluate('clienteId.value'), '')
                self.repository(tab)
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), 0)
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
            finally:
                chrome.stop()

    def test_pending_editor_and_immediate_reload(self):
        with TemporaryDirectory(prefix='sales-drafts-editor-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();quantidade.value="1";quantidade.dispatchEvent(new Event("input",{bubbles:true}));unidade.value="UN";unidade.dispatchEvent(new Event("change",{bubbles:true}));preco.value="3.25";preco.dispatchEvent(new Event("input",{bubbles:true}));true')
                # No explicit flush and no debounce sleep: normal F5 must flush the editor.
                self.reload(tab)
                self.assertEqual(tab.evaluate('quantidade.value'), '1')
                self.assertEqual(tab.evaluate('unidade.value'), 'UN')
                self.assertEqual(tab.evaluate('preco.value'), '3.25')
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.quantidade'), '2')
                tab.evaluate('document.getElementById("btnAdicionarItemVenda").click();true')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.quantidade'), '1')
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.precoUnitario'), '3.25')
                # The draft remains usable even if the catalog was not prepared.
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                worker = chrome.worker(); worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                tab.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                tab.wait('salesOffline.active && !salesOffline.available && document.querySelector("#produto option[data-draft-missing]")!==null', timeout=25)
                self.assertEqual(tab.evaluate('clienteId.value'), str(self.customer.pk))
                self.assertEqual(tab.evaluate('operadorVenda.value'), self.operator.nome)
                self.assertEqual(tab.evaluate('tipoVenda.value'), 'A prazo')
                self.reload(tab)
                self.assertTrue(tab.evaluate('salesOffline.shell'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 1)
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();quantidade.value="2";document.getElementById("btnAdicionarItemVenda").click();true')
                self.reload(tab)
                self.assertEqual(tab.evaluate('document.querySelector("#tabelaProdutos tr").dataset.quantidade'), '2')
                self.assertTrue(tab.evaluate('!btnGravarVenda.disabled && btnGravarVenda.textContent==="Salvar venda offline"'))
                tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/?editar=99'})
                tab.wait('location.search.includes("editar=") && window.salesDraftUI?.ready && window.salesDraftBridge?.eligible===false')
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.repository(tab)
                self.assertIsNotNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
            finally:
                chrome.stop()
