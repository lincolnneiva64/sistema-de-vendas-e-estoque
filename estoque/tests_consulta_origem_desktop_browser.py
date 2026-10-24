"""Desktop tabs and provenance; original tables and approved mobile stay intact."""
import base64
import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import engines
from django.urls import reverse
from django.utils import timezone
from estoque import tests_consulta_listas_mobile as base
from estoque.models import Compra, ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome

ROOT=Path(__file__).resolve().parent.parent
EVIDENCE=ROOT/'evidencias-origem-desktop'
LISTS=ROOT/'estoque/templates/estoque/compras_listas_fornecedor.html'
PURCHASES=ROOT/'estoque/templates/estoque/compras_lista.html'

class ConsultaOrigemDesktopBrowserTests(StaticLiveServerTestCase):
    effects=base.ConsultaListasMobileTests.effects

    def setUp(self):
        base.ConsultaListasMobileTests.setUp(self)
        self.fk_only=Compra.objects.create(lista_fornecedor=self.rows[1],fornecedor=self.rows[1].fornecedor,
            data_compra=timezone.localdate(),status='rascunho',total=0)
        self.direct=Compra.objects.create(fornecedor=self.supplier,data_compra=timezone.localdate(),status='rascunho',total=0)

    def template(self,path,source):
        path.write_text(source,encoding='utf-8')
        for engine in engines.all():
            for loader in engine.engine.template_loaders:
                if hasattr(loader,'reset'):loader.reset()

    def open(self,browser,width,path):
        tab=AuditedChrome(browser).tab();tab.call('Network.enable')
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<=768})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Page.navigate',{'url':self.live_server_url+path})
        tab.wait('document.readyState==="complete" && !!document.querySelector(".listas-fornecedor-page, .compras-lista-page")',timeout=30)
        tab.evaluate('(async()=>{await document.fonts.ready;await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
        return tab

    def capture(self,tab,name):
        data=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
        (EVIDENCE/(name+'.png')).write_bytes(base64.b64decode(data))

    def signature(self,tab,selector,relative=False):
        return tab.evaluate(f'''(()=>{{const root=document.querySelector({selector!r});const p=root.getBoundingClientRect();return [...root.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().width>0).map(e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return [e.tagName,e.className,e.childElementCount?'':e.textContent,e.value||'',e.getAttribute('href'),r.x-({str(relative).lower()}?p.x:-scrollX),r.y-({str(relative).lower()}?p.y:-scrollY),r.width,r.height,[...s].sort().map(k=>[k,s.getPropertyValue(k)])]}})}})()''')

    def same(self,first,second,label):
        digests=[hashlib.sha256(json.dumps(value).encode()).hexdigest() for value in [first,second]]
        if digests[0]!=digests[1]:
            (EVIDENCE/(label+'-before.json')).write_text(json.dumps(first),encoding='utf-8')
            (EVIDENCE/(label+'-after.json')).write_text(json.dumps(second),encoding='utf-8')
        self.assertEqual(digests[0],digests[1],label)

    def test_desktop_tabs_original_table_and_purchase_origins(self):
        EVIDENCE.mkdir(exist_ok=True)
        current=LISTS.read_text(encoding='utf-8')
        original=subprocess.check_output(['git','show','320df4c:estoque/templates/estoque/compras_listas_fornecedor.html']).decode()
        before=self.effects();report=[]
        url=reverse('estoque:compras_listas_fornecedor')
        with TemporaryDirectory(prefix='desktop-origin-browser-') as profile:
            browser=Chrome(base.fixtures.CHROME,profile).start()
            try:
                for width in [1280,1440]:
                    tab=self.open(browser,width,url)
                    self.assertEqual(tab.evaluate('document.querySelector(".listas-fornecedor-abas-desktop [aria-current]").textContent'),'Listas ativas')
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-table tbody > tr").length'),1)
                    self.capture(tab,f'listas-{width}-ativas')
                    tab.evaluate('document.querySelectorAll(".listas-fornecedor-abas-desktop a")[1].click();true')
                    tab.wait('location.search.includes("aba=compras") && document.readyState==="complete"')
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-table tbody > tr").length'),4)
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-table .listas-fornecedor-btn-compra").length'),4)
                    self.assertEqual(tab.evaluate('document.querySelector("#status").options[0].textContent'),'Todos os status')
                    self.capture(tab,f'listas-{width}-viraram-compras')
                    tab.evaluate('document.querySelectorAll(".listas-fornecedor-abas-desktop a")[2].click();true')
                    tab.wait('location.search.includes("aba=todas") && document.readyState==="complete"')
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-table tbody > tr").length'),7)
                    self.capture(tab,f'listas-{width}-todas')
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    # Same rows, same table: the only added desktop UI is outside the table.
                    signatures=[]
                    path=url+'?aba=todas&status=aberta&fornecedor=Fornecedor+Total+Mobile'
                    for label,source in [('antes',original),('depois',current)]:
                        self.template(LISTS,source);tab=self.open(browser,width,path)
                        signatures.append(self.signature(tab,'.listas-fornecedor-table',relative=True))
                        self.capture(tab,f'tabela-{width}-{label}')
                    self.same(*signatures,f'tabela-desktop-{width}')
                    tab=self.open(browser,width,reverse('estoque:compras_lista'))
                    labels=tab.evaluate('[...document.querySelectorAll(".compras-lista-desktop .compras-lista-origem")].map(e=>e.textContent.trim())')
                    self.assertIn('Compra direta',labels)
                    self.assertIn(f'Gerada da Lista #{self.rows[1].pk}',labels)
                    self.assertEqual(len(labels),5)
                    self.capture(tab,f'compras-{width}-origens')
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    tab.evaluate('document.querySelector("a.compras-lista-origem").click();true')
                    tab.wait('location.pathname.includes("/ver/") && document.readyState==="complete"')
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    report.append({'width':width,'tabs_ok':True,'original_table_identical':True,'purchase_origins_ok':True})
                self.assertEqual(self.effects(),before)
            finally:
                self.template(LISTS,current);browser.stop()
        (EVIDENCE/'desktop-validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

    def test_mobile_both_consultations_match_published_templates(self):
        EVIDENCE.mkdir(exist_ok=True);before=self.effects();report=[]
        templates={path:path.read_text(encoding='utf-8') for path in [LISTS,PURCHASES]}
        with TemporaryDirectory(prefix='origin-mobile-regression-') as profile:
            browser=Chrome(base.fixtures.CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    for path,route,selector,label in [(LISTS,'compras_listas_fornecedor','.listas-fornecedor-page','listas'),(PURCHASES,'compras_lista','.compras-lista-page','compras')]:
                        modes=['','?mobile=historico','?mobile=canceladas','?aba=todas'] if path==LISTS else ['']
                        for mode in modes:
                            snapshots=[]
                            original=subprocess.check_output(['git','show','320df4c:'+path.relative_to(ROOT).as_posix()]).decode()
                            for source in [original,templates[path]]:
                                self.template(path,source)
                                tab=self.open(browser,width,reverse('estoque:'+route)+mode)
                                snapshots.append(self.signature(tab,selector))
                                self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                                self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            self.same(*snapshots,f'mobile-{label}-{width}-{mode.replace("?mobile=","").replace("?aba=","aba-") or "pendentes"}')
                            self.capture(tab,f'mobile-{label}-{width}-{mode.replace("?mobile=","").replace("?aba=","aba-") or "pendentes"}')
                    report.append({'width':width,'both_mobile_consultations_identical':True})
                self.assertEqual(self.effects(),before)
            finally:
                for path,source in templates.items():self.template(path,source)
                browser.stop()
        (EVIDENCE/'mobile-validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
