"""Regression tests: purchase form only, financial guards unchanged."""
import base64,copy,json
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal
from django.conf import settings
from django.test import TestCase,override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from estoque.tests_edicao_compra import EdicaoFixture
from datetime import date
from estoque.models import ItemCompra,ListaCompraFornecedor,ItemListaCompraFornecedor,ContaFinanceira
from estoque.models import Produto,EdicaoCompra,Compra,MovimentoFinanceiro
from estoque.services.edicao_compra import estado_compra
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome
ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evidencias-interface'
OUT.mkdir(exist_ok=True)
CHROME=r'C:\Program Files\Google\Chrome\Application\chrome.exe'
WIDTHS=[1280,1440,320,360,390]

class Fixture(EdicaoFixture):
    def setUp(self):
        super().setUp()
        self.fornecedor.nome='Fornecedor ficticio Imperio';self.fornecedor.save()
        self.compra.tipo_pagamento='avista';self.compra.data_compra=date(2026,10,24)
        self.compra.total=Decimal('442.20');self.compra.total_produtos=Decimal('442.20')
        self.compra.data_vencimento=None
        self.lista=ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,data_lista=date(2026,10,24),data_inicio_periodo=date(2026,10,1),data_fim_periodo=date(2026,10,24))
        self.compra.lista_fornecedor=self.lista;self.compra.save();self.conta.delete()
        self.produto.nome='Império Lta 15/269ml';self.produto.preco_compra=Decimal('36.80');self.produto.unidade_compra='PCT';self.produto.preco_vista=50;self.produto.preco_prazo=55;self.produto.save()
        self.item.quantidade=3;self.item.preco_unitario=Decimal('36.80');self.item.valor_total=Decimal('110.40');self.item.unidade='PCT';self.item.save()
        self.other=Produto.objects.create(nome='Império Lta 15/350ml',quantidade=20,preco_compra=Decimal('47.40'),preco_vista=60,preco_prazo=60,unidade_compra='PCT')
        self.second=ItemCompra.objects.create(compra=self.compra,produto=self.other,quantidade=7,preco_unitario=Decimal('47.40'),valor_total=Decimal('331.80'),unidade='PCT')
        self.correct=Produto.objects.create(nome='Império Ultra Lta 12/269ml',quantidade=20,preco_compra=Decimal('35.40'),preco_vista=50,preco_prazo=50,unidade_compra='DZ')
        ItemListaCompraFornecedor.objects.create(lista=self.lista,produto=self.correct,quantidade_final=3,unidade='DZ',preco_unitario=Decimal('35.40'))
        ItemListaCompraFornecedor.objects.create(lista=self.lista,produto=self.produto,quantidade_final=5,unidade='PCT',preco_unitario=Decimal('36.80'))
        ItemListaCompraFornecedor.objects.create(lista=self.lista,produto=self.other,quantidade_final=7,unidade='PCT',preco_unitario=Decimal('47.40'))
        self.lista.total_lista=Decimal('622.00');self.lista.save()
        self.bank=ContaFinanceira.objects.create(nome='Banco ficticio',tipo='banco')
        self.movement=MovimentoFinanceiro.objects.create(compra=self.compra,conta=self.bank,tipo='saida',valor=Decimal('442.20'),data=self.compra.data_compra,origem='compra_a_vista',descricao='Saida ficticia com vinculo explicito')
    def data(self):
        page=self.client.get(self.url)
        return {'acao_compra':'salvar_edicao_finalizada','edicao_token':page.context['edicao_token'],'motivo_edicao':'Diagnostico ficticio','fornecedor_id':str(self.fornecedor.pk),'data_compra':'2026-10-24','tipo_pagamento':'avista','produto_id[]':[str(self.correct.pk),str(self.other.pk)],'item_edicao_id[]':[str(self.item.pk),str(self.second.pk)],'quantidade[]':['3','7'],'preco_unitario[]':['35,40','47,40'],'unidade[]':['DZ','PCT'],'observacao_item[]':['Observacao do item',''],'observacao':'Nota original','valor_cobrado':'442,20'}


