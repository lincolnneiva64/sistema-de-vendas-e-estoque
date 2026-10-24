"""Mobile-only consultation layout; real Chrome and guarded disposable PostgreSQL."""
import base64
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import engines
from django.urls import reverse
from django.utils import timezone
from estoque import tests_compra_mobile_total_browser as fixtures
from estoque.models import ListaCompraFornecedor, ItemListaCompraFornecedor, Fornecedor, Compra
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome

ROOT=Path(__file__).resolve().parent.parent
TEMPLATE=ROOT/'estoque/templates/estoque/compras_listas_fornecedor.html'
EVIDENCE=ROOT/'evidencias-consulta-mobile'

class ConsultaListasMobileTests(StaticLiveServerTestCase):
    effects=fixtures.CompraMobileTotalBrowserTests.effects

    def setUp(self):
        fixtures.CompraMobileTotalBrowserTests.setUp(self)
        self.rows=[]
        for status in ['aberta','enviada','finalizada','cancelada','rascunho','finalizacao_iniciada','compra_finalizada']:
            supplier=self.supplier
            if status=='enviada':
                supplier=Fornecedor.objects.create(nome='Distribuidora de Alimentos e Produtos de Limpeza Nome Extenso para Conferencia')
            row=ListaCompraFornecedor.objects.create(fornecedor=supplier, data_lista=timezone.localdate(),
                data_inicio_periodo=timezone.localdate(),data_fim_periodo=timezone.localdate(),
                total_lista=Decimal('1234567.89') if status=='enviada' else Decimal('250.50'),
                status=status if status in ['aberta','enviada','finalizada','cancelada'] else 'aberta')
            ItemListaCompraFornecedor.objects.create(lista=row,produto=self.products[0],quantidade_final=1,
                unidade='PCT',preco_compra=Decimal('250.50'),preco_unitario=Decimal('250.50'),total=Decimal('250.50'))
            if status in ['rascunho','finalizacao_iniciada','compra_finalizada']:
                Compra.objects.create(lista_fornecedor=row,fornecedor=supplier,data_compra=timezone.localdate(),
                    status='finalizada' if status=='compra_finalizada' else status,total=Decimal('250.50'),
                    observacao=f'Gerada a partir da Lista de Compras #{row.id}')
            self.rows.append(row)

    def template(self,source):
        TEMPLATE.write_text(source,encoding='utf-8')
        for engine in engines.all():
            for loader in engine.engine.template_loaders:
                if hasattr(loader,'reset'):loader.reset()

    def open(self,browser,width,mode=''):
        tab=AuditedChrome(browser).tab();tab.call('Network.enable')
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<=768})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        path=reverse('estoque:compras_listas_fornecedor')+mode
        tab.call('Page.navigate',{'url':self.live_server_url+path})
        tab.wait('document.readyState==="complete" && !!document.querySelector(".listas-fornecedor-mobile-card")',timeout=30)
        tab.evaluate('(async()=>{await document.fonts.ready;await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
        return tab

    def capture(self,tab,name,selector=None):
        args={'format':'png','captureBeyondViewport':True}
        if selector:
            tab.evaluate(f'document.querySelector({selector!r}).scrollIntoView({{block:"center",behavior:"instant"}});true')
            tab.evaluate('(async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
            args['captureBeyondViewport']=False
            args['clip']=tab.evaluate(f'''(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();return {{x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1}}}})()''')
        data=tab.call('Page.captureScreenshot',args)['data']
        (EVIDENCE/(name+'.png')).write_bytes(base64.b64decode(data))

    def actions(self,tab):
        return tab.evaluate('''[...document.querySelectorAll('.listas-fornecedor-mobile-card')].map(c=>[c.dataset.mobileListaId,[...c.querySelectorAll('a,button')].map(e=>[e.textContent.trim(),e.getAttribute('href'),e.target||'',e.closest('form')?.getAttribute('action')||'',e.closest('form')?.method||''])])''')

    def desktop(self,tab):
        return tab.evaluate('''[...document.querySelectorAll('.listas-fornecedor-page, .listas-fornecedor-page *')].filter(e=>e.getBoundingClientRect().width>0).map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return [e.tagName,e.className,e.childElementCount?'':e.textContent,e.getAttribute('href'),r.x+scrollX,r.y+scrollY,r.width,r.height,[...s].sort().map(k=>[k,s.getPropertyValue(k)])]})''')

    def test_visual_desktop_unchanged_and_mobile_cards_actions(self):
        EVIDENCE.mkdir(exist_ok=True)
        current=TEMPLATE.read_text(encoding='utf-8')
        original=subprocess.check_output(['git','show','9b117de:estoque/templates/estoque/compras_listas_fornecedor.html']).decode()
        before=self.effects();report=[]
        with TemporaryDirectory(prefix='consulta-mobile-chrome-') as profile:
            browser=Chrome(fixtures.CHROME,profile).start()
            try:
                for width in [1280,1440]:
                    for mode,name in [('', 'pendentes'),('?mobile=historico','historico'),('?mobile=canceladas','canceladas')]:
                        snapshots=[]
                        for label,source in [('antes',original),('depois',current)]:
                            self.template(source);tab=self.open(browser,width,mode)
                            snapshots.append(self.desktop(tab))
                            self.capture(tab,f'desktop-{width}-{name}-{label}')
                            self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                        for index, snapshot in enumerate(snapshots):
                            (EVIDENCE/f'desktop-{width}-{name}-{index}.json').write_text(json.dumps(snapshot),encoding='utf-8')
                        hashes=[hashlib.sha256(json.dumps(snapshot).encode()).hexdigest() for snapshot in snapshots]
                        self.assertEqual(hashes[0],hashes[1],f'All visible desktop DOM, computed styles and geometry: {width} {name}')
                        report.append({'width':width,'mode':name,'desktop_identical':True})
                for width in [320,360,390]:
                    for mode,name in [('', 'pendentes'),('?mobile=historico','historico'),('?mobile=canceladas','canceladas')]:
                        self.template(original);old=self.open(browser,width,mode);actions=self.actions(old)
                        old_heights=old.evaluate('[...document.querySelectorAll(".listas-fornecedor-mobile-card")].map(c=>c.getBoundingClientRect().height)')
                        self.template(current);tab=self.open(browser,width,mode)
                        self.assertEqual(self.actions(tab),actions)
                        if name=='historico':
                            self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-mobile-card.com-compra").length'),3)
                            self.assertEqual(tab.evaluate('document.querySelectorAll(".listas-fornecedor-mobile-acoes .listas-fornecedor-btn-compra").length'),3)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        metrics=tab.evaluate('''[...document.querySelectorAll('.listas-fornecedor-mobile-card')].map(c=>{
                            const top=c.querySelector('.listas-fornecedor-mobile-card-topo'),data=c.querySelector('.listas-fornecedor-mobile-data'),total=c.querySelector('.listas-fornecedor-mobile-valor'),period=c.querySelector('.listas-fornecedor-mobile-periodo');
                            const buttons=[...c.querySelectorAll('.listas-fornecedor-mobile-acoes a,.listas-fornecedor-mobile-acoes button')];
                            return {height:c.getBoundingClientRect().height,topFits:top.scrollWidth<=top.clientWidth,
                                dataTotalSameRow:data.getBoundingClientRect().top===total.getBoundingClientRect().top,
                                periodSingleLine:period.querySelector('dd').getBoundingClientRect().height<20,
                                buttons:buttons.map(b=>({text:b.textContent.trim(),top:b.getBoundingClientRect().top,width:b.getBoundingClientRect().width,height:b.getBoundingClientRect().height,fits:b.scrollWidth<=b.clientWidth}))};
                        })''')
                        for index,metric in enumerate(metrics):
                            self.assertTrue(metric['topFits']);self.assertTrue(metric['dataTotalSameRow']);self.assertTrue(metric['periodSingleLine'])
                            self.assertLess(metric['height'],old_heights[index])
                            self.assertTrue(all(b['height']>=44 and b['fits'] for b in metric['buttons']))
                            self.assertEqual(len({b['top'] for b in metric['buttons']}),2)
                            self.assertEqual(metric['buttons'][0]['top'],metric['buttons'][2]['top'])
                            if len(metric['buttons'])==6:self.assertEqual(metric['buttons'][3]['top'],metric['buttons'][5]['top'])
                        self.capture(tab,f'card-{width}-{name}', '.listas-fornecedor-mobile-card')
                        tab.evaluate('window.scrollTo(0,0);true')
                        self.capture(tab,f'mobile-{width}-{name}')
                        self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                        report.append({'width':width,'mode':name,'actions_identical':True,'metrics':metrics})
                self.assertEqual(self.effects(),before)
                # Confirm that every existing endpoint still resolves locally.
                for row in self.rows:
                    for route in ['compras_lista_fornecedor_ver','compras_lista_fornecedor_interno','compras_lista_fornecedor_whatsapp','compras_lista_fornecedor_detalhe']:
                        response=self.client.get(reverse('estoque:'+route,args=[row.pk]))
                        self.assertIn(response.status_code,[200,302],route)
                self.assertEqual(self.effects(),before)
            finally:
                self.template(current);browser.stop()
        (EVIDENCE/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

    def test_real_mobile_navigation_and_cancel_confirmation(self):
        before=self.effects();row=self.rows[0]
        with TemporaryDirectory(prefix='consulta-actions-chrome-') as profile:
            browser=Chrome(fixtures.CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    for label,route in [('Ver','compras_lista_fornecedor_ver'),('Editar','compras_lista_fornecedor_editar'),('Conferir','compras_lista_fornecedor_detalhe')]:
                        tab=self.open(browser,width)
                        selector=f'[data-mobile-lista-id="{row.pk}"] .listas-fornecedor-mobile-acoes a'
                        point=tab.evaluate(f'''(async()=>{{const a=[...document.querySelectorAll({selector!r})].find(a=>a.textContent.trim()==={label!r});a.scrollIntoView({{block:'center',behavior:'instant'}});await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);const r=a.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;return {{x,y,hit:a.contains(document.elementFromPoint(x,y))}}}})()''')
                        self.assertTrue(point.pop('hit'))
                        tab.call('Input.dispatchMouseEvent',dict(point,type='mousePressed',button='left',clickCount=1))
                        tab.call('Input.dispatchMouseEvent',dict(point,type='mouseReleased',button='left',clickCount=1))
                        target=reverse('estoque:'+route,args=[row.pk])
                        tab.wait(f'location.pathname==={target!r} && document.readyState==="complete"')
                        self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    tab=self.open(browser,width)
                    tab.evaluate(f'''window.confirm=()=>false;document.querySelector('[data-mobile-lista-id="{row.pk}"] button').click();true''')
                    self.assertEqual(tab.evaluate('location.pathname'),reverse('estoque:compras_listas_fornecedor'))
                    row.refresh_from_db();self.assertEqual(row.status,'aberta')
                self.assertEqual(self.effects(),before)
                tab=self.open(browser,320)
                tab.evaluate(f'''window.confirm=()=>true;document.querySelector('[data-mobile-lista-id="{row.pk}"] button').click();true''')
                tab.wait(f'location.pathname==={reverse("estoque:compras_listas_fornecedor")!r} && !document.querySelector(\'[data-mobile-lista-id="{row.pk}"]\')')
                row.refresh_from_db();self.assertEqual(row.status,'cancelada')
                self.assertEqual(self.effects(),before)
                self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
            finally:browser.stop()
