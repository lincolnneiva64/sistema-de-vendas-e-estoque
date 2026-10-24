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

    def select(self, tab, product):
        tab.evaluate('''if (document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile'))
            document.getElementById('btnToggleAdicionarProdutosMobile').click();true''')
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
                        self.screenshot(tab, f'selecao-manual-disponivel-{width}')
                        # captureBeyondViewport temporarily resizes the page; let pending
                        # resize/render tasks finish before testing the native disclosure.
                        tab.evaluate('(async () => {await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
                        tab.wait('document.querySelector("#produtoManualHistoricoMobile details").open')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#produtoManualHistoricoMobile .sugestao-historico-desktop-item").length'), 3)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
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
