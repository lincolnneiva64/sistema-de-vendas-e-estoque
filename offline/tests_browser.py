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
        tab.wait("!document.getElementById('offline-sync').disabled")
