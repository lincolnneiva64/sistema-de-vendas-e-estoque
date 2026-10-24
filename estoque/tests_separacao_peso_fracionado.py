"""Evidence and regression tests for fractional weights in separation only."""
import base64
import json
import os
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from estoque.models import Cliente, Produto, Venda, ItemVenda, SeparacaoVendaItem, SeparacaoVenda, Compra, ContaPagar, MovimentoFinanceiro
from estoque.services.separacao_vendas import criar_ou_obter_separacao_venda
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evidencias-peso-separacao'
CHROME=r'C:\Program Files\Google\Chrome\Application\chrome.exe'

class PesoFixtures:
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('peso-separacao','test@example.test','test-only')
        self.client.force_login(self.user)
        self.customer=Cliente.objects.create(nome='Cliente Teste Peso')

    def create(self,quantity='0.2',unit='KG'):
        product=Produto.objects.create(nome='Alho Kg' if unit=='KG' else 'Produto '+unit,quantidade=20,
            preco_compra=5,preco_vista=10,preco_prazo=10,unidade_compra=unit,unidade_venda_1=unit)
        sale=Venda.objects.create(cliente=self.customer,data_venda=timezone.localdate(),total=Decimal(quantity)*10)
        line=ItemVenda.objects.create(venda=sale,produto=product,quantidade=Decimal(quantity),unidade=unit,preco_unitario=10,valor_total=Decimal(quantity)*10)
        separation,_,_=criar_ou_obter_separacao_venda(sale,usuario=self.user)
        return separation,separation.itens.get(),line

    def save(self,separation,item,status='peso_separado',value='0.2'):
        return self.client.post(reverse('estoque:separacao_venda_item_salvar',args=[separation.pk,item.pk]),
            {f'status_{item.pk}':status,f'quantidade_separada_{item.pk}':value})

