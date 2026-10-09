"""Post-trial corrections: real isolated records, no production/operational data."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import Permission
from estoque.models import Venda, ItemVenda, ContaReceber, Produto
from estoque.views import _quantidade_estoque_para_unidade_base, _quantidade_estoque_inteira
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sale_sync_browser as sync
from . import tests_sales_consecutive_browser as consecutive


@skipUnless(Path(sync.CHROME).is_file(),'Chrome unavailable')
class PostTrialSalesBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = sync.SaleSyncBrowserTests.setUp
    open_sales = sync.SaleSyncBrowserTests.open_sales
    repository = sync.SaleSyncBrowserTests.repository
    select_customer = sync.SaleSyncBrowserTests.select_customer
    add_product = sync.SaleSyncBrowserTests.add_product
    prepare = sync.SaleSyncBrowserTests.prepare
    stable = sync.SaleSyncBrowserTests.stable
    manual = sync.SaleSyncBrowserTests.manual
    start_manual = sync.SaleSyncBrowserTests.start_manual
    reload = sync.SaleSyncBrowserTests.reload
    disconnect = consecutive.ConsecutiveSalesBrowserTests.disconnect
    reconnect = consecutive.ConsecutiveSalesBrowserTests.reconnect
    next_sale = consecutive.ConsecutiveSalesBrowserTests.next_sale
    conclude = consecutive.ConsecutiveSalesBrowserTests.conclude

    def stock_module(self,tab):
        tab.evaluate('window.stock=await import("/offline/assets/2-8f-fix/sales-stock.js");true')

    def test_new_date_local_timezone_and_old_restored_draft_preserved(self):
        with TemporaryDirectory(prefix='post-trial-date-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.open_sales(chrome)
                tab.call('Emulation.setTimezoneOverride',{'timezoneId':'America/Sao_Paulo'})
                tab.evaluate('window.NativeDate=Date;window.Date=class extends NativeDate{constructor(...args){super(...(args.length?args:["2026-10-09T02:30:00Z"]))}};true')
                self.select_customer(tab,'Cliente Comercial'); self.add_product(tab,'Produto Fracionado','0.5')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft.data_venda'),'2026-10-08')
                self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft.data_vencimento'),'2026-10-22')
                tab.evaluate('dataVenda.value="2020-01-02";dataVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();true')
                self.reload(tab); self.repository(tab)
                self.assertEqual(tab.evaluate('dataVenda.value'),'2020-01-02')
                self.assertIn('Data da venda preservada: 2020-01-02',tab.evaluate('document.getElementById("sales-draft-status").textContent'))
                self.assertEqual(Venda.objects.count(),0)
            finally: chrome.stop()

    def test_consecutive_new_date_and_real_unique_effects(self):
        with TemporaryDirectory(prefix='post-trial-consecutive-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.open_sales(chrome)
                tab.call('Emulation.setTimezoneOverride',{'timezoneId':'America/Sao_Paulo'})
                tab.evaluate('await (await import("/offline/assets/2-8f-fix/commercial.js")).atualizarSnapshotComercial(repo,scope);window.NativeDate=Date;window.day="2026-10-09T02:30:00Z";window.Date=class extends NativeDate{constructor(...args){super(...(args.length?args:[day]))}};true')
                self.disconnect(tab)
                first = self.conclude(tab,product='Produto Fracionado',quantity='0.5')
                tab.evaluate('window.day="2026-10-10T02:30:00Z";true')
                self.next_sale(tab)
                second = self.conclude(tab,product='Produto Fracionado',quantity='0.5')
                self.assertEqual(first['payload']['data_venda'],'2026-10-08')
                self.assertEqual(second['payload']['data_venda'],'2026-10-09')
                self.assertEqual(first['payload']['data_vencimento'],'2026-10-22')
                self.assertEqual(second['payload']['data_vencimento'],'2026-10-23')
                self.assertNotEqual(first['operation_id'],second['operation_id'])
                self.assertEqual(tab.evaluate('await repo.get("operations",'+json.dumps(first['operation_id'])+')'),first)
                self.reconnect(tab); self.stable(tab); self.manual(tab)
                self.assertEqual(Venda.objects.count(),2)
                self.assertEqual(ItemVenda.objects.count(),2)
                self.assertEqual(ContaReceber.objects.count(),2)
                self.assertEqual(OperacaoSincronizacao.objects.count(),2)
                self.product.refresh_from_db(); self.assertEqual(self.product.quantidade,Decimal('1.5'))
            finally: chrome.stop()

    def test_primary_secondary_accumulation_and_finalization_cannot_bypass(self):
        with TemporaryDirectory(prefix='post-trial-stock-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.disconnect(tab); self.next_sale(tab)
                self.stock_module(tab)
                self.select_customer(tab,'Cliente Comercial')
                tab.evaluate('window.before=JSON.stringify({ops:await repo.all("operations"),snapshots:await repo.all("snapshots")});window.snapshot=(await repo.all("snapshots")).find(s=>s.tipo==="comercial_vendas");window.p=snapshot.produtos.find(p=>p.id==='+json.dumps(str(self.product.pk))+');true')
                for unit,quantity in [('CX','3'),('UN','25')]:
                    # Exercise the inclusion gate without creating a command.
                    self.assertFalse(tab.evaluate('salesDraftUI.validateStockInclusion({produto_id:p.id,produto_nome:p.nome,quantidade:'+json.dumps(quantity)+',unidade:'+json.dumps(unit)+'})'))
                self.assertTrue(tab.evaluate('salesDraftUI.validateStockInclusion({produto_id:p.id,quantidade:"24",unidade:"UN"})'))
                # Invalid unit and factor are rejected even with plentiful stock.
                tab.evaluate('window.view=await stock.readStock(repo,scope);view.snapshot.produtos.find(x=>x.id===p.id).fator_conversao="0.00";true')
                self.assertTrue(tab.evaluate('try{stock.validateStock(view,scope,[{produto_id:p.id,quantidade:"1",unidade:"UN"}]);false}catch(e){true}'))
                self.assertTrue(tab.evaluate('try{stock.validateStock(await stock.readStock(repo,scope),scope,[{produto_id:p.id,quantidade:"1",unidade:"BAD"}]);false}catch(e){true}'))
                self.add_product(tab,'Produto Fracionado','1')
                # Persisting a draft does not authorize stock; bypassing the editor
                # must still fail at the atomic conclusion boundary.
                tab.evaluate('window.current=await drafts.loadDraft(repo,scope);current.draft.itens[0].quantidade="3";window.changed=await drafts.saveDraft(repo,scope,current.draft,current.revision);true')
                self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,scope,{revision:changed.revision,draft_id:changed.draft.draft_id});false}catch(e){e.message.includes("Estoque local insuficiente")}'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),1)
                self.assertTrue(tab.evaluate('before===JSON.stringify({ops:await repo.all("operations"),snapshots:await repo.all("snapshots")})'))
                self.assertEqual(Venda.objects.count(),0)
            finally: chrome.stop()

    def test_decimal_unit_rules_match_existing_server(self):
        cases = [('CX','UN',True,'12','CX','0.25'),('CX','UN',True,'12','UN','0.5'),
                 ('PCT','',False,'0','PCT','0.5'),('PCT','',False,'0','PCT','0.25'),
                 ('DZ','',False,'0','DZ','0.25'),('UN','',False,'0','UN','0.5'),
                 ('MIL','CT',True,'100','MIL','0.9'),('MIL','CT',True,'100','CT','90'),
                 ('CX','UN',True,'0','UN','1'),('CX','UN',True,'12','BAD','1')]
        with TemporaryDirectory(prefix='post-trial-parity-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.open_sales(chrome); self.stock_module(tab)
                for base,secondary,fractional,factor,unit,quantity in cases:
                    product = SimpleNamespace(nome='Parity',unidade_venda_1=base,unidade_compra=base,
                        unidade_venda_2=secondary,vende_fracionado=fractional,fator_conversao=Decimal(factor))
                    try:
                        converted,_ = _quantidade_estoque_para_unidade_base(product,Decimal(quantity),unit)
                        if not fractional:_quantidade_estoque_inteira(converted,product.nome,base)
                        expected=True
                    except ValueError:expected=False
                    p={'id':'1','nome':'Parity','unidade_venda_1':base,'unidade_venda_2':secondary,
                       'vende_fracionado':fractional,'fator_conversao':factor,'estoque_referencia':'1000.000'}
                    actual=tab.evaluate('try{stock.validateStock({snapshot:{produtos:['+json.dumps(p)+']},baseline:null,operations:[]},scope,[{produto_id:"1",quantidade:'+json.dumps(quantity)+',unidade:'+json.dumps(unit)+'}]);true}catch(e){false}')
                    self.assertEqual(actual,expected,(base,secondary,factor,unit,quantity))
            finally: chrome.stop()

    def test_conflict_f5_two_tabs_consultation_and_modal_ok(self):
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='estoque',codename='view_venda'))
        with TemporaryDirectory(prefix='post-trial-conflict-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='2')
                Produto.objects.filter(pk=self.product.pk).update(quantidade=0)
                self.stable(tab); self.manual(tab)
                original = tab.evaluate('await repo.get("operations",operation.operation_id)')
                self.assertEqual(original['status'],'conflito')
                self.reload(tab); self.repository(tab)
                other = self.open_sales(chrome)
                for page in [tab,other]:
                    page.wait('salesDraftUI.blocked && !document.getElementById("btnConsultarVendas").disabled')
                    self.assertTrue(page.evaluate('!!document.querySelector("[data-review-sale]")'))
                    page.evaluate('mostrarModalOperadorObrigatorio("Operador obrigatório");true')
                    self.assertFalse(page.evaluate('document.getElementById("modal-operador-obrigatorio-ok").disabled'))
                    page.evaluate('document.getElementById("modal-operador-obrigatorio-ok").click();true')
                    page.wait('!document.getElementById("modal-operador-obrigatorio").classList.contains("ativo")')
                    self.assertEqual(page.evaluate('await repo.get("operations",'+json.dumps(original['operation_id'])+')'),original)
                other.evaluate('document.getElementById("btnConsultarVendas").click();true')
                other.wait('location.pathname.includes("consultar")')
                self.assertEqual(Venda.objects.count(),0)
                self.assertEqual(OperacaoSincronizacao.objects.count(),1)
            finally: chrome.stop()

    def test_confirmed_consumption_not_forgotten_or_double_counted_after_preparation(self):
        with TemporaryDirectory(prefix='post-trial-baseline-') as profile:
            chrome = Chrome(sync.CHROME,profile).start()
            try:
                tab = self.prepare(chrome,quantity='0.5'); self.stable(tab); self.manual(tab)
                self.disconnect(tab); self.stock_module(tab)
                self.assertTrue(tab.evaluate('try{stock.validateStock(await stock.readStock(repo,scope),scope,[{produto_id:'+json.dumps(str(self.product.pk))+',quantidade:"2.25",unidade:"CX"}]);false}catch(e){true}'))
                before = tab.evaluate('await repo.all("operations")')
                tab.evaluate('await (await import("/offline/assets/2-8f-fix/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
                self.assertTrue(tab.evaluate('stock.validateStock(await stock.readStock(repo,scope),scope,[{produto_id:'+json.dumps(str(self.product.pk))+',quantidade:"2",unidade:"CX"}]).hasStock'))
                self.assertEqual(tab.evaluate('await repo.all("operations")'),before)
                self.assertEqual(Venda.objects.count(),1)
                self.product.refresh_from_db();self.assertEqual(self.product.quantidade,Decimal('2'))
            finally: chrome.stop()
