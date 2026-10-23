"""Local PostgreSQL-only measurements and regression of purchase GET rendering."""
import json
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from .models import Produto, Compra, ItemCompra, Fornecedor, ListaCompraFornecedor, ContaPagar, MovimentoFinanceiro, ContaFinanceira
from .tests_precos_vinculados import criar_operador_precos
from offline.browser_support import Chrome

CHROME=r'C:\Program Files\Google\Chrome\Application\chrome.exe'

class OpeningFixture:
    def setUp(self):
        self.user=criar_operador_precos('opening-fixture');self.client.force_login(self.user)
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(Permission.objects.get(codename='change_compra',content_type__app_label='estoque'))
        self.fornecedor=Fornecedor.objects.create(nome='Fornecedor sintetico')
        self.lista=ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,data_lista=timezone.localdate(),data_inicio_periodo=timezone.localdate(),data_fim_periodo=timezone.localdate())
        self.products=[Produto.objects.create(nome=f'Produto sintetico {i:04}',unidade_compra='FD',unidade_venda_1='FD',unidade_venda_2='UN',fator_conversao=24,vende_fracionado=True,quantidade=240,preco_compra=100,preco_vista=146,preco_prazo=148,preco_vista_fracionado=Decimal('6.20'),preco_prazo_fracionado=Decimal('6.30')) for i in range(300)]
        from .views import _garantir_contas_financeiras_padrao
        _garantir_contas_financeiras_padrao()  # Provision fixture, before measuring/snapshotting GET.

    def purchase(self,count):
        self.lista=ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,data_lista=timezone.localdate(),data_inicio_periodo=timezone.localdate(),data_fim_periodo=timezone.localdate())
        compra=Compra.objects.create(fornecedor=self.fornecedor,lista_fornecedor=self.lista,data_compra=timezone.localdate(),status=Compra.STATUS_RASCUNHO,total=Decimal(count)*50)
        for p in self.products[:count]:ItemCompra.objects.create(compra=compra,produto=p,quantidade=Decimal('.5'),unidade='FD',preco_unitario=100,valor_total=50,observacao='Preservar observacao')
        return compra

    def snapshot(self):
        return [list(model.objects.order_by('pk').values()) for model in (Produto,Compra,ItemCompra,ContaPagar,MovimentoFinanceiro,ContaFinanceira)]

class PurchaseOpeningMeasurements(OpeningFixture,TestCase):
    def test_measure_15_30_100(self):
        from . import views
        for count in (15,30,100):
            compra=self.purchase(count);before=self.snapshot();render=[];sql=[]
            original=views.render
            def measured(*args,**kwargs):
                start=time.perf_counter();response=original(*args,**kwargs);render.append(time.perf_counter()-start);return response
            def measure_sql(execute,statement,params,many,context):
                start=time.perf_counter()
                try:return execute(statement,params,many,context)
                finally:sql.append(time.perf_counter()-start)
            with patch.object(views,'render',side_effect=measured),CaptureQueriesContext(connection) as queries,connection.execute_wrapper(measure_sql):
                start=time.perf_counter();response=self.client.get(reverse('estoque:compra_editar',args=[compra.pk]));elapsed=time.perf_counter()-start
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.snapshot(),before)
            self.assertEqual(len(response.context['item_linhas']),count)
            self.assertEqual(response.context['lista_origem_compra'].pk,self.lista.pk)
            self.assertTrue(all(row['quantidade']=='0,500' and row['unidade']=='FD' for row in response.context['item_linhas']))
            self.assertFalse(any('FOR UPDATE' in q['sql'].upper() for q in queries))
            print('OPENING_HTTP='+json.dumps(dict(items=count,catalog=300,total_ms=round(elapsed*1000,2),render_ms=round(sum(render)*1000,2),sql_ms=round(sum(sql)*1000,2),queries=len(queries),bytes=len(response.content),options=response.content.count(b'<option value='))),flush=True)

