"""Same confirmed section and footer in new, suggested and reopened lists."""
import base64
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque import tests_lista_cards_responsivos as base
from estoque import tests_lista_selecao_manual_mobile as selection
from estoque.models import Fornecedor, ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


@skipUnless(Path(base.existing.CHROME).is_file(), 'Chrome unavailable')
class ListaConfirmadosRodapeTests(StaticLiveServerTestCase):
    setUp = base.ListaCardsResponsivosTests.setUp
    open_page = base.ListaCardsResponsivosTests.open_page
    effects = base.ListaCardsResponsivosTests.effects
    clean_console = base.ListaCardsResponsivosTests.clean_console
    manual_add = base.ListaCardsResponsivosTests.manual_add
    select_manual = selection.SelecaoManualMobileTests.select

    def footer(self, tab, count, total):
        self.assertEqual(tab.evaluate('''[document.querySelector('#itensListaMobile footer [data-produtos-confirmados]').textContent,
            document.querySelector('#itensListaMobile footer [data-total-lista-pedido]').textContent.replace(/\u00a0/g,' ')]'''), [str(count), total])
        self.assertTrue(tab.evaluate('''(() => {
            const spans=[...document.querySelectorAll('#itensListaMobile footer > span')];
            return spans.length===2 && spans[0].getBoundingClientRect().top===spans[1].getBoundingClientRect().top
                && document.getElementById('itensListaMobile').lastElementChild.tagName==='FOOTER';
        })()'''))

    def order(self, tab, width):
        if width <= 860:
            self.assertTrue(tab.evaluate('''(() => {
                const pending=document.getElementById('mobileSugestaoProdutos').getBoundingClientRect();
                const add=document.getElementById('btnToggleAdicionarProdutosMobile').getBoundingClientRect();
                const summary=document.querySelector('.sugestao-resumo-final').getBoundingClientRect();
                const confirmed=document.getElementById('itensListaMobile').getBoundingClientRect();
                return pending.bottom<=add.top && add.bottom<=summary.top && summary.bottom<=confirmed.top
                    && document.querySelector('.sugestao-resumo-final').nextElementSibling.id==='itensListaMobile';
            })()'''))
        else:
            self.assertTrue(tab.evaluate('document.getElementById("itensListaMobile").parentElement.classList.contains("sugestao-card")'))
            self.assertTrue(tab.evaluate('document.querySelector(".sugestao-desktop").getBoundingClientRect().width>0'))
        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))

    def screenshot(self, tab, width):
        folder = Path(__file__).resolve().parent.parent / 'evidencias-lista-cards'
        tab.evaluate('document.querySelector(".sugestao-resumo-final").scrollIntoView({block:"start",behavior:"instant"});true')
        data = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']
        (folder/f'ordem-rodape-{width}.png').write_bytes(base64.b64decode(data))

    def test_suggested_edit_remove_return_save_and_reopen_share_footer_and_order(self):
        before = self.effects()
        with TemporaryDirectory(prefix='confirmed-footer-flow-') as profile:
            browser = Chrome(base.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390, 1280]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(browser), width=width)
                        self.order(tab, width)
                        self.footer(tab, 0, 'R$ 0,00')
                        base.existing.CompraMobileTotalBrowserTests.add(self, tab, 0, '4')
                        self.footer(tab, 1, 'R$ 189,60')
                        base.existing.CompraMobileTotalBrowserTests.add(self, tab, 1, '0,5')
                        self.footer(tab, 2, 'R$ 207,30')
                        tab.evaluate('''window.first=document.querySelector('#itensListaMobileLista .sugestao-item-lista-mobile');
                            first.querySelector('.sugestao-editar-lista-mobile').click();
                            first.querySelector('.sugestao-item-lista-qtd-input').value='3';
                            first.querySelector('.sugestao-item-lista-preco-input').value='10,00';
                            first.querySelector('.sugestao-item-lista-preco-input').dispatchEvent(new Event('input',{bubbles:true}));true''')
                        self.footer(tab, 2, 'R$ 47,70')
                        tab.evaluate('first.querySelector(".sugestao-devolver-lista-mobile").click();true')
                        self.footer(tab, 1, 'R$ 17,70')
                        tab.evaluate('document.querySelector(".sugestao-remover-lista-mobile").click();document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                        self.footer(tab, 0, 'R$ 0,00')
                        tab.evaluate(f'adicionarCardSugestaoNaLista(document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.products[0].pk}"]\'));true')
                        self.footer(tab, 1, 'R$ 30,00')
                        self.manual_add(tab)
                        self.footer(tab, 1, 'R$ 30,00')
                        tab.evaluate(f'window.manual=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');adicionarCardSugestaoNaLista(manual);adicionarCardSugestaoNaLista(manual);true')
                        self.footer(tab, 2, 'R$ 50,00')
                        self.order(tab, width)
                        self.screenshot(tab, width)
                        self.clean_console(tab)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait(r'/\/listas-fornecedor\/\d+\/$/.test(location.pathname) && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        self.assertEqual((saved.itens.count(),saved.total_lista), (2,Decimal('50')))
                        tab = self.open_page(AuditedChrome(browser), width=width,
                            path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                        self.order(tab, width)
                        self.footer(tab, 2, 'R$ 50,00')
                        tab.evaluate('window.approvedCards=document.getElementById("itensListaMobileLista").innerHTML;true')
                        for other_width in [1280, 390, width]:
                            tab.call('Emulation.setDeviceMetricsOverride',dict(width=other_width,height=844,deviceScaleFactor=1,mobile=other_width<=860))
                            self.order(tab, other_width)
                            self.footer(tab, 2, 'R$ 50,00')
                            self.assertTrue(tab.evaluate('document.getElementById("itensListaMobileLista").innerHTML===approvedCards'))
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                browser.stop()

    def test_entirely_new_list_manual_selection_count_is_products_not_packages(self):
        self.supplier = Fornecedor.objects.create(nome='Fornecedor novo somente inclusao manual')
        before = self.effects()
        with TemporaryDirectory(prefix='confirmed-footer-new-list-') as profile:
            browser = Chrome(base.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390, 1280]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(browser), width=width,
                            path=reverse('estoque:sugestao_compra_fornecedor')+'?nova=1')
                        tab.wait('document.readyState==="complete"')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length'), 0)
                        self.order(tab, width)
                        self.footer(tab, 0, 'R$ 0,00')
                        import json
                        tab.evaluate('window.supplierSearch=document.getElementById("fornecedorBuscaSugestao");supplierSearch.value='+json.dumps(self.supplier.nome)+';supplierSearch.dispatchEvent(new Event("input",{bubbles:true}));true')
                        selector = f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]'
                        tab.wait(f'!!document.querySelector({selector!r})')
                        tab.evaluate(f'document.querySelector({selector!r}).click();true')
                        tab.evaluate(f'document.getElementById("data_inicio").value="{date.today().isoformat()}";document.getElementById("data_fim").value="{date.today().isoformat()}";true')
                        # Existing flow loads the catalogue only after Gerar lista.
                        # This supplier has no linked suggestions: all items are manual.
                        tab.evaluate('document.getElementById("btnGerarListaSugestao").click();true')
                        tab.wait(f'new URLSearchParams(location.search).get("fornecedor")==="{self.supplier.pk}" && document.readyState==="complete" && typeof adicionarCardSugestaoNaLista==="function"')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length'), 0)
                        self.select_manual(tab, self.manual)
                        tab.evaluate('document.getElementById("produtoManualQtd").value="7";document.getElementById("produtoManualPrecoCompra").value="10,00";document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        self.footer(tab, 0, 'R$ 0,00')
                        tab.evaluate('window.onlyCard=document.querySelector("#mobileSugestaoProdutos .sugestao-mobile-card");adicionarCardSugestaoNaLista(onlyCard);adicionarCardSugestaoNaLista(onlyCard);true')
                        self.footer(tab, 1, 'R$ 70,00')
                        self.order(tab, width)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait(r'/\/listas-fornecedor\/\d+\/$/.test(location.pathname) && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        self.assertEqual((saved.itens.count(), saved.itens.get().quantidade_final, saved.total_lista), (1,Decimal('7'),Decimal('70')))
                        tab = self.open_page(AuditedChrome(browser), width=width,
                            path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                        self.footer(tab, 1, 'R$ 70,00')
                        self.order(tab, width)
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                browser.stop()
