"""Real Chrome layout/interaction checks; fixtures stay in disposable PostgreSQL."""
import base64
from copy import deepcopy
import json
import os
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from estoque import tests_compra_mobile_total_browser as existing
from estoque.models import Compra, ItemCompra, ItemVenda, ListaCompraFornecedor, Produto, ProdutoFornecedor, Venda


@skipUnless(Path(existing.CHROME).is_file(), 'Chrome unavailable')
class CompraMobileCompactaTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('COMPRA_MOBILE_BASELINE'):
            # Serve the exact published template without changing the worktree.
            directory = TemporaryDirectory(prefix='compact-original-template-')
            cls.addClassCleanup(directory.cleanup)
            root = Path(__file__).resolve().parent.parent
            original = subprocess.check_output(['git', 'show', '4d3959f:estoque/templates/estoque/compras_sugestao_fornecedor.html'], cwd=root)
            target = Path(directory.name) / 'estoque' / 'compras_sugestao_fornecedor.html'
            target.parent.mkdir()
            target.write_bytes(original)
            templates = deepcopy(settings.TEMPLATES)
            templates[0]['DIRS'] = [directory.name] + list(templates[0]['DIRS'])
            override = override_settings(TEMPLATES=templates)
            override.enable()
            cls.addClassCleanup(override.disable)
        super().setUpClass()

    setUp = existing.CompraMobileTotalBrowserTests.setUp
    open_page = existing.CompraMobileTotalBrowserTests.open_page
    add = existing.CompraMobileTotalBrowserTests.add
    total = existing.CompraMobileTotalBrowserTests.total
    effects = existing.CompraMobileTotalBrowserTests.effects

    def test_pending_and_confirmed_sections_at_mobile_widths(self):
        with TemporaryDirectory(prefix='compact-sections-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    tab = self.open_page(chrome, width=width)
                    tab.wait('document.readyState==="complete"')
                    self.assertEqual(tab.evaluate('document.getElementById("preListaMobileTitulo").textContent'), 'Pré-lista — Falta comprar')
                    self.assertEqual(tab.evaluate('document.getElementById("listaConfirmadaMobileTitulo").textContent'), 'Lista confirmada — Já peguei')
                    # Let the existing initial-page positioning finish first;
                    # distinguish that motion from a confirmation-triggered jump.
                    tab.evaluate('window.positionReady=false;setTimeout(()=>{window.scrollTo({top:0,behavior:"instant"});window.positionReady=true},600);true')
                    tab.wait('positionReady')
                    tab.evaluate('window.first=document.querySelector("#mobileSugestaoProdutos .sugestao-mobile-card");window.firstHeight=first.getBoundingClientRect().height;window.beforeY=scrollY;window.scrollCalls=0;Element.prototype.scrollIntoView=function(){scrollCalls++};window.scrollBy=function(){scrollCalls++};window.scrollTo=function(){scrollCalls++};true')
                    self.add(tab, 2, '0,500', '24,00')
                    self.assertEqual(tab.evaluate('first.getBoundingClientRect().height'), tab.evaluate('firstHeight'))
                    self.assertTrue(tab.evaluate('''(() => {
                        const pending=[...document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card')].filter(c=>c.getClientRects().length);
                        const section=document.getElementById('itensListaMobile');
                        return pending.length===3 && pending.every(c=>c.getBoundingClientRect().bottom<=section.getBoundingClientRect().top);
                    })()'''))
                    tab.evaluate('window.item=document.querySelector(".sugestao-item-lista-mobile");item.querySelector(".sugestao-item-detalhes").open=true;true')
                    expected = tab.evaluate('card.querySelector(".sugestao-mobile-grid").textContent')
                    self.assertEqual(tab.evaluate('item.querySelector(".sugestao-mobile-grid").textContent'), expected)
                    self.assertEqual(tab.evaluate('item.querySelector("[data-preco-unitario-confirmado]").textContent'), '24,00')
                    self.assertEqual(tab.evaluate('getComputedStyle(item).backgroundColor'), 'rgb(240, 253, 244)')
                    self.assertEqual(tab.evaluate('getComputedStyle(item).borderTopColor'), 'rgb(22, 163, 74)')
                    self.assertEqual(tab.evaluate('getComputedStyle(first).backgroundColor'), 'rgb(255, 243, 244)')
                    self.assertEqual(tab.evaluate('getComputedStyle(item.querySelector(".sugestao-item-lista-mobile-topo span")).fontSize'), '16px')
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    tab.evaluate('window.noScrollCheck=false;setTimeout(()=>window.noScrollCheck=true,1900);true')
                    tab.wait('noScrollCheck', timeout=5)
                    self.assertEqual(tab.evaluate('scrollCalls'), 0)
                    self.assertEqual(tab.evaluate('scrollY'), tab.evaluate('beforeY'))
                    artifacts = Path(__file__).resolve().parent.parent / 'evidencias-mobile-lista'
                    clip = tab.evaluate('(()=>{const r=document.getElementById("itensListaMobile").getBoundingClientRect();return {x:0,y:r.top+scrollY,width:innerWidth,height:r.height+12,scale:1}})()')
                    image = tab.call('Page.captureScreenshot', {'format':'png', 'captureBeyondViewport':True, 'clip':clip})
                    (artifacts / f'secoes-confirmados-{width}.png').write_bytes(base64.b64decode(image['data']))
            finally:
                chrome.stop()

    def test_layout_and_visual_evidence(self):
        product = self.products[0]
        product.nome = 'Sabão Pó Tixan Primavera Embalagem Econômica 24/400G'
        product.save(update_fields=['nome'])
        measurements = []
        phase = 'antes' if os.environ.get('COMPRA_MOBILE_BASELINE') else 'depois'
        artifacts = Path(__file__).resolve().parent.parent / 'evidencias-mobile-lista'
        artifacts.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix='compact-layout-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    tab = self.open_page(chrome, width=width)
                    tab.wait('document.readyState==="complete"')
                    tab.evaluate('window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="' + str(product.pk) + '"]\'); card.scrollIntoView({block:"start",behavior:"instant"});true')
                    metrics = tab.evaluate('''(() => {
                        const name=card.querySelector('.sugestao-mobile-produto');
                        const label=card.querySelector('.sugestao-mobile-label');
                        const input=card.querySelector('.sugestao-input');
                        return {width:innerWidth, height:card.getBoundingClientRect().height,
                            name:name.textContent.trim(), nameFont:getComputedStyle(name).fontSize,
                            nameWeight:getComputedStyle(name).fontWeight,
                            labelFont:getComputedStyle(label).fontSize,
                            inputFont:getComputedStyle(input).fontSize,
                            inputHeight:input.getBoundingClientRect().height,
                            overflow:document.documentElement.scrollWidth>innerWidth,
                            values:card.querySelectorAll('.sugestao-mobile-value').length,
                            fields:card.querySelectorAll('input').length};
                    })()''')
                    measurements.append(metrics)
                    self.assertFalse(metrics['overflow'])
                    self.assertEqual(metrics['values'], 5)
                    self.assertEqual(metrics['fields'], 4)
                    self.assertGreaterEqual(metrics['inputHeight'], 44)
                    self.assertEqual(metrics['name'], product.nome)
                    if phase == 'depois':
                        self.assertLess(metrics['height'], 650)
                        self.assertEqual(metrics['nameFont'], '18px')
                        self.assertEqual(metrics['labelFont'], '11.52px')
                        self.assertEqual(metrics['inputFont'], '15.68px')
                        self.assertTrue(tab.evaluate('[...card.querySelectorAll("input,button")].every(e=>e.getBoundingClientRect().height>=44 && e.scrollWidth<=e.clientWidth)'))
                        self.assertEqual(tab.evaluate('getComputedStyle(document.getElementById("atalho-despesa-global")).position'), 'static')
                        self.assertEqual(tab.evaluate('getComputedStyle(document.getElementById("botao-voltar-topo")).position'), 'static')
                        self.assertEqual(tab.evaluate('getComputedStyle(document.getElementById("atalhosSugestaoFornecedor")).position'), 'relative')
                    clip = tab.evaluate('(()=>{const r=card.getBoundingClientRect();return {x:0,y:r.top+scrollY,width:innerWidth,height:r.height+12,scale:1}})()')
                    image = tab.call('Page.captureScreenshot', {'format':'png', 'captureBeyondViewport':True, 'clip':clip})
                    (artifacts / f'{phase}-{width}.png').write_bytes(base64.b64decode(image['data']))
            finally:
                chrome.stop()
        (artifacts / f'medidas-{phase}.json').write_text(json.dumps(measurements, indent=2, ensure_ascii=False), encoding='utf-8')
        print('MOBILE_CARD_' + phase.upper() + ' ' + json.dumps(measurements, ensure_ascii=False), flush=True)

    def test_out_of_order_return_edit_exclude_and_save(self):
        before = self.effects()
        with TemporaryDirectory(prefix='compact-picking-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                tab = self.open_page(chrome)
                original_ids = tab.evaluate('[...document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card")].map(c=>c.dataset.produtoId)')
                self.add(tab, 2, '0,500', '24,00')
                self.add(tab, 0, '3', '12,00')
                clip = tab.evaluate('(()=>{const r=document.getElementById("itensListaMobile").getBoundingClientRect();return {x:0,y:r.top+scrollY,width:innerWidth,height:r.height+12,scale:1}})()')
                image = tab.call('Page.captureScreenshot', {'format':'png', 'captureBeyondViewport':True, 'clip':clip})
                artifacts = Path(__file__).resolve().parent.parent / 'evidencias-mobile-lista'
                artifacts.mkdir(exist_ok=True)
                (artifacts / 'confirmados-390.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-pendentes]").textContent'), '2')
                self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-confirmados]").textContent'), '2')
                self.assertEqual(tab.evaluate('[...document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card")].map(c=>c.dataset.produtoId)'), original_ids)
                tab.evaluate('window.item=document.querySelector(".sugestao-item-lista-mobile");window.sameInput=item.querySelector("input");item.querySelector(".sugestao-editar-lista-mobile").click();item.querySelector(".sugestao-item-lista-qtd-input").value="1,500";item.querySelector(".sugestao-item-lista-preco-input").value="20,00";item.querySelector(".sugestao-devolver-lista-mobile").click();true')
                returned = '#mobileSugestaoProdutos [data-produto-id="' + str(self.products[2].pk) + '"]'
                self.assertTrue(tab.evaluate(f'document.querySelector({returned!r}).getClientRects().length>0'))
                self.assertEqual(tab.evaluate(f'document.querySelector({returned!r}+" .sugestao-qtd-input").value'), '1,500')
                self.assertEqual(tab.evaluate(f'document.querySelector({returned!r}+" .sugestao-preco-input").value'), '20,00')
                self.assertEqual(self.total(tab), 'R$ 36,00')
                tab.evaluate(f'document.querySelector({returned!r}+" .sugestao-adicionar-lista-mobile").click();true')
                self.assertEqual(self.total(tab), 'R$ 66,00')
                # In-place refresh must preserve the same input/history nodes.
                tab.evaluate('window.item=document.querySelector(".sugestao-item-lista-mobile");window.sameInput=item.querySelector("input");window.source=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="\'+item.dataset.produtoId+\'"]\');source.querySelector("input").dispatchEvent(new Event("input",{bubbles:true}));window.refreshDone=false;setTimeout(()=>window.refreshDone=true,30);true')
                tab.wait('window.refreshDone')
                self.assertTrue(tab.evaluate('item.querySelector("input")===sameInput'))
                tab.evaluate('item.querySelector(".sugestao-remover-lista-mobile").click();document.getElementById("btnConfirmarRemoverItemListaMobile").click();true')
                excluded_id = str(self.products[0].pk)
                self.assertEqual(tab.evaluate('document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="'+excluded_id+'"]\').getClientRects().length'), 0)
                self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-pendentes]").textContent'), '2')
                self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-confirmados]").textContent'), '1')
                self.assertEqual(self.total(tab), 'R$ 30,00')
                tab.evaluate('HTMLFormElement.prototype.submit=function(){window.captured=JSON.parse(document.getElementById("listaFornecedorPayload").value)};document.getElementById("btnGravarListaFornecedor").click();true')
                tab.wait('!!window.captured')
                payload = tab.evaluate('captured')
                self.assertEqual(len(payload['linhas']), 1)
                self.assertEqual(str(payload['linhas'][0]['produtoId']), str(self.products[2].pk))
                response = self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'), {'lista_payload':json.dumps(payload)})
                self.assertEqual(response.status_code, 302)
                saved = ListaCompraFornecedor.objects.get()
                self.assertEqual(saved.total_lista, Decimal('30.00'))
                tab = self.open_page(chrome, path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                self.assertEqual(self.total(tab), 'R$ 30,00')
                self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-confirmados]").textContent'), '1')
                self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_confirmation_does_not_schedule_scrolls_or_rebuild_pending_cards(self):
        phase = 'antes' if os.environ.get('COMPRA_MOBILE_BASELINE') else 'depois'
        sale = Venda.objects.first()
        for index in range(96):
            product = Produto.objects.create(nome=f'Produto adicional {index:03}', preco_compra=10,
                preco_vista=11, preco_prazo=12, quantidade=0, estoque_minimo=1,
                unidade_compra='CX', unidade_venda_1='CX')
            ProdutoFornecedor.objects.create(produto=product, fornecedor=self.supplier)
            ItemVenda.objects.create(venda=sale, produto=product, quantidade=1, unidade='CX', preco_unitario=11, valor_total=11)
        with TemporaryDirectory(prefix='compact-many-cards-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                tab = self.open_page(chrome)
                tab.wait('document.readyState==="complete"')
                result = tab.evaluate('''(() => {
                    const cards=[...document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card')];
                    window.pendingNode=cards[0];window.pendingInput=cards[0].querySelector('input');
                    window.scrollCalls=0;const original=Element.prototype.scrollIntoView;
                    Element.prototype.scrollIntoView=function(...args){scrollCalls++;return original.apply(this,args)};
                    const start=performance.now();window.hiddenAfterMilliseconds=null;
                    const observer=new MutationObserver(()=>{
                        if(cards[50].getClientRects().length===0){
                            hiddenAfterMilliseconds=performance.now()-start;observer.disconnect();
                        }
                    });
                    observer.observe(cards[50], {attributes:true});
                    cards[50].querySelector('.sugestao-adicionar-lista-mobile').click();
                    if(cards[50].getClientRects().length===0){
                        hiddenAfterMilliseconds=performance.now()-start;observer.disconnect();
                    }
                    return {cards:cards.length, milliseconds:performance.now()-start,
                        hidden:cards[50].getClientRects().length===0,
                        confirmed:document.querySelectorAll('.sugestao-item-lista-mobile').length};
                })()''')
                self.assertEqual(result['cards'], 100)
                self.assertEqual(result['confirmed'], 1)
                tab.evaluate('window.delayedScrollCheck=false;setTimeout(()=>window.delayedScrollCheck=true,1900);true')
                tab.wait('window.delayedScrollCheck', timeout=5)
                result['scrollCalls'] = tab.evaluate('scrollCalls')
                result['hiddenAfterMilliseconds'] = tab.evaluate('hiddenAfterMilliseconds')
                self.assertTrue(tab.evaluate('pendingNode.isConnected && pendingNode.querySelector("input")===pendingInput'))
                if phase == 'depois':
                    self.assertTrue(result['hidden'])
                    self.assertEqual(result['scrollCalls'], 0)
                    self.assertEqual(tab.evaluate('document.querySelector("[data-produtos-pendentes]").textContent'), '99')
                artifacts = Path(__file__).resolve().parent.parent / 'evidencias-mobile-lista'
                artifacts.mkdir(exist_ok=True)
                (artifacts / f'confirmacao-100-produtos-{phase}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                print('CONFIRMACAO_100_PRODUTOS_' + phase.upper() + ' ' + json.dumps(result), flush=True)
            finally:
                chrome.stop()

    def test_history_three_purchases_and_package_conversion(self):
        product = Produto.objects.create(nome='Pacote sintético 12/200ml', preco_compra=24,
            preco_vista=25, preco_prazo=26, quantidade=0, estoque_minimo=1,
            unidade_compra='PCT', unidade_venda_1='PCT', unidade_venda_2='UN',
            fator_conversao=12, vende_fracionado=True)
        ProdutoFornecedor.objects.create(produto=product, fornecedor=self.supplier)
        ItemVenda.objects.create(venda=Venda.objects.first(), produto=product, quantidade=1, unidade='PCT', preco_unitario=25, valor_total=25)
        for days in [4, 3, 2, 1]:
            purchase = Compra.objects.create(fornecedor=self.supplier, data_compra=timezone.localdate()-timedelta(days=days), status=Compra.STATUS_FINALIZADA)
            ItemCompra.objects.create(compra=purchase, produto=product, quantidade=days, unidade='PCT', preco_unitario=24, valor_total=days*24)
        before = self.effects()
        with TemporaryDirectory(prefix='compact-history-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    tab = self.open_page(chrome, width=width)
                    tab.evaluate('window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="'+str(product.pk)+'"]\');card.querySelector("[data-historico-toggle]").click();true')
                    self.assertEqual(tab.evaluate('card.querySelectorAll(".sugestao-historico-item").length'), 3)
                    self.assertEqual(tab.evaluate('[...card.querySelectorAll(".sugestao-historico-item")].map(e=>e.children[0].textContent.trim())'), [(timezone.localdate()-timedelta(days=d)).strftime('%d/%m/%Y') for d in [1,2,3]])
                    self.assertTrue(tab.evaluate('[...card.querySelectorAll(".sugestao-historico-item")].every(e=>e.children.length===4 && e.textContent.includes("PCT") && e.textContent.includes("24,00"))'))
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    tab.evaluate('card.querySelector(".sugestao-qtd-input").value="0,500";card.querySelector(".sugestao-qtd-input").dispatchEvent(new Event("input",{bubbles:true}));true')
                    self.assertEqual(tab.evaluate('card.querySelector(".sugestao-preco-unitario-input").value'), '2,00')
                    self.assertEqual(tab.evaluate('card.querySelector(".sugestao-total-input").value'), '12,00')
                    tab.evaluate('card.querySelector(".sugestao-adicionar-lista-mobile").click();true')
                    self.assertEqual(self.total(tab), 'R$ 12,00')
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".sugestao-item-lista-mobile .sugestao-historico-item").length'), 3)
                self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_manual_addition_to_saved_list_stays_visible_and_editable(self):
        payload = {'fornecedorId':self.supplier.pk, 'dataInicio':timezone.localdate().isoformat(),
            'dataFim':timezone.localdate().isoformat(), 'linhas':[{
                'produtoId':self.products[0].pk, 'sugestao':'1', 'precoCompra':'47,40',
                'precoUnitario':'47,40', 'total':'47,40', 'unidade':'PCT'}]}
        self.client.post(reverse('estoque:compras_lista_fornecedor_gravar'), {'lista_payload':json.dumps(payload)})
        saved = ListaCompraFornecedor.objects.get()
        before = self.effects()
        with TemporaryDirectory(prefix='compact-saved-manual-') as profile:
            chrome = Chrome(existing.CHROME, profile).start()
            try:
                tab = self.open_page(chrome, path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                tab.evaluate('document.getElementById("produtoManualSugestao").value="'+str(self.products[1].pk)+'";document.getElementById("produtoManualQtd").value="2";document.getElementById("produtoManualPrecoCompra").value="35,40";document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarProdutoSugestao").click();true')
                tab.wait('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length===2')
                selector = '#mobileSugestaoProdutos [data-produto-id="'+str(self.products[1].pk)+'"]'
                self.assertTrue(tab.evaluate(f'document.querySelector({selector!r}).getClientRects().length>0'))
                self.assertEqual(tab.evaluate('document.querySelectorAll(".sugestao-item-lista-mobile").length'), 0)
                tab.evaluate(f'window.card=document.querySelector({selector!r});card.querySelector(".sugestao-confirmar-card-mobile").click();card.querySelector(".sugestao-qtd-input").value="3";card.querySelector(".sugestao-preco-input").value="40,00";card.querySelector(".sugestao-preco-input").dispatchEvent(new Event("input",{{bubbles:true}}));card.querySelector(".sugestao-confirmar-card-mobile").click();true')
                self.assertTrue(tab.evaluate('card.querySelector(".sugestao-qtd-input").readOnly'))
                self.assertEqual(self.total(tab), 'R$ 167,40')
                self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()
