"""Navigation only, synthetic data in the guarded disposable PostgreSQL runner."""
import json
from pathlib import Path
import base64
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque import tests as fixtures
from estoque import tests_compra_mobile_total_browser as browser
from estoque.models import Compra, ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


class ListaNavegacaoTests(TestCase):
    setUp = fixtures.ComprasListaFornecedorGravarTests.setUp
    criar_produto = fixtures.ComprasListaFornecedorGravarTests.criar_produto
    criar_linha = fixtures.ComprasListaFornecedorGravarTests.criar_linha
    payload = fixtures.ComprasListaFornecedorGravarTests.payload

    def test_create_edit_filters_and_success_messages(self):
        product = self.criar_produto('Produto sintetico navegacao')
        data = {'lista_payload': json.dumps(self.payload([self.criar_linha(product, 2)]))}
        target = reverse('estoque:compras_listas_fornecedor')
        response = self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'), data, secure=True)
        self.assertRedirects(response, target, fetch_redirect_response=False)
        saved = ListaCompraFornecedor.objects.get()
        data['retorno_consulta'] = target + '?fornecedor=Teste&mobile=historico&nao_permitido=1'
        response = self.client.post(reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]), data,
            secure=True, HTTP_ACCEPT='application/json')
        self.assertEqual(response.json(), {'salvo': True, 'lista_id': saved.pk,
            'redirect_url': target + '?fornecedor=Teste&mobile=historico'})
        page = self.client.get(target, secure=True)
        self.assertContains(page, 'atualizada com sucesso')

    def test_external_return_is_ignored_and_get_cannot_save(self):
        product = self.criar_produto('Produto retorno seguro')
        response = self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'), {
            'lista_payload': json.dumps(self.payload([self.criar_linha(product, 2)])),
            'retorno_consulta': 'https://example.test/compras/listas-fornecedor/'}, secure=True, HTTP_ACCEPT='application/json')
        self.assertEqual(response.json()['redirect_url'], reverse('estoque:compras_listas_fornecedor'))
        self.assertEqual(self.client.get(reverse('estoque:compras_lista_fornecedor_gravar'), secure=True).status_code, 405)


