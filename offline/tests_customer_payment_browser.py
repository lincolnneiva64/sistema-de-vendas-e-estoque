"""Delayed customer details must preserve commercial choices before submission."""
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from estoque.models import Venda, ItemVenda, DespesaDiaria, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sales_drafts_browser as drafts
from . import tests_local_sales_browser as local


# Hold actual server responses until each test releases them; no fixed sleeps.
DETAIL_GATE = r"""
window.detailRequests=[];window.salePosts=[];
const customerRealFetch=window.fetch;
window.fetch=async function(url,options){
    if(String(url)==='/api/vendas/online/' && options?.method==='POST') salePosts.push(JSON.parse(options.body));
    const detailRefresh=new Error().stack.includes('atualizarClienteSelecionadoDoServidor');
    const response=await customerRealFetch(url,options);
    if(detailRefresh && String(url).includes('cliente_id=') && String(url).includes('contexto=venda')){
        const entry={id:new URL(url,location.href).searchParams.get('cliente_id'),done:false};
        detailRequests.push(entry);
        await new Promise(resolve=>entry.release=resolve);
        const readJson=response.json.bind(response);
        response.json=async()=>{
            const data=await readJson();
            if(entry.overrides && data.clientes?.length) data.clientes[0]={...data.clientes[0],...entry.overrides};
            setTimeout(()=>entry.done=true,0);
            return data;
        };
    }
    return response;
};
"""


