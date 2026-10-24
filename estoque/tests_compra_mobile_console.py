"""Capture uncaught JS errors from before parsing through real list POSTs."""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from estoque import tests_compra_mobile_total_browser as existing
from estoque.models import Compra, ItemCompra, ListaCompraFornecedor


CONSOLE_CAPTURE = r"""(() => {
    window.purchaseConsoleErrors=[];
    window.purchaseResourceErrors=[];
    addEventListener('error', event => {
        if (!event.message) {
            purchaseResourceErrors.push(event.target?.src || event.target?.href || 'resource');
            return;
        }
        purchaseConsoleErrors.push({kind:'error', message:event.message,
            filename:event.filename, line:event.lineno, column:event.colno,
            stack:event.error?.stack});
    }, true);
    addEventListener('unhandledrejection', event => {
        purchaseConsoleErrors.push({kind:'unhandledrejection', message:String(event.reason),
            stack:event.reason?.stack});
    });
    const original=console.error;
    console.error=function(...args) {
        purchaseConsoleErrors.push({kind:'console.error', message:args.map(String).join(' ')});
        return original.apply(this,args);
    };
})();"""


class AuditedChrome:
    """Install listeners before the first application script, not after load."""
    def __init__(self, chrome):
        self.chrome = chrome

    def tab(self):
        tab = self.chrome.tab()
        tab.call('Page.addScriptToEvaluateOnNewDocument', {'source':CONSOLE_CAPTURE})
        return tab


