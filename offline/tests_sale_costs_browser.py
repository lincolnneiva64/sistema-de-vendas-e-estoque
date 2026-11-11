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

    def test_update_button_blocks_below_cost_without_changing_assembly(self):
        t=self.prepare()
        t.evaluate('document.querySelector("#tabelaProdutos tr").click();preco.value="5";preco.dispatchEvent(new Event("input",{bubbles:true}));window.initialAssembly=JSON.stringify(salesDraftBridge.capture());document.getElementById("btnAdicionarItemVenda").click();true')
        t.wait('!!document.getElementById("aviso-validacao")')
        self.assertIn('Preço de venda abaixo do custo!',t.evaluate('document.getElementById("aviso-validacao-texto").textContent'))
        self.assertEqual(t.evaluate('salesDraftBridge.capture().itens[0].preco_unitario'),'24.00')
        self.assert_preserved(t)
        t.evaluate('document.querySelector("#aviso-validacao button").click();true')
        self.assertTrue(t.evaluate('document.activeElement===preco && preco.selectionStart===0 && preco.selectionEnd===preco.value.length'))
        self.assertIn('Atualizar',t.evaluate('document.getElementById("btnAdicionarItemVenda").textContent'))

    @staticmethod
    def price_enter(t):
        t.evaluate('await new Promise(resolve=>setTimeout(resolve,220));preco.focus();true')
        t.call('Input.dispatchKeyEvent',{'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
        t.call('Input.dispatchKeyEvent',{'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})

    def pending_second(self,t):
        t.evaluate('produtoBusca.value="Segundo Produto";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
        t.wait('!!document.querySelector(".produto-sugestao-item")')
        t.evaluate('document.querySelector(".produto-sugestao-item").click();quantidade.value="1";preco.value="0,99";preco.dispatchEvent(new Event("input",{bubbles:true}));window.initialAssembly=JSON.stringify(salesDraftBridge.capture());true')

    def assert_compact_notice(self,t):
        layout=t.evaluate('''(()=>{const a=document.getElementById('aviso-validacao'),r=a.getBoundingClientRect(),s=getComputedStyle(a),b=a.querySelector('button').getBoundingClientRect();return {width:r.width,height:r.height,left:r.left,right:r.right,viewport:innerWidth,overflow:a.scrollWidth>a.clientWidth,buttonInside:b.left>=r.left&&b.right<=r.right,background:s.backgroundColor,border:s.borderTopColor,title:a.querySelector('strong').textContent}})()''')
        self.assertLess(layout['width'],420)
        self.assertLessEqual(layout['height'],130)
        self.assertGreaterEqual(layout['left'],12)
        self.assertLessEqual(layout['right'],layout['viewport']-12)
        self.assertFalse(layout['overflow'])
        self.assertTrue(layout['buttonInside'])
        self.assertTrue(t.evaluate('''(()=>{const a=document.getElementById('aviso-validacao');return [...a.querySelectorAll('strong, #aviso-validacao-texto > span, button')].every(el=>{const r=el.getBoundingClientRect();return a.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))})})()'''))
        self.assertEqual(layout['background'],'rgb(255, 247, 230)')
        self.assertEqual(layout['border'],'rgb(244, 211, 138)')
        self.assertEqual(layout['title'],'Preço de venda abaixo do custo!')

    def test_price_enter_shows_one_notice_and_valid_enter_adds_once(self):
        t=self.prepare()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=1280,height=900,deviceScaleFactor=1,mobile=False))
        self.pending_second(t)
        t.evaluate('window.priceNotices=0;new MutationObserver(records=>{for(const r of records)for(const n of r.addedNodes)if(n.id==="aviso-validacao")priceNotices++}).observe(document.body,{childList:true});window.pendingBefore=[produtoBusca.value,produto.value,quantidade.value,unidade.value,preco.value];true')
        self.price_enter(t)
        self.assertEqual(t.evaluate('priceNotices'),1)
        self.assertEqual(t.evaluate('document.querySelectorAll("#aviso-validacao button").length'),1)
        self.assertEqual(t.evaluate('document.querySelector("#aviso-validacao button").textContent'),'OK')
        self.assertEqual(
            [value.replace('\xa0',' ') for value in t.evaluate('Array.from(document.getElementById("aviso-validacao-texto").children).map(el=>el.textContent)')],
            ['Preço de venda abaixo do custo!','Venda: R$ 0,99 | Custo: R$ 1,00'])
        self.assert_compact_notice(t)
        self.assertEqual(t.evaluate('[produtoBusca.value,produto.value,quantidade.value,unidade.value,preco.value]'),t.evaluate('pendingBefore'))
        self.assert_preserved(t)
        t.evaluate('document.querySelector("#aviso-validacao button").click();true')
        self.assertTrue(t.evaluate('document.activeElement===preco && preco.selectionStart===0 && preco.selectionEnd===preco.value.length'))
        t.evaluate('preco.value="1";true')
        self.price_enter(t)
        self.assertEqual(t.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'),2)
        self.assertFalse(t.evaluate('!!document.getElementById("aviso-validacao")'))
        self.assertEqual(t.evaluate('priceNotices'),1)

    def test_add_button_blocks_without_enter_on_mobile(self):
        t=self.prepare()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=True))
        self.pending_second(t)
        t.evaluate('document.getElementById("btnAdicionarItemVenda").click();true')
        self.assertTrue(t.evaluate('!!document.getElementById("aviso-validacao")'))
        self.assert_preserved(t)
        for width in (390,320):
            t.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=844,deviceScaleFactor=1,mobile=True))
            self.assert_compact_notice(t)
        t.evaluate('document.querySelector("#aviso-validacao button").click();true')
        self.assertTrue(t.evaluate('document.activeElement===preco && preco.selectionStart===0 && preco.selectionEnd===preco.value.length'))

    def test_fractional_enter_uses_explicit_cost_and_base_unit(self):
        t=self.prepare()
        t.evaluate('document.querySelector("#tabelaProdutos tr").click();unidade.value="UN";preco.value="0,99";window.initialAssembly=JSON.stringify(salesDraftBridge.capture());true')
        self.price_enter(t)
        self.assertIn('Custo: R$ 1,00',t.evaluate('document.getElementById("aviso-validacao-texto").textContent').replace('\xa0',' '))
        self.assert_preserved(t)
        t.evaluate('document.querySelector("#aviso-validacao button").click();preco.value="1";true')
        self.price_enter(t)
        self.assertEqual(t.evaluate('salesDraftBridge.capture().itens[0].unidade'),'UN')
        self.assertEqual(t.evaluate('salesDraftBridge.capture().itens[0].preco_unitario'),'1.00')

    def test_fractional_fallback_uses_final_validation_rounding(self):
        Produto.objects.filter(pk=self.product.pk).update(preco_compra='10.05',preco_compra_fracionado=0,fator_conversao=2)
        t=self.prepare()
        t.evaluate('document.querySelector("#tabelaProdutos tr").click();unidade.value="UN";preco.value="5,01";window.initialAssembly=JSON.stringify(salesDraftBridge.capture());true')
        self.price_enter(t)
        self.assertTrue(t.evaluate('!!document.getElementById("aviso-validacao")'),t.evaluate('JSON.stringify({cost:custoVenda.value,unit:unidade.value,price:preco.value,options:Array.from(document.querySelectorAll("#produto option")).map(o=>({...o.dataset}))})'))
        self.assertIn('Custo: R$ 5,02',t.evaluate('document.getElementById("aviso-validacao-texto").textContent').replace('\xa0',' '))
        self.assert_preserved(t)
        t.evaluate('document.querySelector("#aviso-validacao button").click();preco.value="5,02";true')
        self.price_enter(t)
        self.assertEqual(t.evaluate('salesDraftBridge.capture().itens[0].preco_unitario'),'5.02')

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
