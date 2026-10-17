"""Real Chrome, anonymous session, private profile and disposable test DB."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings, Client
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .tests_commercial import commercial_fixtures
from .tests_sales_drafts_browser import CHROME
from . import tests_sales_drafts_browser as helpers
from .browser_support import Chrome


@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
@override_settings(LOCAL_SALES_LOOPBACK=True)
class LocalSaleBrowserTests(StaticLiveServerTestCase):
    reset_sequences=True
    select_customer=helpers.SalesDraftBrowserTests.select_customer
    add_product=helpers.SalesDraftBrowserTests.add_product

    def setUp(self):
        self.user,self.customer,self.product,self.operator,*_=commercial_fixtures()

    def open(self,chrome,url=None,source=None):
        tab=chrome.tab()
        if source:tab.call('Page.addScriptToEvaluateOnNewDocument',{'source':source})
        tab.call('Page.navigate',{'url':url or self.live_server_url+'/vendas/'})
        tab.wait('window.localSales && window.salesDraftUI?.ready === true')
        return tab

    def prepare(self,tab):
        self.select_customer(tab,'Cliente Comercial')
        self.add_product(tab,'Produto Fracionado','1')
        tab.evaluate('tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')

    def reload(self,tab):
        helpers.SalesDraftBrowserTests.reload(self,tab)
        tab.wait('!!window.localSales')

    def test_anonymous_normal_button_f5_and_private_drafts(self):
        with TemporaryDirectory(prefix='local-sale-browser-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome)
                tab.evaluate("window.core=await import('/offline/assets/2-8f/core.js');window.repo=new core.Repository(await core.openDB());await repo.put('metadata',{key:'sales-identity',actor_id:'987',environment_id:'protected'});await repo.put('metadata',{key:'rascunho_venda:protected-sentinel',payload:'preservar'});true")
                self.prepare(tab)
                key=tab.evaluate('localSales.key')
                self.reload(tab)
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===1')
                self.assertEqual(tab.evaluate('localSales.key'),key)
                self.assertFalse(tab.evaluate('document.getElementById("produtoBusca").disabled'))
                tab.evaluate('btnGravarVenda.click();true')
                tab.wait('localSales.record.result?.sucesso===true || localSales.record.lastReceipt?.sucesso===true')
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(ItemVenda.objects.count(),1)
                self.assertEqual(ContaReceber.objects.count(),1)
                self.assertIsNone(OperacaoSincronizacao.objects.get().actor_id)
                tab.evaluate("window.repo=new (await import('/offline/assets/2-8f/core.js')).Repository(await (await import('/offline/assets/2-8f/core.js')).openDB());true")
                self.assertEqual(tab.evaluate("(await repo.get('metadata','rascunho_venda:protected-sentinel')).payload"),'preservar')
                self.assertEqual(tab.evaluate("(await repo.get('metadata','sales-identity')).actor_id"),'987')
            finally:chrome.stop()

    def test_lost_response_f5_lookup_and_same_uuid(self):
        with TemporaryDirectory(prefix='local-sale-loss-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome);self.prepare(tab)
                tab.evaluate("window.realFetch=fetch;window.fetch=async(url,options)=>{const response=await realFetch(url,options);if(String(url)==='/api/vendas/online/'&&options?.method==='POST')throw Error('Resposta perdida');return response};btnGravarVenda.click();true")
                tab.wait('!!localSales.record.command && !localSales.record.result && document.getElementById("btnGravarVenda").disabled')
                # Wait for the real HTTP commit, not just the locally reserved command.
                tab.wait('!document.getElementById("local-sale-query").hidden')
                import time
                deadline=time.monotonic()+10
                while Venda.objects.count()!=1 and time.monotonic()<deadline:time.sleep(.05)
                self.assertEqual(Venda.objects.count(),1)
                identifier=tab.evaluate('localSales.record.command.operation_id')
                self.reload(tab)
                self.assertEqual(tab.evaluate('localSales.record.command.operation_id'),identifier)
                tab.evaluate('document.getElementById("local-sale-replay").click();true')
                tab.wait('localSales.record.result?.sucesso===true || localSales.record.lastReceipt?.sucesso===true')
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(ContaReceber.objects.count(),1)
                self.assertEqual(OperacaoSincronizacao.objects.count(),1)
            finally:chrome.stop()

    def test_multiple_tabs_mobile_and_real_user_change(self):
        with TemporaryDirectory(prefix='local-sale-mobile-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome)
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                self.prepare(tab)
                other=self.open(chrome)
                self.assertNotEqual(tab.evaluate('localSales.key'),other.evaluate('localSales.key'))
                self.assertEqual(other.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),0)
                browser=Client();browser.force_login(self.user)
                tab.call('Network.setCookie',{'name':'sessionid','value':browser.cookies['sessionid'].value,'url':self.live_server_url})
                tab.evaluate('window.dispatchEvent(new Event("focus"));true')
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("Identidade")')
                self.assertTrue(tab.evaluate('document.getElementById("produtoBusca").disabled'))
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),1)
                self.assertFalse(Venda.objects.exists())
            finally:chrome.stop()

    def test_mobile_finishes_without_login(self):
        with TemporaryDirectory(prefix='local-sale-mobile-save-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome)
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                self.prepare(tab);tab.evaluate('btnGravarVenda.click();true')
                tab.wait('localSales.record.result?.sucesso===true || localSales.record.lastReceipt?.sucesso===true')
                self.assertEqual(Venda.objects.count(),1)
                self.assertIsNone(OperacaoSincronizacao.objects.get().actor_id)
            finally:chrome.stop()

    def test_edit_restores_item_ids_and_saves_once(self):
        with TemporaryDirectory(prefix='local-sale-edit-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome);self.prepare(tab)
                tab.evaluate('btnGravarVenda.click();true');tab.wait('localSales.record.result?.sucesso===true || localSales.record.lastReceipt?.sucesso===true')
                sale=Venda.objects.get();item=ItemVenda.objects.get()
                edit=self.open(chrome,self.live_server_url+'/vendas/?editar='+str(sale.pk))
                edit.evaluate('document.querySelector("#tabelaProdutos tr").click();quantidade.value="2";document.getElementById("btnAdicionarItemVenda").click();true')
                edit.evaluate('await salesDraftUI.flush();true')
                self.reload(edit)
                self.assertEqual(edit.evaluate('document.querySelector("#tabelaProdutos tr").dataset.itemId'),str(item.pk))
                edit.evaluate('btnGravarVenda.click();true');edit.wait('localSales.record.result?.sucesso===true')
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(ItemVenda.objects.count(),1)
                item.refresh_from_db();self.assertEqual(str(item.quantidade),'2.000')
            finally:chrome.stop()

    def test_order_button_uses_operation_receipt(self):
        from estoque.models import Pedido,ItemPedido
        order=Pedido.objects.create(cliente=self.customer,data_pedido='2026-10-07',operador=self.operator.nome)
        ItemPedido.objects.create(pedido=order,produto=self.product,quantidade=1,unidade='CX',preco_unitario=24,valor_total=24)
        with TemporaryDirectory(prefix='local-sale-order-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome,self.live_server_url+'/vendas/?pedido_id='+str(order.pk))
                tab.evaluate('operadorVenda.value="Operador Comercial";tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));btnGravarVenda.click();true')
                tab.wait('localSales.record.result?.sucesso===true || localSales.record.lastReceipt?.sucesso===true')
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(OperacaoSincronizacao.objects.count(),1)
            finally:chrome.stop()

    def test_consecutive_sales_keep_distinct_receipts(self):
        with TemporaryDirectory(prefix='local-sale-consecutive-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome);self.prepare(tab)
                tab.evaluate('btnGravarVenda.click();true');tab.wait('!!localSales.record.lastReceipt')
                first=tab.evaluate('localSales.record.lastReceipt.venda_id')
                self.prepare(tab);tab.evaluate('btnGravarVenda.click();true')
                tab.wait('localSales.record.lastReceipt?.venda_id!=='+str(first)+' && !!localSales.record.lastReceipt')
                self.assertEqual(Venda.objects.count(),2);self.assertEqual(ItemVenda.objects.count(),2)
                self.assertEqual(ContaReceber.objects.count(),2);self.assertEqual(OperacaoSincronizacao.objects.count(),2)
            finally:chrome.stop()

    def test_expired_django_session_keeps_draft_and_allows_save(self):
        with TemporaryDirectory(prefix='local-sale-expiry-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                browser=Client();browser.force_login(self.user)
                tab=chrome.tab()
                tab.call('Network.setCookie',{'name':'sessionid','value':browser.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/vendas/'})
                tab.wait('window.localSales && window.salesDraftUI?.ready===true');self.prepare(tab)
                key=tab.evaluate('localSales.key')
                tab.call('Network.deleteCookies',{'name':'sessionid','url':self.live_server_url})
                self.reload(tab)
                self.assertEqual(tab.evaluate('localSales.key'),key)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),1)
                tab.evaluate('btnGravarVenda.click();true');tab.wait('!!localSales.record.lastReceipt')
                self.assertEqual(Venda.objects.count(),1)
                self.assertIsNone(OperacaoSincronizacao.objects.get().actor_id)
            finally:chrome.stop()

    def test_cloned_tab_gets_an_independent_draft(self):
        with TemporaryDirectory(prefix='local-sale-clone-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome);self.prepare(tab)
                identifier=tab.evaluate("sessionStorage.getItem('local-normal-sales-tab')")
                other=self.open(chrome,source="sessionStorage.setItem('local-normal-sales-tab',"+json.dumps(identifier)+")")
                self.assertNotEqual(other.evaluate('localSales.key'),tab.evaluate('localSales.key'))
                self.assertEqual(other.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),0)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),1)
            finally:chrome.stop()

    def test_closed_tab_draft_can_be_resumed_without_copying(self):
        with TemporaryDirectory(prefix='local-sale-resume-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome);self.prepare(tab)
                key=tab.evaluate('localSales.key')
                target=tab.call('Target.getTargetInfo')['targetInfo']['targetId']
                chrome.browser.call('Target.closeTarget',{'targetId':target})
                other=self.open(chrome)
                other.evaluate('document.getElementById("local-sales-recovery").value='+json.dumps(key)+';document.getElementById("local-sales-recover-button").click();true')
                other.wait('window.localSales?.key==='+json.dumps(key))
                self.assertEqual(other.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),1)
                self.assertFalse(Venda.objects.exists())
            finally:chrome.stop()

    def prepare_autosave_item(self, tab, quantity='2'):
        # Deliberately no salesDraftUI.flush(): exercise the actual autosave.
        self.select_customer(tab, 'Cliente Comercial')
        tab.evaluate('operadorVenda.value="Operador Comercial";operadorVenda.dispatchEvent(new Event("change",{bubbles:true}));tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));produtoBusca.value="Produto Fracionado";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
        tab.wait('!!document.querySelector(".produto-sugestao-item")')
        tab.evaluate('document.querySelector(".produto-sugestao-item").click();unidade.value="UN";unidade.dispatchEvent(new Event("change",{bubbles:true}));quantidade.value='+repr(quantity)+';quantidade.dispatchEvent(new Event("input",{bubbles:true}));preco.value="10,00";preco.dispatchEvent(new Event("input",{bubbles:true}));preco.focus();true')

    def confirm_autosave_item(self, tab, button=False):
        if button:
            tab.evaluate('document.getElementById("btnAdicionarItemVenda").click();true')
        else:
            tab.call('Input.dispatchKeyEvent', {'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
            tab.call('Input.dispatchKeyEvent', {'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
        tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===1')

    def wait_autosave_item(self, tab):
        tab.wait('(async()=>{const core=await import("/offline/assets/local-controlled-v1/core.js");const saved=await new core.Repository(await core.openDB()).get("metadata",localSales.key);return saved?.assembly?.itens?.length===1 && document.querySelector("#sales-draft-status [role=status]").textContent.includes("salvo");})()')

    def assert_autosave_restored(self, tab, quantity='2'):
        data=tab.evaluate('salesDraftBridge.getData()')
        self.assertEqual(data['operador'], 'Operador Comercial')
        self.assertEqual(str(data['cliente_id']), str(self.customer.pk))
        self.assertEqual(data['tipo_pagamento'], 'A prazo')
        self.assertEqual(len(data['itens']), 1)
        row=tab.evaluate('({...document.querySelector("#tabelaProdutos tr").dataset})')
        self.assertEqual(float(row['quantidade']), float(quantity))
        self.assertEqual(float(row['precoUnitario']), 10)
        self.assertIn('Produto Fracionado', tab.evaluate('document.querySelector("#tabelaProdutos tr").textContent'))
        self.assertEqual(float(data['total']), float(quantity)*10)

    def test_autosave_enter_and_button_restore_without_flush(self):
        with TemporaryDirectory(prefix='local-autosave-f5-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                for button in (False, True):
                    with self.subTest(button=button):
                        tab=self.open(chrome)
                        self.prepare_autosave_item(tab)
                        self.confirm_autosave_item(tab,button)
                        self.wait_autosave_item(tab)
                        self.reload(tab)
                        self.assert_autosave_restored(tab)
                self.assertEqual(Venda.objects.count(),0)
                self.assertEqual(OperacaoSincronizacao.objects.count(),0)
            finally:chrome.stop()

    def test_autosave_waits_for_commit_and_document_event(self):
        with TemporaryDirectory(prefix='local-autosave-commit-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=self.open(chrome)
                self.prepare_autosave_item(tab)
                tab.wait('document.querySelector("#sales-draft-status [role=status]").textContent.includes("salvo")')
                tab.evaluate('window.autosaveCore=await import("/offline/assets/local-controlled-v1/core.js");window.originalPut=autosaveCore.Repository.prototype.put;window.releaseAutosave=null;autosaveCore.Repository.prototype.put=async function(store,value){await new Promise(resolve=>{window.releaseAutosave=resolve;});return originalPut.call(this,store,value);};true')
                self.confirm_autosave_item(tab)
                tab.wait('typeof releaseAutosave==="function"')
                self.assertIn('Salvando',tab.evaluate('document.querySelector("#sales-draft-status [role=status]").textContent'))
                self.assertEqual(tab.evaluate('(await new autosaveCore.Repository(await autosaveCore.openDB()).get("metadata",localSales.key)).assembly.itens.length'),0)
                tab.evaluate('autosaveCore.Repository.prototype.put=originalPut;releaseAutosave();true')
                self.wait_autosave_item(tab)
                # Non-bubbling programmatic changes rely on this document event.
                tab.evaluate('operadorVenda.value="";document.dispatchEvent(new Event("sales-draft-change"));true')
                tab.wait('(async()=>{const saved=await new autosaveCore.Repository(await autosaveCore.openDB()).get("metadata",localSales.key);return saved.data.operador==="" && document.querySelector("#sales-draft-status [role=status]").textContent.includes("salvo");})()')
                self.reload(tab)
                self.assertEqual(tab.evaluate('operadorVenda.value'),'')
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),1)
            finally:chrome.stop()

    def test_autosave_independent_tabs_without_flush(self):
        with TemporaryDirectory(prefix='local-autosave-tabs-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                first=self.open(chrome);second=self.open(chrome)
                self.assertNotEqual(first.evaluate('localSales.key'),second.evaluate('localSales.key'))
                for tab,quantity in ((first,'2'),(second,'3')):
                    self.prepare_autosave_item(tab,quantity)
                    self.confirm_autosave_item(tab)
                    self.wait_autosave_item(tab)
                for tab,quantity in ((first,'2'),(second,'3')):
                    self.reload(tab);self.assert_autosave_restored(tab,quantity)
                self.assertEqual(Venda.objects.count(),0)
                self.assertEqual(ItemVenda.objects.count(),0)
                self.assertEqual(OperacaoSincronizacao.objects.count(),0)
            finally:chrome.stop()