@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
class PurchaseOpeningBrowser(OpeningFixture,StaticLiveServerTestCase):
    host='127.0.0.1'
    def scenario(self,width,count):
        compra=self.purchase(count);before=self.snapshot()
        with TemporaryDirectory(prefix='opening-chrome-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.socket.settimeout(120);tab.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
                tab.call('Network.setCookie',dict(name='sessionid',value=self.client.cookies['sessionid'].value,url=self.live_server_url))
                start=time.perf_counter();tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compra_editar',args=[compra.pk])+'?continuar_itens=1'})
                tab.wait('document.readyState==="complete" && !!document.getElementById("btnSalvarCompra")',timeout=120)
                elapsed=time.perf_counter()-start
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaItensCompra .linha-item").length'),count)
                self.assertEqual(tab.evaluate('Array.from(document.querySelectorAll("#tabelaItensCompra .linha-item select")).map(s=>s.value)'),[str(p.pk) for p in self.products[:count]])
                print('OPENING_BROWSER='+json.dumps(dict(items=count,width=width,total_ms=round(elapsed*1000,2),performance=tab.evaluate('performance.getEntriesByType("navigation").map(n=>({response:n.responseEnd,dom:n.domContentLoadedEventEnd,load:n.loadEventEnd}))'),requests=tab.evaluate('performance.getEntriesByType("resource").map(r=>({url:new URL(r.name).pathname,type:r.initiatorType,ms:r.duration}))'),options=tab.evaluate('document.querySelectorAll("option").length'))),flush=True)
                self.assertEqual(self.snapshot(),before)
            finally:chrome.stop()
    def test_desktop_15(self):self.scenario(1280,15)
    def test_mobile_30(self):self.scenario(390,30)
    def test_desktop_100(self):self.scenario(1280,100)

class PurchaseOpeningRegressionTests(OpeningFixture,TestCase):
    def test_catalog_once_and_fresh_values_each_get(self):
        compra=self.purchase(15);url=reverse('estoque:compra_editar',args=[compra.pk])
        response=self.client.get(url)
        self.assertEqual(response.content.count(b'data-consulta-pc='),300)
        self.assertLess(len(response.content),900000)
        from django.db import models
        models.QuerySet.update(Produto.objects.filter(pk=self.products[0].pk),quantidade=123,preco_vista=Decimal('147'))
        fresh=self.client.get(url)
        self.assertContains(fresh,'data-estoque="123,000"')
        self.assertContains(fresh,'data-preco-vista="147,00"')

    def test_finalized_pending_review_open_is_read_only_and_permissions_remain(self):
        compra=self.purchase(15)
        compra.status=Compra.STATUS_FINALIZADA;compra.estoque_entrada_realizada=True
        compra.revisao_precos_pendente=True;compra.tipo_pagamento='aprazo';compra.save()
        ContaPagar.objects.create(compra=compra,data_emissao=timezone.localdate(),valor_original=compra.total,valor_em_aberto=compra.total)
        Produto.objects.filter(pk=self.products[0].pk).update(ativo=False)
        before=self.snapshot();url=reverse('estoque:compra_editar',args=[compra.pk])
        response=self.client.get(url)
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['edicao_finalizada'])
        self.assertIn(self.products[0].pk,[p.pk for p in response.context['produtos']])
        self.assertEqual(self.snapshot(),before)
        self.client.logout();self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.snapshot(),before)

@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
class PurchaseOpeningInteraction(OpeningFixture,StaticLiveServerTestCase):
    host='127.0.0.1'
    def test_select_catalog_product_outside_original_row_and_save_draft(self):
        compra=self.purchase(15);stock=list(Produto.objects.values_list('quantidade',flat=True))
        with TemporaryDirectory(prefix='opening-select-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Emulation.setDeviceMetricsOverride',dict(width=1280,height=900,deviceScaleFactor=1,mobile=False));tab.call('Network.setCookie',dict(name='sessionid',value=self.client.cookies['sessionid'].value,url=self.live_server_url))
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compra_editar',args=[compra.pk])+'?continuar_itens=1'})
                tab.wait('document.readyState==="complete" && typeof window.compraCriarLinhaItem==="function"')
                self.assertTrue(tab.evaluate("(()=>{const select=document.querySelectorAll('#tabelaItensCompra .linha-item select')[1],before=select.value;window.garantirOpcaoProdutoCompra(select,window.opcoesCatalogoProdutosCompra()[0].value);return select.value===before;})()"))
                tab.evaluate('''const row=document.querySelector('#tabelaItensCompra .linha-item:first-child');
                    const input=row.querySelector('.produto-busca');input.focus();input.value='Produto sintetico 0299';input.dispatchEvent(new Event('input',{bubbles:true}));true''')
                tab.wait('!!document.querySelector("#tabelaItensCompra .linha-item:first-child .produto-sugestao")')
                tab.evaluate('document.querySelector("#tabelaItensCompra .linha-item:first-child .produto-sugestao").click();true')
                data=tab.evaluate('''(()=>{const row=document.querySelector('#tabelaItensCompra .linha-item:first-child'),select=row.querySelector('select'),option=select.selectedOptions[0];return {id:select.value,factor:option.dataset.fatorConversao,price:option.dataset.precoPrazoFracionado,unit:row.querySelector('[name="unidade[]"]').value};})()''')
                self.assertEqual(data['id'],str(self.products[-1].pk));self.assertEqual(data['unit'],'FD')
                self.assertEqual(data['factor'],'24,00');self.assertEqual(data['price'],'6,30')
                tab.evaluate('''const row=document.querySelector('#tabelaItensCompra .linha-item:first-child');row.querySelector('[name="quantidade[]"]').value='0,5';
                    row.querySelector('.campo-preco-unitario-compra').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));document.getElementById('btnSalvarRascunhoCompra').click();true''')
                tab.wait('location.search.includes("rascunho_salvo") && document.readyState==="complete"')
                self.assertEqual(compra.itens.count(),15)
                item=compra.itens.get(produto=self.products[-1]);self.assertEqual(item.quantidade,Decimal('.5'));self.assertEqual(item.preco_unitario,100);self.assertEqual(item.unidade,'FD')
                self.assertEqual(list(Produto.objects.values_list('quantidade',flat=True)),stock)
                self.assertFalse(ContaPagar.objects.exists());self.assertFalse(MovimentoFinanceiro.objects.exists())
            finally:chrome.stop()