@skipUnless(Path(existing.CHROME).is_file(), 'Chrome unavailable')
class CompraMobileConsoleTests(StaticLiveServerTestCase):
    setUp = existing.CompraMobileTotalBrowserTests.setUp
    open_page = existing.CompraMobileTotalBrowserTests.open_page
    add = existing.CompraMobileTotalBrowserTests.add
    total = existing.CompraMobileTotalBrowserTests.total
    effects = existing.CompraMobileTotalBrowserTests.effects

    def test_saved_list_suppliers_are_an_array_for_json_script(self):
        saved = ListaCompraFornecedor.objects.create(fornecedor=self.supplier,
            data_lista=timezone.localdate(), data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate())
        response = self.client.get(reverse('estoque:compras_lista_fornecedor_editar',args=[saved.pk]))
        self.assertEqual(response.status_code,200)
        expected = [{'id':self.supplier.pk,'nome':self.supplier.nome}]
        self.assertEqual(response.context['fornecedores_payload'],expected)
        from html.parser import HTMLParser

        class JsonScript(HTMLParser):
            inside = False
            data = ''
            def handle_starttag(self,tag,attrs):
                self.inside = tag=='script' and dict(attrs).get('id')=='fornecedoresSugestaoData'
            def handle_data(self,data):
                if self.inside:
                    self.data += data
            def handle_endtag(self,tag):
                if tag=='script':
                    self.inside = False

        parser = JsonScript()
        parser.feed(response.content.decode())
        self.assertEqual(json.loads(parser.data),expected)

    def assert_console_clean(self, tab, stage):
        errors = tab.evaluate('purchaseConsoleErrors')
        self.assertEqual(errors, [], stage + ': ' + json.dumps(errors, ensure_ascii=False))
        invalid = tab.evaluate('''Array.from(document.scripts).map((script,index)=>{
            if(script.src || script.type && script.type!=='text/javascript')return null;
            try { new Function(script.textContent);return null; }
            catch(error){return {index,message:String(error)};}
        }).filter(Boolean)''')
        self.assertEqual(invalid, [], stage + ': ' + json.dumps(invalid))

    def test_base_script_and_top_button_desktop_and_mobile(self):
        with TemporaryDirectory(prefix='purchase-console-top-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=1280)
                tab.wait('document.readyState==="complete"')
                self.assert_console_clean(tab, 'desktop initial load')
                tab.evaluate('window.scrollTo({top:900,behavior:"instant"});true')
                tab.wait('scrollY>260 && document.getElementById("botao-voltar-topo").classList.contains("visivel")')
                tab.evaluate('document.getElementById("botao-voltar-topo").click();true')
                tab.wait('scrollY<3')
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=390,height=844,deviceScaleFactor=1,mobile=True))
                tab.wait('document.getElementById("botao-voltar-topo").classList.contains("recolhido")')
                tab.evaluate('window.scrollTo({top:900,behavior:"instant"});document.getElementById("botao-voltar-topo").click();true')
                self.assertFalse(tab.evaluate('document.getElementById("botao-voltar-topo").classList.contains("recolhido")'))
                tab.evaluate('document.getElementById("botao-voltar-topo").click();true')
                tab.wait('scrollY<3')
                self.assert_console_clean(tab, 'desktop/mobile Topo click and resize')
            finally:
                chrome.stop()

    def test_full_mobile_flow_save_and_reopen_without_console_errors(self):
        for days in [21,14,7]:
            purchase = Compra.objects.create(fornecedor=self.supplier,
                data_compra=timezone.localdate()-timedelta(days=days),status=Compra.STATUS_FINALIZADA)
            ItemCompra.objects.create(compra=purchase,produto=self.products[0],quantidade=2,
                unidade='PCT',preco_unitario='47.40',valor_total='94.80')
        before = self.effects()
        with TemporaryDirectory(prefix='purchase-console-flow-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320,360,390]:
                    tab = self.open_page(AuditedChrome(chrome), width=width)
                    tab.wait('document.readyState==="complete"')
                    self.assert_console_clean(tab, f'{width} initial load')
                    self.add(tab,0,'2','47,40')
                    self.add(tab,1,'0,500','35,40')
                    self.assertEqual(self.total(tab),'R$ 112,50')
                    tab.evaluate('window.item=document.querySelector(".sugestao-item-lista-mobile");item.querySelector("[data-historico-toggle]").click();true')
                    self.assertEqual(tab.evaluate('item.querySelectorAll(".sugestao-historico-item").length'),3)
                    self.assertFalse(tab.evaluate('item.querySelector("[data-historico-card]").hidden'))
                    tab.evaluate('item.querySelector(".sugestao-editar-lista-mobile").click();item.querySelector(".sugestao-item-lista-qtd-input").value="1,500";item.querySelector(".sugestao-item-lista-preco-input").value="50,00";item.querySelector(".sugestao-editar-lista-mobile").click();true')
                    self.assertEqual(self.total(tab),'R$ 92,70')
                    tab.evaluate('item.querySelector(".sugestao-item-detalhes").open=true;true')
                    self.assertEqual(tab.evaluate('item.querySelector("[data-preco-unitario-confirmado]").textContent'),'50,00')
                    tab.evaluate('item.querySelector(".sugestao-editar-lista-mobile").click();item.querySelector(".sugestao-item-lista-qtd-input").value="9";item.querySelector(".sugestao-cancelar-edicao-lista-mobile").click();window.other=document.querySelectorAll(".sugestao-item-lista-mobile")[1];window.otherId=other.dataset.produtoId;other.querySelector(".sugestao-devolver-lista-mobile").click();true')
                    self.assertEqual(self.total(tab),'R$ 75,00')
                    tab.evaluate('window.source=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="\'+otherId+\'"]\');source.querySelector(".sugestao-adicionar-lista-mobile").click();true')
                    self.assertEqual(self.total(tab),'R$ 92,70')
                    tab.evaluate('document.querySelectorAll(".sugestao-item-lista-mobile")[1].querySelector(".sugestao-remover-lista-mobile").click();document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                    self.assertEqual(self.total(tab),'R$ 75,00')
                    self.assertEqual(tab.evaluate('source.getClientRects().length'),0)
                    self.assert_console_clean(tab,f'{width} all picking actions')
                    # Real browser form submission, not a payload-only assertion.
                    tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                    tab.wait(r'location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    saved = ListaCompraFornecedor.objects.order_by('-pk').first()
                    self.assertEqual(saved.total_lista,Decimal('75.00'))
                    self.assertEqual(saved.itens.count(),1)
                    self.assertEqual(saved.itens.get().quantidade_final,Decimal('1.500'))
                    self.assert_console_clean(tab,f'{width} saved list detail')
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compras_lista_fornecedor_ver',args=[saved.pk])})
                    tab.wait('location.pathname.endsWith("/ver/") && document.readyState==="complete"')
                    self.assert_console_clean(tab,f'{width} saved list view')
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compras_lista_fornecedor_editar',args=[saved.pk])})
                    tab.wait('typeof adicionarCardSugestaoNaLista==="function" && document.readyState==="complete"')
                    self.assertEqual(self.total(tab),'R$ 75,00')
                    self.assert_console_clean(tab,f'{width} reopened saved list')
                    dates = tab.evaluate('[document.getElementById("data_inicio").value,document.getElementById("data_fim").value]')
                    tab.evaluate('window.supplierSearch=document.getElementById("fornecedorBuscaSugestao");supplierSearch.value="Fornecedor";supplierSearch.dispatchEvent(new Event("input",{bubbles:true}));true')
                    tab.wait('document.querySelectorAll("#fornecedorSugestoes .sugestao-autocomplete-opcao").length>0')
                    tab.evaluate('document.querySelector("#fornecedorSugestoes .sugestao-autocomplete-opcao").click();true')
                    self.assertEqual(tab.evaluate('[document.getElementById("data_inicio").value,document.getElementById("data_fim").value]'),dates)
                    self.assertEqual(tab.evaluate('document.getElementById("fornecedor").value'),str(self.supplier.pk))
                    tab.evaluate('window.card=document.querySelector("#itensListaMobileLista .sugestao-item-lista-mobile");card.querySelector(".sugestao-editar-lista-mobile").click();card.querySelector(".sugestao-item-lista-qtd-input").value="2";card.querySelector(".sugestao-item-lista-preco-input").value="51,00";card.querySelector(".sugestao-item-lista-preco-input").dispatchEvent(new Event("input",{bubbles:true}));card.querySelector(".sugestao-editar-lista-mobile").click();true')
                    self.assertEqual(self.total(tab),'R$ 102,00')
                    self.assert_console_clean(tab,f'{width} saved list editing')
                    tab.evaluate('document.getElementById("btnSalvarAlteracoesListaMobile").click();true')
                    tab.wait('location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    saved.refresh_from_db()
                    self.assertEqual(saved.total_lista,Decimal('102.00'))
                    self.assert_console_clean(tab,f'{width} saved modifications')
                    self.assertEqual(self.effects(),before)
            finally:
                chrome.stop()