@skipUnless(Path(drafts.CHROME).is_file(), 'Chrome unavailable')
class CustomerPaymentBrowserTests(StaticLiveServerTestCase):
    reset_sequences=True
    setUp=drafts.SalesDraftBrowserTests.setUp
    open_sales=drafts.SalesDraftBrowserTests.open_sales
    select_customer=drafts.SalesDraftBrowserTests.select_customer
    add_product=drafts.SalesDraftBrowserTests.add_product

    def open(self,chrome,local_mode=False):
        tab=local.LocalSaleBrowserTests.open(self,chrome) if local_mode else self.open_sales(chrome)
        tab.evaluate(DETAIL_GATE+';true')
        return tab

    def release(self,tab,index,overrides=None):
        tab.wait('!!detailRequests['+str(index)+']?.release')
        tab.evaluate('detailRequests['+str(index)+'].overrides='+json.dumps(overrides or {})+';detailRequests['+str(index)+'].release();true')
        tab.wait('detailRequests['+str(index)+'].done')

    def delayed_sale(self,payment,local_mode=False):
        with TemporaryDirectory(prefix='customer-payment-') as profile:
            chrome=Chrome(drafts.CHROME,profile).start()
            try:
                tab=self.open(chrome,local_mode)
                self.select_customer(tab,'Cliente Comercial')
                self.assertEqual(tab.evaluate('tipoVenda.value'),'A prazo')
                self.add_product(tab,'Produto Fracionado','1')
                tab.evaluate('tipoVenda.value='+json.dumps(payment)+';tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));true')
                self.release(tab,0)
                self.assertEqual(tab.evaluate('tipoVenda.value'),payment)
                if local_mode:
                    tab.evaluate('await localSales.submit(salesDraftBridge.getData());true')
                else:
                    origin='{caixa:"10",banco:"14"}' if payment=='\u00c0 vista' else 'undefined'
                    tab.evaluate('await salesDraftUI.submitOnline('+origin+',decodeURIComponent(document.cookie.split("; ").find(c=>c.startsWith("csrftoken=")).split("=")[1]));true')
                self.assertEqual(tab.evaluate('salePosts.length'),1)
                payload=tab.evaluate('salePosts[0].payload')
                data=payload['dados'] if local_mode else payload
                self.assertEqual(data['tipo_pagamento'],payment)
                sale=Venda.objects.get()
                self.assertEqual(sale.total,Decimal('24'))
                self.assertEqual(ItemVenda.objects.get().custo_total_snapshot,Decimal('12'))
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade,Decimal('1.5'))
                self.assertEqual(OperacaoSincronizacao.objects.count(),1)
                if payment=='consumo_proprio':
                    self.assertEqual(sale.tipo_pagamento,'consumo_proprio')
                    self.assertEqual(DespesaDiaria.objects.get(venda_origem=sale).valor,sale.total)
                    self.assertEqual(ContaReceber.objects.count(),0)
                    self.assertEqual(MovimentoFinanceiro.objects.count(),0)
                elif payment=='A prazo':
                    self.assertEqual(sale.tipo_pagamento,'A prazo')
                    self.assertEqual(ContaReceber.objects.count(),1)
                    self.assertEqual(DespesaDiaria.objects.count(),0)
                    self.assertEqual(MovimentoFinanceiro.objects.count(),0)
                else:
                    self.assertEqual(ContaReceber.objects.count(),0)
                    self.assertEqual(DespesaDiaria.objects.count(),0)
                    self.assertEqual(MovimentoFinanceiro.objects.count(),2)
            finally:chrome.stop()

    def test_delayed_consumption_preserves_wire_and_effects(self):
        self.delayed_sale('consumo_proprio')

    def test_delayed_cash_preserves_wire_and_effects(self):
        self.delayed_sale('\u00c0 vista')

    def test_delayed_credit_preserves_wire_and_effects(self):
        self.delayed_sale('A prazo')

    @override_settings(LOCAL_SALES_LOOPBACK=True)
    def test_delayed_local_consumption_preserves_wire_and_effects(self):
        self.delayed_sale('consumo_proprio',local_mode=True)

    def test_initial_defaults_and_manually_changed_due_date(self):
        with TemporaryDirectory(prefix='customer-due-date-') as profile:
            chrome=Chrome(drafts.CHROME,profile).start()
            try:
                tab=self.open(chrome)
                self.select_customer(tab,'Cliente Comercial')
                initial=tab.evaluate('dataVenda.value')
                self.assertEqual(tab.evaluate('vencimentoVenda.value'),(date.fromisoformat(initial)+timedelta(days=14)).isoformat())
                # Untouched conditions still accept refreshed server defaults.
                self.release(tab,0,{'prazo':21})
                self.assertEqual(tab.evaluate('vencimentoVenda.value'),(date.fromisoformat(initial)+timedelta(days=21)).isoformat())
                self.select_customer(tab,'Segundo Cliente')
                self.assertEqual(tab.evaluate('tipoVenda.value'),'A prazo')
                self.assertEqual(tab.evaluate('vencimentoVenda.value'),(date.fromisoformat(initial)+timedelta(days=7)).isoformat())
                due=(date.fromisoformat(initial)+timedelta(days=30)).isoformat()
                tab.evaluate('vencimentoVenda.value='+json.dumps(due)+';vencimentoVenda.dispatchEvent(new Event("input",{bubbles:true}));true')
                self.release(tab,1)
                self.assertEqual(tab.evaluate('vencimentoVenda.value'),due)
                self.assertEqual(tab.evaluate('tipoVenda.value'),'A prazo')
            finally:chrome.stop()

    def test_same_value_manual_choice_is_preserved(self):
        with TemporaryDirectory(prefix='customer-same-payment-') as profile:
            chrome=Chrome(drafts.CHROME,profile).start()
            try:
                tab=self.open(chrome)
                self.select_customer(tab,'Cliente Comercial')
                tab.evaluate('tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));true')
                self.release(tab,0,{'prazo':0})
                self.assertEqual(tab.evaluate('tipoVenda.value'),'A prazo')
            finally:chrome.stop()

    def test_fast_customer_switch_and_old_response_after_return(self):
        with TemporaryDirectory(prefix='customer-switch-') as profile:
            chrome=Chrome(drafts.CHROME,profile).start()
            try:
                tab=self.open(chrome)
                self.select_customer(tab,'Cliente Comercial')
                tab.wait('!!detailRequests[0]?.release')
                self.select_customer(tab,'Segundo Cliente')
                tab.wait('!!detailRequests[1]?.release')
                self.assertEqual(tab.evaluate('clienteId.value'),str(self.second_customer.pk))
                self.assertEqual(tab.evaluate('clienteBusca.value'),'Segundo Cliente')
                self.select_customer(tab,'Cliente Comercial')
                tab.wait('!!detailRequests[2]?.release')
                self.release(tab,2,{'nome':'Cliente Comercial atualizado','prazo':21})
                # A -> B -> A: even an old response for the current ID is stale.
                self.release(tab,0,{'nome':'Resposta antiga A','prazo':90})
                self.release(tab,1,{'nome':'Resposta antiga B','prazo':90})
                self.assertEqual(tab.evaluate('clienteId.value'),str(self.customer.pk))
                self.assertEqual(tab.evaluate('clienteBusca.value'),'Cliente Comercial atualizado')
                initial=tab.evaluate('dataVenda.value')
                self.assertEqual(tab.evaluate('vencimentoVenda.value'),(date.fromisoformat(initial)+timedelta(days=21)).isoformat())
            finally:chrome.stop()
