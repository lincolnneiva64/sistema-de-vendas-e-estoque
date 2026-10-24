"""Historical desktop and unchanged mobile checks, disposable PostgreSQL only."""
import base64
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from django.template import engines
from django.urls import reverse
from django.test import override_settings
from estoque import tests_lista_cards_responsivos as base
from estoque.models import ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / 'estoque/templates/estoque/compras_sugestao_fornecedor.html'
EVIDENCE = ROOT / 'evidencias-desktop'

class RestaurarDesktopTests(base.ListaCardsResponsivosTests):
    # Reuse fixtures only; inherited checks exercise the superseded desktop cards.
    test_create_manual_edit_pick_return_save_reopen_on_all_widths = None
    test_remove_does_not_return_and_new_manual_product_is_pending_in_edit_mode = None

    def open_page(self, chrome, width=390, path=None):
        from django.utils import timezone
        tab=chrome.tab();tab.call('Network.enable')
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<=860})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        today=timezone.localdate().isoformat()
        path=path or reverse('estoque:sugestao_compra_fornecedor')+'?fornecedor='+str(self.supplier.pk)+'&data_inicio='+today+'&data_fim='+today
        tab.call('Page.navigate',{'url':self.live_server_url+path})
        try:
            tab.wait('typeof atualizarTotalListaPedidoMobile==="function" && typeof adicionarCardSugestaoNaLista==="function"', timeout=30)
        except AssertionError:
            raise AssertionError(tab.evaluate('[location.href,document.body.innerText,window.purchaseConsoleErrors]'))
        return tab

    def template(self, source):
        TEMPLATE.write_text(source, encoding='utf-8')
        for engine in engines.all():
            for loader in engine.engine.template_loaders:
                if hasattr(loader, 'reset'):
                    loader.reset()

    def capture(self, tab, name, selector='.sugestao-desktop'):
        tab.evaluate(f'document.querySelector({selector!r}).scrollIntoView({{block:"start",behavior:"instant"}});true')
        # Wait two paint frames before capturing stable layout.
        tab.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))')
        data=tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']
        (EVIDENCE / (name+'.png')).write_bytes(base64.b64decode(data))

    def table(self, tab):
        return tab.evaluate('''(() => {
            const root=document.querySelector('.sugestao-desktop');
            const rect=e=>{const r=e.getBoundingClientRect();const p=root.getBoundingClientRect();return [r.x-p.x,r.y-p.y,r.width,r.height]};
            return [...root.querySelectorAll('th,td,input,button')].filter(e=>e.getBoundingClientRect().width>0).map(e=>[e.tagName,e.className,e.value || e.textContent.trim(),rect(e)]);
        })()''')

    def test_historical_desktop_and_unchanged_mobile(self):
        EVIDENCE.mkdir(exist_ok=True)
        restored=TEMPLATE.read_text(encoding='utf-8')
        original=subprocess.check_output(['git','show','9e96a13:estoque/templates/estoque/compras_sugestao_fornecedor.html']).decode()
        published=subprocess.check_output(['git','show','cdb28fd:estoque/templates/estoque/compras_sugestao_fornecedor.html']).decode()
        report=[]
        before=self.effects()
        with TemporaryDirectory(prefix='restore-desktop-browser-') as profile:
            chrome=Chrome(base.existing.CHROME,profile).start()
            try:
                for width in [1280,1440]:
                    # Create/save/reopen/edit real lists using the restored desktop table.
                    self.template(restored)
                    tab=self.open_page(AuditedChrome(chrome),width=width)
                    self.manual_add(tab)
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#tbodySugestaoProdutos tr[data-produto-id]").length'),4)
                    self.assertFalse(tab.evaluate('document.getElementById("itensListaMobile").getBoundingClientRect().height>0'))
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#tbodySugestaoProdutos [data-confirmar-pre-lista]").length'),0)
                    tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                    tab.wait('location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    saved=ListaCompraFornecedor.objects.latest('pk')
                    self.assertEqual(saved.itens.count(),4)
                    edit=reverse('estoque:compras_lista_fornecedor_editar',args=[saved.pk])
                    for mode,path in [('nova',None),('edicao',edit)]:
                        tables=[]
                        for label,source in [('antigo',original),('restaurado',restored)]:
                            self.template(source)
                            tab=self.open_page(AuditedChrome(chrome),width=width,path=path)
                            tables.append(self.table(tab))
                            self.capture(tab,f'{mode}-{width}-{label}')
                            self.clean_console(tab)
                            self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.assertEqual(tables[0],tables[1],f'{mode} {width}: historical table geometry and controls')
                        report.append({'mode':mode,'width':width,'historical_table_identical':True})
                    tab.evaluate('window.row=document.querySelector("#tbodySugestaoProdutos tr[data-produto-id]");row.querySelector(".sugestao-qtd-input").value="3";row.querySelector(".sugestao-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                    self.clean_console(tab)
                    tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                    tab.wait('location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    tab=self.open_page(AuditedChrome(chrome),width=width,path=edit)
                    self.assertEqual(tab.evaluate('document.querySelector("#tbodySugestaoProdutos .sugestao-qtd-input").value'),'3,000')
                    self.assertEqual(self.effects(),before)
                for width in [320,360,390]:
                    # Same requests/data: compare rendered mobile DOM/styles against cdb28fd.
                    states=[]
                    for label,source in [('aprovado',published),('preservado',restored)]:
                        self.template(source)
                        tab=self.open_page(AuditedChrome(chrome),width=width)
                        self.manual_add(tab)
                        self.capture(tab,f'mobile-{width}-{label}','#mobileSugestaoProdutos')
                        states.append(tab.evaluate('''[...document.querySelectorAll('#mobileSugestaoProdutos *, #areaAdicionarProdutosManual *, #itensListaMobile *')].filter(e=>e.getBoundingClientRect().width>0).map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return [e.tagName,e.className,e.textContent.trim(),e.value||'',r.x+scrollX,r.y+scrollY,r.width,r.height,s.display,s.backgroundColor,s.fontSize]})'''))
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.clean_console(tab)
                    self.assertEqual(states[0],states[1],f'mobile {width}: identical approved DOM and computed layout')
                    report.append({'width':width,'approved_mobile_identical':True})
            finally:
                self.template(restored)
                chrome.stop()
        (EVIDENCE/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
