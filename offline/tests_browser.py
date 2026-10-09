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
    def test_checklist_navigation_uses_cached_shell_when_offline(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-checklist-shell-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                self.load(tab)
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                previous_time_origin = tab.evaluate('performance.timeOrigin')
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                tab.wait('performance.timeOrigin !== ' + str(previous_time_origin) + ' && !!navigator.serviceWorker.controller', timeout=20)

                worker = chrome.worker(self.live_server_url + '/service-worker.js')
                worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Page.navigate', {'url': self.live_server_url + '/locacoes/checklist-operacional/'})
                tab.wait("document.title==='Checklist local' && document.getElementById('local-message')?.textContent.includes('não foi preparado')", timeout=20)
                self.assertEqual(tab.evaluate('location.pathname'), '/locacoes/checklist-operacional/')
            finally:
                chrome.stop()

    def test_background_checks_and_unobserved_resume(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-background-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': """
                    window.timers=new Map();let timerId=0;
                    window.setInterval=(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId};
                    window.clearInterval=id=>timers.delete(id);
                    const wall=Date.now;window.offset=0;Date.now=()=>wall()+offset;
                    Object.defineProperty(document,'hidden',{value:false,configurable:true});
                """})
                tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
                tab.wait("!!document.getElementById('offline-global')")
                tab.evaluate("""window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;
                    const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.r=new Repository(await openDB());
                    window.scope={actor_id:document.getElementById('offline-global').dataset.actor,environment_id:document.getElementById('offline-global').dataset.environment};
                    window.baseFetch=fetch;window.healthCalls=0;window.healthFetch=(u,o)=>{if(u==='/api/offline/health/')healthCalls++;return baseFetch(u,o)};window.fetch=healthFetch;true""")
                def value(): return tab.evaluate('await r.communication(scope)')
                def hide(hidden):
                    tab.evaluate(f"Object.defineProperty(document,'hidden',{{value:{str(hidden).lower()},configurable:true}});document.dispatchEvent(new Event('visibilitychange'));true")
                    if not hidden: tab.evaluate('await app.probe();true')
                for _ in range(10): tab.evaluate('offset+=30000;await app.probe();true')
                baseline = value()['observed_ms']
                self.assertGreaterEqual(baseline, 300000)
                # No callbacks during 30s, 2min or 10min: no invented time on return.
                for gap in (30000, 120000, 600000):
                    hide(True)
                    tab.evaluate(f'offset+={gap};true')
                    self.assertEqual(value()['observed_ms'], baseline)
                    calls = tab.evaluate('healthCalls')
                    hide(False)
                    self.assertGreater(tab.evaluate('healthCalls'), calls)
                    self.assertEqual(value()['observed_ms'], baseline)
                    self.assertIsNone(value().get('failed_at'))
                # Hidden does not stop actual health checks. Four proven intervals credit 2min.
                hide(True)
                for _ in range(4):
                    tab.evaluate('offset+=30000;for(const t of timers.values())if(t.delay===30000)t.callback();await app.probe();true')
                self.assertGreaterEqual(value()['observed_ms'], baseline+120000)
                baseline = value()['observed_ms']
                # A delayed unsuccessful response is suspension, not a suspicion.
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?new Promise((resolve,reject)=>window.rejectSleep=()=>reject(Error('sleep'))):baseFetch(u,o);window.sleepProbe=app.probe();true")
                tab.wait('!!window.rejectSleep')
                tab.evaluate('offset+=600000;rejectSleep();await sleepProbe;window.fetch=healthFetch;await app.probe();true')
                self.assertEqual(value()['observed_ms'], baseline)
                self.assertFalse(value().get('suspect_id'))
                self.assertIsNone(value().get('failed_at'))
                # A genuinely stalled old promise cannot block the first timer on wake.
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?new Promise(resolve=>window.releaseSleep=()=>resolve(new Response(JSON.stringify({})))):baseFetch(u,o);window.oldProbe=app.probe();true")
                tab.wait('!!window.releaseSleep')
                calls = tab.evaluate('healthCalls')
                tab.evaluate('offset+=600000;window.fetch=healthFetch;await app.probe();releaseSleep();await oldProbe;true')
                self.assertGreater(tab.evaluate('healthCalls'), calls)
                self.assertEqual(value()['observed_ms'], baseline)
                self.assertFalse(value().get('suspect_id'))
                self.assertIsNone(value().get('failed_at'))
                # Even a valid response delivered after sleep cannot prove the gap.
                tab.evaluate("window.fetch=async(u,o)=>{const response=await healthFetch(u,o);if(u==='/api/offline/health/')offset+=600000;return response};await app.probe();window.fetch=healthFetch;await app.probe();true")
                self.assertEqual(value()['observed_ms'], baseline)
                self.assertFalse(value().get('suspect_id'))
                # A real failure on visible resumption still needs confirmation.
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('real resume failure')):baseFetch(u,o);true")
                hide(False)
                self.assertTrue(value().get('suspect_id'))
                self.assertEqual(value()['observed_ms'], baseline)
                tab.evaluate('offset+=5000;await app.probe();true')
                self.assertEqual(value()['observed_ms'], 0)
                self.assertIsNotNone(value().get('failed_at'))
            finally:
                chrome.stop()

    def test_legacy_reconnection_initializes_cursor_then_observes_full_window(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-legacy-recovery-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': """
                    const realWall=Date.now;window.healthOffset=0;Date.now=()=>realWall()+healthOffset;
                    window.setInterval=()=>0;window.clearInterval=()=>{};
                    Object.defineProperty(document,'hidden',{value:false,configurable:true});
                """})
                tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
                tab.wait("!!document.getElementById('offline-global') && [...document.scripts].some(s=>s.src.includes('/app.js'))")
                tab.evaluate("""window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);
                    await app.initialized;await app.probe();
                    const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());
                    window.scope={actor_id:document.getElementById('offline-global').dataset.actor,
                        environment_id:document.getElementById('offline-global').dataset.environment};true""")
                for legacy_events, future_cursor in ((False, False), (True, False), (True, True)):
                    with self.subTest(legacy_events=legacy_events, future_cursor=future_cursor):
                        tab.evaluate("""window.seedAt=Date.now();window.oldFailure=seedAt-3600000;
                            window.legacy={key:'communication:'+JSON.stringify([scope.environment_id,scope.actor_id]),
                                ...scope,connected:true,reconnecting:true,failed_at:oldFailure,
                                reset_reason:'falha de comunicação',stable_since:seedAt-3500000,
                                last_success_at:seedAt-30000,observed_ms:0,
                                observed_until:seedAt""" + ('+86400000' if future_cursor else '-3500000') + """};
                            await testRepo.put('metadata',legacy);true""")
                        # Exercise successful HTTP health checks, including the old event shape.
                        tab.evaluate("""window.core=await import('/offline/assets/2-8f/core.js');
                            window.originalCommunication=core.Repository.prototype.communication;true""")
                        if legacy_events:
                            tab.evaluate("""core.Repository.prototype.communication=function(scope,event){
                                if(event?.type==='success'){event={...event};delete event.observed_since;}
                                return originalCommunication.call(this,scope,event);
                            };true""")
                        try:
                            tab.evaluate('await app.probe();true')
                            first = tab.evaluate('await testRepo.communication(scope)')
                            self.assertEqual(first['observed_ms'], 0)
                            self.assertEqual(first['observed_since'], first['last_success_at'])
                            self.assertEqual(first['observed_until'], first['observed_since'])
                            self.assertTrue(first['reconnecting'])
                            tab.wait("document.getElementById('offline-global').dataset.state==='waiting'")
                            for minute in range(1, 16):
                                for _ in range(2):
                                    tab.evaluate('healthOffset+=30000;await app.probe();true')
                                state = tab.evaluate('await testRepo.communication(scope)')
                                self.assertEqual(state['observed_ms']//60000, minute)
                                self.assertEqual(state['reconnecting'], minute < 15)
                                self.assertEqual(state['failed_at'], first['failed_at'])
                                self.assertEqual(state['reset_reason'], first['reset_reason'])
                            tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                        finally:
                            tab.evaluate('core.Repository.prototype.communication=originalCommunication;true')
            finally:
                chrome.stop()

    def test_completed_stability_survives_reload_tabs_and_browser_restart(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        clock = """const realWall=Date.now;Date.now=()=>realWall()+Number(localStorage.getItem('stable-reopen-offset')||0);
            window.monitorCallbacks=new Map();let timerSequence=0;
            window.setInterval=(callback,delay)=>{const id=++timerSequence;monitorCallbacks.set(id,{callback,delay});return id};
            window.clearInterval=id=>monitorCallbacks.delete(id);"""
        setup = """window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;
            const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());
            window.scope={actor_id:document.getElementById('offline-global').dataset.actor,environment_id:document.getElementById('offline-global').dataset.environment};true"""
        with TemporaryDirectory(prefix='offline-stable-reopen-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                def load(path='/vendas/'):
                    tab = chrome.tab()
                    tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': clock})
                    tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                    tab.call('Page.navigate', {'url':self.live_server_url+path})
                    tab.wait("!!document.getElementById('offline-global') && [...document.scripts].some(s=>s.src.includes('/app.js'))")
                    tab.evaluate(setup)
                    return tab
                tab = load()
                # Exercise a real failed health request, followed by the complete 15-minute recovery.
                tab.evaluate("window.realFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('real outage')):realFetch(u,o);await app.probe();true")
                tab.wait("document.getElementById('offline-global').dataset.state==='offline'", timeout=25)
                self.confirm_failed_probe(tab)
                tab.evaluate('window.fetch=realFetch;await app.probe();true')
                tab.wait("document.getElementById('offline-global').dataset.state==='waiting'")
                for _ in range(30):
                    tab.evaluate("localStorage.setItem('stable-reopen-offset',Number(localStorage.getItem('stable-reopen-offset')||0)+30000);await Promise.all([...monitorCallbacks.values()].filter(t=>t.delay===30000).map(t=>t.callback()));true")
                tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                baseline = tab.evaluate('await testRepo.communication(scope)')
                self.assertGreaterEqual(baseline['observed_ms'], 900000)
                for path in ('/caixa-banco/', '/vendas/'):
                    tab.evaluate('window.oldStableDocument=true;true')
                    tab.call('Page.navigate', {'url':self.live_server_url+path})
                    tab.wait("!window.oldStableDocument && [...document.scripts].some(s=>s.src.includes('/app.js'))")
                    tab.evaluate(setup)
                    tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                tab.evaluate('window.oldStableDocument=true;true')
                tab.call('Page.reload'); tab.wait("!window.oldStableDocument && [...document.scripts].some(s=>s.src.includes('/app.js'))"); tab.evaluate(setup)
                tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                another = load()
                another.wait("document.getElementById('offline-global').dataset.state==='online'")
                tab.call('Page.close'); another.call('Page.close')
                tab = load()
                tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                tab.evaluate("localStorage.setItem('stable-reopen-offset',Number(localStorage.getItem('stable-reopen-offset')||0)+3600000);true")
                chrome.stop(); chrome.start()
                tab = load()
                tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                restored = tab.evaluate('await testRepo.communication(scope)')
                self.assertEqual(restored['stable_since'], baseline['stable_since'])
                self.assertEqual(restored['failed_at'], baseline['failed_at'])
                self.assertGreaterEqual(restored['observed_ms'], baseline['observed_ms'])
                self.assertFalse(restored['reconnecting'])
                self.assertEqual(tab.evaluate("document.getElementById('offline-reset-log').textContent"), '')
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                chrome.stop()

    def test_navigation_cancels_health_before_pagehide_without_false_reset(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-navigation-cancel-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                self.load(tab)
                tab.evaluate("""window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);
                    await app.initialized;await app.probe();
                    window.beforeLeaving=JSON.stringify((await testRepo.all('metadata')).filter(v=>v.key.startsWith('communication:')));
                    window.realFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?new Promise((resolve,reject)=>{
                        window.rejectNavigation=()=>reject(Error('navigation cancelled request'));
                        o.signal.addEventListener('abort',rejectNavigation,{once:true});
                    }):realFetch(u,o);
                    window.interruptedProbe=app.probe();true""")
                tab.wait('!!window.rejectNavigation')
                # Some cancellations occur during unload preparation, while document.hidden is still false.
                tab.evaluate("window.dispatchEvent(new Event('beforeunload'));rejectNavigation();await interruptedProbe;true")
                self.assertEqual(tab.evaluate("JSON.stringify((await testRepo.all('metadata')).filter(v=>v.key.startsWith('communication:')))"), tab.evaluate('beforeLeaving'))
                # A cancelled navigation has no pageshow. It must remain usable anyway.
                tab.evaluate("window.fetch=realFetch;await app.probe();true")
                tab.wait("document.getElementById('offline-status').dataset.state==='online'")
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('real failure')):realFetch(u,o);await app.probe();true")
                tab.wait("document.getElementById('offline-status').dataset.state==='offline'", timeout=25)
                self.confirm_failed_probe(tab)
                tab.evaluate('window.fetch=realFetch;await app.probe();true')
                tab.wait("document.getElementById('offline-status').dataset.state==='waiting'")
                self.clock(tab)
                for _ in range(4): self.advance_probe(tab)
                tab.wait("document.querySelector('.offline-status-counter')?.textContent.trim()==='2 de 15 minutos'")
            finally:
                chrome.stop()

    def test_checklist_visible_resume_observed_minutes(self):
        self.checklist_resume_observed_minutes(suspend_probe=False)

    def test_checklist_visible_resume_with_suspended_probe(self):
        self.checklist_resume_observed_minutes(suspend_probe=True)

    def checklist_resume_observed_minutes(self, suspend_probe):
        from estoque.models import Venda, Cliente, EntregaRota, EntregaRotaItem
        from django.core.signals import got_request_exception
        from django.db import connection
        import traceback
        server_errors = []
        def capture_snapshot_error(sender, request, **kwargs):
            if request.path == '/api/offline/snapshot/':
                server_errors.append(traceback.format_exc())
        if connection.vendor == 'sqlite':
            self.assertFalse(connection.is_in_memory_db())
            self.assertEqual(self.server_thread.connections_override, {})
        user, _, task, _ = fixtures()
        sale = Venda.objects.create(data_venda=task.data_agendada, cliente=Cliente.objects.create(nome='Retomada'))
        route = EntregaRota.objects.create(pk=136, tipo='unitaria')
        EntregaRotaItem.objects.create(rota=route, venda=sale)
        client = Client(); client.force_login(user)
        # Drive the actual registered interval callbacks, not app.probe(), with a shared clock.
        clock = """const realWall=Date.now;Date.now=()=>realWall()+Number(localStorage.getItem('resume-offset')||0);
            window.monitorCallbacks=new Map();let timerSequence=0;
            window.setInterval=(callback,delay)=>{const id=++timerSequence;monitorCallbacks.set(id,{callback,delay});return id};
            window.clearInterval=id=>monitorCallbacks.delete(id);"""
        setup = """const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());
            window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;
            window.scope={actor_id:document.getElementById('offline-global').dataset.actor,environment_id:document.getElementById('offline-global').dataset.environment};
            window.realFetch=fetch;window.healthCalls=0;
            window.fetch=(...args)=>{if(args[0]==='/api/offline/health/')healthCalls++;return realFetch(...args)};true"""
        with TemporaryDirectory(prefix='offline-checklist-resume-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                got_request_exception.connect(capture_snapshot_error, weak=False)
                def load(path):
                    tab = chrome.tab()
                    tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': clock})
                    tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                    tab.call('Page.navigate', {'url':self.live_server_url+path})
                    tab.wait("!!document.getElementById('offline-global')")
                    tab.evaluate(setup)
                    return tab
                checklist = load('/entregas/136/checklist/')
                try:
                    checklist.wait("!!document.querySelector('[data-local-note]')")
                except AssertionError:
                    self.fail(str({'server_errors':server_errors, 'browser':checklist.evaluate("({path:location.pathname,panels:[...document.querySelectorAll('[data-checklist-task]')].map(p=>({data:{...p.dataset},text:p.textContent})),state:Object.fromEntries([...document.getElementById('offline-global').attributes].map(a=>[a.name,a.value])),stores:await testRepo.all('snapshots'),session:await realFetch('/api/offline/session/').then(r=>r.json())})")}))
                checklist.evaluate("document.querySelector('[data-local-note] textarea').value='Retomar contador';document.querySelector('[data-local-note]').requestSubmit()")
                checklist.wait("(await testRepo.all('operations')).length===1")
                original = checklist.evaluate("JSON.stringify(await testRepo.all('operations'))")
                def state(tab): return tab.evaluate('(await testRepo.communication(scope))')
                def step(tab, amount=30000):
                    # Drain any startup/resume probe before advancing the virtual
                    # clock; the registered monitor still drives the observed step.
                    tab.evaluate('await app.probe();true')
                    before = state(tab)['last_success_at']
                    tab.evaluate(f"localStorage.setItem('resume-offset',Number(localStorage.getItem('resume-offset')||0)+{amount});await Promise.all([...monitorCallbacks.values()].filter(timer=>timer.delay===30000).map(timer=>timer.callback()));true")
                    tab.wait(f"(await testRepo.communication(scope)).last_success_at>{before}")
                    tab.wait("document.querySelector('.offline-status-counter')?.textContent.trim()===" + repr(f"{state(tab)['observed_ms']//60000} de 15 minutos"))
                for _ in range(8): step(checklist)
                self.assertEqual(state(checklist)['observed_ms']//60000, 4)
                # Suspend an old check at transport completion. Restoration must issue a fresh
                # check immediately, even if the old promise has not settled yet.
                if suspend_probe:
                    checklist.evaluate("""window.fetch=(...args)=>args[0]==='/api/offline/health/'?
                        new Promise((resolve,reject)=>window.releaseOldHealth=()=>reject(Error('old suspended check'))):realFetch(...args);
                        window.oldProbe=app.probe();true""")
                    checklist.wait('!!window.releaseOldHealth')
                checklist.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));true")
                paused = state(checklist)
                sales = load('/vendas/')
                sales.call('Page.bringToFront')
                sales.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));true")
                # Model suspension: no monitor callback is delivered during the gap.
                checklist.evaluate("localStorage.setItem('resume-offset',Number(localStorage.getItem('resume-offset')||0)+600000);true")
                self.assertEqual(state(checklist)['observed_ms'], paused['observed_ms'])
                checklist.call('Page.bringToFront')
                calls_before_resume = checklist.evaluate('healthCalls')
                checklist.evaluate("window.fetch=(...args)=>{if(args[0]==='/api/offline/health/')healthCalls++;return realFetch(...args)};Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));true")
                checklist.wait(f'healthCalls>{calls_before_resume}')
                checklist.wait(f"(await testRepo.communication(scope)).last_success_at>{paused['last_success_at']}")
                checklist.evaluate('await app.probe();true')  # Drain the immediate lifecycle check.
                self.assertEqual(state(checklist)['observed_ms']//60000, 4)
                resumed = state(checklist)
                for minute in (5, 6):
                    step(checklist); step(checklist)
                    self.assertEqual(state(checklist)['observed_ms']//60000, minute)
                self.assertEqual(state(checklist)['stable_since'], paused['stable_since'])
                self.assertGreater(state(checklist)['observed_until'], resumed['last_success_at'])
                self.assertGreaterEqual(checklist.evaluate('healthCalls'), 5)
                # A late old failure cannot reset the new observation generation.
                if suspend_probe:
                    checklist.evaluate('releaseOldHealth();await oldProbe;true')
                self.assertEqual(state(checklist)['observed_ms']//60000, 6)
                sales.wait("document.querySelector('.offline-status-counter')?.textContent.trim()==='6 de 15 minutos'")
                checklist.evaluate("window.fetch=(...args)=>args[0]==='/api/offline/health/'?Promise.reject(Error('real failure')):realFetch(...args);for(const timer of monitorCallbacks.values())if(timer.delay===30000)timer.callback();true")
                checklist.evaluate("await app.probe();true")
                self.confirm_failed_probe(checklist)
                checklist.wait("(await testRepo.communication(scope)).observed_ms===0")
                self.assertEqual(checklist.evaluate("JSON.stringify(await testRepo.all('operations'))"), original)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                try:
                    chrome.stop()
                finally:
                    got_request_exception.disconnect(capture_snapshot_error)

    def test_multi_tab_observed_stability(self):
        from estoque.models import Venda, Cliente, EntregaRota, EntregaRotaItem
        user, _, task, _ = fixtures()
        sale = Venda.objects.create(data_venda=task.data_agendada, cliente=Cliente.objects.create(nome='Estabilidade'))
        route = EntregaRota.objects.create(pk=135, tipo='unitaria')
        EntregaRotaItem.objects.create(rota=route, venda=sale)
        client = Client(); client.force_login(user)
        clock = "const realWall=Date.now;Date.now=()=>realWall()+Number(localStorage.getItem('test-offset')||0);"
        setup = """const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());
            window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;
            window.scope={actor_id:document.getElementById('offline-global').dataset.actor,environment_id:document.getElementById('offline-global').dataset.environment};true"""
        with TemporaryDirectory(prefix='offline-multi-stability-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                def load(path):
                    t = chrome.tab()
                    t.call('Page.addScriptToEvaluateOnNewDocument', {'source': clock})
                    t.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                    t.call('Page.navigate', {'url':self.live_server_url+path})
                    t.wait("!!document.getElementById('offline-global')")
                    t.evaluate(setup)
                    return t
                first = load('/entregas/135/checklist/')
                try:
                    first.wait("!!document.querySelector('[data-local-note]')")
                except AssertionError:
                    self.fail(str(first.evaluate("({path:location.pathname,panels:[...document.querySelectorAll('[data-checklist-task]')].map(p=>({data:{...p.dataset},text:p.textContent})),state:document.getElementById('offline-global').dataset,stores:await testRepo.all('snapshots'),session:await fetch('/api/offline/session/').then(r=>r.json())})")))
                first.evaluate("document.querySelector('[data-local-note] textarea').value='Preservar pend?ncia';document.querySelector('[data-local-note]').requestSubmit()")
                first.wait("(await testRepo.all('operations')).length===1")
                original = first.evaluate("JSON.stringify(await testRepo.all('operations'))")
                def step(t, amount=30000):
                    if amount <= 60000:
                        t.evaluate('await app.probe();true')
                    t.evaluate(f"localStorage.setItem('test-offset',Number(localStorage.getItem('test-offset')||0)+{amount});await app.probe();true")
                for _ in range(10): step(first)
                def elapsed(t): return t.evaluate("(await testRepo.communication(scope)).observed_ms")
                baseline = elapsed(first)
                self.assertGreaterEqual(baseline, 300000)
                tabs = [first, load('/vendas/'), load('/caixa-banco/')]
                for t in tabs:
                    self.assertGreaterEqual(elapsed(t), baseline)
                    t.call('Page.bringToFront')
                    t.evaluate('await app.probe();true')
                    t.wait("document.querySelector('.offline-status-counter')?.textContent.trim()==='5 de 15 minutos'")
                # Deterministic hidden/restored lifecycle (headless Chrome has no desktop minimize button).
                first.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));true")
                tabs[1].evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                shared_before = elapsed(first)
                for _ in range(2):
                    step(tabs[1])
                    first.evaluate('await app.probe();true')
                self.assertGreaterEqual(elapsed(first)-shared_before, 60000)
                self.assertLess(elapsed(first)-shared_before, 62000)
                # All tabs without callbacks: an unobserved gap must remain uncredited.
                for t in tabs:
                    t.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));true")
                paused = elapsed(first)
                step(first, 3600000)
                self.assertEqual(elapsed(first), paused)
                for t in tabs:
                    t.evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                    self.assertLess(elapsed(t)-paused, 2000)
                tabs[2].call('Page.close')
                step(first)
                self.assertGreater(elapsed(first), paused)
                first.call('Page.reload');first.wait("!!document.getElementById('offline-global')");first.evaluate(setup)
                self.assertGreaterEqual(elapsed(first), paused)
                tabs[1].call('Page.close')
                reopened = load('/vendas/')
                self.assertGreaterEqual(elapsed(reopened), paused)
                first.call('Page.bringToFront')
                first.evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                # Overlapping successful checks must not multiply the elapsed time.
                self.assertTrue(first.evaluate("""const isolated={actor_id:'concurrency',environment_id:'isolated'};
                    await testRepo.communication(isolated,{type:'success',at:100,started_at:90});
                    await Promise.all([testRepo.communication(isolated,{type:'success',at:30100,started_at:30090,observed_since:100}),
                        testRepo.communication(isolated,{type:'success',at:30100,started_at:30090,observed_since:100})]);
                    const value=await testRepo.communication(isolated);
                    if(value.observed_ms!==30000)throw Error('double credit');
                    await testRepo.communication(isolated,{type:'success',at:45100,started_at:45090});
                    const continued=await testRepo.communication(isolated,{type:'success',at:60100,started_at:60090,observed_since:30100});
                    if(continued.observed_ms!==60000)throw Error('new tab discarded observed time');
                    const legacy={key:'communication:'+JSON.stringify(['legacy','legacy']),actor_id:'legacy',environment_id:'legacy',connected:true,stable_since:100,last_success_at:300100};
                    await testRepo.put('metadata',legacy);
                    const adopted=await testRepo.communication(legacy,{type:'success',at:400100,started_at:400090});
                    adopted.observed_ms===300000 && adopted.stable_since===100"""))
                # Drain the extra probe scheduled by visibility restoration before installing the barrier.
                first.evaluate("await app.probe();await app.probe();true")
                # A request interrupted by hiding the page must not publish a false failure.
                first.evaluate("""window.beforeSuspension=(await testRepo.communication(scope)).observed_ms;
                    window.pauseFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?new Promise((resolve,reject)=>window.finishSuspended=()=>reject(Error('suspended request'))):pauseFetch(u,o);
                    window.suspendedProbe=app.probe();true""")
                first.wait('!!window.finishSuspended')
                first.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));finishSuspended();await suspendedProbe;window.fetch=pauseFetch;true")
                self.assertEqual(first.evaluate('(await testRepo.communication(scope)).observed_ms'), first.evaluate('beforeSuspension'))
                first.evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                # Storage/render errors cannot masquerade as failed health checks.
                first.evaluate("window.originalCommunication=testRepo.constructor.prototype.communication;testRepo.constructor.prototype.communication=()=>Promise.reject(Error('storage'));try{await app.probe()}catch{};testRepo.constructor.prototype.communication=originalCommunication;true")
                self.assertGreaterEqual(elapsed(first), paused)
                first.evaluate("window.baseFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('real health failure')):baseFetch(u,o);await app.probe();true")
                self.confirm_failed_probe(first)
                for t in [first,reopened]:
                    t.wait("(await testRepo.communication(scope)).observed_ms===0")
                    t.wait("document.getElementById('offline-reset-log').textContent.includes('motivo: falha de comunica\u00e7\u00e3o')")
                first.evaluate('window.fetch=baseFetch;await app.probe();true')
                self.assertLess(elapsed(first), 2000)
                self.assertEqual(first.evaluate("JSON.stringify(await testRepo.all('operations'))"), original)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                print('Chrome: checklist 135, Vendas, Caixa/Banco, altern?ncia, suspens?o/retomada, fechamento, refresh/reabertura, falha real e fila preservada: OK', flush=True)
            finally:
                chrome.stop()

    def test_probe_completion_and_stale_session_response(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-probe-race-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name':'sessionid', 'value':client.cookies['sessionid'].value, 'url':self.live_server_url})
                self.load(tab)
                # Hold real response processing at explicit barriers, not elapsed-time guesses.
                tab.evaluate("""window.probeBaseFetch=fetch;window.holdHealth=true;window.holdSession=true;
                    window.fetch=async(...args)=>{const r=await probeBaseFetch(...args);
                        if(args[0]==='/api/offline/health/'&&holdHealth){holdHealth=false;await new Promise(resolve=>window.releaseHealth=resolve)}
                        if(args[0]==='/api/offline/session/'&&holdSession){holdSession=false;await new Promise(resolve=>window.releaseSession=resolve)}
                        return r};
                    window.probeCompleted=false;window.appModule=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);
                    window.firstProbe=appModule.probe();firstProbe.then(()=>window.probeCompleted=true);true""")
                tab.wait('!!window.releaseHealth')
                self.assertTrue(tab.evaluate('appModule.probe()===firstProbe'))
                self.assertFalse(tab.evaluate('probeCompleted'))
                tab.evaluate('releaseHealth();true')
                tab.wait('!!window.releaseSession')
                self.assertFalse(tab.evaluate('probeCompleted'))
                tab.evaluate("""window.holdLatest=true;window.fetch=async(...args)=>{
                    if(args[0]==='/api/offline/session/'&&holdLatest){holdLatest=false;await new Promise(resolve=>window.releaseLatest=resolve)}
                    return probeBaseFetch(...args)};
                    window.dispatchEvent(new Event('online'));true""")
                tab.wait('!!window.releaseLatest')
                tab.evaluate('releaseSession();await new Promise(resolve=>setTimeout(resolve,0));true')
                self.assertFalse(tab.evaluate('probeCompleted'))
                tab.evaluate('releaseLatest();await firstProbe;window.fetch=probeBaseFetch;true')
                self.assertTrue(tab.evaluate('probeCompleted'))
                self.assertTrue(tab.evaluate("document.getElementById('offline-session').textContent.includes('Autenticado como')"))
                # online starts a session request and a probe; complete the old failure last.
                tab.evaluate("""window.holdStale=true;window.staleReturned=false;
                    window.fetch=async(...args)=>{if(args[0]==='/api/offline/session/'&&holdStale){holdStale=false;
                        await new Promise(resolve=>window.releaseStale=resolve);window.staleReturned=true;throw Error('old session request failed')}
                        return probeBaseFetch(...args)};
                    window.dispatchEvent(new Event('online'));true""")
                tab.wait('!!window.releaseStale')
                tab.evaluate('await appModule.probe();true')
                self.assertTrue(tab.evaluate("document.getElementById('offline-session').textContent.includes('Autenticado como')"))
                tab.evaluate('releaseStale();await new Promise(resolve=>setTimeout(resolve,0));true')
                self.assertTrue(tab.evaluate('staleReturned'))
                self.assertTrue(tab.evaluate("document.getElementById('offline-session').textContent.includes('Autenticado como')"))
                tab.evaluate('window.fetch=probeBaseFetch;true')
            finally:
                chrome.stop()

    def test_real_delivery_and_pickup_routes(self):
        from estoque.models import Venda, Cliente, EntregaRota, EntregaRotaItem, EntregaChecklistItem, EventoVenda, Produto
        from locacoes.models import TarefaOperacionalLocacao, EventoLocacao, Locacao, MovimentoEstoqueLocacao
        real_fixture = os.environ.get('OFFLINE_REAL_ROUTE_FIXTURE')
        if real_fixture:
            from django.core.management import call_command
            call_command('loaddata', real_fixture, verbosity=0)
        user, rental, task, _ = fixtures()
        if real_fixture:
            route = EntregaRota.objects.get(pk=134)
            item = route.itens.exclude(status='cancelada').first()
            pickup = TarefaOperacionalLocacao.objects.get(pk=os.environ['OFFLINE_REAL_PICKUP'])
        else:
            sale = Venda.objects.create(data_venda=task.data_agendada, cliente=Cliente.objects.create(nome='Teste da rota'))
            route = EntregaRota.objects.create(pk=134, tipo='unitaria')
            item = EntregaRotaItem.objects.create(rota=route, venda=sale)
            rental.status = 'entregue'
            rental._permitir_alterar_status = True
            rental.save(update_fields=['status'])
            pickup = TarefaOperacionalLocacao.objects.create(locacao=rental, tipo='recolhimento', data_agendada=task.data_agendada)
        client = Client(); client.force_login(user)
        for path, template, entity, event_model in (
            ('/entregas/134/checklist/', 'estoque/entrega_checklist.html', item, EventoVenda),
            (f'/locacoes/tarefas-operacionais/{pickup.pk}/conferencia-recolhimento/', 'locacoes/conferencia_recolhimento.html', pickup, EventoLocacao),
        ):
            response = client.get(path)
            self.assertTemplateUsed(response, template)
            before = type(entity).objects.filter(pk=entity.pk).values().get()
            physical_before = {
                model._meta.label: list(model.objects.values())
                for model in (EntregaChecklistItem, Produto, Locacao, MovimentoEstoqueLocacao)
            }
            with TemporaryDirectory(prefix='offline-real-route-') as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = chrome.tab(); tab.call('Network.enable')
                    tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                    tab.call('Page.navigate', {'url': self.live_server_url + path})
                    try:
                        tab.wait("!!document.querySelector('[data-local-note]') && !!navigator.serviceWorker.controller")
                    except AssertionError:
                        self.fail(str(tab.evaluate("({panels:[...document.querySelectorAll('[data-checklist-task]')].map(p=>({data:{...p.dataset},text:p.textContent})),registrations:(await navigator.serviceWorker.getRegistrations()).map(r=>({active:r.active?.state,installing:r.installing?.state})),scripts:[...document.scripts].map(s=>s.src)})")))
                    try:
                        tab.wait("['online','waiting'].includes(document.getElementById('offline-status').dataset.state)")
                    except AssertionError:
                        self.fail(str(tab.evaluate("({path:location.pathname,status:document.getElementById('offline-status').outerHTML,body:document.body.innerText.slice(-1500)})")))
                    self.assertEqual(tab.evaluate("document.querySelectorAll('#offline-global').length"), 1)
                    self.network(tab, True)
                    tab.wait("document.getElementById('offline-status').dataset.state==='offline'", timeout=25)
                    tab.evaluate("document.querySelector('[data-local-note] textarea').value='Observação da rota real';document.querySelector('[data-local-note]').requestSubmit()")
                    tab.wait("document.querySelector('[data-local-note] [role=status]').textContent==='Salvo neste dispositivo'")
                    import json
                    from urllib.request import urlopen
                    from .browser_support import DevTools
                    worker = chrome.worker()
                    worker.call('Network.enable')
                    worker.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                    tab.call('Page.reload')
                    tab.wait("document.body.dataset.localChecklist==='true' && !!document.querySelector('[data-local-note]')")
                    tab.wait("document.querySelector('[data-checklist-task] summary').textContent.includes('1 pendência')")
                    self.assertEqual(tab.evaluate('location.pathname'), path)
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('form:not([data-local-note]) input,form:not([data-local-note]) select,form:not([data-local-note]) textarea,form:not([data-local-note]) button[type=submit]')].every(e=>e.disabled)"))
                    worker.call('Network.emulateNetworkConditions', {'offline': False, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                    self.network(tab, False)
                    tab.evaluate("window.dispatchEvent(new Event('online'))")
                    try:
                        tab.wait("document.getElementById('offline-status').dataset.state==='waiting'", timeout=40)
                    except AssertionError:
                        self.fail(str(tab.evaluate("({path:location.pathname,status:document.getElementById('offline-status').outerHTML,online:navigator.onLine,indicator:document.getElementById('offline-global').dataset,health:await fetch('/api/offline/health/').then(r=>r.json()),session:await fetch('/api/offline/session/').then(r=>r.json())})")))
                    tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")
                    self.clock(tab); self.advance_to_ready(tab)
                    tab.evaluate("document.getElementById('offline-global-sync').click()")
                    tab.wait("(await testRepo.all('operations'))[0].status==='confirmada'")
                    self.assertEqual(event_model.objects.filter(**({'tipo_evento':'observacao_offline'} if event_model is EventoVenda else {'tipo':'observacao_offline'})).count(), 1)
                    self.assertEqual(type(entity).objects.filter(pk=entity.pk).values().get(), before)
                    self.assertEqual({model._meta.label: list(model.objects.values()) for model in (EntregaChecklistItem, Produto, Locacao, MovimentoEstoqueLocacao)}, physical_before)
                    offset = tab.evaluate('clockOffset')
                    tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': f'const auditWall=Date.now;Date.now=()=>auditWall()+{offset};'})
                    tab.call('Page.reload')
                    try:
                        tab.wait("!!document.querySelector('[data-local-note]') && !document.body.dataset.localChecklist && document.getElementById('offline-status').dataset.state!=='checking' && !document.querySelector('[data-offline-blocked]')")
                    except AssertionError:
                        self.fail(str(tab.evaluate("({path:location.pathname,local:document.body.dataset.localChecklist,status:document.getElementById('offline-status').outerHTML,blocked:[...document.querySelectorAll('[data-offline-blocked]')].map(e=>e.outerHTML)})")))
                    if event_model is EventoVenda:
                        tab.wait("document.querySelectorAll('[data-synced-history] article').length===1")
                        self.assertEqual(tab.evaluate("document.querySelector('.check-offline-history-text').textContent"), 'Observação da rota real')
                        self.assertEqual(tab.evaluate("document.querySelector('[data-local-note] textarea').value"), '')
                        self.assertTrue(tab.evaluate("document.querySelector('[data-checklist-task] summary').textContent.startsWith('0 pendência')"))
                finally:
                    chrome.stop()
            print(f'Chrome OK: {path} — online, offline, observação local, refresh, pendência, reconexão e sincronização manual; dados físicos preservados.', flush=True)

    def test_delivery_synced_history_mobile_and_local_queue_independent(self):
        from estoque.models import Cliente, EntregaRota, EntregaRotaItem, EventoVenda, Venda
        user, _, task, _ = fixtures()
        sale = Venda.objects.create(data_venda=task.data_agendada, cliente=Cliente.objects.create(nome='Histórico mobile'))
        route = EntregaRota.objects.create(tipo='unitaria')
        item = EntregaRotaItem.objects.create(rota=route, venda=sale)
        text = 'Texto sincronizado: ' + 'observação' * 100 + '\nSegunda linha'
        EventoVenda.objects.create(venda=sale, tipo_evento='observacao_offline', canal='offline',
            usuario='Lincoln', descricao=f'Rota #{route.pk}, bloco #{item.pk}: {text}')
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-history-mobile-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + f'/entregas/{route.pk}/checklist/'})
                tab.wait("!!document.querySelector('[data-local-note]')")
                self.assertFalse(tab.evaluate("document.querySelector('[data-synced-history]').open"))
                self.assertEqual(tab.evaluate("document.querySelector('.check-offline-history-text').textContent"), text)
                self.assertTrue(tab.evaluate("document.querySelector('[data-checklist-task] summary').textContent.startsWith('0 pendência')"))
                tab.evaluate("document.querySelector('[data-local-note] textarea').value='Nova nota local independente';document.querySelector('[data-local-note]').requestSubmit()")
                tab.wait("document.querySelector('[data-checklist-task] summary').textContent.startsWith('1 pendência')")
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 600})
                    tab.evaluate("document.querySelector('[data-synced-history] summary').click()")
                    self.assertTrue(tab.evaluate("document.querySelector('[data-synced-history]').open"))
                    self.assertTrue(tab.evaluate("(()=>{const h=document.querySelector('[data-synced-history]'),r=h.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&h.scrollWidth<=h.clientWidth&&document.documentElement.scrollWidth<=innerWidth&&!h.querySelector('form,input,textarea,button')})()"))
                    self.assertEqual(tab.evaluate("document.querySelectorAll('[data-synced-history] article').length"), 1)
                    self.assertEqual(tab.evaluate("document.querySelector('[data-local-note] textarea').value"), '')
                    tab.evaluate("document.querySelector('[data-synced-history] summary').click()")
                    self.assertFalse(tab.evaluate("document.querySelector('[data-synced-history]').open"))
                self.assertEqual(EventoVenda.objects.filter(tipo_evento='observacao_offline').count(), 1)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                chrome.stop()

    def test_rental_checklist_local_note_refresh_and_manual_sync(self):
        user, rental, task, _ = fixtures()
        client = Client()
        client.force_login(user)
        path = f'/locacoes/tarefas-operacionais/{task.pk}/conferencia-entrega/'
        with TemporaryDirectory(prefix='offline-checklist-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': "window.checklistErrors=[];addEventListener('error',e=>checklistErrors.push(e.message));addEventListener('unhandledrejection',e=>checklistErrors.push(String(e.reason)))"})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + path})
                tab.wait("!!document.querySelector('[data-local-note]') && !!navigator.serviceWorker.controller")
                tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")
                tab.wait("document.getElementById('offline-status').dataset.state==='online'")
                self.assertTrue(tab.evaluate("!!(await testRepo.get('snapshots','checklist:'+location.pathname+location.search))"))
                self.network(tab, True)
                tab.wait("document.getElementById('offline-status').dataset.state==='offline'", timeout=25)
                tab.evaluate("document.querySelector('[data-local-note] textarea').value='Portão lateral';document.querySelector('[data-local-note]').requestSubmit()")
                tab.wait("document.querySelector('[data-local-note] [role=status]').textContent==='Salvo neste dispositivo'")
                original = tab.evaluate("(await testRepo.all('operations'))[0].operation_id")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                # Another prepared page can replace the pilot's task list.
                # This checklist must retain its own task snapshot.
                tab.evaluate("(async()=>{const s=await testRepo.get('snapshots','pilot');await testRepo.put('snapshots',{...s,tasks:[]});return true})()")
                # CDP page emulation does not disconnect the Service Worker target.
                import json
                from urllib.request import urlopen
                from .browser_support import DevTools
                worker = chrome.worker()
                worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Page.reload')
                try:
                    tab.wait("document.body.dataset.localChecklist==='true' && !!document.querySelector('[data-local-note]')")
                except AssertionError:
                    self.fail(str(tab.evaluate("({html:document.body.innerText,errors:window.checklistErrors,url:location.href})")))
                self.assertEqual(tab.evaluate('location.pathname'), path)
                tab.wait("document.querySelector('[data-checklist-task] summary').textContent.includes('1 pendência')")
                self.assertTrue(tab.evaluate("[...document.querySelectorAll('form:not([data-local-note]) input,form:not([data-local-note]) select,form:not([data-local-note]) textarea,form:not([data-local-note]) button[type=submit]')].every(b=>b.disabled)"))
                # Restart the browser process with the same persistent profile.
                chrome.stop()
                chrome = Chrome(CHROME, profile).start()
                tab = chrome.tab()
                tab.call('ServiceWorker.enable')
                tab.call('ServiceWorker.startWorker', {'scopeURL': self.live_server_url + '/'})
                worker = chrome.worker()
                worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Network.enable')
                tab.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Page.navigate', {'url': self.live_server_url + path})
                tab.wait("document.body.dataset.localChecklist==='true' && !!document.querySelector('[data-local-note]')")
                tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations'))[0].operation_id"), original)
                for width in (390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                    self.assertTrue(tab.evaluate("document.querySelector('[data-local-note] textarea').getBoundingClientRect().right<=innerWidth"))
                self.network(tab, False)
                worker.call('Network.emulateNetworkConditions', {'offline': False, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.evaluate("window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-status').dataset.state==='waiting'")
                self.assertTrue(tab.evaluate("document.getElementById('offline-global-sync').disabled"))
                self.clock(tab)
                self.advance_to_ready(tab)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.evaluate("document.getElementById('offline-global-sync').click()")
                tab.wait("(await testRepo.all('operations'))[0].status==='confirmada'")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                rental.refresh_from_db(); task.refresh_from_db()
                self.assertEqual(task.status, 'pendente')
                self.assertEqual(rental.status, 'reservada')
            finally:
                chrome.stop()

    def test_shared_state_visuals_on_pilot_and_sales(self):
        user, _, _, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='offline-state-visuals-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': "const visualWall=Date.now;const visualOffset=Number(localStorage.getItem('visual-offset')||0);Date.now=()=>visualWall()+visualOffset;"})
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                self.load(tab)
                tab.wait("document.getElementById('offline-status').dataset.state === 'online'")
                self.assert_state_visual(tab, 'online', 'rgb(240, 253, 244)')
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Visual sem envio')
                tab.wait("!!navigator.serviceWorker.controller")
                self.assertTrue(tab.evaluate("""(async()=>{
                    const keys=await caches.keys();
                    if(!keys.includes('offline-pilot-shell-v28-2-8f') || keys.includes('offline-pilot-shell-v21') || keys.includes('offline-pilot-shell-v20') || keys.includes('offline-pilot-shell-v19') || keys.includes('offline-pilot-shell-v18'))return false;
                    const cache=await caches.open('offline-pilot-shell-v28-2-8f');
                    for(const asset of ['app.js','indicator.css','pilot.css','presentation.js','commercial.js','commercial-ui.js'])
                        if(!(await cache.match('/static/offline/'+asset)))return false;
                    if(!(await cache.match('/offline/vendas-shell/')))return false;
                    return true;
                })()"""))
                for path in ('/offline/', '/vendas/'):
                    # Page.navigate returns before the previous document is replaced.
                    # Its indicator/clock must not satisfy readiness for the next page.
                    tab.evaluate('window.previousVisualDocument=true;true')
                    tab.call('Page.navigate', {'url': self.live_server_url + path})
                    tab.wait("!window.previousVisualDocument && !!document.getElementById('offline-status')?.dataset.presentation")
                    tab.evaluate("(async()=>{const app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;return true})()")
                    tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")
                    # Restore only the isolated fixture between the two pages.
                    tab.evaluate("(async()=>{const op=(await testRepo.all('operations'))[0];await testRepo.put('operations',{...op,status:'pendente'});return true})()")
                    self.network(tab, True)
                    tab.wait("document.getElementById('offline-status').dataset.state === 'offline'", timeout=25)
                    self.assert_state_visual(tab, 'offline', 'rgb(254, 226, 226)', '1 operação pendente')
                    self.confirm_failed_probe(tab)
                    self.network(tab, False)
                    client = Client(); client.force_login(user)
                    tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url,'httpOnly':True})
                    tab.evaluate("const app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.probe();true")
                    try:
                        tab.wait("document.getElementById('offline-status').dataset.state === 'waiting'")
                    except AssertionError:
                        self.fail(str(tab.evaluate("({path:location.pathname,now:Date.now(),hidden:document.hidden,state:document.getElementById('offline-status').dataset.state,session:await fetch('/api/offline/session/').then(r=>r.json()),metadata:await testRepo.all('metadata')})")))
                    self.assert_state_visual(tab, 'waiting', 'rgb(254, 243, 199)', '1 operação aguardando')
                    self.clock(tab)
                    # Advance in observed 30-second steps; gaps pause stability.
                    for _ in range(3):
                        self.advance_probe(tab)
                    tab.wait("document.querySelector('.offline-status-counter')?.textContent.includes('1 de 15 minutos')")
                    self.advance_to_ready(tab)
                    self.assert_state_visual(tab, 'ready', 'rgb(220, 252, 231)', '1 operação pronta para sincronizar')
                    button = 'offline-sync' if path == '/offline/' else 'offline-global-sync'
                    self.assertEqual(tab.evaluate(f"document.getElementById('{button}').textContent"), 'Sincronizar agora — 1 operação')
                    self.assertEqual(tab.evaluate(f"getComputedStyle(document.getElementById('{button}')).backgroundColor"), 'rgb(29, 78, 216)')
                    self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                    tab.evaluate("(async()=>{const op=(await testRepo.all('operations'))[0];await testRepo.put('operations',{...op,status:'confirmada'});window.dispatchEvent(new Event('online'));return true})()")
                    tab.wait("document.getElementById('offline-status').dataset.state === 'online'")
                    self.assert_state_visual(tab, 'online', 'rgb(240, 253, 244)')
                    tab.evaluate("localStorage.setItem('visual-offset', Number(localStorage.getItem('visual-offset')||0)+(window.clockOffset||0));true")
            finally:
                chrome.stop()

    def assert_state_visual(self, tab, state, color, text=None):
        global_tab = tab.evaluate("!!document.getElementById('offline-toggle')")
        if global_tab:
            tab.evaluate("document.querySelector('.offline-disclosure').open=true")
            if state == 'ready':
                color = 'rgb(29, 78, 216)'
            elif state == 'online':
                color = 'rgb(22, 163, 74)'
        for width in (1280, 390, 320):
            with self.subTest(state=state, width=width):
                tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 600})
                self.assertEqual(tab.evaluate("document.getElementById('offline-status').dataset.state"), state)
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle') || document.getElementById('offline-status')).backgroundColor"), color)
                self.assertTrue(tab.evaluate("(()=>{const r=document.getElementById('offline-status').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&document.documentElement.scrollWidth<=innerWidth})()"))
                if text:
                    self.assertIn(text, tab.evaluate("document.getElementById('offline-status').textContent"))
                if state == 'waiting':
                    self.assertEqual(tab.evaluate("document.querySelector('.offline-status-title').textContent"), 'Conexão restabelecida')
                    self.assertIn('Verificando estabilidade para sincronização segura', tab.evaluate("document.querySelector('.offline-status-detail').textContent"))
                    self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('.offline-status-counter')).fontWeight"), '750')

    def test_sales_and_offline_health_200_session_denied_without_snapshot(self):
        # /vendas/ is also accessible anonymously: its empty data-actor must
        # not discard a successful health check. Exercise the actual pages.
        with TemporaryDirectory(prefix='offline-sales-anonymous-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                for code in (401, 403):
                    tab = chrome.tab()
                    tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': f"""
                        window.testErrors=[]; window.healthResponses=[]; window.sessionResponses=[];
                        addEventListener('error',e=>testErrors.push(e.message));
                        addEventListener('unhandledrejection',e=>testErrors.push(String(e.reason)));
                        const originalFetch=window.fetch;
                        window.fetch=async(u,o)=>{{
                            if(u==='/api/offline/session/'){{sessionResponses.push({code});return new Response('{{}}',{{status:{code}}});}}
                            const response=await originalFetch(u,o);
                            if(u==='/api/offline/health/')healthResponses.push({{status:response.status,body:await response.clone().json()}});
                            return response;
                        }};
                    """})
                    states = []
                    for path in ('/vendas/', '/offline/'):
                        tab.call('Page.navigate', {'url': self.live_server_url + path})
                        tab.wait('healthResponses.length > 0 && sessionResponses.length > 0')
                        tab.wait("document.getElementById('offline-status')?.textContent.startsWith('ONLINE — autenticação necessária')")
                        self.assertEqual(tab.evaluate('testErrors'), [])
                        self.assertEqual(tab.evaluate('healthResponses[0]'), {
                            'status': 200, 'body': {'ok': True, 'environment': 'offline-isolated-tests', 'protocol_version': 1}})
                        self.assertEqual(tab.evaluate('sessionResponses[0]'), code)
                        self.assertTrue(tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');const r=new Repository(await openDB());return !(await r.get('snapshots','pilot')) && !(await r.all('operations')).length;})()"))
                        states.append(tab.evaluate("document.getElementById('offline-status').textContent"))
                        if path == '/vendas/':
                            self.assertEqual(tab.evaluate("document.getElementById('offline-global').dataset.state"), 'auth')
                            self.assertEqual(tab.evaluate("document.getElementById('offline-global').dataset.actor"), '')
                        # A cross-tab refresh must preserve the healthy projection.
                        tab.evaluate("window.testChannel=new BroadcastChannel('offline-pilot');testChannel.postMessage({type:'changed',actor:'other',environment:'other'});true")
                        tab.evaluate('await new Promise(r=>setTimeout(r,1100));true')
                        self.assertEqual(tab.evaluate("document.getElementById('offline-status').textContent"), states[-1])
                    self.assertEqual(states[0], states[1])
                    tab.call('Page.close')
            finally:
                chrome.stop()

    def test_health_and_authentication_are_independent(self):
        user, _, _, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='offline-classification-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                self.load(tab)
                tab.wait("document.getElementById('offline-status').textContent.startsWith('ONLINE — autenticação')")
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.evaluate("window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-status').dataset.state === 'online'")
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Preservar durante autenticação')
                operation = tab.evaluate("(await testRepo.all('operations'))[0]")
                self.clock(tab)
                self.advance_to_ready(tab)
                for code in (401, 403):
                    tab.evaluate(f"window.authFetch=window.authFetch||fetch;window.fetch=(u,o)=>u==='/api/offline/session/'?Promise.resolve(new Response('{{}}',{{status:{code}}})):authFetch(u,o);window.dispatchEvent(new Event('online'))")
                    tab.wait("document.getElementById('offline-status').textContent.startsWith('ONLINE — autenticação')")
                    self.assertTrue(tab.evaluate("document.getElementById('offline-sync').disabled"))
                    self.assertEqual(tab.evaluate("(await testRepo.all('operations'))[0]"), operation)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.evaluate("window.fetch=authFetch;window.dispatchEvent(new Event('online'));await (await import([...document.scripts].find(s=>s.src.includes('/app.js')).src)).probe();true")
                tab.wait("!document.getElementById('offline-sync').disabled")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations'))[0]"), operation)
                offset = tab.evaluate('window.clockOffset')
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': f'const actualNow=Date.now;Date.now=()=>actualNow()+{offset};'})
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'ready'")
                tab.evaluate("window.normalFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/session/'?Promise.resolve(new Response('{}',{status:401})):normalFetch(u,o);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'auth'")
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('network failure')):normalFetch(u,o);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'offline'", timeout=25)
                self.confirm_failed_probe(tab)
                tab.evaluate("window.fetch=normalFetch;window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'waiting'")
                self.load(tab)
                for width in (1366, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    if width == 1366:
                        self.assertTrue(tab.evaluate("document.getElementById('offline-save').getBoundingClientRect().top === document.getElementById('offline-sync').getBoundingClientRect().top"))
            finally:
                chrome.stop()

    def test_sales_shared_indicator_layout_and_reload(self):
        user, _, _, _ = fixtures()
        client = Client()
        client.force_login(user)
        response = client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'estoque/vendas_layout_teste.html')
        self.assertTemplateUsed(response, 'estoque/includes/offline_global.html')
        self.assertEqual(response.context['offline_environment_id'], 'offline-isolated-tests')
        with TemporaryDirectory(prefix='offline-sales-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': "window.testErrors=[];addEventListener('error',e=>testErrors.push(e.message));addEventListener('unhandledrejection',e=>testErrors.push(String(e.reason)));"})
                self.load(tab)
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Sales indicator test')
                tab.wait("document.getElementById('offline-status').textContent.includes(' de 15 minutos')")
                started = tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).stable_since")
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'waiting'")
                self.assertEqual(tab.evaluate('testErrors'), [])
                self.assertEqual(tab.evaluate("document.querySelectorAll('#offline-global').length"), 1)
                for width in (1366, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    self.assertTrue(tab.evaluate("(()=>{const i=document.getElementById('offline-global').getBoundingClientRect();return i.height>0 && i.height<=80 && i.top>=0 && getComputedStyle(document.getElementById('offline-global')).position==='fixed' && document.documentElement.scrollWidth<=innerWidth})()"))
                    self.assertTrue(tab.evaluate("performance.getEntriesByType('resource').some(e=>new URL(e.name).pathname.endsWith('/offline/indicator.css')) && performance.getEntriesByType('resource').some(e=>new URL(e.name).pathname.endsWith('/app.js'))"))
                    if width == 1366:
                        self.assertTrue(tab.evaluate("document.getElementById('layout-vendas').getBoundingClientRect().bottom <= innerHeight + 1"))
                tab.call('Page.reload')
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'waiting'")
                self.assertEqual(tab.evaluate('testErrors'), [])
                tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")
                self.assertEqual(tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).stable_since"), started)
                self.network(tab, True)
                tab.wait("document.getElementById('offline-global').dataset.state === 'offline'", timeout=25)
                self.network(tab, False)
                tab.wait("document.getElementById('offline-global').dataset.state === 'waiting'")
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                self.ready(tab)
                tab.wait("document.getElementById('offline-status').textContent.includes(' de 15 minutos')")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).length"), 1)
            finally:
                chrome.stop()

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
                # Preparation changes the scope and installs the worker. Drain its
                # probe before advancing time; a generic connected row may still
                # belong to the anonymous scope used before preparation.
                tab.evaluate("window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;await app.probe();true")
                tab.wait("(await testRepo.all('metadata')).some(v=>v.key.startsWith('communication:') && v.connected)")
                def step(milliseconds=30000, drain=True):
                    # Await the complete probe instead of guessing a 100ms delay.
                    if drain:
                        tab.evaluate("window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js'))?.src || '/offline/assets/2-8f/app.js');await app.initialized;await app.probe();true")
                    tab.evaluate(f"localStorage.setItem('test-offset',Number(localStorage.getItem('test-offset')||0)+{milliseconds});window.dispatchEvent(new Event('online'));await app.probe();true")
                    tab.wait("(await testRepo.all('metadata')).some(v=>v.key.startsWith('communication:') && v.last_success_at > Date.now()-2000)")
                def minutes(target):
                    tab.wait(f"document.querySelector('.offline-status-counter')?.textContent.trim() === '{target} de 15 minutos'")
                for _ in range(16):
                    step()
                minutes(8)
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait("document.querySelector('.offline-status-counter')?.textContent.trim() === '8 de 15 minutos'")
                tab.call('Page.reload')
                tab.wait("document.querySelector('.offline-status-counter')?.textContent.trim() === '8 de 15 minutos'")
                second = chrome.tab()
                second.call('Page.addScriptToEvaluateOnNewDocument', {'source': script})
                self.load(second)
                second.wait("document.querySelector('.offline-status-counter')?.textContent.trim() === '8 de 15 minutos'")
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
                tab.evaluate('await app.probe();true')
                before_gap = tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).observed_ms")
                step(61000, drain=False)
                self.assertEqual(tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).observed_ms"), before_gap)
                step()
                tab.evaluate('await app.probe();true')
                before_gap = tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).observed_ms")
                step(3600000, drain=False)
                self.assertEqual(tab.evaluate("(await testRepo.all('metadata')).find(v=>v.key.startsWith('communication:')).observed_ms"), before_gap)
                tab.evaluate("window.savedFetch=fetch;window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('failure')):savedFetch(u,o);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-status').dataset.state === 'offline'", timeout=25)
                self.confirm_failed_probe(tab)
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
                tab.wait("document.getElementById('offline-message').textContent.includes('Dados preparados')")
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
                tab.wait("document.getElementById('offline-message').textContent.includes('Dados preparados')")
                tab.wait("!!navigator.serviceWorker.controller")
                self.assertTrue(tab.evaluate("!!(await navigator.serviceWorker.ready).active"))
                # Core invariants with an injected clock, using production policy unchanged.
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Stability,validHealth,validReceipt}=await import('/offline/assets/2-8f/core.js');
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
                # A failed IDB commit must not announce success.
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Repository}=await import('/offline/assets/2-8f/core.js');
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
                tab.wait("!(await testRepo.all('metadata')).some(v=>v.key.startsWith('communication:') && v.suspect_id)")
                # Reauthenticate after reconnection and its old requests have completed.
                # Do not depend on restoration of the pre-restart session cookie.
                client = Client(); client.force_login(user)
                session_cookie = client.cookies['sessionid'].value
                tab.call('Network.setCookie', {'name':'sessionid', 'value':session_cookie, 'url':self.live_server_url, 'httpOnly':True})
                tab.evaluate("(async()=>{const app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.probe();return true})()")
                # The quota-error notice deliberately remains visible for 10 seconds.
                # Assert completed connectivity/authentication, not its transient UI priority.
                self.assertTrue(tab.evaluate("(async()=>{const s=await testRepo.get('snapshots','pilot');return (await testRepo.communication({actor_id:s.actor.id,environment_id:s.environment_id})).connected})()"))
                if not tab.evaluate("document.getElementById('offline-session').textContent.includes('Autenticado como')"):
                    from django.contrib.sessions.models import Session
                    cookies = tab.call('Network.getCookies', {'urls':[self.live_server_url]})['cookies']
                    details = tab.evaluate("(async()=>{const response=await fetch('/api/offline/session/');return {sessionStatus:response.status,session:await response.json(),ui:document.getElementById('offline-session').textContent,state:document.getElementById('offline-status').dataset.state,online:navigator.onLine}})()")
                    details.update(cookie_matches=any(c['name']=='sessionid' and c['value']==session_cookie for c in cookies),
                        session_cookie_count=sum(c['name']=='sessionid' for c in cookies),
                        server_session_exists=Session.objects.filter(session_key=session_cookie).exists())
                    self.fail(str(details))
                self.assertTrue(tab.evaluate("document.getElementById('offline-sync').disabled"))
                self.clock(tab)
                self.advance_to_ready(tab)
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)  # No automatic synchronization.
                self.assertFalse(tab.evaluate("document.getElementById('offline-sync').disabled"))
                # Second tab holds the lock: clicking cannot start another sender.
                second = chrome.tab()
                self.load(second)
                second.evaluate("window.lockHeld=false; navigator.locks.request('offline-pilot-sync',async()=>{window.lockHeld=true;await new Promise(r=>window.releaseLock=r)}); true")
                second.wait('window.lockHeld')
                tab.evaluate("window.autoConfirmObserver?.disconnect();window.autoConfirmObserver=new MutationObserver(()=>{const b=document.getElementById('offline-modal-confirm');if(document.getElementById('offline-sync-modal')?.open&&!b.disabled&&!b.hidden)b.click()});autoConfirmObserver.observe(document.body,{subtree:true,attributes:true});document.getElementById('offline-sync').click()")
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
                tab.wait("document.getElementById('offline-modal-text').textContent==='Sincronização interrompida — operações preservadas'")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).filter(o=>o.status==='pendente').length"), 1)
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).find(o=>o.status==='resultado_desconhecido').operation_id"), original_id)
                # A lost send response with a successful health-check preserves stability.
                self.assertFalse(tab.evaluate("document.getElementById('offline-sync').disabled"))
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
                tab.evaluate("window.dispatchEvent(new Event('online'))")
                tab.wait("!document.getElementById('offline-sync').disabled")
                # Missing task produces retained conflict.
                task.delete()
                tab.evaluate("document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-operations').textContent.includes('conflito')")
                tab.wait("document.getElementById('offline-modal-text').textContent==='Conflito de sincronização — revisão necessária'")
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
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).color"), 'rgb(255, 255, 255)')
                self.assertTrue(tab.evaluate("document.getElementById('offline-pending-badge').hidden"))
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 600})
                    tab.evaluate("document.getElementById('offline-toggle').click()")
                    self.assertTrue(tab.evaluate("document.querySelector('.offline-disclosure').open"))
                    self.assertTrue(tab.evaluate("(()=>{const r=document.querySelector('.offline-panel').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&!document.querySelector('dialog[open]')})()"))
                    tab.evaluate("document.getElementById('offline-toggle').click()")
                    self.assertFalse(tab.evaluate("document.querySelector('.offline-disclosure').open"))
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {Stability,indicatorState}=await import('/offline/assets/2-8f/core.js');
                    const s=new Stability(); const op={status:'pendente'};
                    const state=(ops=[op], extra={})=>indicatorState({operations:ops,stability:s,now:s.last||0,wall:s.lastWall||0,...extra});
                    if(state().kind!=='checking'||state().count!==1)throw Error('unverified count');
                    s.success(0,0);if(state().kind!=='waiting'||state().canSync)throw Error('reconnect');
                    for(let t=30000;t<=900000;t+=30000)s.success(t,t);
                    if(!state().canSync||state().kind!=='ready')throw Error('ready');
                    if(state([{status:'conflito'}]).kind!=='conflict')throw Error('conflict');
                    if(state([{status:'confirmada'}]).count!==0)throw Error('confirmed');
                    if(state([], {notice:{kind:'success',label:'ok',until:900001}}).kind!=='success')throw Error('success');
                    if(state([op],{syncing:true,progress:'1/3'}).kind!=='syncing')throw Error('progress');
                    s.reset();if(state().canSync||state().kind!=='checking')throw Error('reset');
                    return true;
                })()"""))
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                self.assertTrue(tab.evaluate("document.getElementById('offline-global').getBoundingClientRect().right <= innerWidth"))
                # Real failed health request on a normal page, without navigator.onLine.
                tab.evaluate("window.originalFetch=fetch;window.fetch=(url,opts)=>url==='/api/offline/health/'?Promise.reject(Error('server down')):originalFetch(url,opts);window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'offline'", timeout=25)
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).color"), 'rgb(153, 27, 27)')
                tab.evaluate("window.fetch=originalFetch;window.dispatchEvent(new Event('online'))")
                tab.wait("document.getElementById('offline-global').dataset.state === 'waiting'")
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).color"), 'rgb(120, 53, 15)')
                self.load(tab)
                tab.evaluate("document.getElementById('offline-prepare').click()")
                tab.wait("document.getElementById('offline-task').options.length === 1")
                self.save(tab, 'Global manual sync')
                tab.call('Page.navigate', {'url': self.live_server_url + '/'})
                tab.wait("document.getElementById('offline-global')?.dataset.state === 'waiting'")
                self.assertIn('1 ', tab.evaluate("document.getElementById('offline-status').textContent"))
                self.clock(tab)
                self.advance_to_ready(tab)
                self.assertFalse(tab.evaluate("document.querySelector('.offline-disclosure').open"))
                self.assertEqual(tab.evaluate("document.getElementById('offline-pending-badge').textContent"), '1')
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).backgroundColor"), 'rgb(29, 78, 216)')
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.evaluate("document.getElementById('offline-toggle').click()")
                tab.evaluate("document.getElementById('offline-global-sync').click()")
                tab.wait("document.getElementById('offline-global').dataset.state === 'success'")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
                self.assertTrue(tab.evaluate("document.getElementById('offline-global-sync').hidden"))
            finally:
                chrome.stop()

    def test_offline_ux_busy_cancel_progress_and_mobile(self):
        user, _, _, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='offline-ux-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                self.load(tab)
                tab.evaluate("window.baseFetch=fetch;window.snapshotCalls=0;window.fetch=async(u,o)=>{if(u==='/api/offline/snapshot/'){snapshotCalls++;await new Promise(r=>window.releasePrepare=r)}return baseFetch(u,o)};const b=document.getElementById('offline-prepare');b.click();b.dispatchEvent(new Event('click'));window.prepareImmediate={disabled:b.disabled,text:b.textContent}")
                self.assertEqual(tab.evaluate('prepareImmediate'), {'disabled': True, 'text': 'Preparando...'})
                tab.wait('!!window.releasePrepare')
                tab.evaluate('window.releasePrepare();true')
                tab.wait("!document.getElementById('offline-prepare').disabled")
                self.assertEqual(tab.evaluate('snapshotCalls'), 1)
                self.assertIn('Dados preparados', tab.evaluate("document.getElementById('offline-message').textContent"))
                self.assertEqual(tab.evaluate("document.getElementById('offline-task').options.length"), 1)
                tab.evaluate("window.fetch=baseFetch;window.originalCreate=testRepo.constructor.prototype.create;window.createCalls=0;testRepo.constructor.prototype.create=async function(...a){createCalls++;await new Promise(r=>window.releaseSave=r);return originalCreate.apply(this,a)};document.getElementById('offline-note').value='Uma operação';document.getElementById('offline-form').requestSubmit();document.getElementById('offline-form').dispatchEvent(new Event('submit',{cancelable:true}));window.saveImmediate={disabled:document.getElementById('offline-save').disabled,text:document.getElementById('offline-save').textContent}")
                self.assertEqual(tab.evaluate('saveImmediate'), {'disabled': True, 'text': 'Salvando...'})
                self.assertEqual(tab.evaluate('createCalls'), 1)
                self.assertEqual(tab.evaluate("document.getElementById('offline-note').value"), 'Uma operação')
                tab.evaluate('window.releaseSave();true')
                tab.wait("!document.getElementById('offline-save').disabled")
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).length"), 1)
                self.assertEqual(tab.evaluate("document.getElementById('offline-note').value"), '')
                tab.evaluate("testRepo.constructor.prototype.create=async()=>{throw Error('quota simulation')};document.getElementById('offline-note').value='Preservar texto';document.getElementById('offline-form').requestSubmit()")
                tab.wait("!document.getElementById('offline-save').disabled")
                self.assertIn('Não foi possível salvar', tab.evaluate("document.getElementById('offline-message').textContent"))
                self.assertEqual(tab.evaluate("document.getElementById('offline-note').value"), 'Preservar texto')
                self.assertEqual(tab.evaluate("(await testRepo.all('operations')).length"), 1)
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/snapshot/'?Promise.reject(Error('network simulation')):baseFetch(u,o);document.getElementById('offline-prepare').click()")
                tab.wait("!document.getElementById('offline-prepare').disabled")
                self.assertIn('Não foi possível preparar', tab.evaluate("document.getElementById('offline-message').textContent"))
                tab.evaluate('window.fetch=baseFetch;testRepo.constructor.prototype.create=originalCreate;true')
                self.save(tab, 'Segunda operação')
                self.clock(tab)
                tab.evaluate('autoConfirmObserver.disconnect();true')
                tab.wait("document.getElementById('offline-status').dataset.state==='waiting'")
                self.assertIn('de 15 minutos', tab.evaluate("document.getElementById('offline-stability').textContent"))
                self.assertFalse(tab.evaluate("document.getElementById('offline-sync').classList.contains('offline-sync-ready')"))
                self.advance_to_ready(tab)
                self.assertTrue(tab.evaluate("document.getElementById('offline-sync').classList.contains('offline-sync-ready')"))
                tab.evaluate("window.confirm=window.alert=()=>{throw Error('native dialog')};document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-sync-modal').open")
                for width in (390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 640, 'deviceScaleFactor': 1, 'mobile': True})
                    self.assertTrue(tab.evaluate("(()=>{const r=document.getElementById('offline-sync-modal').getBoundingClientRect();const a=document.getElementById('offline-modal-confirm').getBoundingClientRect(),b=document.getElementById('offline-modal-cancel').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&(a.left>=b.right||a.top>=b.bottom)})()"))
                tab.evaluate("document.getElementById('offline-modal-cancel').click()")
                tab.wait("!document.getElementById('offline-sync').disabled")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                tab.evaluate("window.progressTexts=[];window.progressObserver=new MutationObserver(()=>progressTexts.push(document.getElementById('offline-modal-text').textContent));progressObserver.observe(document.getElementById('offline-modal-text'),{childList:true});document.getElementById('offline-sync').click()")
                tab.wait("document.getElementById('offline-sync-modal').open")
                tab.evaluate("document.getElementById('offline-modal-confirm').click();document.getElementById('offline-modal-confirm').click()")
                tab.wait("document.getElementById('offline-modal-text').textContent==='Sincronização concluída'")
                self.assertEqual(OperacaoSincronizacao.objects.count(), 2)
                self.assertIn('Sincronizando 1 de 2...', tab.evaluate('progressTexts'))
                self.assertIn('Sincronizando 2 de 2...', tab.evaluate('progressTexts'))
                self.assertTrue(tab.evaluate("document.getElementById('offline-sync').disabled"))
                self.assertEqual(tab.evaluate("document.getElementById('offline-status').dataset.state"), 'success')
            finally:
                chrome.stop()

    def ready(self, tab):
        tab.wait("!!document.getElementById('offline-device')?.textContent")
        tab.evaluate("(async()=>{const app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;return true})()")
        tab.evaluate("(async()=>{const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.testRepo=new Repository(await openDB());return true})()")

    def load(self, tab):
        tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
        self.ready(tab)

    def network(self, tab, offline):
        # A blank tab has no app yet; otherwise drain the complete probe first.
        tab.evaluate("(async()=>{if(document.getElementById('offline-status')){const app=await import([...document.scripts].find(s=>s.src.includes('/app.js'))?.src || '/offline/assets/2-8f/app.js');await app.initialized;await app.probe()}return true})()")
        tab.call('Network.emulateNetworkConditions', {'offline': offline, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
        tab.evaluate("window.dispatchEvent(new Event('offline'))" if offline else "window.dispatchEvent(new Event('online'))")
        # Drain the probe triggered by native/emulated events, then verify the new conditions.
        tab.evaluate("(async()=>{if(document.getElementById('offline-status')){const app=await import([...document.scripts].find(s=>s.src.includes('/app.js'))?.src || '/offline/assets/2-8f/app.js');await app.probe();await app.probe()}return true})()")

    def save(self, tab, text):
        import json
        tab.evaluate("document.getElementById('offline-message').textContent='';document.getElementById('offline-note').value=" + json.dumps(text) + ";document.getElementById('offline-form').requestSubmit()")
        tab.wait("document.getElementById('offline-message').textContent.includes('Salvo neste dispositivo')")

    def confirm_failed_probe(self, tab):
        tab.evaluate("""const app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;await app.probe();
            await new Promise(resolve=>setTimeout(resolve,5200));true""")

    def test_transient_suspicion_confirmation_and_stale_tabs(self):
        user, _, _, _ = fixtures()
        client = Client(); client.force_login(user)
        with TemporaryDirectory(prefix='offline-suspicion-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
                tab.wait("!!document.getElementById('offline-global')")
                tab.evaluate("""window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);await app.initialized;await app.probe();
                    const {Repository,openDB,Stability}=await import('/offline/assets/2-8f/core.js');
                    window.r=new Repository(await openDB());window.other=new Repository(await openDB());
                    window.scope={actor_id:document.getElementById('offline-global').dataset.actor,
                        environment_id:document.getElementById('offline-global').dataset.environment};
                    window.wall=Date.now;window.offset=0;Date.now=()=>wall()+offset;
                    window.baseFetch=fetch;true""")
                # All OK: 30 observed intervals complete the unchanged 15-minute window.
                for _ in range(26):
                    tab.evaluate('offset+=30000;await app.probe();true')
                baseline = tab.evaluate('(await r.communication(scope)).observed_ms')
                self.assertGreaterEqual(baseline, 780000)
                tab.evaluate("window.fetch=(u,o)=>u==='/api/offline/health/'?Promise.reject(Error('transient')):baseFetch(u,o);await app.probe();true")
                state = tab.evaluate('await r.communication(scope)')
                self.assertTrue(state['suspect_id'])
                self.assertEqual(state['observed_ms'], baseline)
                tab.wait("document.getElementById('offline-global-sync').disabled")
                self.assertTrue(tab.evaluate("""const v=await other.communication(scope,{type:'success',at:Date.now(),started_at:Date.now(),observed_since:Date.now()-30000});
                    v.suspect_id!==null && v.observed_ms===""" + str(baseline)))
                tab.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));offset+=30000;true")
                self.assertEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), baseline)
                tab.evaluate("window.fetch=baseFetch;Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));await app.probe();true")
                self.assertEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), baseline)
                self.assertFalse(tab.evaluate('(await r.communication(scope)).suspect_id'))
                tab.evaluate('offset+=30000;await app.probe();true')
                self.assertGreaterEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), baseline+30000)
                for _ in range(3): tab.evaluate('offset+=30000;await app.probe();true')
                self.assertGreaterEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), 900000)
                # Actual timer runs a confirmation about five seconds after the first failure.
                tab.evaluate("window.calls=0;window.fetch=(u,o)=>u==='/api/offline/health/'?(++calls===1?Promise.reject(Error('transient')):baseFetch(u,o)):baseFetch(u,o);await app.probe();true")
                tab.wait('(await r.communication(scope)).suspect_id')
                self.assertFalse(tab.evaluate("const {Stability}=await import('/offline/assets/2-8f/core.js');const gate=new Stability();gate.restore(await r.communication(scope),performance.now(),Date.now());gate.ready(performance.now(),Date.now())"))
                tab.wait('calls===2 && !(await r.communication(scope)).suspect_id', timeout=9)
                self.assertGreaterEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), 900000)
                self.assertTrue(tab.evaluate("gate.restore(await r.communication(scope),performance.now(),Date.now());gate.ready(performance.now(),Date.now())"))
                # The success marker is persisted before session/render finish.
                # Drain that healthy probe so the first call below cannot join it.
                tab.evaluate("await app.probe();window.persistentCalls=0;window.fetch=(u,o)=>u==='/api/offline/health/'?(++persistentCalls,Promise.reject(Error('persistent'))):baseFetch(u,o);await app.probe();window.oldId=(await r.communication(scope)).suspect_id;offset+=5001;await app.probe();true")
                self.assertEqual(tab.evaluate('persistentCalls'), 2)
                self.assertEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), 0,
                    tab.evaluate('({persistentCalls,oldId,state:await r.communication(scope)})'))
                tab.evaluate('offset+=30000;await app.probe();true')
                self.assertTrue(tab.evaluate('(await r.communication(scope)).suspect_id!==oldId'))
                self.assertTrue(tab.evaluate("""const v=await other.communication(scope,{type:'confirmation',suspect_id:oldId,ok:true,at:Date.now(),started_at:Date.now()});!!v.suspect_id && v.observed_ms===0"""))
                tab.evaluate('window.fetch=baseFetch;offset+=5001;await app.probe();true')
                self.assertEqual(tab.evaluate('(await r.communication(scope)).observed_ms'), 0)
            finally:
                chrome.stop()

    def clock(self, tab):
        tab.evaluate("(async()=>{const app=await import([...document.scripts].find(s=>s.src.includes('/app.js'))?.src || '/offline/assets/2-8f/app.js');await app.initialized;await app.probe();return true})()")
        tab.evaluate("window.clockOffset=0;window.realNow=performance.now.bind(performance);window.realWall=Date.now;performance.now=()=>realNow()+clockOffset;Date.now=()=>realWall()+clockOffset;window.autoConfirmObserver?.disconnect();window.autoConfirmObserver=new MutationObserver(()=>{const b=document.getElementById('offline-modal-confirm');if(document.getElementById('offline-sync-modal')?.open&&!b.disabled&&!b.hidden)b.click()});autoConfirmObserver.observe(document.body,{subtree:true,attributes:true})")

    def advance_to_ready(self, tab):
        # Each success is observed; no test shortcut exists in the production frontend.
        for _ in range(32):
            self.advance_probe(tab)
        tab.wait("!(document.getElementById('offline-sync') || document.getElementById('offline-global-sync')).disabled")

    def advance_probe(self, tab):
        # Advance only between completed probes, without replacing fetch or guessing delays.
        tab.evaluate("(async()=>{const app=await import([...document.scripts].find(s=>s.src.includes('/app.js'))?.src || '/offline/assets/2-8f/app.js');await app.probe();window.clockOffset+=30000;await app.probe();return true})()")
