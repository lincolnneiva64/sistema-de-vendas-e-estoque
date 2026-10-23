"""Historical display never derives an unknown unit from today's catalogue."""
from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from estoque import views
from estoque.models import Compra, ItemCompra, Produto, Fornecedor, ContaPagar, MovimentoFinanceiro
from estoque.services.unidades_historicas_compra import unidade_exibicao


class UnidadesHistoricasCompraTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('historico','example@example.test','local-only'))
        self.supplier = Fornecedor.objects.create(nome='Atacado sintético')
        self.purchase = Compra.objects.create(fornecedor=self.supplier,data_compra=date(2026,10,15),
            status=Compra.STATUS_FINALIZADA,estoque_entrada_realizada=True,total=380)
        examples = [('Açúcar sintético 30/1kg','FD',30,1),('Sardinha sintética 54/75g','CX',54,25),('Café sintético 50/100g','FD',50,12)]
        self.items = []
        for name,unit,factor,quantity in examples + [(f'Produto sintético {i}','FD',12,1) for i in range(7)]:
            product = Produto.objects.create(nome=name,unidade_compra=unit,unidade_venda_1=unit,
                unidade_venda_2='UN',vende_fracionado=True,fator_conversao=factor,
                quantidade=7,estoque_minimo=1,preco_compra=10,preco_vista=20,preco_prazo=21)
            self.items.append(ItemCompra.objects.create(compra=self.purchase,produto=product,
                quantidade=quantity,unidade='',preco_unitario=10,valor_total=quantity*10))

    def snapshot(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            [Produto,Compra,ItemCompra,ContaPagar,MovimentoFinanceiro]}

    def test_ten_missing_units_in_note_history_and_edit_never_invent_packaging(self):
        before = self.snapshot()
        response = self.client.get(reverse('estoque:compras_detalhe',args=[self.purchase.pk]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'<td>Não informada</td>',count=10,html=True)
        lines = views._linhas_item_compra(self.purchase)
        self.assertEqual([line['unidade'] for line in lines],['']*10)
        self.assertTrue(all(line['unidade_historica_ausente'] for line in lines))
        self.assertEqual([Decimal(line['quantidade'].replace(',','.')) for line in lines[:3]],
            [Decimal('1'),Decimal('25'),Decimal('12')])
        self.assertEqual(lines[0]['unidade_estoque'],'FD')
        edit = self.client.get(reverse('estoque:compra_editar',args=[self.purchase.pk]))
        self.assertEqual(edit.status_code,200)
        self.assertContains(edit,'Unidade histórica não informada.')
        histories = views._ultimas_compras_produtos_para_lista_fornecedor([i.produto_id for i in self.items])
        for history in histories.values():
            self.assertEqual(history[0]['unidade'],'')
            self.assertEqual(history[0]['unidade_exibicao'],'Não informada')
            payload = views._historico_compras_produto_para_payload(history)
            self.assertEqual(payload[0]['unidade'],'')
            self.assertEqual(payload[0]['unidade_exibicao'],'Não informada')
        self.assertEqual(self.snapshot(),before)

    def test_explicit_unit_is_authoritative_even_if_different_from_catalogue(self):
        item = self.items[0]
        item.unidade='UN'; item.save(update_fields=['unidade'])
        before = self.snapshot()
        self.assertEqual(views._linhas_item_compra(self.purchase)[0]['unidade'],'UN')
        history = views._ultimas_compras_produtos_para_lista_fornecedor([item.produto_id])[item.produto_id]
        self.assertEqual(history[0]['unidade_exibicao'],'UN')
        self.assertEqual(self.snapshot(),before)

    def test_old_open_status_with_stock_already_entered_is_historical(self):
        self.purchase.status=Compra.STATUS_ABERTA
        self.purchase.save(update_fields=['status'])
        self.assertEqual(views._linhas_item_compra(self.purchase)[0]['unidade'],'')

    def test_blank_whitespace_and_deleted_product_do_not_get_current_unit(self):
        item=self.items[0];item.unidade='   ';item.produto=None
        item.save(update_fields=['unidade','produto'])
        self.assertEqual(unidade_exibicao(item.unidade),'Não informada')
        self.assertTrue(views._linhas_item_compra(self.purchase)[0]['unidade_historica_ausente'])
        response=self.client.get(reverse('estoque:compras_detalhe',args=[self.purchase.pk]))
        self.assertContains(response,'<td>Não informada</td>',count=10,html=True)

    def test_new_purchase_rejects_missing_unit_without_writes(self):
        before = self.snapshot()
        data={'fornecedor_id':str(self.supplier.pk),'data_compra':'2026-10-23',
            'tipo_pagamento':'aprazo','data_vencimento':'2026-11-23',
            'produto_id[]':[str(self.items[0].produto_id)],'quantidade[]':['1'],
            'preco_unitario[]':['10'],'unidade[]':[' '],'fechamento_token':'somente-sintetico'}
        response=self.client.post(reverse('estoque:compras_nova'),data)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.snapshot(),before)

    def test_direct_finalization_with_blank_unit_rolls_back_everything(self):
        draft=Compra.objects.create(fornecedor=self.supplier,data_compra=date(2026,10,23),
            status=Compra.STATUS_RASCUNHO,total=10)
        ItemCompra.objects.create(compra=draft,produto=self.items[0].produto,
            quantidade=1,unidade='',preco_unitario=10,valor_total=10)
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'unidade utilizada'):
            views._finalizar_compra_com_financeiro(draft)
        self.assertEqual(self.snapshot(),before)

    def test_new_purchase_persists_explicit_package_without_conversion(self):
        product=self.items[0].produto
        response=self.client.post(reverse('estoque:compras_nova'),{
            'fornecedor_id':str(self.supplier.pk),'data_compra':'2026-10-23',
            'tipo_pagamento':'aprazo','data_vencimento':'2026-11-23',
            'produto_id[]':[str(product.pk)],'quantidade[]':['1'],
            'preco_unitario[]':['10'],'unidade[]':['FD'],'fechamento_token':'sintetico-explicito'})
        self.assertEqual(response.status_code,302)
        purchase=Compra.objects.get(fechamento_token='sintetico-explicito')
        self.assertEqual(purchase.itens.get().unidade,'FD')
        self.assertEqual(purchase.itens.get().quantidade,Decimal('1'))
        product.refresh_from_db()
        self.assertEqual(product.quantidade,Decimal('8'))

    def test_new_draft_preserves_explicit_commercial_unit_and_quantity(self):
        from django.test import RequestFactory
        draft=Compra.objects.create(fornecedor=self.supplier,data_compra=date(2026,10,23),status=Compra.STATUS_RASCUNHO)
        request=RequestFactory().post('/',{'fornecedor_id':str(self.supplier.pk),'data_compra':'2026-10-23',
            'produto_id[]':[str(self.items[1].produto_id)],'quantidade[]':['25'],
            'preco_unitario[]':['10'],'unidade[]':['UN']})
        before=list(Produto.objects.values())
        data=views._dados_compra_post(request,exigir_itens=False)
        views._salvar_compra_e_itens(draft,data,Compra.STATUS_RASCUNHO)
        saved=draft.itens.get()
        self.assertEqual((saved.quantidade,saved.unidade,saved.valor_total),(Decimal('25'),'UN',Decimal('250')))
        self.assertEqual(list(Produto.objects.values()),before)


from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
class UnidadesHistoricasBrowserTests(StaticLiveServerTestCase):
    setUp=UnidadesHistoricasCompraTests.setUp

    def test_finalized_editor_does_not_autofill_missing_historical_units(self):
        before=list(ItemCompra.objects.values())
        with TemporaryDirectory(prefix='historic-units-') as profile:
            browser=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=browser.tab()
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                for width in [1280,320,360,390]:
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=844,deviceScaleFactor=1,mobile=width<860))
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compra_editar',args=[self.purchase.pk])})
                    tab.wait('document.readyState==="complete" && !!document.getElementById("formCompra")')
                    self.assertEqual(tab.evaluate('[...document.querySelectorAll(\'#tabelaItensCompra [name="unidade[]"]\')].map(i=>i.value)'),['']*10)
                    self.assertTrue(tab.evaluate('document.body.textContent.includes("Unidade hist")'))
            finally:
                browser.stop()
        self.assertEqual(list(ItemCompra.objects.values()),before)
