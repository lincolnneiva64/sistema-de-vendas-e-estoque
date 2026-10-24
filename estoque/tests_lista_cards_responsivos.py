"""Real Chrome flows against synthetic PostgreSQL: one component, both list modes."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from estoque import tests_compra_mobile_total_browser as existing
from estoque.models import Compra, ItemCompra, ListaCompraFornecedor, ProdutoFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


@skipUnless(Path(existing.CHROME).is_file(), 'Chrome unavailable')
class ListaCardsResponsivosTests(StaticLiveServerTestCase):
    open_page = existing.CompraMobileTotalBrowserTests.open_page
    effects = existing.CompraMobileTotalBrowserTests.effects

    def setUp(self):
        existing.CompraMobileTotalBrowserTests.setUp(self)
        self.manual = self.products[-1]
        ProdutoFornecedor.objects.filter(produto=self.manual).delete()
        for index in range(3):
            purchase = Compra.objects.create(fornecedor=self.supplier, data_compra=timezone.localdate(),
                status=Compra.STATUS_FINALIZADA, estoque_entrada_realizada=True, total=10 + index)
            ItemCompra.objects.create(compra=purchase, produto=self.manual, quantidade=1,
                unidade='PCT', preco_unitario=10 + index, valor_total=10 + index)

    def clean_console(self, tab):
        self.assertEqual(tab.evaluate('window.purchaseConsoleErrors'), [])

    def screenshot(self, tab, name):
        folder = Path(__file__).resolve().parent.parent / 'evidencias-lista-cards'
        folder.mkdir(exist_ok=True)
        tab.evaluate('window.scrollTo(0, document.getElementById("mobileSugestaoProdutos").getBoundingClientRect().top + scrollY - 65);true')
        data = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})['data']
        (folder / (name + '.png')).write_bytes(base64.b64decode(data))
        clip = tab.evaluate('''(() => {
            const card = document.querySelector('#itensListaMobileLista .sugestao-item-lista-mobile')
                || [...document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card')].at(-1);
            const r=card.getBoundingClientRect();
            return {x:Math.max(0,r.left+scrollX-5),y:Math.max(0,r.top+scrollY-5),width:r.width+10,height:r.height+10,scale:1};
        })()''')
        data = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True, 'clip': clip})['data']
        (folder / (name + '-card.png')).write_bytes(base64.b64decode(data))

    def manual_add(self, tab):
        tab.evaluate(f'''document.getElementById("produtoManualSugestao").value="{self.manual.pk}";
            document.getElementById("produtoManualQtd").value="2";
            document.getElementById("produtoManualPrecoCompra").value="10,00";
            document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{{bubbles:true}}));
            document.getElementById("btnAdicionarProdutoSugestao").click();true''')
        tab.wait(f'!!document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\')')

    def test_create_manual_edit_pick_return_save_reopen_on_all_widths(self):
        before = self.effects()
        with TemporaryDirectory(prefix='list-cards-responsive-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390, 1280]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.manual_add(tab)
                        self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                        self.assertTrue(tab.evaluate('document.getElementById("sugestaoListaVazia").classList.contains("oculto")'))
                        tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');true')
                        self.assertEqual(tab.evaluate('card.dataset.naLista'), '0')
                        self.assertEqual(tab.evaluate('[...card.querySelectorAll(".sugestao-mobile-edit-actions button")].map(b=>b.textContent.trim())'), ['Editar', 'Peguei', 'Excluir'])
                        # The clone uses exactly the initial card's field structure and compact CSS.
                        self.assertEqual(tab.evaluate('''(() => {
                            const first = document.querySelector('#mobileSugestaoProdutos .sugestao-mobile-card');
                            const structure = c => [...c.querySelector('.sugestao-mobile-edit').children].map(e=>e.tagName+':'+e.className);
                            return JSON.stringify(structure(first))===JSON.stringify(structure(card));
                        })()'''), True)
                        root = f'#tbodySugestaoProdutos [data-produto-id="{self.manual.pk}"]' if width > 860 else f'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]'
                        tab.evaluate(f'window.pending=document.querySelector({root!r});pending.querySelector({"[data-editar-pre-lista]" if width > 860 else ".sugestao-confirmar-card-mobile"!r}).click();true')
                        tab.evaluate('''pending.querySelector('.sugestao-qtd-input').value='3';
                            pending.querySelector('.sugestao-unidade-input').value='FD';
                            pending.querySelector('.sugestao-preco-input').value='12,50';
                            pending.querySelector('.sugestao-preco-input').dispatchEvent(new Event('input',{bubbles:true}));true''')
                        self.screenshot(tab, f'pre-lista-{width}')
                        tab.evaluate(f'pending.querySelector({"[data-confirmar-pre-lista]" if width > 860 else ".sugestao-adicionar-lista-mobile"!r}).click();true')
                        tab.wait('card.dataset.naLista==="1"')
                        self.assertEqual(tab.evaluate('pending.getClientRects().length'), 0)
                        tab.evaluate(f'window.item=document.querySelector(\'#itensListaMobileLista [data-produto-id="{self.manual.pk}"]\');true')
                        self.assertEqual(tab.evaluate('''[...item.querySelectorAll('.sugestao-mobile-edit-actions button')].filter(b=>!b.hidden).map(b=>b.textContent.trim())'''), ['Editar', 'Remover', 'Devolver à pré-lista'])
                        self.assertEqual(tab.evaluate('item.querySelector(".sugestao-item-lista-unidade-input").value'), 'FD')
                        self.assertEqual(tab.evaluate('item.querySelector(".sugestao-item-lista-mobile-total span").textContent'), '37,50')
                        self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-lista-total]").textContent'), '1')
                        # Editing then returning must save the edit, including its unit.
                        tab.evaluate('''item.querySelector('.sugestao-editar-lista-mobile').click();
                            item.querySelector('.sugestao-item-lista-unidade-input').value='CX';
                            item.querySelector('.sugestao-item-lista-qtd-input').value='4';
                            item.querySelector('.sugestao-item-lista-preco-input').value='13,00';
                            item.querySelector('.sugestao-devolver-lista-mobile').click();true''')
                        self.assertEqual(tab.evaluate('[card.dataset.naLista,card.querySelector(".sugestao-qtd-input").value,card.querySelector(".sugestao-preco-input").value,card.querySelector(".sugestao-unidade-input").value]'), ['0', '4,000', '13,00', 'CX'])
                        tab.evaluate('adicionarCardSugestaoNaLista(card);adicionarCardSugestaoNaLista(card);true')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#itensListaMobileLista .sugestao-item-lista-mobile").length'), 1)
                        tab.evaluate('window.item=document.querySelector("#itensListaMobileLista .sugestao-item-lista-mobile");item.querySelector("[data-historico-toggle]").click();true')
                        self.assertEqual(tab.evaluate('item.querySelectorAll(".sugestao-historico-item").length'), 3)
                        tab.evaluate('item.querySelector("[data-historico-toggle]").click();true')
                        self.screenshot(tab, f'confirmado-{width}')
                        self.clean_console(tab)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait(r'location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        self.assertEqual(saved.itens.count(), 1)
                        source = saved.itens.get()
                        self.assertEqual((source.quantidade_final, source.unidade, source.preco_compra, source.total), (Decimal('4'), 'CX', Decimal('13'), Decimal('52')))
                        tab = self.open_page(AuditedChrome(chrome), width=width,
                            path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#itensListaMobileLista .sugestao-item-lista-mobile").length'), 1)
                        self.assertEqual(tab.evaluate('''[...document.querySelectorAll('#itensListaMobileLista .sugestao-mobile-edit-actions button')].filter(b=>!b.hidden).map(b=>b.textContent.trim())'''), ['Editar', 'Remover', 'Devolver à pré-lista'])
                        self.assertEqual(tab.evaluate('document.querySelector(".sugestao-item-lista-unidade-input").value'), 'CX')
                        self.screenshot(tab, f'reaberta-{width}')
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_remove_does_not_return_and_new_manual_product_is_pending_in_edit_mode(self):
        with TemporaryDirectory(prefix='list-card-remove-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [390, 1280]:
                    tab = self.open_page(AuditedChrome(chrome), width=width)
                    tab.evaluate('window.card=document.querySelector("#mobileSugestaoProdutos .sugestao-mobile-card");adicionarCardSugestaoNaLista(card);true')
                    tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                    tab.wait(r'location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    saved = ListaCompraFornecedor.objects.latest('pk')
                    tab = self.open_page(AuditedChrome(chrome), width=width,
                        path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                    self.manual_add(tab)
                    tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');true')
                    self.assertEqual(tab.evaluate('card.dataset.naLista'), '0')
                    tab.evaluate('adicionarCardSugestaoNaLista(card);document.querySelector("#itensListaMobileLista .sugestao-remover-lista-mobile").click();document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#itensListaMobileLista .sugestao-item-lista-mobile").length'), 1)
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card:not(.sugestao-tratada-mobile)").length'), 0)
                    tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                    tab.wait('location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                    self.assertEqual(saved.itens.count(), 1)
                    self.assertEqual(saved.itens.get().produto_id, self.manual.pk)
                    self.clean_console(tab)
            finally:
                chrome.stop()

    def test_duplicate_manual_add_preserves_pending_edits_and_removed_product_can_be_added_again(self):
        with TemporaryDirectory(prefix='list-card-duplicates-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [390, 1280]:
                    tab = self.open_page(AuditedChrome(chrome), width=width)
                    self.manual_add(tab)
                    tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');card.querySelector(".sugestao-qtd-input").value="7";true')
                    self.manual_add(tab)
                    self.assertEqual(tab.evaluate('card.querySelector(".sugestao-qtd-input").value'), '7')
                    self.assertEqual(tab.evaluate(f'document.querySelectorAll(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\').length'), 1)
                    tab.evaluate('adicionarCardSugestaoNaLista(card);document.querySelector(".sugestao-remover-lista-mobile").click();document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                    self.manual_add(tab)
                    self.assertEqual(tab.evaluate(f'document.querySelectorAll(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\').length'), 1)
                    self.assertEqual(tab.evaluate(f'document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\').dataset.naLista'), '0')
                    self.clean_console(tab)
            finally:
                chrome.stop()

    def test_exclude_pending_product_removes_its_history_without_writes(self):
        before = self.effects()
        with TemporaryDirectory(prefix='list-card-exclude-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 390, 1280]:
                    tab = self.open_page(AuditedChrome(chrome), width=width)
                    self.manual_add(tab)
                    root = '#tbodySugestaoProdutos' if width > 860 else '#mobileSugestaoProdutos'
                    tab.evaluate(f'''document.querySelector('{root} [data-produto-id="{self.manual.pk}"] .sugestao-remover-item').click();
                        if(document.getElementById('modalRemoverItemListaMobile').classList.contains('aberto'))
                            document.getElementById('btnConfirmarRemoverItemListaMobile').click();true''')
                    self.assertEqual(tab.evaluate(f'document.querySelectorAll(\'[data-produto-id="{self.manual.pk}"]\').length'), 0)
                    self.assertTrue(tab.evaluate('document.querySelectorAll("#tbodySugestaoProdutos [data-historico-desktop-row]").length===document.querySelectorAll("#tbodySugestaoProdutos tr[data-produto-id]").length'))
                    self.assertEqual(self.effects(), before)
                    self.clean_console(tab)
            finally:
                chrome.stop()

    def test_only_pending_mobile_buttons_share_one_compact_row(self):
        with TemporaryDirectory(prefix='list-pink-buttons-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.manual_add(tab)
                        tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');true')
                        metrics = tab.evaluate('''(() => {
                            const buttons=[...card.querySelectorAll('.sugestao-mobile-edit-actions button')];
                            return {buttons:buttons.map(b=>({text:b.textContent.trim(),top:b.getBoundingClientRect().top,
                                height:b.getBoundingClientRect().height,width:b.getBoundingClientRect().width,
                                fits:b.scrollWidth<=b.clientWidth})), overflow:document.documentElement.scrollWidth>innerWidth};
                        })()''')
                        self.assertEqual([b['text'] for b in metrics['buttons']], ['Editar', 'Peguei', 'Excluir'])
                        self.assertLess(max(b['top'] for b in metrics['buttons']) - min(b['top'] for b in metrics['buttons']), 1)
                        self.assertTrue(all(b['height'] == 32 and b['fits'] for b in metrics['buttons']))
                        self.assertLess(max(b['width'] for b in metrics['buttons']) - min(b['width'] for b in metrics['buttons']), 1)
                        self.assertFalse(metrics['overflow'])
                        tab.evaluate('adicionarCardSugestaoNaLista(card);true')
                        green = tab.evaluate('''[...document.querySelectorAll('#itensListaMobileLista .sugestao-mobile-edit-actions button')]
                            .filter(b=>!b.hidden).map(b=>({text:b.textContent.trim(),top:b.getBoundingClientRect().top,height:b.getBoundingClientRect().height}))''')
                        self.assertEqual([b['text'] for b in green], ['Editar', 'Remover', 'Devolver à pré-lista'])
                        self.assertEqual(green[0]['top'], green[1]['top'])
                        self.assertGreater(green[2]['top'], green[1]['top'])
                        self.assertEqual([b['height'] for b in green], [48, 48, 48])
                        self.clean_console(tab)
            finally:
                chrome.stop()
