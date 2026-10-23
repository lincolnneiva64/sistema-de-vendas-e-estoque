"""Synthetic Tixan cases: selective purchase prices and the actual confirmation UI."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import models, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from . import tests_precos_seletivos as fixtures
from .models import Produto, Compra, ContaPagar, MovimentoFinanceiro, AlteracaoPrecoVinculado
from .services.precos_compra import aplicar_precos_compra

FIELDS = ('preco_vista', 'preco_prazo', 'preco_vista_fracionado', 'preco_prazo_fracionado')
NEW = dict(zip(FIELDS, map(Decimal, ('146', '148', '6.20', '6.30'))))
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'


class TixanFixture(fixtures.SeletivosFixture):
    def setUp(self):
        super().setUp()
        for index, values in enumerate((('140', '142', '6', '6.30'), ('135', '138', '5.80', '6'), ('130', '132', '5.70', '5.90'))):
            models.QuerySet.update(Produto.objects.filter(pk=self.produtos[index].pk),
                nome=('Tixan Primavera sintetico', 'Tixan Maciez sintetico', 'Outro sintetico')[index],
                unidade_compra='FD', unidade_venda_1='FD', fator_conversao=24,
                **dict(zip(FIELDS, map(Decimal, values))))
            self.produtos[index].refresh_from_db()
        self.grupo.precos_regularizados=True
        self.grupo.save(update_fields=['precos_regularizados'])

    def compra(self, indices=(0,1)):
        compra,itens=super().compra(indices)
        compra.total=Decimal('2153.11')
        compra.estoque_entrada_realizada=True
        compra.save(update_fields=['total','estoque_entrada_realizada'])
        ContaPagar.objects.create(compra=compra,data_emissao=timezone.localdate(),
            valor_original=Decimal('2153.11'),valor_em_aberto=Decimal('2153.11'))
        return compra,itens

    def prices(self):
        return list(Produto.objects.order_by('pk').values_list(*FIELDS))

    def protected(self):
        # Whole persisted finance rows, purchase status/total, and individual stock/costs.
        return (
            list(ContaPagar.objects.order_by('pk').values()),
            list(MovimentoFinanceiro.objects.order_by('pk').values()),
            list(Compra.objects.order_by('pk').values('status', 'total', 'estoque_entrada_realizada')),
            list(Produto.objects.order_by('pk').values('quantidade', 'preco_compra', 'preco_compra_fracionado',
                'fator_conversao', 'unidade_compra', 'unidade_venda_1', 'unidade_venda_2', 'preco_venda_1', 'preco_venda_2')),
        )

    def review(self, values):
        compra, itens=self.compra((0,1))
        payload=self.payload(compra,itens[0],[p.pk for p in self.produtos[:2]])
        payload.update({
            'atualizar_preco_venda_produto_ids[]':[str(self.produtos[0].pk)]*len(values),
            'atualizar_preco_venda_nomes[]':list(values),
            'atualizar_preco_venda_valores[]':[str(v) for v in values.values()],
        })
        before=self.protected()
        response=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), payload)
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(self.protected(),before)
        return response


class TixanPurchaseTests(TixanFixture, TestCase):
    def test_all_four_eligible_but_unchanged_source_fourth_not_propagated(self):
        preview=self.client.get(self.url).json()['precos_vinculados']
        self.assertEqual(set(preview['campos']),set(FIELDS))
        optional=Produto.objects.filter(pk=self.produtos[2].pk).values().get()
        self.review(NEW)
        self.assertEqual(self.prices()[:2],[(Decimal('146'),Decimal('148'),Decimal('6.20'),Decimal('6.30')),
                                          (Decimal('146'),Decimal('148'),Decimal('6.20'),Decimal('6'))])
        self.assertEqual(Produto.objects.filter(pk=self.produtos[2].pk).values().get(),optional)
        self.assertEqual(set(AlteracaoPrecoVinculado.objects.get().evidencia['campos']),set(FIELDS[:3]))

    def test_explicit_changed_fourth_propagates_to_both(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[0].pk),preco_prazo_fracionado=Decimal('6.10'))
        self.review(NEW)
        self.assertEqual(self.prices()[:2],[tuple(NEW.values())]*2)
        self.assertEqual(set(AlteracaoPrecoVinculado.objects.get().evidencia['campos']),set(FIELDS))

    def test_maciez_can_change_only_fourth_without_equalizing_other_prices(self):
        compra,itens=self.compra((0,1));before=self.prices();protected=self.protected()
        aplicar_precos_compra(itens[1],{'preco_prazo_fracionado':Decimal('6.30')},
            **self.comando([p.pk for p in self.produtos[:2]],fonte=1))
        after=self.prices()
        self.assertEqual([row[:3] for row in after],[row[:3] for row in before])
        self.assertEqual([row[3] for row in after[:2]],[Decimal('6.30')]*2)
        self.assertEqual(after[2],before[2]);self.assertEqual(self.protected(),protected)

    def test_each_field_changes_independently_and_preserves_all_others(self):
        for field in FIELDS:
            with self.subTest(field=field), transaction.atomic():
                before=self.prices();protected=self.protected()
                _,itens=self.compra((0,1));protected=self.protected()
                aplicar_precos_compra(itens[0],{field:NEW[field]+Decimal('.10')},
                    **self.comando([p.pk for p in self.produtos[:2]]))
                for index,row in enumerate(self.prices()):
                    for column,other in enumerate(FIELDS):
                        self.assertEqual(row[column],NEW[field]+Decimal('.10') if index<2 and other==field else before[index][column])
                self.assertEqual(self.protected(),protected)
                transaction.set_rollback(True)

    def test_conflicting_copurchased_prices_rejected_in_both_orders(self):
        from . import views
        class Commands(dict):
            pass
        compra,_=self.compra((0,1));before=self.estado();protected=self.protected()
        for products in (self.produtos[:2],list(reversed(self.produtos[:2]))):
            command=Commands(((p.pk,'preco_vista'),Decimal('146')+i) for i,p in enumerate(products))
            command.versoes_grupos={p.pk:self.grupo.versao_precos for p in products}
            with self.assertRaises(ValueError),transaction.atomic():
                views._atualizar_precos_venda_produtos_compra(command,compra=compra)
            self.assertEqual(self.estado(),before);self.assertEqual(self.protected(),protected)
            self.assertFalse(AlteracaoPrecoVinculado.objects.exists())


@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
@override_settings(ROOT_URLCONF='estoque.tests_precos_seletivos')
class TixanConfirmationTests(TixanFixture, StaticLiveServerTestCase):
    host='127.0.0.1'

    def test_preview_distinguishes_excluded_field_from_included_equal_value(self):
        before=self.estado()
        for width in (390,1280):
            with self.subTest(width=width),TemporaryDirectory(prefix='tixan-chrome-') as profile:
                chrome=Chrome(CHROME,profile).start()
                try:
                    tab=chrome.tab()
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                    tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:produto_editar',args=[self.produtos[0].pk])})
                    tab.wait('typeof confirmarPrecosSeletivos==="function"')
                    for include_fourth in (False,True):
                        proposed={field:str(value) for field,value in NEW.items() if include_fourth or field!=FIELDS[3]}
                        tab.evaluate('window.result=null;confirmarPrecosSeletivos('+json.dumps(self.url)+','+str(self.produtos[0].pk)+','+json.dumps(proposed)+','+json.dumps([p.pk for p in self.produtos[:2]])+').then(r=>window.result=r);true')
                        tab.wait('!!document.querySelector("dialog[open]")')
                        rows=tab.evaluate('Array.from(document.querySelectorAll(".ps-confirm-card")).map(card=>({id:card.querySelector("input").value,checked:card.querySelector("input").checked,prices:Array.from(card.querySelectorAll(".ps-confirm-price")).map(r=>({field:r.dataset.campoPreco,value:r.querySelector(".ps-confirm-after").textContent,reason:r.querySelector(".ps-confirm-reason").textContent}))}))')
                        self.assertTrue(rows[0]['checked']);self.assertTrue(rows[1]['checked']);self.assertFalse(rows[2]['checked'])
                        self.assertIn('Campos inclu',tab.evaluate('document.querySelector(".ps-confirm-scope").textContent'))
                        fourth0=rows[0]['prices'][3];fourth1=rows[1]['prices'][3]
                        self.assertEqual(fourth1['value'].replace('\xa0',' '),'R$ 6,30' if include_fourth else 'R$ 6,00')
                        self.assertIn('valor proposto' if include_fourth else 'Fora deste reajuste',fourth0['reason'])
                        self.assertIn('selecionado',rows[2]['prices'][0]['reason'])
                        tab.evaluate('document.querySelector(".ps-confirm-primary").click();true')
                        tab.wait('!!window.result')
                        self.assertEqual(set(tab.evaluate('result.ids')),set(str(p.pk) for p in self.produtos[:2]))
                        self.assertEqual(self.estado(),before)
                finally:
                    chrome.stop()

    def test_purchase_browser_preview_matches_persisted_values(self):
        compra,_=self.compra((0,1));protected=self.protected()
        optional=Produto.objects.filter(pk=self.produtos[2].pk).values().get()
        with TemporaryDirectory(prefix='tixan-review-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/seletivos-revisao-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("rpAgora")')
                tab.evaluate('document.getElementById("rpAgora").click();true')
                tab.wait('!!document.querySelector("#rpLista .campoNovoPrecoVendaCompra")')
                tab.evaluate("const values={preco_vista:'146,00',preco_prazo:'148,00',preco_vista_fracionado:'6,20',preco_prazo_fracionado:'6,30'};\n                    document.querySelectorAll('#rpLista [data-campo-preco]').forEach(row=>{\n                        const input=row.querySelector('.campoNovoPrecoVendaCompra');\n                        if(input){input.value=values[row.dataset.campoPreco];input.dispatchEvent(new Event('input',{bubbles:true}));row.querySelector('.marcarAtualizarVendaCompra').checked=true;}\n                    });document.getElementById('rpSalvar').click();true")
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                preview=tab.evaluate('Array.from(document.querySelectorAll(".ps-confirm-card")).slice(0,2).map(card=>Array.from(card.querySelectorAll(".ps-confirm-after")).map(n=>n.textContent.replace(/[^0-9,]/g,"").replace(",",".")))')
                self.assertEqual(preview,[['146.00','148.00','6.20','6.30'],['146.00','148.00','6.20','6.00']])
                tab.evaluate('document.querySelector(".ps-confirm-primary").click();true')
                tab.wait('!document.getElementById("revisaoPrecosPosterior").classList.contains("visivel")')
                self.assertEqual([[Decimal(v) for v in row] for row in preview],[list(row) for row in self.prices()[:2]])
                self.assertEqual(self.protected(),protected)
                self.assertEqual(Produto.objects.filter(pk=self.produtos[2].pk).values().get(),optional)
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
                compra.refresh_from_db();self.assertFalse(compra.revisao_precos_pendente)
            finally:
                chrome.stop()

@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
@override_settings(ROOT_URLCONF='estoque.tests_precos_seletivos')
class TixanCompleteSequenceTests(TixanFixture, StaticLiveServerTestCase):
    """Real review navigation, browser serialization, HTTP writes and replay."""
    host='127.0.0.1'

    def sequence(self, source, width, raw='6,30', cancel=False):
        from .models import ItemCompra
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[source].pk),
                               preco_prazo_fracionado=Decimal('6'))
        compra,itens=self.compra((source,1-source))
        # One further independent item forces the actual "Salvar e proximo" path.
        independente=Produto.objects.create(nome='Produto seguinte sintetico',preco_compra=10,
                                            preco_vista=30,preco_prazo=32)
        ItemCompra.objects.create(compra=compra,produto=independente,quantidade=1,
            unidade='UN',preco_unitario=10,valor_total=10,preco_compra_anterior=9)
        protected=self.protected();optional=Produto.objects.filter(pk=self.produtos[2].pk).values().get()
        initial=self.estado()
        with TemporaryDirectory(prefix='tixan-sequence-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/seletivos-revisao-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("rpAgora")')
                # Observe real requests without changing their body or response.
                tab.evaluate('''window.pricePosts=[];const originalFetch=window.fetch;
                    window.fetch=function(url,options){if(options && options.method==='POST')window.pricePosts.push({url:String(url),body:String(options.body)});return originalFetch.apply(this,arguments);};
                    document.getElementById('rpAgora').click();true''')
                tab.wait('!!document.querySelector("#rpLista .campoNovoPrecoVendaCompra")')
                self.assertIn(self.produtos[source].nome,tab.evaluate('document.querySelector(".rp-compras-preco-produto").textContent'))
                self.assertIn('2',tab.evaluate('document.querySelector(".rp-compras-preco-progresso").textContent'))
                self.assertIn('Salvar e',tab.evaluate('document.getElementById("rpSalvar").textContent'))
                values={field:str(value).replace('.',',') for field,value in NEW.items()};values[FIELDS[3]]=raw
                # Edit ONLY rendered inputs, as a user does (no hidden-input synchronization).
                tab.evaluate('window.entered='+json.dumps(values)+';'+'''
                    document.querySelectorAll('#rpLista .rp-compras-preco-simulador').forEach(row=>{
                        if(!row.getClientRects().length)return;
                        const input=row.querySelector('.campoNovoPrecoVendaCompra');
                        input.value=entered[row.dataset.campoPreco];input.dispatchEvent(new Event('input',{bubbles:true}));
                        row.querySelector('.marcarAtualizarVendaCompra').checked=true;
                    });document.getElementById('rpSalvar').click();true''')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                if cancel:
                    tab.evaluate('document.querySelector(".ps-confirm-button:not(.ps-confirm-primary)").click();true')
                    tab.wait('!document.querySelector("dialog[open]") && !document.getElementById("rpSalvar").disabled')
                    self.assertEqual(tab.evaluate('pricePosts.length'),0);self.assertEqual(self.estado(),initial)
                    tab.evaluate('document.getElementById("rpSalvar").click();true')
                    tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertEqual(tab.evaluate('document.querySelectorAll("dialog[open] input:checked:disabled").length'),2)
                preview=tab.evaluate('''Array.from(document.querySelectorAll('.ps-confirm-card')).slice(0,2).map(card=>({id:card.querySelector('input').value,values:Array.from(card.querySelectorAll('.ps-confirm-after')).map(n=>n.textContent.replace(/[^0-9,]/g,'').replace(',','.'))}))''')
                for row in preview:self.assertEqual([Decimal(v) for v in row['values']],list(NEW.values()))
                self.assertEqual(self.estado(),initial)  # Editing did not mutate the original comparison snapshot or DB.
                tab.evaluate('document.querySelector(".ps-confirm-primary").click();true')
                try:
                    tab.wait('document.querySelector(".rp-compras-preco-produto").textContent.toLowerCase().includes("produto seguinte")')
                except AssertionError:
                    print('TIXAN_DIAGNOSTIC',tab.evaluate('JSON.stringify({error:document.getElementById("rpErro").textContent,posts:window.pricePosts,html:document.getElementById("rpLista").textContent})'),flush=True)
                    raise
                self.assertEqual(tab.evaluate('pricePosts.length'),1)
                from urllib.parse import parse_qs
                sent=parse_qs(tab.evaluate('pricePosts[0].body'))
                self.assertEqual(sent['atualizar_preco_venda_nomes[]'],list(FIELDS))
                self.assertEqual([Decimal(v.replace(',','.')) for v in sent['atualizar_preco_venda_valores[]']],list(NEW.values()))
                self.assertEqual(set(sent['destinatarios_precos_produto_'+str(self.produtos[source].pk)]),set(str(p.pk) for p in self.produtos[:2]))
                for row in preview:
                    product=Produto.objects.get(pk=int(row['id']))
                    self.assertEqual([getattr(product,field) for field in FIELDS],[Decimal(v) for v in row['values']])
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
                self.assertEqual(set(AlteracaoPrecoVinculado.objects.get().evidencia['campos']),set(FIELDS))
                self.assertFalse(compra.itens.filter(produto_id__in=[p.pk for p in self.produtos[:2]],revisao_preco_concluida=False).exists())
                # Going back to the saved group and forward must not send another command.
                saved=self.estado()
                tab.evaluate('document.getElementById("rpVoltar").click();true')
                self.assertTrue(tab.evaluate('Array.from(document.querySelectorAll("#rpLista input")).every(input=>input.disabled)'))
                tab.evaluate('document.getElementById("rpSalvar").click();true')
                try:
                    tab.wait('document.querySelector(".rp-compras-preco-produto").textContent.toLowerCase().includes("produto seguinte")')
                except AssertionError:
                    print('TIXAN_DIAGNOSTIC',tab.evaluate('JSON.stringify({error:document.getElementById("rpErro").textContent,posts:window.pricePosts,html:document.getElementById("rpLista").textContent})'),flush=True)
                    raise
                self.assertEqual(tab.evaluate('pricePosts.length'),1);self.assertEqual(self.estado(),saved)
                # A stale second participant cannot silently undo the first confirmed group review.
                before=self.estado()
                replay={**sent,'item_id':str(itens[1].pk),'produto_id':str(itens[1].produto_id)}
                response=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),replay)
                self.assertEqual(response.status_code,409);self.assertEqual(self.estado(),before)
                tab.evaluate('document.querySelectorAll("#rpLista .marcarAtualizarVendaCompra").forEach(b=>b.checked=false);document.getElementById("rpSalvar").click();true')
                tab.wait('!document.getElementById("revisaoPrecosPosterior").classList.contains("visivel")')
                self.assertEqual(self.estado(),saved);self.assertEqual(self.protected(),protected)
                self.assertEqual(Produto.objects.filter(pk=self.produtos[2].pk).values().get(),optional)
                compra.refresh_from_db();self.assertFalse(compra.revisao_precos_pendente)
            finally:chrome.stop()

    def test_primavera_initiates_desktop_full_sequence(self):self.sequence(0,1280)
    def test_maciez_initiates_mobile_full_sequence(self):self.sequence(1,390)
    def test_primavera_mobile_rounding_and_cancel_preserve_four_fields(self):self.sequence(0,390,'6,299',True)
    def test_maciez_desktop_decimal_dot_preserves_four_fields(self):self.sequence(1,1280,'6.30')

@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
class TixanDraftSequenceTests(TixanFixture, StaticLiveServerTestCase):
    """Review during synthetic purchase finalization, including hidden fields."""
    host='127.0.0.1'

    def sequence(self, source, width):
        from datetime import date
        from .models import Fornecedor, ListaCompraFornecedor, ItemCompra
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[source].pk),preco_prazo_fracionado=Decimal('6'))
        fornecedor=Fornecedor.objects.create(nome='Fornecedor sintetico')
        lista=ListaCompraFornecedor.objects.create(fornecedor=fornecedor,data_lista=date(2026,10,8),
            data_inicio_periodo=date(2026,10,1),data_fim_periodo=date(2026,10,8))
        compra=Compra.objects.create(fornecedor=fornecedor,lista_fornecedor=lista,
            data_compra=date(2026,10,8),total=36,status=Compra.STATUS_RASCUNHO)
        independente=Produto.objects.create(nome='Produto seguinte sintetico',preco_compra=10,preco_vista=30,preco_prazo=32,unidade_compra='UN')
        for product in (self.produtos[source],self.produtos[1-source],independente):
            ItemCompra.objects.create(compra=compra,produto=product,quantidade=1,preco_unitario=12,valor_total=12,unidade=product.unidade_compra)
        optional=Produto.objects.filter(pk=self.produtos[2].pk).values().get()
        with TemporaryDirectory(prefix='tixan-draft-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:compra_editar',args=[compra.pk])})
                tab.wait('document.readyState === "complete" && !!document.getElementById("compraQuantidadeBoletos")')
                tab.evaluate('''document.addEventListener('submit',event=>{
                    const entries=Array.from(new FormData(event.target).entries());
                    if(entries.some(([key])=>key.startsWith('confirmar_precos_produto_')))sessionStorage.setItem('tixanPosted',JSON.stringify(entries));
                },true);
                document.getElementById('tipoPagamentoCompra').value='aprazo';
                document.getElementById('tipoPagamentoCompra').dispatchEvent(new Event('change'));
                document.getElementById('compraFormaCobrancaNota').value='varios_boletos';
                document.getElementById('compraQuantidadeBoletos').value='3';
                document.getElementById('compraFormaCobrancaNota').dispatchEvent(new Event('change'));
                for(let n=1;n<=3;n++){
                    document.querySelector('[name="compra_parcela_valor_'+n+'"]').value='12,00';
                    document.querySelector('[name="compra_parcela_vencimento_'+n+'"]').value='2026-11-'+String(n+10).padStart(2,'0');
                }document.getElementById('btnSalvarCompra').click();true''')
                tab.wait('document.getElementById("modalConferenciaPrecosCompra").classList.contains("visivel")')
                if width<600:tab.evaluate('document.getElementById("btnRevisarPrecosAgoraCompra").click();true')
                tab.wait('!!document.querySelector("#modalConferenciaPrecosCompra .campoNovoPrecoVendaCompra")')
                tab.evaluate('window.entered='+json.dumps({f:str(v).replace('.',',') for f,v in NEW.items()})+';'+'''
                    document.querySelectorAll('#modalConferenciaPrecosCompra .compras-preco-simulador').forEach(row=>{
                        if(!row.getClientRects().length)return;
                        const input=row.querySelector('.campoNovoPrecoVendaCompra');input.value=entered[row.dataset.campoPreco];
                        input.dispatchEvent(new Event('input',{bubbles:true}));row.querySelector('.marcarAtualizarVendaCompra').checked=true;
                    });document.getElementById('btnAtualizarPrecosContinuarCompra').click();true''')
                tab.wait('document.getElementById("listaConferenciaPrecosCompra").textContent.toLowerCase().includes("produto seguinte")')
                # Return before confirmation: stored decisions must retain all four edits.
                tab.evaluate('document.getElementById("btnVoltarProdutoPrecoCompra").click();true')
                inputs=tab.evaluate('Array.from(document.querySelectorAll("#modalConferenciaPrecosCompra .campoNovoPrecoVendaCompra")).filter(e=>e.getClientRects().length).map(e=>e.value)')
                self.assertEqual([Decimal(v.replace(',','.')) for v in inputs],list(NEW.values()))
                tab.evaluate('document.getElementById("btnAtualizarPrecosContinuarCompra").click();true')
                tab.wait('document.getElementById("listaConferenciaPrecosCompra").textContent.toLowerCase().includes("produto seguinte")')
                tab.evaluate('document.querySelectorAll("#modalConferenciaPrecosCompra .marcarAtualizarVendaCompra").forEach(b=>b.checked=false);document.getElementById("btnAtualizarPrecosContinuarCompra").click();true')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertEqual(tab.evaluate('document.querySelectorAll("dialog[open] input:checked:disabled").length'),2)
                preview=tab.evaluate('Array.from(document.querySelectorAll(".ps-confirm-card")).slice(0,2).map(card=>Array.from(card.querySelectorAll(".ps-confirm-after")).map(n=>n.textContent.replace(/[^0-9,]/g,"").replace(",",".")))')
                self.assertEqual([[Decimal(v) for v in row] for row in preview],[list(NEW.values())]*2)
                tab.evaluate('document.querySelector(".ps-confirm-primary").click();true')
                tab.wait('location.pathname === "/compras/" && document.readyState === "complete"')
                posted=dict(tab.evaluate('JSON.parse(sessionStorage.getItem("tixanPosted"))'))
                for field,value in NEW.items():
                    self.assertEqual(Decimal(posted['novo_preco_venda_produto_'+str(self.produtos[source].pk)+'_'+field].replace(',','.')),value)
                self.assertEqual(self.prices()[:2],[tuple(NEW.values())]*2)
                self.assertEqual(Produto.objects.filter(pk=self.produtos[2].pk).values().get(),optional)
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
                compra.refresh_from_db();self.assertEqual(compra.status,Compra.STATUS_FINALIZADA)
                self.assertEqual(ContaPagar.objects.filter(compra=compra).count(),3)
            finally:chrome.stop()

    def test_primavera_draft_desktop_four_fields_hidden_and_navigation(self):self.sequence(0,1280)
    def test_maciez_draft_mobile_four_fields_hidden_and_navigation(self):self.sequence(1,390)
