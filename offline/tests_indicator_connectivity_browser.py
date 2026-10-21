"""Real Chrome, isolated profiles and fictitious data; no synchronization POST."""
import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from .browser_support import Chrome
from .tests import fixtures
from .tests_browser import CHROME
from .models import OperacaoSincronizacao


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class IndicatorConnectivityBrowserTests(StaticLiveServerTestCase):
    def run_browser(self, width):
        user, _, task, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='indicador-conexao-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900,
                    deviceScaleFactor=1, mobile=width < 600))
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=client.cookies['sessionid'].value,
                    url=self.live_server_url))
                tab.call('Page.navigate', dict(url=self.live_server_url + '/'))
                tab.wait("document.getElementById('offline-global')?.dataset.state==='online'")
                tab.evaluate("""(async()=>{
                    window.app=await import([...document.scripts].find(s=>s.src.includes('/app.js')).src);
                    await app.initialized;
                    const c=await import('/offline/assets/2-8f/core.js');
                    window.repo=new c.Repository(await c.openDB());
                    window.g=document.getElementById('offline-global');
                    window.offset=0;const mono=performance.now.bind(performance),wall=Date.now;
                    performance.now=()=>mono()+offset;Date.now=()=>wall()+offset;
                    window.realFetch=fetch;window.healthFails=false;window.authFails=false;window.preparationFails=false;window.posts=[];
                    window.fetch=async(u,o)=>{
                        if(o?.method && o.method.toUpperCase()!=='GET')posts.push(String(u));
                        if(String(u)==='/api/offline/health/' && healthFails)throw TypeError('isolated transport failure');
                        const response=await realFetch(u,o);
                        if(String(u)==='/api/offline/session/' && (authFails||preparationFails)){
                            const data=await response.json();
                            return new Response(JSON.stringify({...data,authenticated:!authFails,can_prepare:false}),{status:200,headers:{'Content-Type':'application/json'}});
                        }
                        return response;
                    };
                    return true;
                })()""")
                tab.evaluate('await app.probe();true')
                self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'false')
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).animationName"), 'none')
                # A valid endpoint is not made offline by session/preparation problems.
                for flag in ('authFails', 'preparationFails'):
                    tab.evaluate(f'{flag}=true;await app.probe();true')
                    self.assertEqual(tab.evaluate('g.dataset.state'), 'online')
                    self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'true')
                    self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).animationName"), 'offline-action-pulse')
                    for _ in range(2):
                        tab.evaluate("document.getElementById('offline-toggle').click();true")
                        self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'true')
                    self.assertIn('autentique-se', tab.evaluate("document.getElementById('offline-status').textContent").lower())
                    tab.evaluate(f'{flag}=false;await app.probe();true')
                    self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'false')
                # A real local queue is preserved throughout health failures/recovery.
                tab.evaluate(f"window.op=await repo.create({{actor:{{id:g.dataset.actor}},environment_id:g.dataset.environment}},{{id:'{task.pk}',locacao_id:'{task.locacao_id}',status:'{task.status}'}},'Observação fictícia preservada');await app.probe();window.originalOperation=JSON.stringify(await repo.all('operations'));true")
                self.assertEqual(tab.evaluate('g.dataset.state'), 'online')
                self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'true')
                # Click uses the existing disclosure; opening never acknowledges the warning.
                tab.evaluate("document.getElementById('offline-toggle').click();true")
                self.assertTrue(tab.evaluate("document.querySelector('.offline-disclosure').open"))
                self.assertIn('Detalhes offline', tab.evaluate("document.getElementById('offline-status').textContent"))
                self.assertTrue(tab.evaluate("(()=>{const r=document.querySelector('.offline-panel').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&document.documentElement.scrollWidth<=innerWidth})()"))
                self.capture(tab, width)
                tab.evaluate("document.getElementById('offline-toggle').click();healthFails=true;await app.probe();true")
                self.assertEqual(tab.evaluate('g.dataset.state'), 'checking')
                self.assertEqual(tab.evaluate("document.getElementById('offline-tab-label').textContent"), 'Reconectando')
                tab.evaluate('offset+=5001;healthFails=false;await app.probe();true')
                self.assertEqual(tab.evaluate('g.dataset.state'), 'online')
                # navigator.onLine alone never turns failed health checks green.
                tab.evaluate("Object.defineProperty(navigator,'onLine',{value:true,configurable:true});healthFails=true;offset+=1;await app.probe();offset+=5001;await app.probe();offset+=5001;await app.probe();true")
                self.assertEqual(tab.evaluate('g.dataset.state'), 'offline')
                self.assertEqual(tab.evaluate("getComputedStyle(document.getElementById('offline-toggle')).color"), 'rgb(153, 27, 27)')
                tab.evaluate('healthFails=false;offset+=5001;await app.probe();true')
                self.assertEqual(tab.evaluate('g.dataset.state'), 'online')
                self.assertEqual(tab.evaluate('g.dataset.requiresAction'), 'true')
                self.assertTrue(tab.evaluate("originalOperation===JSON.stringify(await repo.all('operations'))"))
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
                # Same projector used by the app: all warnings remain separate from color.
                self.assertTrue(tab.evaluate("""(async()=>{
                    const {indicatorState,Stability}=await import('/offline/assets/2-8f/core.js');
                    const s=new Stability();s.success(100,100);s.connection={kind:'online',checked_at:100};
                    const state=(operations=[],extra={})=>indicatorState({operations,stability:s,now:100,wall:100,...extra});
                    for(const status of ['pendente','erro','resultado_desconhecido','conflito']){
                        const result=state([{status}]);
                        if(result.connectionKind!=='online'||!result.requiresAction||!result.actionDetails.length)throw Error(status);
                    }
                    if(state([{status:'confirmada'}]).requiresAction)throw Error('confirmed must stop pulse');
                    if(state([], {notice:{kind:'success',label:'ok',until:200}}).requiresAction)throw Error('success is not a pending action');
                    if(state([], {authenticated:false}).connectionKind!=='online')throw Error('authentication must not recolor');
                    if(state([], {wall:60101,now:60101}).connectionKind!=='checking')throw Error('stale health is not online');
                    return true;
                })()"""))
            finally:
                chrome.stop()

    def capture(self, tab, width):
        destination = os.environ.get('OFFLINE_INDICADOR_CAPTURAS')
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            image = tab.call('Page.captureScreenshot', dict(format='png', captureBeyondViewport=True))
            (folder / f'online-pendente-{width}.png').write_bytes(base64.b64decode(image['data']))

    def test_desktop_1440(self):
        self.run_browser(1440)

    def test_mobile_390(self):
        self.run_browser(390)

    def test_mobile_320(self):
        self.run_browser(320)
