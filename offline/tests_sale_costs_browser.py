"""Real browser, temporary profile and synthetic sale data only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from estoque.models import Produto, Venda, ItemVenda, ContaReceber
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_online_sales_browser as online


@skipUnless(Path(online.CHROME).is_file(), 'Chrome unavailable')
class SaleCostBrowserTests(StaticLiveServerTestCase):
    setUp=online.OnlineSaleBrowserTests.setUp
    open_sales=online.OnlineSaleBrowserTests.open_sales
    select_customer=online.OnlineSaleBrowserTests.select_customer
    add_product=online.OnlineSaleBrowserTests.add_product
    repository=online.OnlineSaleBrowserTests.repository

    def browser(self):
        profile=TemporaryDirectory(prefix='sale-cost-fictional-',ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        browser=Chrome(online.CHROME,profile.name).start()
        self.addCleanup(browser.stop)
        return browser

    def prepare(self):
        t=online.prepare(self,self.browser())
        t.evaluate("window.costs=await import('/offline/assets/sale-cost-check-v1/sales-costs.js');window.costDrafts=await import('/offline/assets/sale-cost-check-v1/sales-drafts.js');window.initialDraft=await costDrafts.loadDraft(repo,scope);window.initialAssembly=JSON.stringify(salesDraftBridge.capture());true")
        return t

    def assert_preserved(self,t):
        self.assertEqual(t.evaluate('JSON.stringify(salesDraftBridge.capture())'), t.evaluate('initialAssembly'))
        self.assertEqual(t.evaluate('(await costDrafts.loadDraft(repo,scope)).draft.draft_id'),t.evaluate('initialDraft.draft.draft_id'))
        self.assertEqual(t.evaluate('(await repo.all("operations")).length'),0)
        self.assertEqual(OperacaoSincronizacao.objects.count(),0)
        self.assertEqual(Venda.objects.count(),0)
        self.assertFalse(t.evaluate('clienteBusca.disabled'))

    def test_online_changed_cost_modal_refresh_same_draft_and_sale(self):
        t=self.prepare()
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=30)
        t.evaluate('btnGravarVenda.click();true')
        t.wait('document.getElementById("sale-cost-dialog")?.open && !btnGravarVenda.disabled')
        self.assertIn('Produto Fracionado',t.evaluate('document.getElementById("sale-cost-dialog").textContent'))
        self.assertIn('30,00',t.evaluate('document.getElementById("sale-cost-dialog").textContent'))
        self.assertEqual(t.evaluate('document.querySelector("#sale-cost-dialog a").getAttribute("target")'),'_blank')
        self.assert_preserved(t)
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=12)
        t.evaluate('document.querySelector("#sale-cost-dialog button").click();true')
        t.wait('!document.getElementById("sale-cost-dialog")')
        self.assert_preserved(t)
        t.evaluate('btnGravarVenda.click();true')
        t.wait('document.getElementById("btnGravarVenda").textContent==="Venda gravada!"')
        self.assertEqual(str(OperacaoSincronizacao.objects.get().operation_id),t.evaluate('initialDraft.draft.draft_id'))
        self.assertEqual(Venda.objects.count(),1)
        self.assertEqual(ItemVenda.objects.count(),1)
        self.assertEqual(ContaReceber.objects.count(),1)

    def test_multiple_items_can_be_edited_but_not_finalized_below_cost(self):
        t=self.prepare()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=True))
        self.add_product(t,'Segundo Produto','1')
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=30)
        Produto.objects.filter(pk=self.second_product.pk).update(preco_compra=10)
        t.evaluate('window.initialAssembly=JSON.stringify(salesDraftBridge.capture());btnGravarVenda.click();true')
        t.wait('document.getElementById("sale-cost-dialog")?.open && !btnGravarVenda.disabled')
        self.assertEqual(t.evaluate('document.querySelectorAll("#sale-cost-dialog section").length'),2)
        self.assertTrue(t.evaluate('(()=>{const d=document.getElementById("sale-cost-dialog"),r=d.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && d.scrollWidth<=d.clientWidth})()'))
        self.assertTrue(t.evaluate('[...document.querySelectorAll("#sale-cost-dialog button, #sale-cost-dialog a")].every(b=>b.getBoundingClientRect().height>=44)'))
        self.assert_preserved(t)
        t.evaluate('[...document.querySelectorAll("#sale-cost-dialog button")].find(b=>b.textContent==="Voltar à venda").click();true')
        self.assertFalse(t.evaluate('!!document.getElementById("sale-cost-dialog")'))
        self.assertFalse(t.evaluate('btnGravarVenda.disabled'))

    def offline_reference(self,t):
        t.evaluate("window.commercial=await import('/offline/assets/2-8f-fix/commercial.js');await commercial.atualizarSnapshotComercial(repo,scope);true")
        t.call('Network.emulateNetworkConditions',dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))

    def finalize_direct(self,t):
        t.evaluate('try{window.costResult=await costDrafts.finalizeDraftOffline(repo,scope,{revision:initialDraft.revision,draft_id:initialDraft.draft.draft_id})}catch(e){window.costError=e.message}true')

    def test_offline_stale_cost_is_labelled_and_draft_is_preserved(self):
        t=self.prepare()
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=30)
        self.offline_reference(t)
        # The server changes again; offline data must never be called current.
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=12)
        self.finalize_direct(t)
        self.assertTrue(t.evaluate('document.getElementById("sale-cost-dialog").open'))
        self.assertIn('pode estar desatualizada',t.evaluate('document.getElementById("sale-cost-dialog").textContent'))
        t.evaluate('document.querySelector("#sale-cost-dialog a").click();true')
        self.assertIn('Conecte-se para abrir o cadastro',t.evaluate('document.querySelector("#sale-cost-dialog [role=status]").textContent'))
        self.assert_preserved(t)
        t.call('Network.emulateNetworkConditions',dict(offline=False,latency=0,downloadThroughput=0,uploadThroughput=0))
        t.evaluate('document.querySelector("#sale-cost-dialog button").click();true')
        t.wait('!document.getElementById("sale-cost-dialog")')
        self.assert_preserved(t)

    def test_offline_missing_reference_blocks_before_operation(self):
        t=self.prepare()
        t.call('Network.emulateNetworkConditions',dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
        self.finalize_direct(t)
        self.assertIn('Não há custos de referência',t.evaluate('document.getElementById("sale-cost-dialog").textContent'))
        self.assert_preserved(t)

    def test_local_fractional_precision_equal_cost_and_safe_finalization(self):
        t=self.prepare()
        result=t.evaluate("costs.inspectLocalCosts([{produto_id:'1',unidade:'UN',preco_unitario:'5.02'},{produto_id:'1',unidade:'UN',preco_unitario:'5.01'}],{produtos:[{id:'1',ativo:true,nome:'Fracionado',unidade_venda_1:'PCT',unidade_venda_2:'UN',vende_fracionado:true,custo_referencia:'10.05',custo_fracionado_referencia:'0',fator_conversao:'2'}]})")
        self.assertEqual([(r['custo'],r['estado']) for r in result],[('5.02','ok'),('5.02','abaixo')])
        self.offline_reference(t)
        self.finalize_direct(t)
        self.assertEqual(t.evaluate('(await repo.all("operations")).length'),1)
        self.assertEqual(t.evaluate('(await repo.all("operations"))[0].operation_id'),t.evaluate('initialDraft.draft.draft_id'))
        self.assertEqual(Venda.objects.count(),0)

    def test_online_lookup_failure_never_finalizes_or_replaces_draft(self):
        t=self.prepare()
        t.evaluate("window.beforeCostFetch=fetch;fetch=(u,o)=>String(u)==='/api/vendas/online/custos/'?Promise.resolve(new Response('{}',{status:503})):beforeCostFetch(u,o);btnGravarVenda.click();true")
        t.wait('document.getElementById("sale-cost-dialog")?.open && !btnGravarVenda.disabled')
        self.assert_preserved(t)
        self.assertIn('conexão',t.evaluate('document.getElementById("sale-cost-dialog").textContent'))

    def test_below_cost_line_can_be_edited_and_is_blocked_only_at_finalization(self):
        t=self.prepare()
        t.evaluate('document.querySelector("#tabelaProdutos tr").click();preco.value="5";preco.dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarItemVenda").click();await salesDraftUI.flush();window.initialAssembly=JSON.stringify(salesDraftBridge.capture());btnGravarVenda.click();true')
        t.wait('document.getElementById("sale-cost-dialog")?.open && !btnGravarVenda.disabled')
        self.assertEqual(t.evaluate('salesDraftBridge.capture().itens[0].preco_unitario'),'5.00')
        self.assert_preserved(t)

    @override_settings(LOCAL_SALES_LOOPBACK=True)
    def test_anonymous_local_sale_checks_cost_before_creating_command(self):
        from . import tests_local_sales_browser as local
        t=local.LocalSaleBrowserTests.open(self,self.browser())
        local.LocalSaleBrowserTests.prepare(self,t)
        before=t.evaluate('JSON.stringify(salesDraftBridge.capture())')
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=30)
        t.evaluate('btnGravarVenda.click();true')
        t.wait('document.getElementById("sale-cost-dialog")?.open && !btnGravarVenda.disabled')
        self.assertIsNone(t.evaluate('localSales.record.command || null'))
        self.assertEqual(t.evaluate('JSON.stringify(salesDraftBridge.capture())'),before)
        self.assertEqual(OperacaoSincronizacao.objects.count(),0)
