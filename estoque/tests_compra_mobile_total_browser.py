"""Totals on the real supplier-list page, private browser and isolated database."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from estoque.models import (Fornecedor, Produto, ProdutoFornecedor, Cliente, Venda,
    ItemVenda, ListaCompraFornecedor, ItemListaCompraFornecedor, Compra,
    ContaPagar, MovimentoFinanceiro)

CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class CompraMobileTotalBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('mobile-total','test@example.test','test-only')
        self.client.force_login(self.user)
        self.supplier=Fornecedor.objects.create(nome='Fornecedor Total Mobile')
        customer=Cliente.objects.create(nome='Cliente Isolado')
        sale=Venda.objects.create(cliente=customer,data_venda=timezone.localdate(),total=0)
        self.products=[]
        for index,price in enumerate(['47.40','35.40','7.00','9.00']):
            product=Produto.objects.create(nome='Produto Mobile '+str(index),preco_compra=price,
                preco_vista=50,preco_prazo=51,quantidade=0,estoque_minimo=1,
                unidade_compra='PCT',unidade_venda_1='PCT')
            ProdutoFornecedor.objects.create(produto=product,fornecedor=self.supplier)
            ItemVenda.objects.create(venda=sale,produto=product,quantidade=1,unidade='PCT',preco_unitario=50,valor_total=50)
            self.products.append(product)

    def open_page(self,chrome,width=390,path=None):
        tab=chrome.tab();tab.call('Network.enable')
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<=860})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        today=timezone.localdate().isoformat()
        path=path or reverse('estoque:sugestao_compra_fornecedor')+'?fornecedor='+str(self.supplier.pk)+'&data_inicio='+today+'&data_fim='+today
        tab.call('Page.navigate',{'url':self.live_server_url+path})
        tab.wait('typeof atualizarTotalListaPedidoMobile==="function" && typeof adicionarCardSugestaoNaLista==="function"')
        return tab

    def total(self,tab):
        totals=tab.evaluate('[...document.querySelectorAll("[data-total-lista-pedido]")].map(e=>e.textContent.replace(/\u00a0/g," "))')
        self.assertTrue(totals);self.assertEqual(len(set(totals)),1)
        return totals[0]

    def add(self,tab,index,quantity='4',price=None):
        identifier=str(self.products[index].pk)
        tab.evaluate('window.card=[...document.querySelectorAll("#mobileSugestaoProdutos [data-produto-id]")].find(c=>c.dataset.produtoId==='+json.dumps(identifier)+');card.querySelector(".sugestao-qtd-input").value='+json.dumps(quantity)+';'+
            ('card.querySelector(".sugestao-preco-input").value='+json.dumps(price)+';' if price is not None else '')+
            'card.querySelector(".sugestao-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));card.querySelector(".sugestao-adicionar-lista-mobile").click();true')
        tab.wait('card.dataset.naLista==="1" && card.getClientRects().length===0')

    def effects(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in [Compra,ContaPagar,MovimentoFinanceiro,Produto,Venda,ItemVenda]}

    def test_hidden_added_cards_total_payload_and_no_commercial_writes(self):
        before=self.effects()
        with TemporaryDirectory(prefix='purchase-mobile-total-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome)
                original=tab.evaluate('document.querySelector(".sugestao-conferencia-item.total .sugestao-conferencia-valor.total").textContent')
                self.assertEqual(self.total(tab),'R$ 0,00')
                self.add(tab,0);self.assertEqual(self.total(tab),'R$ 189,60')
                self.add(tab,1);self.assertEqual(self.total(tab),'R$ 331,20')
                self.assertEqual(tab.evaluate('document.querySelector("[data-itens-lista-mobile-total]").textContent'),'2')
                self.assertEqual(tab.evaluate('document.querySelector(".sugestao-conferencia-item.total .sugestao-conferencia-valor.total").textContent'),original)
                # Capture the actual serializer without submitting the form.
                tab.evaluate('HTMLFormElement.prototype.submit=function(){window.captured=JSON.parse(document.getElementById("listaFornecedorPayload").value)};document.getElementById("btnGravarListaFornecedor").click();true')
                tab.wait('!!window.captured')
                payload=tab.evaluate('captured')
                self.assertEqual(len(payload['linhas']),2)
                self.assertEqual([row['total'] for row in payload['linhas']],['189,60','141,60'])
                self.assertEqual(self.effects(),before)
                self.assertEqual(ListaCompraFornecedor.objects.count(),0)
                # Persist only in the isolated test database and compare the
                # existing backend result with the amount displayed before save.
                response=self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'),{'lista_payload':json.dumps(payload)})
                self.assertEqual(response.status_code,302)
                self.assertEqual(ListaCompraFornecedor.objects.get().total_lista,Decimal('331.20'))
                self.assertEqual(self.effects(),before)
            finally:chrome.stop()

    def test_edit_cancel_remove_readd_and_empty(self):
        with TemporaryDirectory(prefix='purchase-mobile-edit-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome);self.add(tab,0);self.add(tab,1)
                tab.evaluate('window.item=document.querySelector(".sugestao-item-lista-mobile");item.querySelector(".sugestao-editar-lista-mobile").click();item.querySelector(".sugestao-item-lista-qtd-input").value="2";item.querySelector(".sugestao-item-lista-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                self.assertEqual(self.total(tab),'R$ 236,40')
                tab.evaluate('item.querySelector(".sugestao-item-lista-preco-input").value="50,00";item.querySelector(".sugestao-item-lista-preco-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                self.assertEqual(self.total(tab),'R$ 241,60')
                tab.evaluate('item.querySelector(".sugestao-cancelar-edicao-lista-mobile").click();true')
                self.assertEqual(self.total(tab),'R$ 331,20')
                tab.evaluate('item.querySelector(".sugestao-editar-lista-mobile").click();item.querySelector(".sugestao-item-lista-qtd-input").value="2";item.querySelector(".sugestao-item-lista-preco-input").value="50,00";item.querySelector(".sugestao-editar-lista-mobile").click();true')
                self.assertEqual(self.total(tab),'R$ 241,60')
                for _ in range(2):
                    tab.evaluate('document.querySelector(".sugestao-item-lista-mobile .sugestao-remover-lista-mobile").click();true')
                    tab.evaluate('document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                self.assertEqual(self.total(tab),'R$ 0,00')
                # Re-add from the same hidden source card, preserving its values.
                tab.evaluate('adicionarCardSugestaoNaLista([...document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card")][0],false);true')
                self.assertEqual(self.total(tab),'R$ 100,00')
            finally:chrome.stop()

    def test_decimal_rounding_matches_backend_and_excludes_unselected(self):
        with TemporaryDirectory(prefix='purchase-mobile-cents-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome)
                self.add(tab,0,'0,125','0,20') # 0.025 -> 0.02, Decimal half-even
                self.assertEqual(self.total(tab),'R$ 0,02')
                self.add(tab,1,'0,175','0,20') # 0.035 -> 0.04
                self.assertEqual(self.total(tab),'R$ 0,06')
                self.add(tab,2,'3','0,10');self.assertEqual(self.total(tab),'R$ 0,36')
            finally:chrome.stop()

    def test_manual_search_addition_cannot_overwrite_total_with_zero(self):
        with TemporaryDirectory(prefix='purchase-mobile-manual-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome);self.add(tab,0)
                identifier=str(self.products[1].pk)
                tab.evaluate('document.getElementById("produtoManualSugestao").value='+json.dumps(identifier)+';document.getElementById("produtoManualQtd").value="4";document.getElementById("produtoManualPrecoCompra").value="35,40";document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarProdutoSugestao").click();true')
                tab.wait('document.querySelectorAll(".sugestao-item-lista-mobile").length===2')
                self.assertEqual(self.total(tab),'R$ 331,20')
            finally:chrome.stop()

    def test_restored_saved_list_and_direct_edit(self):
        saved=ListaCompraFornecedor.objects.create(fornecedor=self.supplier,data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),data_fim_periodo=timezone.localdate(),total_lista=Decimal('331.20'),total_sugerido_original=Decimal('176.94'))
        for product,price in zip(self.products,['47.40','35.40']):
            ItemListaCompraFornecedor.objects.create(lista=saved,produto=product,quantidade_final=4,unidade='PCT',preco_compra=price,preco_unitario=price,total=Decimal(price)*4)
        path=reverse('estoque:compras_lista_fornecedor_editar',kwargs={'pk':saved.pk})
        with TemporaryDirectory(prefix='purchase-mobile-restore-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome,path=path)
                self.assertEqual(self.total(tab),'R$ 331,20')
                tab.evaluate('document.querySelector("#mobileSugestaoProdutos .sugestao-qtd-input").value="5";document.querySelector("#mobileSugestaoProdutos .sugestao-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                self.assertEqual(self.total(tab),'R$ 378,60')
                tab.call('Page.reload');tab.wait('typeof atualizarTotalListaPedidoMobile==="function"')
                self.assertEqual(self.total(tab),'R$ 331,20')
                saved.refresh_from_db();self.assertEqual(saved.total_lista,Decimal('331.20'))
            finally:chrome.stop()

    def test_desktop_keeps_existing_total_and_does_not_select_mobile_items(self):
        with TemporaryDirectory(prefix='purchase-desktop-total-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open_page(chrome,width=1280)
                self.assertFalse(tab.evaluate('atualizarTotalListaPedidoMobile()'))
                tab.evaluate('window.row=document.querySelector("#tbodySugestaoProdutos tr[data-produto-id]");row.querySelector(".sugestao-qtd-input").value="4";row.querySelector(".sugestao-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                self.assertTrue(tab.evaluate('[...document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card")].every(c=>c.dataset.naLista!=="1")'))
                self.assertTrue(tab.evaluate('(()=>{const number=v=>Number(v.includes(",")?v.replace(/\\./g,"").replace(",","."):v);const sum=[...document.querySelectorAll("#tbodySugestaoProdutos .sugestao-total-input")].reduce((s,c)=>s+number(c.value),0);return document.querySelector("[data-total-lista-pedido]").textContent==="R$ "+sum.toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})})()'))
            finally:chrome.stop()


    def test_saved_desktop_list_mobile_edit_confirm_persist_and_reopen(self):
        from offline.test_sales_sidebars_browser import SalesSidebarBrowserTests
        def click(tab, selector):
            tab.evaluate(f'document.querySelector({selector!r}).scrollIntoView({{block:"center",behavior:"instant"}});true')
            tab.wait(f'(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight;}})()')
            SalesSidebarBrowserTests.click(self, tab, selector)
        payload = {"fornecedorId": self.supplier.pk, "dataInicio": timezone.localdate().isoformat(), "dataFim": timezone.localdate().isoformat(), "linhas": [
            {"produtoId": p.pk, "sugestao": "4", "precoCompra": str(p.preco_compra),
             "precoUnitario": str(p.preco_compra), "total": str(p.preco_compra * 4), "unidade": "PCT"}
            for p in self.products[:2]]}
        response = self.client.post(reverse("estoque:compras_lista_fornecedor_gravar"),
                                    {"lista_payload": json.dumps(payload)})
        self.assertEqual(response.status_code, 302)
        saved = ListaCompraFornecedor.objects.get()
        path = reverse("estoque:compras_lista_fornecedor_editar", args=[saved.pk])
        before = self.effects()
        with TemporaryDirectory(prefix="saved-list-mobile-edit-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_page(chrome, width=1280, path=path)
                self.assertFalse(tab.evaluate('document.querySelector("#tbodySugestaoProdutos .sugestao-qtd-input").readOnly'))
                tab.call("Emulation.setDeviceMetricsOverride", {"width":390,"height":844,"deviceScaleFactor":1,"mobile":True})
                for product, qty, price in [(self.products[0], "2", "50,00"), (self.products[1], "3", "40,00")]:
                    card = '#mobileSugestaoProdutos [data-produto-id="' + str(product.pk) + '"]'
                    button = card + " .sugestao-confirmar-card-mobile"
                    self.assertEqual(tab.evaluate(f'document.querySelector({button!r}).textContent'), "Editar")
                    self.assertTrue(tab.evaluate(f'document.querySelector({card!r}+" .sugestao-qtd-input").readOnly'))
                    click(tab, button)
                    self.assertEqual(tab.evaluate(f'document.querySelector({button!r}).textContent'), "Confirmar")
                    self.assertFalse(tab.evaluate(f'document.querySelector({card!r}+" .sugestao-qtd-input").readOnly'))
                    tab.evaluate(f'window.editCard=document.querySelector({card!r});editCard.querySelector(".sugestao-qtd-input").value={qty!r};editCard.querySelector(".sugestao-preco-input").value={price!r};editCard.querySelector(".sugestao-preco-input").dispatchEvent(new Event("input",{{bubbles:true}}));true')
                    click(tab, button)
                    self.assertEqual(tab.evaluate(f'document.querySelector({button!r}).textContent'), "Editar")
                self.assertEqual(self.total(tab), "R$ 220,00")
                tab.evaluate('localStorage.setItem("saved-list-preserved","draft");true')
                click(tab, "#btnSalvarAlteracoesListaMobile")
                tab.wait('location.pathname.endsWith("/ver/") && document.readyState==="complete"')
                saved.refresh_from_db()
                self.assertEqual(saved.total_lista, Decimal("220"))
                self.assertEqual(list(saved.itens.order_by("produto_id").values_list("quantidade_final","preco_compra","total")),
                                 [(Decimal("2"),Decimal("50"),Decimal("100")),(Decimal("3"),Decimal("40"),Decimal("120"))])
                tab = self.open_page(chrome, path=path)
                self.assertEqual(self.total(tab), "R$ 220,00")
                self.assertEqual(tab.evaluate('localStorage.getItem("saved-list-preserved")'), "draft")
                self.assertEqual(self.effects(), before)
                # The backend restrictions apply to direct POST as well as GET.
                saved.status = ListaCompraFornecedor.STATUS_CANCELADA
                saved.save(update_fields=["status"])
                for method in (self.client.get, self.client.post):
                    self.assertEqual(method(path, {"lista_payload":json.dumps(payload)}).status_code, 302)
                saved.refresh_from_db()
                self.assertEqual(saved.total_lista, Decimal("220"))
                self.assertEqual(self.effects(), before)
                saved.status = ListaCompraFornecedor.STATUS_ABERTA
                saved.save(update_fields=["status"])
                Compra.objects.create(lista_fornecedor=saved, data_compra=timezone.localdate(), status=Compra.STATUS_FINALIZADA)
                blocked_before = self.effects()
                items_before = list(saved.itens.values())
                for method in (self.client.get, self.client.post):
                    self.assertEqual(method(path, {"lista_payload":json.dumps(payload)}).status_code, 302)
                self.assertEqual(list(saved.itens.values()), items_before)
                self.assertEqual(self.effects(), blocked_before)
            finally:
                chrome.stop()
