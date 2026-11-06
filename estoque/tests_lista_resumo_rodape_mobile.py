"""Resumo de pendentes/confirmados e ações compartilhadas, somente fixtures."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque import tests_lista_cards_responsivos as base
from estoque import tests_lista_selecao_manual_mobile as manual
from estoque.models import Compra, Fornecedor, ItemVenda, ListaCompraFornecedor, Produto, ProdutoFornecedor, Venda
from estoque import tests_coleta_lista_browser as coleta
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


@skipUnless(Path(base.existing.CHROME).is_file(), 'Chrome unavailable')
class ResumoRodapeMobileTests(StaticLiveServerTestCase):
    setUp = base.ListaCardsResponsivosTests.setUp
    open_page = base.ListaCardsResponsivosTests.open_page
    clean_console = base.ListaCardsResponsivosTests.clean_console
    select = manual.SelecaoManualMobileTests.select
    filtros_catalogo = manual.SelecaoManualMobileTests.filtros_catalogo
    tocar = coleta.ColetaBrowserTests.tocar

    def navegador(self, nova=False, width=400):
        profile = TemporaryDirectory(prefix='resumo-rodape-ficticio-', ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        browser = Chrome(base.existing.CHROME, profile.name).start()
        self.addCleanup(browser.stop)
        tab = self.open_page(AuditedChrome(browser), width=width,
                             path=reverse('estoque:sugestao_compra_fornecedor')+'?nova=1' if nova else None)
        tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=width<=860, maxTouchPoints=1))
        return tab

    def resumo(self, t, count, total, confirmados=0, valor_confirmado='0,00'):
        self.assertEqual(t.evaluate("document.querySelector('[data-itens-lista-mobile-total]').textContent"), str(count))
        self.assertEqual(t.evaluate("document.querySelector('[data-total-lista-selecionada]').textContent.replace(/\u00a0/g,' ')"), 'R$ '+total)
        self.assertEqual(t.evaluate("document.querySelector('#itensListaMobile [data-produtos-confirmados]').textContent"), str(confirmados))
        self.assertEqual(t.evaluate("document.querySelector('#itensListaMobile [data-total-lista-pedido]').textContent.replace(/\u00a0/g,' ')"), 'R$ '+valor_confirmado)

    def incluir(self, t, produto):
        self.select(t, produto)
        t.evaluate("document.getElementById('produtoManualQtd').value='2';document.getElementById('produtoManualPrecoCompra').value='10,00';document.getElementById('produtoManualQtd').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t, '#btnAdicionarProdutoSugestao')

    def test_resumo_nao_vinculados_incluir_excluir_editar_pegar_salvar_reabrir(self):
        self.supplier = Fornecedor.objects.create(nome='Atacadista ficticio sem vinculos')
        vinculos = list(ProdutoFornecedor.objects.values())
        t = self.navegador(nova=True)
        self.filtros_catalogo(t)
        t.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy') && document.getElementById('msgAdicionarProdutoSugestao').textContent===''")
        self.incluir(t, self.products[0])
        self.incluir(t, self.products[1])
        self.resumo(t, 2, '40,00')
        self.incluir(t, self.products[2])
        self.resumo(t, 3, '60,00')
        root = lambda p: f'#mobileSugestaoProdutos [data-produto-id="{p.pk}"]'
        self.tocar(t, root(self.products[2])+' .sugestao-remover-item')
        self.tocar(t, '#btnConfirmarRemoverItemListaMobile')
        t.wait("document.querySelector('[data-itens-lista-mobile-total]').textContent==='2'")
        self.resumo(t, 2, '40,00')
        self.tocar(t, root(self.products[0])+' .sugestao-confirmar-card-mobile')
        t.evaluate('window.editando=document.querySelector('+json.dumps(root(self.products[0]))+");editando.querySelector('.sugestao-qtd-input').value='3';editando.querySelector('.sugestao-preco-input').value='12,00';editando.querySelector('.sugestao-preco-input').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.resumo(t, 2, '56,00')
        self.tocar(t, root(self.products[0])+' .sugestao-confirmar-card-mobile')
        self.tocar(t, root(self.products[0])+' .sugestao-adicionar-lista-mobile')
        self.resumo(t, 2, '56,00', 1, '36,00')
        self.tocar(t, '#btnGravarListaMobile')
        t.wait("location.pathname==='/compras/listas-fornecedor/' && document.readyState==='complete'")
        saved = ListaCompraFornecedor.objects.latest('pk')
        self.assertEqual((saved.itens.count(), saved.total_lista), (2, Decimal('56')))
        self.assertEqual(list(ProdutoFornecedor.objects.values()), vinculos)
        t.call('Page.navigate', dict(url=self.live_server_url+reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk])))
        t.wait("document.readyState==='complete' && !!window.prepararProdutoListaFornecedor")
        self.resumo(t, 2, '56,00', 1, '36,00')
        self.clean_console(t)

    def test_lista_longa_acoes_superiores_inferiores_e_repeticao(self):
        extras = Produto.objects.bulk_create([Produto(nome=f'Produto longo ficticio {n:02}', quantidade=0,
            estoque_minimo=1, preco_compra=2, preco_vista=3, preco_prazo=4,
            unidade_compra='PCT', unidade_venda_1='PCT') for n in range(20)])
        ProdutoFornecedor.objects.bulk_create([ProdutoFornecedor(produto=p, fornecedor=self.supplier) for p in extras])
        ItemVenda.objects.bulk_create([ItemVenda(venda=Venda.objects.first(), produto=p,
            quantidade=1, unidade='PCT', preco_unitario=3, valor_total=3) for p in extras])
        payloads = {}
        for gerar in (False, True):
            for inferior in (False, True):
                with self.subTest(gerar=gerar, inferior=inferior):
                    t = self.navegador()
                    # Confirm through the existing state transition; do not clone source cards.
                    quantidade = t.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length")
                    self.assertGreaterEqual(quantidade, 20)
                    t.evaluate("(async()=>{for(const c of document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card'))await adicionarCardSugestaoNaLista(c);return true})()")
                    t.wait("document.querySelector('#itensListaMobile [data-produtos-confirmados]').textContent==="+json.dumps(str(quantidade)))
                    self.assertTrue(t.evaluate("(()=>{const r=document.getElementById('acoesRodapeNovaLista').getBoundingClientRect();return [...document.querySelectorAll('#itensListaMobileLista .sugestao-item-lista-mobile')].every(c=>c.getBoundingClientRect().bottom<=r.top)})()"))
                    t.evaluate("window.enviados=[];window.fetchRodape=fetch;fetch=async(u,o)=>{if(o?.body instanceof FormData&&o.body.has('lista_payload')){enviados.push({payload:JSON.parse(o.body.get('lista_payload')),gerar:o.body.get('gerar_compra')});const r=await fetchRodape(u,o);await new Promise(resolve=>window.liberarRodape=resolve);return r;}return fetchRodape(u,o)};const f=document.getElementById('formGravarListaFornecedor');f.submit=function(){enviados.push({payload:JSON.parse(new FormData(this).get('lista_payload')),gerar:new FormData(this).get('gerar_compra')});window.enviarRodape=()=>HTMLFormElement.prototype.submit.call(this)};true")
                    selector = ('#btnGravarGerarCompraMobile' if gerar else '#btnGravarListaMobile') if inferior else ('#btnGravarGerarCompraFornecedor' if gerar else '#btnGravarListaFornecedor')
                    listas_antes = ListaCompraFornecedor.objects.count()
                    compras_antes = Compra.objects.count()
                    self.tocar(t, selector)
                    t.wait('enviados.length===1 && '+('!!window.enviarRodape' if gerar else '!!window.liberarRodape'))
                    if inferior:
                        self.assertGreater(t.evaluate('scrollY'), 1000)
                    self.assertTrue(t.evaluate("['btnGravarListaMobile','btnGravarGerarCompraMobile','btnGravarListaFornecedor','btnGravarGerarCompraFornecedor'].every(id=>document.getElementById(id).disabled)"))
                    t.evaluate("['btnGravarListaMobile','btnGravarGerarCompraMobile','btnGravarListaFornecedor','btnGravarGerarCompraFornecedor'].forEach(id=>document.getElementById(id).dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})));true")
                    self.assertEqual(t.evaluate('enviados.length'), 1)
                    payload = t.evaluate('enviados[0]')
                    if gerar in payloads:
                        self.assertEqual(payload, payloads[gerar])
                    payloads[gerar] = payload
                    self.clean_console(t)
                    t.evaluate(('enviarRodape()' if gerar else 'liberarRodape()')+';true')
                    t.wait("!location.pathname.includes('sugestao-fornecedor') && document.readyState==='complete'")
                    self.assertEqual(ListaCompraFornecedor.objects.count(), listas_antes+1)
                    self.assertEqual(Compra.objects.count(), compras_antes+int(gerar))
                    saved = ListaCompraFornecedor.objects.latest('pk')
                    self.assertEqual(saved.itens.count(), quantidade)
                    if gerar:
                        compra = Compra.objects.latest('pk')
                        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
                        self.assertEqual(compra.itens.count(), quantidade)

    def test_desktop_rodape_permanece_oculto_e_total_preservado(self):
        t = self.navegador(width=1280)
        self.assertFalse(t.evaluate("!!document.getElementById('acoesRodapeNovaLista').getClientRects().length"))
        self.assertEqual(t.evaluate("getComputedStyle(document.querySelector('.sugestao-rotulo-selecionados-mobile')).display"), 'none')
        self.assertEqual(t.evaluate("document.querySelector('[data-total-lista-selecionada]').textContent"), t.evaluate("document.querySelector('[data-total-lista-pedido]').textContent"))
        self.assertTrue(t.evaluate("document.getElementById('acoesRodapeNovaLista').parentElement.classList.contains('sugestao-card')"))
        self.clean_console(t)
