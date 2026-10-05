import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from .browser_support import Chrome
from .models import OperacaoSincronizacao
from .tests import fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(os.environ.get('OFFLINE_BROWSER_TESTS') != '0' and Path(CHROME).is_file(), 'Chrome unavailable or OFFLINE_BROWSER_TESTS=0')
class OfflineBrowserTests(StaticLiveServerTestCase):
    def test_shared_stability_navigation_reload_restart_and_gaps(self):
        user, _, task, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='offline-continuity-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                cookie = client.cookies['sessionid'].value
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': cookie, 'url': self.live_server_url})
                # Shared test clock survives documents and browser restart. Production policy is unchanged.
                script = "const realWall=Date.now;Date.now=()=>realWall()+Number(localStorage.getItem('test-offset')||0);"
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': script})
                self.load(tab)
                self.assertTrue(tab.evaluate("!!navigator.locks && isSecureContext"))
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Continuity manual only')
                tab.wait("(await testRepo.all('metadata')).some(v=>v.key.startsWith('communication:') && v.connected)")
                def step(milliseconds=30000):
                    # Allow the previous probe's finally/render to finish before changing the clock.
                    tab.evaluate("await new Promise(resolve=>setTimeout(resolve,100));true")
                    tab.evaluate(f"localStorage.setItem('test-offset',Number(localStorage.getItem('test-offset')||0)+{milliseconds});window.dispatchEvent(new Event('online'))")
                    tab.wait("(await testRepo.all('metadata')).some(v=>v.key.startsWith('communication:') && v.last_success_at > Date.now()-2000)")
                def minutes(target):
                    tab.wait(f"document.getElementById('offline-status').textContent.includes('({target}/15 min)')")
                for _ in range(16):
                    step()
                minutes(8)
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait("document.getElementById('offline-status')?.textContent.includes('(8/15 min)')")
                tab.call('Page.reload')
                tab.wait("document.getElementById('offline-status')?.textContent.includes('(8/15 min)')")
                second = chrome.tab()
                second.call('Page.addScriptToEvaluateOnNewDocument', {'source': script})
                self.load(second)
                second.wait("document.getElementById('offline-status').textContent.includes('(8/15 min)')")
                second.call('Page.close')
                chrome.stop()
                chrome.start()
                tab = chrome.tab()
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': script})
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': cookie, 'url': self.live_server_url})
                self.load(tab)
                minutes(8)
                for _ in range(14):
                    step()
                tab.wait("!document.getElementById('offline-sync').disabled")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                # Safe fallback even when a script tries to click the disabled button.
                tab.evaluate("Object.defineProperty(navigator,'locks',{value:undefined,configurable:true})")
                tab.wait("document.getElementById('offline-sync').disabled")
                tab.evaluate("document.getElementById('offline-sync').dispatchEvent(new Event('click'))")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                step(61000)
                minutes(0)
                step()
                step(3600000)
                minutes(0)
                tab.evaluate("window.savedFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('failure')):savedFetch(u,o);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-status').textContent.startsWith('OFFLINE')")
                tab.evaluate("window.fetch=savedFetch;window.dispatchEvent(new Event('online'))")
                minutes(0)
                self.assertTrue(tab.evaluate("""(async()=>{
                    const scope={actor_id:'isolated',environment_id:'isolated'};
                    await testRepo.communication(scope,{type:'success',started_at:100,at:100});
                    await testRepo.communication(scope,{type:'failure',at:200});
                    const stale=await testRepo.communication(scope,{type:'success',started_at:150,at:300});
                    if(stale.connected)throw Error('stale success erased failure');
                    const other=await testRepo.communication({...scope,actor_id:'other'});
                    if(other.connected)throw Error('actor leak');
                    const environment=await testRepo.communication({...scope,environment_id:'other'});
                    return !environment.connected;
                })()"""))
            finally:
                chrome.stop()

    def test_login_return_session_and_prepare(self):
        user, rental, task, _ = fixtures()
        with TemporaryDirectory(prefix='offline-auth-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': "window.authErrors=[]; const originalConsoleError=console.error; console.error=(...args)=>{authErrors.push(args.join(' ')); originalConsoleError(...args)}; window.addEventListener('error',e=>authErrors.push(e.message)); window.addEventListener('unhandledrejection',e=>authErrors.push(String(e.reason)));"})
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                tab.wait("document.getElementById('offline-session')?.textContent.includes('Autentique-se online')")
                self.assertFalse(tab.evaluate("document.getElementById('offline-login').hidden"))
                self.assertEqual(tab.evaluate('authErrors'), [])
                tab.evaluate("document.getElementById('offline-login').click()")
                tab.wait("location.pathname === '/offline/login/' && !!document.querySelector('[name=username]')")
                self.assertEqual(tab.evaluate("document.querySelector('[name=next]').value"), '/offline/')
                self.assertEqual(tab.evaluate('authErrors'), [])
                tab.evaluate("document.querySelector('[name=username]').value='offline-pilot'; document.querySelector('[name=password]').value='test-only'; document.querySelector('form button').click()")
                tab.wait("location.pathname === '/offline/' && document.getElementById('offline-session')?.textContent.includes('Autenticado como offline-pilot')")
                self.assertTrue(tab.evaluate("document.getElementById('offline-login').hidden"))
                tab.evaluate("window.prepareRequests=[]; const originalFetch=window.fetch; window.fetch=(url,options)=>{prepareRequests.push(String(url));return originalFetch(url,options)};")
                tab.wait("!document.getElementById('offline-prepare').disabled && !!document.getElementById('offline-device').textContent")
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-message').textContent.includes('Preparação salva')")
                self.assertIn('/api/offline/snapshot/', tab.evaluate('prepareRequests'))
                self.assertEqual(tab.evaluate("document.getElementById('offline-task').value"), str(task.pk))
                self.assertEqual(tab.evaluate("document.getElementById('offline-task').options.length"), 1)
                self.assertEqual(tab.evaluate('authErrors'), [])
                tab.wait('!!navigator.serviceWorker.controller')
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/login/?next=/offline/'})
                tab.wait("location.pathname === '/offline/' && document.getElementById('offline-login')?.hidden")
                self.assertEqual(tab.evaluate('authErrors'), [])
            finally:
                chrome.stop()

    def test_persistence_offline_sync_failures_and_multiple_tabs(self):
        user, rental, task, _ = fixtures()
        client = Client()
        client.force_login(user)
        session_cookie = client.cookies['sessionid'].value
        with TemporaryDirectory(prefix='offline-pilot-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': session_cookie, 'url': self.live_server_url, 'httpOnly': True})
                self.load(tab)
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-message').textContent.includes('Preparação salva')")
                tab.wait("!!navigator.serviceWorker.controller")
                self.assertTrue(tab.evaluate("!!(await navigator.serviceWorker.ready).active"))
                # Core invariants with an injected clock, using production policy unchanged.
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Stability,validHealth,validReceipt}=await import('/static/offline/core.js');
                    const s=new Stability(); s.success(0,0);
                    for(let t=30000;t<=900000;t+=30000)s.success(t,t);
                    if(!s.ready(900000,900000))throw Error('not ready at 15 minutes');
                    s.reset(); if(s.ready(900000,900000))throw Error('failure did not reset');
                    s.success(0,0);s.success(900000,900000);if(s.ready(900000,900000))throw Error('closed time counted');
                    if(validHealth({ok:true,environment:'other',protocol_version:1},'correct'))throw Error('wrong environment accepted');
                    if(validReceipt({status:'confirmada'},{}))throw Error('ambiguous receipt accepted');
                    return true;
                })()"""))
                # Save while the actual browser network is offline.
                self.network(tab, True)
                self.assertTrue(tab.evaluate("await fetch('/api/offline/health/').then(()=>false,()=>true)"))
                self.save(tab, 'Ligar antes de chegar. 😀')
                original_id = tab.evaluate("(await testRepo.all('operations'))[0].operation_id")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.call('Page.reload')
                self.ready(tab)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations'))[0].operation_id"), original_id)
                self.assertTrue(tab.evaluate("document.getElementById('offline-operations').textContent.includes('Ligar antes')"))
                # Full browser process restart with the same profile.
                chrome.stop()
                chrome.start()
                tab = chrome.tab()
                tab.call('Network.enable')
                self.network(tab, True)
                self.load(tab)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations'))[0].operation_id"), original_id)
                self.assertTrue(tab.evaluate("document.getElementById('offline-sync').disabled"))
                # Browser restart may discard a session cookie; simulate fresh online authentication.
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': session_cookie, 'url': self.live_server_url, 'httpOnly': True})
                # A failed IDB commit must not announce success.
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Repository}=await import('/static/offline/core.js');
                    const broken=new Repository(testRepo.db);
                    broken.transaction=(stores,write,work)=>stores[0]==='operations'?Promise.reject(Error('quota simulation')):testRepo.transaction(stores,write,work);
                    const snap=await testRepo.get('snapshots','pilot');const before=(await testRepo.all('operations')).length;
                    try{await broken.create(snap,snap.tasks[0],'must fail');throw Error('false success')}catch(e){if(e.message!=='quota simulation')throw e}
                    return (await testRepo.all('operations')).length===before;
                })()"""))
                tab.evaluate("window.realAdd=IDBObjectStore.prototype.add;IDBObjectStore.prototype.add=function(...a){if(this.name==='operations')throw new DOMException('quota simulation','QuotaExceededError');return realAdd.apply(this,a)};document.getElementById('offline-note').value='Não pode anunciar sucesso';document.getElementById('offline-form').requestSubmit();true")
                tab.wait("document.getElementById('offline-message').textContent.includes('Não foi possível salvar neste dispositivo')")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).length"), 1)
                tab.evaluate("IDBObjectStore.prototype.add=realAdd;true")
                self.network(tab, False)
                tab.wait("document.getElementById('offline-stability').textContent.includes('CONEXÃO ESTÁVEL')")
                self.clock(tab)
                self.advance_to_ready(tab)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)  # No automatic synchronization.
                self.assertFalse(tab.evaluate("document.getElementById('offline-sync').disabled"))
                # Second tab holds the lock: clicking cannot start another sender.
                second = chrome.tab()
                self.load(second)
                second.evaluate("window.lockHeld=false; navigator.locks.request('offline-pilot-sync',async()=>{window.lockHeld=true;await new Promise(r=>window.releaseLock=r)}); true")
                second.wait('window.lockHeld')
                tab.evaluate("window.confirm=()=>true;document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-message').textContent.includes('Outra aba')")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                second.evaluate('window.releaseLock();true')
                second.call('Page.close')
                tab.wait("(await navigator.locks.query()).held.every(lock=>lock.name!=='offline-pilot-sync')")
                self.save(tab, 'Segunda operação deve aguardar se a primeira falhar')
                # Commit succeeds, but the JS caller loses the response.
                tab.evaluate("""window.realFetch=window.fetch;window.fetch=async(...args)=>{
                    const r=await realFetch(...args);if(args[0]==='/api/offline/observations/'){await r.text();window.fetch=realFetch;throw Error('response lost after commit')}return r};
                    document.getElementById('offline-sync').click();true""")
                tab.wait("document.getElementById('offline-operations').textContent.includes('resultado_desconhecido')")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).filter(o=>o.status==='pendente').length"), 1)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).find(o=>o.status==='resultado_desconhecido').operation_id"), original_id)
                self.assertTrue(tab.evaluate("document.getElementById('offline-sync').disabled"))
                self.advance_to_ready(tab)
                tab.evaluate("document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-operations').textContent.includes('confirmada')")
                tab.wait("(await testRepo.all('operations')).every(o=>o.status==='confirmada')")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 2)
                self.assertEqual(tab.evaluate("(await testRepo.all('history'))[0].status"), 'confirmada')
                # Actual five-second timeout: no acknowledgement, no loss, no subsequent send.
                self.save(tab, 'Timeout preserva operação')
                self.save(tab, 'Não enviar depois do timeout')
                tab.evaluate("window.timeoutFetch=window.fetch;window.fetch=(...a)=>a[0]==='/api/offline/observations/'?new Promise((resolve,reject)=>a[1].signal.addEventListener('abort',()=>reject(new DOMException('Timeout','AbortError')))):timeoutFetch(...a);document.getElementById('offline-sync').click();true")
                tab.wait("document.getElementById('offline-operations').textContent.includes('resultado_desconhecido')", timeout=10)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 2)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).filter(o=>o.status==='pendente').length"), 1)
                tab.evaluate('window.fetch=timeoutFetch;true')
                self.advance_to_ready(tab)
                tab.evaluate("document.getElementById('offline-sync').click()")
                tab.wait("(await testRepo.all('operations')).every(o=>o.status==='confirmada')")
                # Expired session preserves new pending operation.
                self.save(tab, 'Sessão expirada preserva fila')
                tab.call('Network.deleteCookies', {'name': 'sessionid', 'url': self.live_server_url})
                tab.evaluate("document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-message').textContent.includes('Autentique-se novamente')")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).filter(o=>o.status==='pendente').length"), 1)
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': session_cookie, 'url': self.live_server_url, 'httpOnly': True})
                # Missing task produces retained conflict.
                task.delete()
                tab.evaluate("document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-operations').textContent.includes('conflito')")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).find(o=>o.status==='conflito').payload.observacao"), 'Sessão expirada preserva fila')
            finally:
                chrome.stop()

    def test_global_indicator_on_normal_page_and_mobile(self):
        user, rental, task, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='offline-global-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'online'")
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Stability,indicatorState}=await import('/static/offline/core.js');
                    const s=new Stability(); const op={status:'pendente'};
                    const state=(ops=[op], extra={})=>indicatorState({operations:ops,stability:s,now:s.last||0,wall:s.lastWall||0,...extra});
                    if(state().kind!=='offline'||state().count!==1)throw Error('offline count');
                    s.success(0,0);if(state().kind!=='waiting'||state().canSync)throw Error('reconnect');
                    for(let t=30000;t<=900000;t+=30000)s.success(t,t);
                    if(!state().canSync||state().kind!=='ready')throw Error('ready');
                    if(state([{status:'conflito'}]).kind!=='conflict')throw Error('conflict');
                    if(state([{status:'confirmada'}]).count!==0)throw Error('confirmed');
                    if(state([], {notice:{kind:'success',label:'ok',until:900001}}).kind!=='success')throw Error('success');
                    if(state([op],{syncing:true,progress:'1/3'}).kind!=='syncing')throw Error('progress');
                    s.reset();if(state().canSync||state().kind!=='offline')throw Error('reset');
                    return true;
                })()"""))
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                self.assertTrue(tab.evaluate("document.getElementById('offline-global').getBoundingClientRect().right <= innerWidth"))
                # Real failed health request on a normal page, without navigator.onLine.
                tab.evaluate("window.originalFetch=fetch;window.fetch=(url,opts)=>url==='/api/offline/health/'?Promise.reject(Error('server down')):originalFetch(url,opts);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'offline'")
                tab.evaluate("window.fetch=originalFetch;window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'waiting'")
                self.load(tab)
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Global manual sync')
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'waiting'")
                self.assertIn('1 ', tab.evaluate("document.getElementById('offline-status').textContent"))
                self.clock(tab)
                self.advance_to_ready(tab)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.evaluate("document.getElementById('offline-global-sync').click()")
                tab.wait("document.getElementById('offline-global').dataset.state === 'success'")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                self.assertTrue(tab.evaluate("document.getElementById('offline-global-sync').hidden"))
            finally:
                chrome.stop()

    def ready(self, tab):
        tab.wait("!!document.getElementById('offline-device')?.textContent")
        tab.evaluate("(async()=>{const {Repository,openDB}=await import('/static/offline/core.js');window.testRepo=new Repository(await openDB());return true})()")

    def load(self, tab):
        tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
        self.ready(tab)

    def network(self, tab, offline):
        tab.call('Network.emulateNetworkConditions', {'offline': offline, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
        tab.evaluate("window.dispatchEvent(new Event('offline'))" if offline else "window.dispatchEvent(new Event('online'))")

    def save(self, tab, text):
        import json
        tab.evaluate("document.getElementById('offline-message').textContent='';document.getElementById('offline-note').value=" + json.dumps(text) + ";document.getElementById('offline-form').requestSubmit()")
        tab.wait("document.getElementById('offline-message').textContent.includes('Observação salva neste dispositivo')")

    def clock(self, tab):
        tab.evaluate("window.clockOffset=0;window.realNow=performance.now.bind(performance);window.realWall=Date.now;performance.now=()=>realNow()+clockOffset;Date.now=()=>realWall()+clockOffset;window.confirm=()=>true")

    def advance_to_ready(self, tab):
        # Each success is observed; no test shortcut exists in the production frontend.
        for _ in range(32):
            tab.evaluate("window.clockOffset+=30000;window.probeFinished=false;(()=>{const previous=window.fetch;window.fetch=async(...a)=>{const r=await previous(...a);if(a[0]==='/api/offline/health/'){window.fetch=previous;const clone=r.clone();await clone.text();setTimeout(()=>window.probeFinished=true,50)}return r}})();window.dispatchEvent(new Event('online'));true")
            tab.wait('window.probeFinished')
        tab.wait("!(document.getElementById('offline-sync') || document.getElementById('offline-global-sync')).disabled")
