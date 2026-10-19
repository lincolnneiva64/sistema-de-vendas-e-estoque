"""Connectivity confirmation and safe recovery through the actual sales page."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from . import tests_sale_sync_browser as sync_tests
from .browser_support import Chrome


@skipUnless(Path(sync_tests.CHROME).is_file(), 'Chrome indisponível')
class SalesConnectivityBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync_tests.SaleSyncBrowserTests.setUp
    def open_sales(self, chrome):
        tab = sync_tests.SaleSyncBrowserTests.open_sales(self, chrome)
        tab.evaluate('window.posts=[];window.transport=realFetch;window.realFetch=(url,options)=>{if(options?.method && options.method.toUpperCase()!=="GET")posts.push({url:String(url),method:options.method});return transport(url,options)};true')
        return tab
    repository = sync_tests.SaleSyncBrowserTests.repository
    select_customer = sync_tests.SaleSyncBrowserTests.select_customer
    add_product = sync_tests.SaleSyncBrowserTests.add_product

    def fail_health(self, tab):
        tab.evaluate("window.fetch=(u,o)=>String(u)==='/api/offline/health/'?Promise.reject(TypeError('route lost')):realFetch(u,o);await app.probe();true")

    def confirm_offline(self, tab):
        self.fail_health(tab)
        tab.evaluate('offset+=5001;await app.probe();offset+=5001;await app.probe();await salesOffline.ready;true')
        tab.wait('salesOffline.active && !salesOffline.activating')

    def test_microfailures_confirmation_and_editable_recovery(self):
        with TemporaryDirectory(prefix='sales-connectivity-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                assembly = tab.evaluate('salesDraftBridge.capture()')
                # Native event and transient navigator flag are hints, never authority.
                tab.evaluate("Object.defineProperty(navigator,'onLine',{value:false,configurable:true});window.dispatchEvent(new Event('offline'));await app.probe();true")
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate("Object.defineProperty(navigator,'onLine',{value:true,configurable:true});true")
                self.fail_health(tab)
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.state'), 'checking')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('offset+=5001;await app.probe();true')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('window.fetch=realFetch;offset+=5001;await app.probe();true')
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'online')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                # Isolated transport failure is checked by health. HTTP/JSON/processing
                # errors and cancelled requests cannot activate offline either.
                for result in ["Promise.reject(TypeError('autocomplete'))", "Promise.reject(new DOMException('cancel','AbortError'))",
                               "Promise.resolve(new Response('bad json'))", "Promise.resolve(new Response('{}',{status:401}))",
                               "Promise.resolve(new Response('{}',{status:403}))", "Promise.resolve(new Response('{}',{status:500}))",
                               "Promise.resolve(new Response(JSON.stringify({clientes:{bad:true}})))"]:
                    tab.evaluate("window.fetch=(u,o)=>String(u).includes('/clientes/autocomplete/')?" + result + ":realFetch(u,o);window.autocompleteDone=false;const faultyFetch=fetch;window.fetch=(u,o)=>{const pending=faultyFetch(u,o);if(String(u).includes('/clientes/autocomplete/'))pending.finally(()=>window.autocompleteDone=true).catch(()=>{});return pending};offset+=1;clienteBusca.value='audit'+offset;clienteBusca.dispatchEvent(new Event('input',{bubbles:true}));true")
                    tab.wait('autocompleteDone');tab.evaluate('await app.probe();true')
                    self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('window.fetch=realFetch;clienteBusca.value="Cliente Comercial";true')
                self.select_customer(tab, 'Cliente Comercial')
                assembly = tab.evaluate('salesDraftBridge.capture()')
                # Persistent false navigator + failed health, third probe >=10s.
                tab.evaluate("Object.defineProperty(navigator,'onLine',{value:false,configurable:true});true")
                self.confirm_offline(tab)
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'offline')
                self.assertEqual(tab.evaluate('document.body.dataset.salesOffline'), 'true')
                self.assertEqual(tab.evaluate('salesDraftBridge.capture()'), assembly)
                # Even an editable draft must not hide another pending sale in this scope.
                for status in ['enviando', 'resultado_desconhecido', 'conflito']:
                    tab.evaluate('await repo.put("operations",{operation_id:"connectivity-test",type:"criar_venda",actor_id:scope.actor_id,environment_id:scope.environment_id,status:'+json.dumps(status)+'});true')
                    self.assertFalse(tab.evaluate('await salesDraftUI.canRecoverOnline()'))
                tab.evaluate('await repo.transaction(["operations"],true,tx=>tx.objectStore("operations").delete("connectivity-test"));true')
                # GET catalog recovery preserves the current draft and sends no sale.
                tab.evaluate("Object.defineProperty(navigator,'onLine',{value:true,configurable:true});window.fetch=realFetch;offset+=5001;await app.probe();true")
                tab.wait('!salesOffline.active && !salesOffline.recovering')
                self.assertIsNone(tab.evaluate('document.body.dataset.salesOffline'))
                self.assertEqual(tab.evaluate('salesDraftBridge.capture()'), assembly)
                self.assertTrue(tab.evaluate('!!document.querySelector("#produto option[data-produto-id]")'))
                self.assertTrue(tab.evaluate('JSON.stringify(drafts.projectAssembly((await drafts.loadDraft(repo,scope)).draft))===JSON.stringify(drafts.projectAssembly(salesDraftBridge.capture()))'))
                self.assertEqual(tab.evaluate('sent'), [])
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(tab.evaluate('core.POLICY.window'), 900000)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                # Stale observation/suspension changes UI to checking, not offline.
                tab.evaluate('offset+=120000;for(const t of timers.values())if(t.delay===1000)t.callback();true')
                tab.wait('document.getElementById("offline-global").dataset.connection === "checking"')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                # The pure policy rejects stale/overlapping observations and resets
                # consecutive failures after an unobserved suspension gap.
                self.assertTrue(tab.evaluate('''(() => {
                    const observe=core.connectionObservation;
                    let c=observe(null,{ok:false,started_at:0,at:1});
                    c=observe(c,{ok:false,started_at:5001,at:5002});
                    if(c.kind!=='checking'||c.failures!==2)return false;
                    c=observe(c,{ok:false,started_at:10002,at:10003});
                    if(c.kind!=='offline')return false;
                    const stale=observe(c,{ok:true,started_at:5000,at:10004});
                    if(stale.kind!=='offline')return false;
                    c=observe(c,{ok:false,started_at:130000,at:130001});
                    if(c.kind!=='checking'||c.failures!==1)return false;
                    c=observe(c,{ok:true,started_at:130002,at:130003});
                    return c.kind==='online'&&c.failures===0;
                })()'''))
                tab.evaluate("document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                self.assertFalse(tab.evaluate('salesOffline.active'))
                # Expired offline metadata must not activate this online page.
                tab.evaluate('const stored=await repo.communication(scope);await repo.put("metadata",{...stored,connected:false,connection:{kind:"offline",failures:3,checked_at:Date.now()-120000,failed_since:Date.now()-130000}});for(const t of timers.values())if(t.delay===1000)t.callback();true')
                tab.wait('document.getElementById("offline-global").dataset.connection === "checking"')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('await app.probe();true')
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'online')
                self.assertFalse(tab.evaluate('salesOffline.active'))
            finally:
                chrome.stop()

    def test_completed_unknown_and_conflict_are_preserved_on_recovery(self):
        with TemporaryDirectory(prefix='sales-connectivity-completed-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.confirm_offline(tab)
                tab.evaluate('await salesDraftUI.finalizeOffline();true')
                tab.wait('!!salesDraftUI.finalization')
                marker = tab.evaluate('salesDraftUI.finalization')
                tab.evaluate('window.fetch=realFetch;offset+=5001;await app.probe();await salesOffline.deactivate();true')
                self.assertTrue(tab.evaluate('salesOffline.active'))
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'online')
                self.assertIn('Servidor online', tab.evaluate('document.getElementById("sales-offline-notice").textContent'))
                self.assertEqual(tab.evaluate('salesDraftUI.finalization'), marker)
                self.assertEqual(tab.evaluate('salesDraftUI.lastOnlineRecoveryBlock'), 'local-finalization')
                for status in ['pendente', 'enviando', 'resultado_desconhecido', 'conflito']:
                    tab.evaluate('const op=(await repo.all("operations"))[0];await repo.put("operations",{...op,status:'+json.dumps(status)+'});await salesDraftUI.refreshCompletion();window.beforeRecovery=JSON.stringify(await repo.all("operations"));await salesOffline.deactivate();true')
                    self.assertTrue(tab.evaluate('beforeRecovery === JSON.stringify(await repo.all("operations"))'))
                    self.assertIn('Servidor online', tab.evaluate('document.getElementById("sales-offline-notice").textContent'))
                    self.assertTrue(tab.evaluate('salesOffline.active'))
                    self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].operation_id'), marker['operation_id'])
                    self.assertFalse(tab.evaluate('await salesDraftUI.canRecoverOnline()'))
                self.assertEqual(tab.evaluate('sent'), [])
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(tab.evaluate('core.POLICY.window'), 900000)
            finally:
                chrome.stop()

    def test_autocomplete_transport_failure_needs_health_confirmation(self):
        with TemporaryDirectory(prefix='sales-connectivity-route-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                tab.evaluate('''window.autocompleteFailed=false;
                    window.fetch=(url,options)=>{
                        if(String(url).includes('/clientes/autocomplete/')) {
                            autocompleteFailed=true;return Promise.reject(TypeError('route'));
                        }
                        if(String(url)==='/api/offline/health/')return Promise.reject(TypeError('route'));
                        return realFetch(url,options);
                    };
                    clienteBusca.value='route-audit';clienteBusca.dispatchEvent(new Event('input',{bubbles:true}));true''')
                tab.wait('autocompleteFailed && document.getElementById("offline-global").dataset.connection === "checking"')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('offset+=5001;await app.probe();true')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                tab.evaluate('offset+=5001;await app.probe();await salesOffline.ready;true')
                tab.wait('salesOffline.active && !salesOffline.activating')
                # A healthy health endpoint cannot replace a failed catalogue.
                tab.evaluate('window.fetch=(u,o)=>String(u)==="/vendas/"?Promise.resolve(new Response("unavailable",{status:503})):realFetch(u,o);offset+=5001;await app.probe();true')
                tab.wait('!salesOffline.recovering && document.getElementById("sales-offline-notice").textContent.includes("catálogo online")')
                self.assertTrue(tab.evaluate('salesOffline.active'))
                self.assertEqual(tab.evaluate('sent'), [])
                for status in (401, 403, 503):
                    tab.evaluate('window.fetch=(u,o)=>String(u)==="/vendas/"?Promise.resolve(new Response("unavailable",{status:'+str(status)+'})):realFetch(u,o);await salesOffline.deactivate();true')
                    self.assertTrue(tab.evaluate('salesOffline.active'))
                    text = tab.evaluate('document.getElementById("sales-offline-notice").textContent')
                    self.assertIn('Servidor respondeu', text)
                    self.assertIn('autenticação' if status in (401,403) else 'HTTP 503', text)
                tab.evaluate('window.fetch=(u,o)=>String(u)==="/vendas/"?Promise.resolve(new Response("<html>Login</html>",{status:200})):realFetch(u,o);await salesOffline.deactivate();true')
                self.assertIn('identidade', tab.evaluate('document.getElementById("sales-offline-notice").textContent'))
                tab.evaluate('window.fetch=realFetch;await app.probe();true')
                tab.wait('!salesOffline.active && !salesOffline.recovering')
                self.assertEqual(tab.evaluate('sent'), [])
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('posts'), [])
            finally:
                chrome.stop()


    def test_session_expired_is_not_offline_and_pending_bytes_are_preserved(self):
        with TemporaryDirectory(prefix='sales-session-recovery-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.confirm_offline(tab)
                tab.evaluate('await salesDraftUI.finalizeOffline();window.originalOperations=JSON.stringify(await repo.all("operations"));true')
                tab.wait('!!salesDraftUI.finalization')
                tab.evaluate('window.fetch=(u,o)=>String(u)==="/api/offline/session/"?Promise.resolve(new Response(JSON.stringify({authenticated:false,actor_id:"",can_prepare:false,username:""}),{headers:{"Content-Type":"application/json"}})):realFetch(u,o);offset+=5001;await app.probe();await salesOffline.deactivate();true')
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'online')
                self.assertEqual(tab.evaluate('document.getElementById("sales-offline-notice").dataset.reason'), 'authentication')
                self.assertIn('Sessão ausente ou expirada', tab.evaluate('document.getElementById("sales-offline-notice").textContent'))
                self.assertTrue(tab.evaluate('originalOperations === JSON.stringify(await repo.all("operations"))'))
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(tab.evaluate('sent'), [])
                # Reauthentication can refresh the diagnosis, but may not release the pending sale.
                tab.evaluate('window.fetch=realFetch;await app.probe();await salesOffline.deactivate();true')
                self.assertEqual(tab.evaluate('document.getElementById("sales-offline-notice").dataset.reason'), 'protected-local-sale')
                self.assertTrue(tab.evaluate('salesOffline.active'))
                self.assertTrue(tab.evaluate('originalOperations === JSON.stringify(await repo.all("operations"))'))
                self.assertEqual(tab.evaluate('posts'), [])
            finally:
                chrome.stop()


    def test_old_cached_module_is_bypassed_without_clearing_pending_sale(self):
        from estoque.models import Produto, Venda, ItemVenda
        from .models import OperacaoSincronizacao
        with TemporaryDirectory(prefix='sales-old-cache-pending-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.confirm_offline(tab)
                tab.evaluate('await salesDraftUI.finalizeOffline();true')
                tab.wait('!!salesDraftUI.finalization')
                operation = tab.evaluate('JSON.stringify((await repo.all("operations"))[0])')
                operation_id = json.loads(operation)['operation_id']
                tab.evaluate('window.fetch=realFetch;offset+=5001;await app.probe();true')
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                models = (Produto, Venda, ItemVenda, OperacaoSincronizacao)
                before = {m.__name__:list(m.objects.order_by('pk').values()) for m in models}
                tab.evaluate("""(async()=>{
                    const key=(await caches.keys()).find(k=>k.startsWith('offline-pilot-shell-'));
                    const cache=await caches.open(key);
                    await cache.put('/offline/assets/local-controlled-v1/sales.js',new Response(
                        'throw new Error("obsolete-module-must-not-run")',{headers:{'Content-Type':'application/javascript'}}));
                    return true;
                })()""")
                origin = tab.evaluate('performance.timeOrigin')
                tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
                tab.wait('performance.timeOrigin !== '+str(origin)+' && !!window.salesDraftUI?.ready')
                self.repository(tab)
                self.assertEqual(tab.evaluate('JSON.stringify(await repo.get("operations",'+json.dumps(operation_id)+'))'), operation)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#sales-offline-notice").length'), 1)
                self.assertTrue(tab.evaluate("""(async()=>{
                    const key=(await caches.keys()).find(k=>k.startsWith('offline-pilot-shell-'));
                    const cache=await caches.open(key);
                    return (await (await cache.match('/offline/assets/local-controlled-v1/sales.js')).text())
                        .includes('obsolete-module-must-not-run');
                })()""") )
                for model in models:
                    self.assertEqual(list(model.objects.order_by('pk').values()), before[model.__name__])
            finally:
                chrome.stop()


    def test_reconnection_before_draft_ready_is_rechecked_without_another_probe(self):
        with TemporaryDirectory(prefix='sales-ready-recovery-') as profile:
            chrome = Chrome(sync_tests.CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.select_customer(tab, 'Cliente Comercial')
                self.add_product(tab, 'Produto Fracionado')
                tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);await salesDraftUI.flush();window.savedAssembly=salesDraftBridge.capture();true')
                self.confirm_offline(tab)
                tab.evaluate('salesDraftUI.ready=false;window.fetch=realFetch;offset+=5001;await app.probe();true')
                self.assertEqual(tab.evaluate('document.getElementById("offline-global").dataset.connection'), 'online')
                self.assertTrue(tab.evaluate('salesOffline.active'))
                tab.evaluate('salesDraftUI.ready=true;document.dispatchEvent(new Event("sales-draft-ready"));true')
                tab.wait('!salesOffline.active && !salesOffline.recovering')
                self.assertEqual(tab.evaluate('salesDraftBridge.capture()'), tab.evaluate('savedAssembly'))
                self.assertEqual(tab.evaluate('sent'), [])
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
            finally:
                chrome.stop()
