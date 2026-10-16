"""Ciclo de inclusao no Chrome real, exclusivamente com SQLite descartavel."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from offline.browser_support import Chrome
from .models import Compra, ContaPagar, Fornecedor, ItemCompra, ListaCompraFornecedor, MovimentoFinanceiro, Produto

CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponivel')
class CompraConfirmacaoBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('confirmacao', 'test@example.test', 'test-only'))
        supplier = Fornecedor.objects.create(nome='Fornecedor isolado')
        today = timezone.localdate()
        source = ListaCompraFornecedor.objects.create(fornecedor=supplier, data_lista=today,
            data_inicio_periodo=today, data_fim_periodo=today)
        self.purchase = Compra.objects.create(fornecedor=supplier, lista_fornecedor=source,
            data_compra=today, total=88, total_produtos=88, status=Compra.STATUS_RASCUNHO)
        self.products = []
        for name, price in [('Produto A', 40), ('Produto B', 48), ('Abacate Kg', 8)]:
            product = Produto.objects.create(nome=name, preco_compra=price, preco_vista=price+10,
                preco_prazo=price+11, quantidade=10, unidade_compra='KG')
            self.products.append(product)
            if name != 'Abacate Kg':
                ItemCompra.objects.create(compra=self.purchase, produto=product, quantidade=1,
                    unidade='KG', preco_unitario=price, valor_total=price, observacao='Preservar '+name)

    def open(self, chrome, width):
        tab = chrome.tab()
        tab.call('Network.enable')
        # Only the local test server is reachable by this page.
        tab.call('Network.setBlockedURLs', {'urls': ['https://*', '*neon.tech*']})
        tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844,
            'deviceScaleFactor': 1, 'mobile': width < 860})
        tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value,
            'url': self.live_server_url})
        tab.call('Page.navigate', {'url': self.live_server_url+reverse('estoque:compra_editar', args=[self.purchase.pk])})
        tab.wait('typeof compraCriarLinhaItem === "function"')
        return tab

    def total(self, tab, amount, count):
        self.assertEqual(tab.evaluate('document.getElementById("totalCompra").textContent.replace(/\u00a0/g," ")'), 'R$ '+amount)
        self.assertEqual(tab.evaluate('document.getElementById("totalItensCompraMobile").textContent'), str(count))

    def enter(self, tab):
        tab.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})
        tab.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})

    def fill(self, tab, selector, value):
        tab.evaluate('window.field=row.querySelector('+json.dumps(selector)+');field.focus();field.select();true')
        tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,60));return true})()')
        tab.call('Input.insertText', {'text': value})

    def add(self, tab, mobile):
        if not mobile:
            tab.evaluate('document.getElementById("btnAdicionarItem").click();true')
        tab.evaluate('window.row=document.querySelector('+json.dumps('#tbodyAdicionarProdutoMobile .linha-item' if mobile else '#tabelaItensCompra tbody .linha-item:last-child')+');true')
        self.assertTrue(tab.evaluate('row.dataset.itemProvisorio === "1"'))
        self.total(tab, '88,00', 2)
        self.fill(tab, '[name="produto_busca[]"]', 'Abacate')
        self.enter(tab)
        tab.wait('document.activeElement === row.querySelector(\'[name="quantidade[]"]\')')
        self.total(tab, '88,00', 2)
        self.fill(tab, '[name="quantidade[]"]', '1')
        self.total(tab, '88,00', 2)
        self.enter(tab)
        self.assertTrue(tab.evaluate('document.activeElement === row.querySelector(\'[name="unidade[]"]\')'))
        self.fill(tab, '[name="unidade[]"]', 'KG')
        self.total(tab, '88,00', 2)
        self.enter(tab)
        self.fill(tab, '.campo-preco-unitario-compra', '8,00')
        self.total(tab, '88,00', 2)
        self.assertTrue(tab.evaluate('row.dataset.itemProvisorio === "1"'))

    def capture_save(self, tab):
        # Observe the actual form submission after application submit handlers.
        tab.evaluate('window.saved=null;document.getElementById("formCompra").addEventListener("submit",e=>{e.preventDefault();window.saved=Array.from(new FormData(e.target).entries())});document.getElementById("btnSalvarRascunhoCompra").click();true')
        tab.wait('window.saved !== null')
        return tab.evaluate('saved.filter(([k])=>k==="produto_id[]").map(([,v])=>v)')

    def exercise(self, width, button=False):
        mobile = width < 860
        with TemporaryDirectory(prefix='compra-confirmacao-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open(chrome, width)
                self.total(tab, '88,00', 2)
                self.add(tab, mobile)
                # A provisional cost change must never enter price review.
                tab.evaluate('row.querySelector(".campo-preco-unitario-compra").value="9,00";row.querySelector(".campo-preco-unitario-compra").dispatchEvent(new Event("input",{bubbles:true}));true')
                ids = self.capture_save(tab)
                self.assertEqual(ids, [str(p.pk) for p in self.products[:2]])
                self.assertFalse(tab.evaluate('document.getElementById("modalConferenciaPrecosCompra").classList.contains("visivel")'))
                # Reload resets only this disposable browser page.
                tab.evaluate('window.reloadingTest=true');tab.call('Page.reload');tab.wait('!window.reloadingTest && typeof compraCriarLinhaItem === "function"')
                self.add(tab, mobile)
                if button:
                    self.assertEqual(tab.evaluate('document.getElementById("btnAdicionarItem").textContent'), 'Concluir item')
                    tab.evaluate('document.getElementById("btnAdicionarItem").click();true')
                else:
                    self.enter(tab)
                self.total(tab, '96,00', 3)
                # Editing an earlier confirmed row keeps its identity and other values.
                tab.evaluate('window.row=document.querySelector("#tabelaItensCompra tbody .linha-item");window.original=row.querySelector(\'[name="observacao_item[]"]\').value;true')
                if mobile:
                    tab.evaluate('row.querySelector(".btn-editar-item-mobile").click();true')
                self.fill(tab, '[name="quantidade[]"]', '2')
                self.assertTrue(tab.evaluate('row.dataset.itemProvisorio !== "1"'))
                if mobile:
                    tab.evaluate('row.querySelector(".btn-cancelar-edicao-item-mobile").click();true')
                    self.total(tab, '96,00', 3)
                    tab.evaluate('row.querySelector(".btn-editar-item-mobile").click();true')
                    self.fill(tab, '[name="quantidade[]"]', '2')
                    tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                else:
                    self.enter(tab);self.enter(tab);self.enter(tab)
                self.total(tab, '136,00', 3)
                self.assertTrue(tab.evaluate('row.querySelector(\'[name="observacao_item[]"]\').value===original'))
                # Remove only a pending draft; confirmed values and count survive.
                if not mobile:
                    tab.evaluate('document.getElementById("btnAdicionarItem").click();true')
                tab.evaluate('window.pending=document.querySelector('+json.dumps('#tbodyAdicionarProdutoMobile .linha-item' if mobile else '#tabelaItensCompra tbody .linha-item:last-child')+');pending.querySelector(".btn-remover").click();document.getElementById("btnConfirmarRemoverItemCompra").click();true')
                self.total(tab, '136,00', 3)
                if mobile:
                    self.assertTrue(tab.evaluate('!!document.querySelector("#tbodyAdicionarProdutoMobile .linha-item[data-item-provisorio=\'1\']")'))
                    self.assertEqual(tab.evaluate('document.querySelector(\'#tbodyAdicionarProdutoMobile [name="produto_id[]"]\').value'), '')
                self.purchase.refresh_from_db()
                self.assertEqual(self.purchase.total, Decimal('88'))
                self.assertEqual(self.purchase.itens.count(), 2)
                self.assertFalse(ContaPagar.objects.exists())
                self.assertFalse(MovimentoFinanceiro.objects.exists())
                self.assertEqual(list(Produto.objects.values_list('quantidade', flat=True)), [Decimal('10')]*3)
            finally:
                chrome.stop()

    def test_desktop_confirmation_edit_cancel_and_payload(self):
        self.exercise(1280)

    def test_mobile_confirmation_by_enter(self):
        self.exercise(390)

    def test_mobile_confirmation_by_button(self):
        self.exercise(390, button=True)

    def restore_draft(self, width):
        with TemporaryDirectory(prefix='compra-session-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open(chrome, width)
                self.add(tab, width < 860)
                # Run the real quick-product submit handler, preventing its POST.
                tab.evaluate('document.getElementById("produtoRapidoNome").value="Produto temporario";window.quick=document.getElementById("formProdutoRapidoCompra");quick.addEventListener("submit",e=>e.preventDefault());quick.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));true')
                stored = tab.evaluate('JSON.parse(sessionStorage.getItem("compraNovaRascunhoProdutoRapido"))')
                self.assertEqual([item['provisorio'] for item in stored['itens']], [False, False, True])
                tab.evaluate('window.reloadingTest=true')
                tab.call('Page.reload')
                tab.wait('!window.reloadingTest && typeof compraCriarLinhaItem === "function"')
                self.total(tab, '88,00', 2)
                tab.evaluate('window.row=document.querySelector(".linha-item[data-item-provisorio=\'1\']");true')
                self.assertEqual(tab.evaluate('row.querySelector(\'[name="produto_id[]"]\').value'), str(self.products[2].pk))
                self.assertEqual(tab.evaluate('row.querySelector(".campo-preco-unitario-compra").value'), '8,00')
                self.assertEqual(tab.evaluate('document.querySelector(\'#tabelaItensCompra [name="observacao_item[]"]\').value'), 'Preservar Produto A')
                self.assertIsNone(tab.evaluate('sessionStorage.getItem("compraNovaRascunhoProdutoRapido")'))
                tab.evaluate('row.querySelector(".campo-preco-unitario-compra").focus();true')
                self.enter(tab)
                self.total(tab, '96,00', 3)
            finally:
                chrome.stop()

    def test_desktop_session_restore_preserves_provisional_and_confirmed(self):
        self.restore_draft(1280)

    def test_mobile_session_restore_preserves_provisional_and_confirmed(self):
        self.restore_draft(390)

    def test_provisional_price_change_is_excluded_from_review_and_final_payload(self):
        for width in [1280, 390]:
            with self.subTest(width=width), TemporaryDirectory(prefix='compra-review-', ignore_cleanup_errors=True) as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = self.open(chrome, width)
                    self.add(tab, width < 860)
                    self.fill(tab, '.campo-preco-unitario-compra', '9,00')
                    # If provisional rows entered the review, this cost divergence
                    # would prevent submission and open the price-review modal.
                    tab.evaluate('window.payment=document.querySelector(\'[name="tipo_pagamento"]\');payment.value="aprazo";payment.dispatchEvent(new Event("change",{bubbles:true}));document.querySelector(\'[name="data_vencimento"]\').value="2027-12-01";window.finalPayload=null;document.getElementById("formCompra").addEventListener("submit",e=>{window.prevented=e.defaultPrevented;e.preventDefault();window.finalPayload=Array.from(new FormData(e.target).entries())});document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('window.finalPayload !== null')
                    self.assertFalse(tab.evaluate('prevented'))
                    self.assertEqual(tab.evaluate('finalPayload.filter(([k])=>k==="produto_id[]").map(([,v])=>v)'), [str(p.pk) for p in self.products[:2]])
                    self.assertFalse(tab.evaluate('document.getElementById("modalConferenciaPrecosCompra").classList.contains("visivel")'))
                finally:
                    chrome.stop()