class PesoSeparacaoBrowserTests(PesoFixtures,StaticLiveServerTestCase):
    def open(self,browser,width,separation):
        tab=AuditedChrome(browser).tab();tab.call('Network.enable')
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':True})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:separacao_venda_detalhe',args=[separation.pk])})
        tab.wait('document.readyState==="complete" && !!document.querySelector("[data-sep-item].is-active")')
        tab.evaluate('window.card=document.querySelector("[data-sep-item]");window.q=card.querySelector("[data-qtd-input]");true')
        return tab

    def shot(self,tab,name):
        OUT.mkdir(exist_ok=True)
        tab.evaluate('window.scrollTo(0,0);true')
        data=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
        (OUT/(name+'.png')).write_bytes(base64.b64decode(data))

    def reproduce_baseline(self):
        if not os.environ.get('PESO_REPRO_BASELINE'):self.skipTest('Historical reproduction only')
        OUT.mkdir(exist_ok=True);report=[]
        with TemporaryDirectory(prefix='peso-baseline-browser-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    separation,item,line=self.create()
                    tab=self.open(browser,width,separation)
                    tab.evaluate('card.querySelector("[value=peso_separado]").click();q.value="0.2";q.dispatchEvent(new Event("input",{bubbles:true}));true')
                    item.refresh_from_db()
                    self.assertTrue(tab.evaluate('card.classList.contains("is-done")'))
                    self.assertEqual(item.status,'pendente')
                    report.append({'width':width,'typed_only_visual_done':True,'persisted_before_ok':item.status})
                    tab.evaluate('card.querySelector("[data-peso-ok]").click();true')
                    tab.wait('card.dataset.status==="conferido"')
                    state=tab.evaluate('({value:q.value,step:q.getAttribute("step"),stepMismatch:q.validity.stepMismatch,valid:q.checkValidity(),done:card.classList.contains("is-done")})')
                    item.refresh_from_db()
                    self.assertEqual(item.quantidade_separada,Decimal('0.200'))
                    self.assertTrue(state['stepMismatch'])
                    tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                    self.assertIn('Revise a quantidade',tab.evaluate('document.getElementById("sepSubmitFeedback").textContent'))
                    self.shot(tab,f'antes-bloqueio-{width}')
                    report.append({'width':width,'after_ok':state,'database_quantity':str(item.quantidade_separada),'database_status':item.status})
            finally:browser.stop()
        (OUT/'baseline-reproduction.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

class PesoSeparacaoBackendTests(PesoFixtures,TestCase):
    def test_localized_weights_persist_exactly(self):
        for typed,expected in [('0,2','0.200'),('0.2','0.200'),('0,200','0.200'),('0,08','0.080'),('0,5','0.500'),('1,25','1.250')]:
            with self.subTest(typed=typed):
                separation,item,line=self.create(expected)
                response=self.save(separation,item,value=typed)
                self.assertEqual(response.status_code,200)
                item.refresh_from_db();line.refresh_from_db()
                self.assertEqual(item.quantidade_separada,Decimal(expected))
                self.assertEqual(line.quantidade,Decimal(expected))
                self.assertEqual(item.status,'conferido')

class PesoSeparacaoFluxoBrowserTests(PesoSeparacaoBrowserTests):
    def fill(self,tab,value):
        tab.evaluate('card.querySelector("[value=peso_separado]").click();q.focus();q.select();true')
        tab.call('Input.insertText',{'text':value})

    def commercial(self):
        return {model.__name__:list(model.objects.order_by('pk').values()) for model in [Produto,Venda,ItemVenda,Compra,ContaPagar,MovimentoFinanceiro]}

    def test_formats_confirm_finish_reopen_all_widths(self):
        OUT.mkdir(exist_ok=True);report=[]
        cases=[('0,2','0.200'),('0.2','0.200'),('0,200','0.200'),('0,08','0.080'),('0,5','0.500'),('1,25','1.250')]
        with TemporaryDirectory(prefix='peso-mobile-formats-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    for typed,expected in cases:
                        separation,item,line=self.create(expected);before=self.commercial()
                        tab=self.open(browser,width,separation);self.fill(tab,typed)
                        self.assertEqual(tab.evaluate('q.value'),typed)
                        self.assertFalse(tab.evaluate('card.classList.contains("is-done")'))
                        item.refresh_from_db();self.assertEqual(item.status,'pendente')
                        tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                        feedback=tab.evaluate('document.getElementById("sepSubmitFeedback").textContent')
                        self.assertIn('Alho Kg',feedback);self.assertIn('OK',feedback)
                        if typed=='0,2':self.shot(tab,f'pendente-OK-{width}')
                        tab.evaluate('card.querySelector("[data-peso-ok]").click();true')
                        tab.wait('card.dataset.confirmado==="true" && !card._sepSaving')
                        self.assertTrue(tab.evaluate('document.getElementById("separacaoChecklistForm").checkValidity()'))
                        self.assertTrue(tab.evaluate('card.classList.contains("is-done")'))
                        item.refresh_from_db();line.refresh_from_db()
                        self.assertEqual(item.quantidade_separada,Decimal(expected))
                        self.assertEqual(line.quantidade,Decimal(expected))
                        self.assertEqual(item.quantidade_solicitada,Decimal(expected))
                        self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        if typed=='0,2':self.shot(tab,f'confirmado-{width}')
                        tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                        tab.wait(f'location.pathname==={reverse("estoque:separacao_vendas_fila")!r} && document.readyState==="complete"')
                        tab=self.open(browser,width,separation)
                        self.assertTrue(tab.evaluate('card.classList.contains("is-done") && q.checkValidity()'))
                        self.assertEqual(separation.itens.count(),1)
                        self.assertEqual(self.commercial(),before)
                        self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                        if typed=='0,2':self.shot(tab,f'reaberta-{width}')
                        report.append({'width':width,'typed':typed,'persisted':str(item.quantidade_separada),'finished_and_reopened':True})
            finally:browser.stop()
        (OUT/'mobile-matrix.json').write_text(json.dumps(report,indent=2),encoding='utf-8')

    def test_invalid_weights_stay_pending_without_saving(self):
        with TemporaryDirectory(prefix='peso-invalid-mobile-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    separation,item,line=self.create();tab=self.open(browser,width,separation)
                    tab.evaluate('window.realFetch=fetch;window.saveCount=0;window.fetch=(...args)=>{if(String(args[0])===card.dataset.saveUrl)saveCount++;return realFetch(...args)};true')
                    for value in ['0','-0.2','abc','NaN','0.0001','0,,2']:
                        self.fill(tab,value)
                        tab.evaluate('card.querySelector("[data-peso-ok]").click();document.getElementById("sepSubmitBtn").click();true')
                        self.assertFalse(tab.evaluate('card.classList.contains("is-done")'))
                        self.assertEqual(tab.evaluate('saveCount'),0)
                        item.refresh_from_db();self.assertEqual(item.status,'pendente')
                        self.assertIsNone(item.quantidade_separada)
                        self.assertIn('Alho Kg',tab.evaluate('document.getElementById("sepSubmitFeedback").textContent'))
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
            finally:browser.stop()

    def test_delayed_response_edit_and_duplicate_ok(self):
        with TemporaryDirectory(prefix='peso-delayed-confirm-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    separation,item,line=self.create();tab=self.open(browser,width,separation)
                    tab.evaluate('''window.realFetch=fetch;window.saveCount=0;
                        window.releaseResponse=null;window.waitResponse=new Promise(resolve=>releaseResponse=resolve);
                        window.fetch=async(...args)=>{if(String(args[0])!==card.dataset.saveUrl)return realFetch(...args);const n=++saveCount;const response=await realFetch(...args);if(n===1)await waitResponse;return response};true''')
                    self.fill(tab,'0,2')
                    tab.evaluate('card.querySelector("[data-peso-ok]").click();card.querySelector("[data-peso-ok]").click();true')
                    tab.wait('saveCount===1')
                    self.assertFalse(tab.evaluate('card.classList.contains("is-done")'))
                    tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                    self.assertIn('aguarde',tab.evaluate('document.getElementById("sepSubmitFeedback").textContent'))
                    tab.evaluate('q.focus();q.select();true');tab.call('Input.insertText',{'text':'0,08'})
                    tab.evaluate('releaseResponse();true');tab.wait('!card._sepSaving')
                    self.assertFalse(tab.evaluate('card.classList.contains("is-done")'))
                    self.assertEqual(tab.evaluate('q.value'),'0,08')
                    item.refresh_from_db();self.assertEqual(item.quantidade_separada,Decimal('0.200'))
                    tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                    self.assertIn('OK',tab.evaluate('document.getElementById("sepSubmitFeedback").textContent'))
                    tab.evaluate('card.querySelector("[data-peso-ok]").click();card.querySelector("[data-peso-ok]").click();true')
                    tab.wait('card.dataset.confirmado==="true" && !card._sepSaving')
                    self.assertEqual(tab.evaluate('saveCount'),2)
                    item.refresh_from_db();line.refresh_from_db()
                    self.assertEqual(item.quantidade_separada,Decimal('0.080'))
                    self.assertEqual(line.quantidade,Decimal('0.200'))
                    self.assertEqual(separation.itens.count(),1)
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
            finally:browser.stop()

    def test_new_confirmation_queued_during_save_keeps_latest_weight(self):
        with TemporaryDirectory(prefix='peso-queued-confirm-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    separation,item,line=self.create();tab=self.open(browser,width,separation)
                    tab.evaluate("""window.realFetch=fetch;window.saveCount=0;
                        window.releaseResponse=null;window.waitResponse=new Promise(resolve=>releaseResponse=resolve);
                        window.fetch=async(...args)=>{if(String(args[0])!==card.dataset.saveUrl)return realFetch(...args);const n=++saveCount;const response=await realFetch(...args);if(n===1)await waitResponse;return response};true""")
                    self.fill(tab,'0,2')
                    tab.evaluate('card.querySelector("[data-peso-ok]").click();true');tab.wait('saveCount===1')
                    self.fill(tab,'0,08')
                    tab.evaluate('card.querySelector("[data-peso-ok]").click();releaseResponse();true')
                    tab.wait('card.dataset.confirmado==="true" && !card._sepSaving')
                    self.assertEqual(tab.evaluate('saveCount'),2)
                    item.refresh_from_db();line.refresh_from_db()
                    self.assertEqual(item.quantidade_separada,Decimal('0.080'))
                    self.assertEqual(line.quantidade,Decimal('0.200'))
                    self.assertEqual(separation.itens.count(),1)
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
            finally:browser.stop()

    def test_units_boxes_packages_bales_preserve_flows(self):
        with TemporaryDirectory(prefix='peso-unit-regression-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [320,360,390]:
                    for unit in ['UN','CX','PCT','FD']:
                        for status in ['conferido','quantidade_insuficiente']:
                            separation,item,line=self.create('2',unit);before=self.commercial()
                            tab=self.open(browser,width,separation)
                            tab.evaluate(f'card.querySelector("[value={status}]").click();true')
                            if status=='quantidade_insuficiente':
                                tab.evaluate('q.value="1";q.dispatchEvent(new Event("input",{bubbles:true}));q.dispatchEvent(new Event("change",{bubbles:true}));true')
                            tab.wait('card.dataset.confirmado==="true" && !card._sepSaving')
                            self.assertTrue(tab.evaluate('q.checkValidity()'))
                            item.refresh_from_db();self.assertEqual(item.quantidade_separada,Decimal('2' if status=='conferido' else '1'))
                            tab.evaluate('document.getElementById("sepSubmitBtn").click();true')
                            tab.wait(f'location.pathname==={reverse("estoque:separacao_vendas_fila")!r} && document.readyState==="complete"')
                            self.assertEqual(self.commercial(),before)
                            self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
            finally:browser.stop()

class PesoSeparacaoValidacaoBackendTests(PesoFixtures,TestCase):
    def test_invalid_values_rejected_without_mutation(self):
        for value in ['0','-0.2','abc','NaN','Infinity','0.0001','1.2345','0,,2']:
            with self.subTest(value=value):
                separation,item,line=self.create()
                response=self.save(separation,item,value=value)
                self.assertEqual(response.status_code,400)
                item.refresh_from_db();self.assertEqual(item.status,'pendente');self.assertIsNone(item.quantidade_separada)
                line.refresh_from_db();self.assertEqual(line.quantidade,Decimal('0.200'))

    def test_finish_requires_persisted_confirmation_and_names_product(self):
        separation,item,line=self.create()
        path=reverse('estoque:separacao_venda_detalhe',args=[separation.pk])
        response=self.client.post(path,{f'status_{item.pk}':'peso_separado',f'quantidade_separada_{item.pk}':'0,2'})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Alho Kg');self.assertContains(response,'OK do campo Peso separado')
        item.refresh_from_db();self.assertEqual(item.status,'pendente')
        self.assertEqual(self.save(separation,item,value='0,2').status_code,200)
        response=self.client.post(path,{f'status_{item.pk}':'peso_separado',f'quantidade_separada_{item.pk}':'0,08'})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Alho Kg')
        item.refresh_from_db();self.assertEqual(item.quantidade_separada,Decimal('0.200'))
        response=self.client.post(path,{f'status_{item.pk}':'peso_separado',f'quantidade_separada_{item.pk}':'0,200'})
        self.assertEqual(response.status_code,302)

    def test_repeated_confirmation_preserves_one_item_and_finalization(self):
        separation,item,line=self.create()
        self.assertEqual(self.save(separation,item,value='0.2').status_code,200)
        separation.refresh_from_db();finished=separation.finalizado_em
        self.assertEqual(self.save(separation,item,value='0,200').status_code,200)
        separation.refresh_from_db();self.assertEqual(separation.finalizado_em,finished)
        self.assertEqual(separation.itens.count(),1)
        item.refresh_from_db();self.assertEqual(item.quantidade_separada,Decimal('0.200'))