@skipUnless(Path(browser.CHROME).is_file(), 'Chrome unavailable')
class ListaNavegacaoBrowserTests(StaticLiveServerTestCase):
    setUp = browser.CompraMobileTotalBrowserTests.setUp
    open_page = browser.CompraMobileTotalBrowserTests.open_page
    add = browser.CompraMobileTotalBrowserTests.add
    effects = browser.CompraMobileTotalBrowserTests.effects

    def test_confirmed_footer_actions_share_validation_and_prevent_duplicate_requests(self):
        before = self.effects()
        with TemporaryDirectory(prefix='list-footer-actions-') as profile:
            chrome = Chrome(browser.CHROME, profile).start()
            try:
                for width in [320, 360, 390, 1280]:
                    with self.subTest(width=width):
                        self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'), {'lista_payload': json.dumps({
                            'fornecedorId': self.supplier.pk, 'dataInicio': '2026-10-24', 'dataFim': '2026-10-24',
                            'linhas': [{'produtoId': self.products[0].pk, 'sugestao': '2', 'precoCompra': '10',
                                'precoUnitario': '10', 'total': '20', 'unidade': 'PCT'}]})})
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        path = reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk])
                        tab = self.open_page(AuditedChrome(chrome), width=width, path=path)
                        self.assertTrue(tab.evaluate('''(() => {
                            const actions=document.getElementById('acoesRodapeConfirmados');
                            return actions.previousElementSibling.matches('footer.sugestao-confirmados-rodape')
                                && !!document.getElementById('btnGravarListaFornecedor')
                                && !!document.getElementById('btnGravarGerarCompraFornecedor');
                        })()'''))
                        visible = tab.evaluate('document.getElementById("acoesRodapeConfirmados").getClientRects().length>0')
                        self.assertEqual(visible, width <= 860)
                        if width > 860:
                            continue
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        tab.evaluate('document.getElementById("acoesRodapeConfirmados").scrollIntoView({block:"center",behavior:"instant"});true')
                        folder = Path(__file__).resolve().parent.parent / 'evidencias-navegacao'
                        folder.mkdir(exist_ok=True)
                        capture=tab.call('Page.captureScreenshot', {'format':'png'})['data']
                        (folder / f'rodape-botoes-{width}.png').write_bytes(base64.b64decode(capture))
                        # Open edits are rejected by the existing validation, without any request.
                        tab.evaluate('''window.alerts=[];window.alert=m=>alerts.push(m);
                            window.realFetch=fetch;window.requests=0;
                            window.saveAction=document.getElementById('formGravarListaFornecedor').action;
                            window.isSaveRequest=args=>String(args[0])===saveAction && args[1]?.method==='POST';
                            window.fetch=(...args)=>{if(isSaveRequest(args))requests++;return realFetch(...args)};
                            document.querySelector('.sugestao-editar-lista-mobile').click();
                            document.getElementById('btnSalvarAlteracoesConfirmados').click();true''')
                        self.assertEqual(tab.evaluate('requests'), 0)
                        self.assertEqual(tab.evaluate('alerts.length'), 1)
                        self.assertEqual(tab.evaluate('location.pathname'), path)
                        # A server error preserves values and restores both pairs of controls.
                        tab.evaluate('''document.querySelector('.sugestao-editar-lista-mobile').click();
                            window.alerts=[];window.alert=m=>alerts.push(m);
                            window.fetch=()=>Promise.resolve(new Response('erro',{status:500}));
                            document.getElementById('btnSalvarAlteracoesConfirmados').click();true''')
                        tab.wait('alerts.length===1')
                        self.assertTrue(tab.evaluate('!document.getElementById("btnSalvarAlteracoesConfirmados").disabled && !document.getElementById("btnGravarListaFornecedor").disabled'))
                        tab.evaluate('''sessionStorage.setItem('footerSaveRequests','0');
                            window.fetch=(...args)=>{if(isSaveRequest(args))sessionStorage.footerSaveRequests=String(Number(sessionStorage.footerSaveRequests)+1);return realFetch(...args)};
                            document.getElementById('btnSalvarAlteracoesConfirmados').click();
                            document.getElementById('btnSalvarAlteracoesConfirmados').click();
                            document.getElementById('btnGravarListaFornecedor').click();true''')
                        tab.wait('location.pathname==="'+reverse('estoque:compras_listas_fornecedor')+'" && document.readyState==="complete"')
                        self.assertEqual(tab.evaluate('sessionStorage.footerSaveRequests'), '1')
                        self.assertEqual(self.effects(), before)
                        tab = self.open_page(AuditedChrome(chrome), width=width, path=path)
                        purchases = Compra.objects.count()
                        tab.evaluate('''sessionStorage.setItem('footerGenerateRequests','0');
                            window.originalSubmit=HTMLFormElement.prototype.submit;
                            HTMLFormElement.prototype.submit=function(){sessionStorage.footerGenerateRequests=String(Number(sessionStorage.footerGenerateRequests)+1);originalSubmit.call(this)};
                            document.getElementById('btnSalvarGerarCompraConfirmados').click();
                            document.getElementById('btnSalvarGerarCompraConfirmados').click();
                            document.getElementById('btnGravarGerarCompraFornecedor').click();true''')
                        tab.wait('location.search.includes("continuar_itens=1") && document.readyState==="complete"')
                        self.assertEqual(tab.evaluate('sessionStorage.footerGenerateRequests'), '1')
                        self.assertEqual(Compra.objects.count(), purchases + 1)
                        purchase = Compra.objects.latest('pk')
                        self.assertEqual(purchase.status, Compra.STATUS_RASCUNHO)
                        tab = self.open_page(AuditedChrome(chrome), width=width, path=path)
                        self.assertTrue(tab.evaluate('!document.getElementById("btnSalvarGerarCompraConfirmados")'))
                        self.assertEqual(tab.evaluate('window.purchaseConsoleErrors'), [])
                        # Keep the effects baseline current only for the deliberately generated draft.
                        before = self.effects()
            finally:
                chrome.stop()

    def test_explicit_protected_draft_keeps_its_existing_in_page_flow(self):
        before = self.effects()
        with TemporaryDirectory(prefix='list-navigation-draft-') as profile:
            chrome = Chrome(browser.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=390)
                self.add(tab, 0)
                tab.evaluate('document.getElementById("btnSalvarRascunhoListaMobile").click();true')
                tab.wait('!document.getElementById("avisoRascunhoProtegidoLista").hidden')
                saved = ListaCompraFornecedor.objects.get()
                self.assertEqual(tab.evaluate('location.pathname'), reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                self.assertEqual(saved.itens.count(), 1)
                self.assertEqual(self.effects(), before)
                self.assertEqual(tab.evaluate('window.purchaseConsoleErrors'), [])
            finally:
                chrome.stop()

    def test_generate_purchase_keeps_original_navigation_new_and_edit(self):
        with TemporaryDirectory(prefix='list-navigation-purchase-') as profile:
            chrome = Chrome(browser.CHROME, profile).start()
            try:
                for width in [390, 1280]:
                    tab = self.open_page(AuditedChrome(chrome), width=width)
                    self.add(tab, 0)
                    tab.evaluate('document.getElementById("btnGravarGerarCompraFornecedor").click();true')
                    tab.wait('location.pathname.includes("/compras/") && location.pathname.endsWith("/editar/") && location.search.includes("continuar_itens=1") && document.readyState==="complete"')
                    saved = ListaCompraFornecedor.objects.latest('pk')
                    purchase = Compra.objects.latest('pk')
                    self.assertEqual(purchase.status, Compra.STATUS_RASCUNHO)
                    count = Compra.objects.count()
                    # A list that already generated a purchase correctly offers
                    # Continuar Compra instead of another generation button.
                    payload = {'fornecedorId': self.supplier.pk, 'dataInicio': '2026-10-24',
                        'dataFim': '2026-10-24', 'linhas': [{'produtoId': self.products[0].pk,
                        'sugestao': '2', 'precoCompra': '10', 'precoUnitario': '10', 'total': '20', 'unidade': 'PCT'}]}
                    self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'),
                        {'lista_payload': json.dumps(payload)})
                    saved = ListaCompraFornecedor.objects.latest('pk')
                    tab = self.open_page(AuditedChrome(chrome), width=width,
                        path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                    tab.evaluate('document.getElementById("btnGravarGerarCompraFornecedor").click();true')
                    tab.wait('location.pathname.endsWith("/editar/") && location.search.includes("continuar_itens=1") && document.readyState==="complete"')
                    self.assertEqual(Compra.objects.count(), count + 1)
                    purchase = Compra.objects.latest('pk')
                    self.assertEqual(tab.evaluate('location.pathname'), reverse('estoque:compra_editar', args=[purchase.pk]))
                    self.products[0].refresh_from_db()
                    self.assertEqual(self.products[0].quantidade, 0)
            finally:
                chrome.stop()

    def test_save_new_edit_error_and_home_button_desktop_mobile(self):
        before = self.effects()
        with TemporaryDirectory(prefix='list-navigation-') as profile:
            chrome = Chrome(browser.CHROME, profile).start()
            try:
                for width in [320, 360, 390, 1280]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.add(tab, 0)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait('location.pathname==="'+reverse('estoque:compras_listas_fornecedor')+'" && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        path = reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk])
                        tab = self.open_page(AuditedChrome(chrome), width=width, path=path)
                        tab.evaluate('''window.item=document.querySelector('#itensListaMobileLista .sugestao-item-lista-mobile');
                            item.querySelector('.sugestao-editar-lista-mobile').click();
                            item.querySelector('.sugestao-item-lista-qtd-input').value='2';
                            item.querySelector('.sugestao-item-lista-preco-input').value='51,00';
                            item.querySelector('.sugestao-item-lista-preco-input').dispatchEvent(new Event('input',{bubbles:true}));
                            item.querySelector('.sugestao-editar-lista-mobile').click();
                            window.alerts=[];window.alert=m=>alerts.push(m);
                            window.realFetch=window.fetch;window.fetch=()=>Promise.resolve(new Response('erro',{status:500}));
                            document.getElementById('btnGravarListaFornecedor').click();true''')
                        tab.wait('alerts.length===1')
                        self.assertEqual(tab.evaluate('location.pathname'), path)
                        self.assertEqual(tab.evaluate('item.querySelector(".sugestao-item-lista-qtd-input").value'), '2,000')
                        # Real validation failure also leaves the edited DOM intact.
                        tab.evaluate('''window.fetch=(url,options)=>{
                            const payload=JSON.parse(options.body.get('lista_payload'));payload.linhas=[];
                            options.body.set('lista_payload',JSON.stringify(payload));return realFetch(url,options);
                        };document.getElementById('btnGravarListaFornecedor').click();true''')
                        tab.wait('alerts.length===2')
                        self.assertEqual(tab.evaluate('location.pathname'), path)
                        self.assertEqual(tab.evaluate('window.purchaseConsoleErrors'), [])
                        saved.refresh_from_db()
                        self.assertEqual(saved.itens.get().quantidade_final, 4)
                        tab.evaluate('window.fetch=realFetch;document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait('location.pathname==="'+reverse('estoque:compras_listas_fornecedor')+'" && document.readyState==="complete"')
                        saved.refresh_from_db()
                        self.assertEqual(saved.total_lista, 102)
                        self.assertEqual(self.effects(), before)
                        tab.call('Page.navigate', {'url': self.live_server_url + reverse('estoque:home')})
                        tab.wait('document.readyState==="complete" && !!document.getElementById("btnConsultarListasFornecedorPainel")')
                        self.assertTrue(tab.evaluate('''(() => {
                            const old=document.getElementById('btnNovaListaFornecedorPainel'),button=old.nextElementSibling;
                            const r=button.getBoundingClientRect();
                            return button.id==='btnConsultarListasFornecedorPainel' && button.className===old.className
                                && r.width>0 && r.left>=0 && r.right<=innerWidth+1;
                        })()'''))
                        self.assertEqual(tab.evaluate('window.purchaseConsoleErrors'), [])
                        tab.evaluate('document.getElementById("btnConsultarListasFornecedorPainel").click();true')
                        tab.wait('location.pathname==="'+reverse('estoque:compras_listas_fornecedor')+'" && document.readyState==="complete"')
            finally:
                chrome.stop()
