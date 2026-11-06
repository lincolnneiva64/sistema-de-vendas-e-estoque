"""Manual selection preview: compact fields, duplicate warning and real save."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque import tests_lista_cards_responsivos as existing
from estoque.models import ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


@skipUnless(Path(existing.existing.CHROME).is_file(), 'Chrome unavailable')
class SelecaoManualMobileTests(StaticLiveServerTestCase):
    setUp = existing.ListaCardsResponsivosTests.setUp
    open_page = existing.ListaCardsResponsivosTests.open_page
    effects = existing.ListaCardsResponsivosTests.effects
    clean_console = existing.ListaCardsResponsivosTests.clean_console

    def test_catalogo_manual_fornecedor_ativo_fora_sugestao_mesmos_calculos(self):
        from estoque.models import ProdutoFornecedor
        product=self.products[0]
        product.quantidade=1000
        product.save(update_fields=['quantidade'])
        ProdutoFornecedor.objects.filter(produto=self.products[1]).update(ativo=False)
        self.products[2].ativo=False
        self.products[2].save(update_fields=['ativo'])
        url=reverse('estoque:sugestao_compra_fornecedor')
        params={'fornecedor':self.supplier.pk,'periodo':'7'}
        automatic=self.client.get(url, params)
        self.assertNotIn(product.pk, [row['produto_id'] for row in automatic.context['linhas']])
        before=self.effects()
        response=self.client.get(url,{**params,'consulta_manual':'1'})
        self.assertEqual(response.status_code,200)
        payload=response.json()
        self.assertEqual([p['id'] for p in payload['produtos']], [product.pk])
        manual=payload['produtos'][0]
        original=next(p for p in automatic.context['produtos_manual_payload'] if p['id']==product.pk)
        self.assertEqual(manual,original)
        self.assertTrue(payload['periodo_valido'])
        self.assertEqual(self.effects(),before)

    def test_fluxo_real_nova_lista_dois_produtos_sem_gerar(self):
        import json
        from estoque.models import ProdutoFornecedor, Compra
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        tocar = lambda tab, selector: ColetaBrowserTests.tocar(self, tab, selector)
        self.manual.quantidade = 1000
        self.manual.save(update_fields=['quantidade'])
        ProdutoFornecedor.objects.get_or_create(produto=self.manual, fornecedor=self.supplier)
        with TemporaryDirectory(prefix='manual-interface-real-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=1440,
                    path=reverse('estoque:sugestao_compra_fornecedor'))
                # Same sequence as opening F12 and then enabling the mobile viewport.
                tab.call('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=True))
                tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=1))
                tab.wait("innerWidth===390 && !!document.getElementById('produtoManualBuscaSugestao').getClientRects().length")
                tocar(tab, '#fornecedorBuscaSugestao')
                tab.call('Input.insertText', dict(text=self.supplier.nome))
                tab.wait('!!document.querySelector('+json.dumps(f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]')+')')
                tocar(tab, f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]')
                tocar(tab, '#periodo')
                tab.call('Input.insertText', dict(text='7'))
                tab.call('Input.dispatchKeyEvent', dict(type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9))
                tab.call('Input.dispatchKeyEvent', dict(type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9))
                tab.wait("!document.getElementById('produtoManualBuscaSugestao').disabled && document.getElementById('msgAdicionarProdutoSugestao').textContent===''")
                self.assertTrue(tab.evaluate("!!document.getElementById('produtoManualBuscaSugestao').getClientRects().length"))
                for product in [self.manual, self.products[0]]:
                    if tab.evaluate("document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile')"):
                        tocar(tab, '#btnToggleAdicionarProdutosMobile')
                        tab.wait("document.getElementById('btnToggleAdicionarProdutosMobile').getAttribute('aria-expanded')==='true' && document.activeElement.id==='produtoManualBuscaSugestao'")
                    tocar(tab, '#produtoManualBuscaSugestao')
                    tab.call('Input.insertText', dict(text=product.nome))
                    selector=f'#produtoManualSugestoes [data-produto-id="{product.pk}"]'
                    try:
                        tab.wait('!!document.querySelector('+json.dumps(selector)+')')
                    except AssertionError:
                        print('Diagnostico busca:', tab.evaluate("({fornecedor:document.getElementById('fornecedor').value,periodo:document.getElementById('periodo').value,inicio:document.getElementById('data_inicio').value,fim:document.getElementById('data_fim').value,busca:document.getElementById('produtoManualBuscaSugestao').value,msg:document.getElementById('msgAdicionarProdutoSugestao').textContent,opcoes:document.getElementById('produtoManualSugestoes').innerText,erros:purchaseConsoleErrors})"))
                        self.screenshot(tab, 'manual-fluxo-real-falha')
                        raise
                    tocar(tab, selector)
                    self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"), '1.000')
                    tocar(tab, '#produtoManualQtd')
                    tab.call('Input.insertText', dict(text='2'))
                    self.screenshot(tab, 'manual-fluxo-real-'+str(product.pk))
                    tocar(tab, '#btnAdicionarProdutoSugestao')
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 2)
                tocar(tab, '#btnGravarListaFornecedor')
                tab.wait('location.pathname==="/compras/listas-fornecedor/"')
                saved=ListaCompraFornecedor.objects.latest('pk')
                self.assertEqual(set(saved.itens.values_list('produto_id', flat=True)), {self.manual.pk,self.products[0].pk})
                tab=self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:compras_lista_fornecedor_editar',args=[saved.pk]))
                for product in [self.manual,self.products[0]]:
                    tocar(tab,f'#mobileSugestaoProdutos [data-produto-id="{product.pk}"] .sugestao-adicionar-lista-mobile')
                    tab.wait('!coletaLista.ocupado')
                tocar(tab,'#btnSalvarGerarCompraConfirmados')
                tab.wait("location.search.includes('continuar_itens=1')")
                self.assertEqual(Compra.objects.filter(lista_fornecedor=saved).get().itens.count(),2)
                self.clean_console(tab)
            finally:
                chrome.stop()

    def select(self, tab, product):
        if tab.evaluate("document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile')"):
            tab.evaluate("document.getElementById('btnToggleAdicionarProdutosMobile').click();true")
            tab.wait("document.activeElement.id==='produtoManualBuscaSugestao'")
        tab.wait('document.getElementById("btnToggleAdicionarProdutosMobile").getAttribute("aria-expanded")==="true"')
        import json
        tab.evaluate('window.search=document.getElementById("produtoManualBuscaSugestao");search.value=' + json.dumps(product.nome) + ';search.dispatchEvent(new Event("input",{bubbles:true}));true')
        selector = f'#produtoManualSugestoes [data-produto-id="{product.pk}"]'
        tab.wait(f'!!document.querySelector({selector!r})')
        tab.evaluate(f'document.querySelector({selector!r}).click();true')
        tab.wait(f'document.getElementById("produtoManualSugestao").value==="{product.pk}"')

    def screenshot(self, tab, name):
        folder = Path(__file__).resolve().parent.parent / 'evidencias-lista-cards'
        folder.mkdir(exist_ok=True)
        clip = tab.evaluate('''(() => {
            const r=document.getElementById('areaAdicionarProdutosManual').getBoundingClientRect();
            return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1};
        })()''')
        data = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True, 'clip': clip})['data']
        (folder / (name + '.png')).write_bytes(base64.b64decode(data))

    def test_compact_selection_available_and_already_listed_keep_all_fields_and_history(self):
        before = self.effects()
        with TemporaryDirectory(prefix='manual-selection-states-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Adicionar produto', False])
                        metrics = tab.evaluate('''(() => {
                            const cells=[...document.getElementById('linhaProdutoManualSugestao').children];
                            return {height:cells[0].parentElement.getBoundingClientRect().height,
                                stock:cells[1].getBoundingClientRect().top,min:cells[2].getBoundingClientRect().top,
                                qty:cells[5].getBoundingClientRect().top,price:cells[7].getBoundingClientRect().top,
                                qtyInput:cells[5].querySelector('input').getBoundingClientRect().top,
                                priceInput:cells[7].querySelector('input').getBoundingClientRect().top,
                                unitPrice:cells[8].getBoundingClientRect().top,total:cells[9].getBoundingClientRect().top,
                                fields:cells.length,labelsFit:cells.slice(1).every(c=>c.scrollWidth<=c.clientWidth),
                                searchFits:cells[0].querySelector('input[type=text]').getBoundingClientRect().width<=cells[0].clientWidth,
                                overflow:document.documentElement.scrollWidth>innerWidth};
                        })()''')
                        self.assertEqual(metrics['stock'], metrics['min'])
                        self.assertEqual(metrics['qty'], metrics['price'])
                        self.assertLess(abs(metrics['qtyInput'] - metrics['priceInput']), 1)
                        self.assertEqual(metrics['unitPrice'], metrics['total'])
                        self.assertEqual(metrics['fields'], 11)
                        self.assertLess(metrics['height'], 650)
                        self.assertFalse(metrics['overflow'])
                        self.assertTrue(metrics['labelsFit'])
                        self.assertTrue(metrics['searchFits'])
                        # Exercise the native disclosure before captureBeyondViewport,
                        # which temporarily resizes the page.
                        tab.evaluate('(async () => {await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
                        tab.wait('document.querySelector("#produtoManualHistoricoMobile details").open')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#produtoManualHistoricoMobile .sugestao-historico-desktop-item").length'), 3)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
                        self.screenshot(tab, f'selecao-manual-disponivel-{width}')
                        self.select(tab, self.products[0])
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        count = tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length')
                        tab.evaluate('document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length'), count)
                        self.screenshot(tab, f'selecao-manual-ja-incluido-{width}')
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_search_add_edit_pick_save_reopen_and_duplicate_warning(self):
        before = self.effects()
        with TemporaryDirectory(prefix='manual-selection-full-flow-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.select(tab, self.manual)
                        tab.evaluate('document.getElementById("produtoManualQtd").value="2";document.getElementById("produtoManualPrecoCompra").value="10,00";document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        tab.wait(f'!!document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\')')
                        tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');true')
                        self.assertEqual(tab.evaluate('[...card.querySelectorAll(".sugestao-mobile-edit-actions button")].map(b=>b.textContent.trim())'), ['Editar', 'Peguei', 'Excluir'])
                        self.assertEqual(tab.evaluate('new Set([...card.querySelectorAll(".sugestao-mobile-edit-actions button")].map(b=>b.getBoundingClientRect().top)).size'), 1)
                        tab.evaluate('''card.querySelector('.sugestao-confirmar-card-mobile').click();
                            card.querySelector('.sugestao-qtd-input').value='3';
                            card.querySelector('.sugestao-preco-input').value='12,50';
                            card.querySelector('.sugestao-preco-input').dispatchEvent(new Event('input',{bubbles:true}));
                            card.querySelector('.sugestao-adicionar-lista-mobile').click();true''')
                        self.assertEqual(tab.evaluate('card.dataset.naLista'), '1')
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        tab.evaluate('document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        self.assertEqual(tab.evaluate(f'document.querySelectorAll(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\').length'), 1)
                        self.clean_console(tab)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait(r'location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        item = saved.itens.get()
                        self.assertEqual((item.produto_id,item.quantidade_final,item.unidade,item.preco_compra,item.total),
                            (self.manual.pk,Decimal('3'),'PCT',Decimal('12.50'),Decimal('37.50')))
                        tab = self.open_page(AuditedChrome(chrome), width=width,
                            path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#itensListaMobileLista .sugestao-item-lista-mobile").length'), 1)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_nova_manual_sem_gerar_consulta_periodo_e_salva(self):
        import json
        from estoque.models import ProdutoFornecedor
        self.manual.quantidade = 1000
        self.manual.save(update_fields=["quantidade"])
        ProdutoFornecedor.objects.get_or_create(produto=self.manual, fornecedor=self.supplier)
        with TemporaryDirectory(prefix='manual-sem-gerar-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:sugestao_compra_fornecedor')+'?nova=1')
                tab.evaluate('window.purchaseConsoleErrors=[];window.alertasManual=[];window.alert=m=>alertasManual.push(m);window.consultasManuais=0;window.fetchManualOriginal=fetch;window.fetch=async(...a)=>{const r=await fetchManualOriginal(...a);if(String(a[0]).includes("consulta_manual"))consultasManuais++;return r};true')
                tab.evaluate("document.getElementById('fornecedor').value="+json.dumps(str(self.supplier.pk))+";document.getElementById('periodo').value='7';document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("consultasManuais>0 && !document.getElementById('produtoManualBuscaSugestao').disabled && document.getElementById('msgAdicionarProdutoSugestao').textContent===''")
                self.select(tab, self.manual)
                self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"), '1.000')
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 0)
                tab.evaluate("document.getElementById('data_inicio').value='2020-01-01';document.getElementById('data_fim').value='2020-01-02';document.getElementById('data_fim').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("document.getElementById('produtoManualVendido').textContent==='0.000'")
                tab.evaluate("document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("document.getElementById('produtoManualVendido').textContent==='1.000'")
                tab.evaluate("document.getElementById('produtoManualQtd').value='2';document.getElementById('produtoManualQtd').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('btnAdicionarProdutoSugestao').click();document.getElementById('btnAdicionarProdutoSugestao').click();true")
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 1)
                tab.evaluate("document.getElementById('btnGravarListaFornecedor').click();true")
                self.assertEqual(tab.evaluate('alertasManual'), [])
                tab.wait('location.pathname==="/compras/listas-fornecedor/"')
                saved = ListaCompraFornecedor.objects.latest('pk')
                self.assertEqual(saved.itens.count(), 1)
                self.assertEqual(saved.itens.get().produto_id, self.manual.pk)
                tab = self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 1)
                self.assertEqual(saved.itens.get().quantidade_final, Decimal('2'))
                self.assertEqual(tab.evaluate("document.querySelector('#mobileSugestaoProdutos .sugestao-mobile-card').dataset.naLista==='1'"), False)
                self.clean_console(tab)
            finally:
                chrome.stop()
