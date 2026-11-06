"""Dashboard navigation only: real Chromium, synthetic user, disposable database."""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .tests_compra_mobile_console import AuditedChrome


CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class PainelFeedbackBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser(
            'painel-ficticio', 'painel@example.test', 'isolado'))
        profile = TemporaryDirectory(prefix='painel-feedback-', ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        self.browser = Chrome(CHROME, profile.name).start()
        self.addCleanup(self.browser.stop)

    def abrir(self, width=390):
        tab = AuditedChrome(self.browser).tab()
        tab.call('Network.enable')
        tab.call('Network.setBlockedURLs', {'urls':['https://*']})
        tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value,
                                          url=self.live_server_url))
        tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844,
                    deviceScaleFactor=1, mobile=width < 860))
        tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=width < 860, maxTouchPoints=1))
        tab.call('Page.navigate', dict(url=self.live_server_url + reverse('estoque:home')))
        tab.wait("document.readyState==='complete' && !!document.querySelector('.produtos-nav-grupos')")
        return tab

    def ponto(self, tab, selector):
        return tab.evaluate("(async()=>{const e=document.querySelector("+json.dumps(selector)+
            ");e.scrollIntoView({block:'center',behavior:'instant'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));"
            "const b=e.getBoundingClientRect();return{x:b.x+b.width/2-visualViewport.offsetLeft,y:b.y+b.height/2-visualViewport.offsetTop}})()")

    def tocar(self, tab, selector):
        point=self.ponto(tab,selector)
        tab.call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[point]))
        tab.call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))

    def capturar(self, tab, nome):
        folder=Path('artifacts/painel-feedback'); folder.mkdir(parents=True, exist_ok=True)
        (folder/(nome+'.png')).write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png'})['data']))

    def test_layout_destinos_e_pressao_cancelada_mobile_desktop(self):
        for width in [390,1440]:
            with self.subTest(width=width):
                tab=self.abrir(width)
                snapshot="""[...document.querySelectorAll('.produtos-nav-grupos a')].map(e=>{
                  const b=e.getBoundingClientRect(),s=getComputedStyle(e);
                  return [e.innerHTML,e.getAttribute('href'),b.x,b.y,b.width,b.height,
                    s.backgroundColor,s.color,s.fontSize,s.padding,s.borderRadius,s.boxShadow];})"""
                before=tab.evaluate(snapshot)
                tab.evaluate("window.estiloFeedback=[...document.querySelectorAll('style')].find(e=>e.textContent.includes('Same pressed feedback'));estiloFeedback.sheet.disabled=true;true")
                self.assertEqual(tab.evaluate(snapshot),before)
                tab.evaluate('estiloFeedback.sheet.disabled=false;true')
                point=self.ponto(tab,'#btnNovaListaFornecedorPainel')
                if width==390:
                    tab.call('Input.dispatchTouchEvent', dict(type='touchStart',touchPoints=[point]))
                else:
                    tab.call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',clickCount=1,**point))
                self.assertTrue(tab.evaluate("document.getElementById('btnNovaListaFornecedorPainel').classList.contains('is-pressed')"))
                self.assertNotEqual(tab.evaluate("getComputedStyle(document.getElementById('btnNovaListaFornecedorPainel')).transform"),'none')
                if width==390:
                    self.capturar(tab,'pressionado-mobile')
                    tab.call('Input.dispatchTouchEvent',dict(type='touchCancel',touchPoints=[]))
                else:
                    # Release outside the button: pressing alone must not start navigation.
                    tab.call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=1,y=1,button='none'))
                    tab.call('Input.dispatchMouseEvent',dict(type='mouseReleased',x=1,y=1,button='left',clickCount=1))
                tab.wait("!document.querySelector('.produtos-nav-grupos .is-pressed')")
                self.assertFalse(tab.evaluate("!!document.querySelector('.produtos-nav-grupos [aria-busy]')"))
                self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])

    def test_toque_navegacao_lenta_repeticao_e_voltar(self):
        from .urls import urlpatterns
        pattern=next(p for p in urlpatterns if p.name=='sugestao_compra_fornecedor')
        original=pattern.callback
        received=[]

        def lenta(request, *args, **kwargs):
            received.append(request.path)
            Event().wait(.8)
            return original(request,*args,**kwargs)

        tab=self.abrir()
        # Record before native navigation: touchEnd can acknowledge only after loading.
        tab.evaluate("""document.addEventListener('click',e=>{
          const l=e.target.closest('#btnNovaListaFornecedorPainel');
          if(!l||e.defaultPrevented||window.feedbackRegistrado)return;
          window.feedbackRegistrado=true;
          const duplicates=[1,2].map(()=>!l.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})));
          sessionStorage.setItem('painel-feedback',JSON.stringify({duplicates,
            loading:l.classList.contains('is-loading'),busy:l.getAttribute('aria-busy'),
            label:l.textContent.trim(),indicator:getComputedStyle(l,'::after').content,
            other:document.getElementById('btnVendasPainel').getAttribute('aria-busy')}));
        });true""")
        with patch.object(pattern,'callback',lenta):
            self.tocar(tab,'#btnNovaListaFornecedorPainel')
            tab.wait("location.pathname==="+json.dumps(reverse('estoque:sugestao_compra_fornecedor'))+" && document.readyState==='complete'")
            state=tab.evaluate("JSON.parse(sessionStorage.getItem('painel-feedback'))")
            self.assertEqual(state,dict(duplicates=[True,True],loading=True,busy='true',
                label='Nova Lista por Fornecedor',indicator='"Abrindo..."',other=None))
            self.assertEqual(len(received),1)
        tab.evaluate('history.back();true')
        tab.wait("location.pathname==="+json.dumps(reverse('estoque:home'))+" && document.readyState==='complete' && !!document.getElementById('btnNovaListaFornecedorPainel')")
        tab.wait("!document.querySelector('.produtos-nav-grupos [aria-busy], .produtos-nav-grupos .is-loading, .produtos-nav-grupos .is-pressed')")
        self.tocar(tab,'#btnNovaListaFornecedorPainel')
        tab.wait("location.pathname==="+json.dumps(reverse('estoque:sugestao_compra_fornecedor')))

    def test_clique_cancelado_modificadores_e_recuperacao_sem_navegar(self):
        tab=self.abrir()
        # Another handler can cancel a valid click after this component handles it.
        tab.evaluate("document.addEventListener('click',e=>e.preventDefault());true")
        tab.evaluate("document.getElementById('btnVendasPainel').dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true}));true")
        self.assertFalse(tab.evaluate("!!document.querySelector('.produtos-nav-grupos [aria-busy]')"))
        self.tocar(tab,'#btnVendasPainel')
        tab.wait("document.getElementById('btnVendasPainel').getAttribute('aria-busy')==='true'")
        self.capturar(tab,'abrindo-mobile')
        tab.wait("!document.querySelector('.produtos-nav-grupos [aria-busy]')",timeout=14)
        self.assertFalse(tab.evaluate("!!document.querySelector('.produtos-nav-grupos .is-loading, .produtos-nav-grupos .is-pressed')"))
        self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