class Backend(Fixture,TestCase):
    def test_ajax_block_reports_real_error_and_leaves_records_untouched(self):
        before=estado_compra(self.compra);stocks=list(Produto.objects.order_by('pk').values())
        response=self.client.post(self.url,self.data(),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertFalse(response.json()['sucesso'])
        self.assertIn('Existe movimento financeiro',response.json()['erro'])
        self.compra.refresh_from_db();self.assertEqual(estado_compra(self.compra),before)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),stocks);self.assertFalse(EdicaoCompra.objects.exists())
    def test_ajax_success_is_confirmed_and_repeated_post_is_idempotent(self):
        self.movement.delete();payload=self.data();payload['valor_cobrado']='438,00'
        for repeat in range(2):
            response=self.client.post(self.url,payload,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(response.status_code,200);self.assertTrue(response.json()['sucesso'])
            self.assertEqual(response.json()['redirect_url'],reverse('estoque:compras_detalhe',args=[self.compra.pk]))
        self.item.refresh_from_db();self.assertEqual(self.item.produto_id,self.correct.pk)
        self.assertEqual(EdicaoCompra.objects.count(),1);self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,Decimal('23'))
    def test_ajax_invalid_quantity_preserves_validation(self):
        payload=self.data();payload['quantidade[]']=['0','7']
        response=self.client.post(self.url,payload,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertIn('quantidade maior que zero',response.json()['erro'])
        self.assertFalse(EdicaoCompra.objects.exists())

class Browser(Fixture,StaticLiveServerTestCase):
    def open(self,browser,width,url=None):
        tab=AuditedChrome(browser).tab();tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<820})
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Page.navigate',{'url':self.live_server_url+(url or self.url)})
        # Existing purchase startup scripts schedule focus changes; wait for those before CDP typing.
        tab.wait('document.readyState==="complete" && !!document.getElementById("formCompra") && performance.now()>700')
        return tab
    def shot(self,tab,name):
        data=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
        (OUT/(name+'.png')).write_bytes(base64.b64decode(data))
    def select(self,tab,row_expression):
        tab.evaluate('window.row='+row_expression+';true')
        tab.evaluate(r'window.busca=row.querySelector("[name=\"produto_busca[]\"]");busca.focus();busca.select();true')
        tab.call('Input.insertText',{'text':'Ultra'})
        tab.wait('!!row.querySelector(".produto-sugestao")')
        tab.evaluate(rf'row.querySelector(".produto-sugestao[data-produto-id=\"{self.correct.pk}\"]").click();true')
    def test_pending_manual_item_blocks_but_automatic_suggestion_does_not(self):
        with TemporaryDirectory(prefix='compra-pendente-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in WIDTHS:
                    tab=self.open(browser,width)
                    if width>=820:tab.evaluate('document.getElementById("btnAdicionarItem").click();true')
                    self.select(tab,'document.querySelector("#areaAdicionarProdutoMobile .linha-item")' if width<820 else 'document.querySelector("#tabelaItensCompra tbody .linha-item:last-child")')
                    tab.evaluate(r'row.querySelector("[name=\"quantidade[]\"]").value="3";row.querySelector("[name=\"quantidade[]\"]").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("motivoEdicaoCompra").value="Produto manual";window.saveCalls=0;const original=window.fetch;window.fetch=(...args)=>{if(args[1]?.method==="POST")saveCalls++;return original(...args)};document.getElementById("btnSalvarCompra").click();true')
                    self.assertEqual(tab.evaluate('saveCalls'),0)
                    self.assertIn('Ultra',tab.evaluate('document.getElementById("erroSalvarPreRevisaoCompra").textContent'))
                    self.assertIn('Concluir item',tab.evaluate('document.getElementById("erroSalvarPreRevisaoCompra").textContent'))
                    self.shot(tab,f'pendente-{width}')
                    tab.evaluate('document.getElementById("btnAdicionarItem").click();document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('!document.getElementById("erroSalvarPreRevisaoCompra").hidden && document.getElementById("erroSalvarPreRevisaoCompra").textContent.includes("Existe movimento financeiro")')
                    self.assertEqual(tab.evaluate('saveCalls'),1)
                    self.assertEqual(self.compra.itens.count(),2)
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.shot(tab,f'inclusao-bloqueada-preservada-{width}')
            finally:browser.stop()
    def test_substitution_cancel_block_retry_and_success_preserve_values(self):
        original=estado_compra(self.compra);stocks=list(Produto.objects.order_by('pk').values())
        report=[]
        with TemporaryDirectory(prefix='compra-substituicao-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in WIDTHS:
                    tab=self.open(browser,width)
                    if width<820:
                        tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    if width<820:
                        tab.evaluate('row.querySelector(".btn-cancelar-edicao-item-mobile").click();true')
                        self.assertEqual(tab.evaluate('row.querySelector("select").value'),str(self.produto.pk))
                        self.assertNotIn('Ultra',tab.evaluate('busca.value'))
                        tab.evaluate('row.querySelector(".btn-editar-item-mobile").click();true')
                        self.select(tab,'row')
                        tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    self.assertEqual(tab.evaluate('row.querySelector("select").value'),str(self.correct.pk))
                    self.assertIn('Ultra',tab.evaluate('busca.value'))
                    tab.evaluate('document.getElementById("motivoEdicaoCompra").value="Justificativa preservada";document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('!document.getElementById("erroSalvarPreRevisaoCompra").hidden && !document.getElementById("btnSalvarCompra").disabled')
                    self.assertIn('Existe movimento financeiro',tab.evaluate('document.getElementById("erroSalvarPreRevisaoCompra").textContent'))
                    self.assertEqual(tab.evaluate('location.pathname'),self.url)
                    self.assertEqual(tab.evaluate('row.querySelector("select").value'),str(self.correct.pk))
                    self.assertIn('Ultra',tab.evaluate('busca.value'))
                    self.assertEqual(tab.evaluate('document.getElementById("motivoEdicaoCompra").value'),'Justificativa preservada')
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.compra.refresh_from_db();self.assertEqual(estado_compra(self.compra),original)
                    self.assertEqual(list(Produto.objects.order_by('pk').values()),stocks)
                    self.shot(tab,f'substituicao-bloqueada-preservada-{width}')
                    report.append({'width':width,'blocked_error_shown':True,'fields_preserved':True,'database_unchanged':True})
            finally:browser.stop()
        (OUT/'fluxos-bloqueados.json').write_text(json.dumps(report,indent=2))
    def test_permitted_finalized_substitution_persists_after_server_ack(self):
        self.movement.delete()
        with TemporaryDirectory(prefix='compra-sucesso-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in [1280,390]:
                    # Restore only fixture values in the disposable database for each independent flow.
                    if width==390:
                        self.item.produto=self.produto;self.item.preco_unitario=Decimal('36.80');self.item.valor_total=Decimal('110.40');self.item.unidade='PCT';self.item.save()
                        self.compra.total=Decimal('442.20');self.compra.total_produtos=Decimal('442.20');self.compra.save()
                    tab=self.open(browser,width)
                    if width<820:tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    if width<820:tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    tab.evaluate('document.getElementById("motivoEdicaoCompra").value="Correcao ficticia permitida";document.getElementById("btnSalvarCompra").click();true')
                    tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                    self.item.refresh_from_db();self.compra.refresh_from_db()
                    self.assertEqual(self.item.produto_id,self.correct.pk);self.assertEqual(self.compra.total,Decimal('438.00'))
                    self.assertEqual(self.compra.itens.count(),2);self.assertFalse(MovimentoFinanceiro.objects.exists())
                    self.shot(tab,f'sucesso-confirmado-{width}')
            finally:browser.stop()
    def test_initial_layout_before_after(self):
        templates=copy.deepcopy(settings.TEMPLATES)
        templates[0]['APP_DIRS']=False
        templates[0]['OPTIONS']['loaders']=[('django.template.loaders.locmem.Loader',{'estoque/compras_nova.html':(OUT/'original-compras_nova.html').read_text(encoding='utf-8')}),'django.template.loaders.filesystem.Loader','django.template.loaders.app_directories.Loader']
        report=[]
        with TemporaryDirectory(prefix='compra-layout-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for width in WIDTHS:
                    states=[]
                    for old in [True,False]:
                        from contextlib import nullcontext
                        with override_settings(TEMPLATES=templates) if old else nullcontext():
                            tab=self.open(browser,width)
                            tab.wait('document.querySelectorAll("#tabelaItensCompra tbody .linha-item").length===2')
                            state=tab.evaluate('({styles:Array.from(document.querySelectorAll("style")).map(x=>x.textContent),headers:Array.from(document.querySelectorAll("#tabelaItensCompra th")).map(x=>x.textContent),boxes:["#tabelaItensCompra","#areaAdicionarProdutoMobile","#btnSalvarCompra","#valorCobradoCompra"].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [s,r.x,r.width,r.height]})})')
                            states.append(state);self.shot(tab,('antes' if old else 'depois')+f'-layout-{width}')
                    self.assertEqual(states[0],states[1]);report.append({'width':width,'styles_headers_and_dimensions_equal':True})
            finally:browser.stop()
        (OUT/'layout.json').write_text(json.dumps(report,indent=2))

    def test_new_purchase_manual_item_creates_only_one_draft_item(self):
        for width in [1280,390]:
            with TemporaryDirectory(prefix='compra-criacao-') as profile:
                browser=Chrome(CHROME,profile).start()
                try:
                    tab=self.open(browser,width,reverse('estoque:compras_nova'))
                    self.select(tab,'document.querySelector("#areaAdicionarProdutoMobile .linha-item")' if width<820 else 'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    tab.evaluate(r'row.querySelector("[name=\"quantidade[]\"]").value="3";row.querySelector("[name=\"quantidade[]\"]").dispatchEvent(new Event("input",{bubbles:true}));document.querySelector("[name=fornecedor_id]").value="'+str(self.fornecedor.pk)+r'";document.querySelector("[name=fornecedor_busca]").value="Fornecedor ficticio";document.querySelector("[name=data_compra]").value="2026-10-24";true')
                    tab.evaluate('document.getElementById("btnAdicionarItem").click();document.getElementById("btnSalvarRascunhoCompra").click();true')
                    try:
                        tab.wait('document.readyState==="complete" && location.pathname!=="/compras/nova/"')
                    except AssertionError:
                        self.shot(tab,f'criacao-diagnostico-{width}')
                        (OUT/f'criacao-diagnostico-{width}.json').write_text(json.dumps(tab.evaluate('({text:document.body.innerText,fields:Array.from(document.getElementById("formCompra").elements).map(x=>({name:x.name,value:x.value,disabled:x.disabled,valid:x.checkValidity(),message:x.validationMessage})),errors:purchaseConsoleErrors})'),indent=2))
                        raise
                    draft=Compra.objects.filter(status=Compra.STATUS_RASCUNHO).latest('pk')
                    self.assertEqual(draft.itens.count(),1);self.assertEqual(draft.itens.get().produto_id,self.correct.pk)
                    self.assertEqual(draft.itens.get().quantidade,Decimal('3'))
                    self.assertEqual(draft.total,Decimal('106.20'));self.assertFalse(draft.estoque_entrada_realizada)
                    self.assertEqual(MovimentoFinanceiro.objects.count(),1)
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.shot(tab,f'criacao-rascunho-{width}')
                finally:browser.stop()
    def test_success_waits_for_server_response_and_double_click_does_not_duplicate(self):
        self.movement.delete()
        with TemporaryDirectory(prefix='compra-confirmacao-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                tab=self.open(browser,390)
                tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                tab.evaluate(r'row.querySelector(".btn-salvar-edicao-item-mobile").click();document.getElementById("motivoEdicaoCompra").value="Confirmacao controlada";window.postCount=0;window.releaseSave=null;window.savedResponse=new Promise(resolve=>releaseSave=resolve);const originalFetch=fetch;window.fetch=async(...args)=>{if(args[1]?.method!=="POST")return originalFetch(...args);postCount++;const response=await originalFetch(...args);window.serverResponded=true;await savedResponse;return response};document.getElementById("btnSalvarCompra").click();document.getElementById("btnSalvarCompra").click();true')
                tab.wait('window.serverResponded===true')
                self.assertEqual(tab.evaluate('postCount'),1);self.assertEqual(tab.evaluate('location.pathname'),self.url)
                self.assertTrue(tab.evaluate('document.getElementById("btnSalvarCompra").disabled'))
                self.assertFalse(tab.evaluate('document.body.innerText.includes("Compra corrigida com historico preservado")'))
                self.shot(tab,'aguardando-confirmacao-servidor')
                tab.evaluate('releaseSave();true')
                tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                self.assertEqual(EdicaoCompra.objects.count(),1);self.assertEqual(self.compra.itens.count(),2)
                self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,Decimal('23'))
            finally:browser.stop()
