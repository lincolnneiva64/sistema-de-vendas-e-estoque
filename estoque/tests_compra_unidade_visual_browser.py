"""Inspeção de layout com 11 itens sintéticos e rolagem; sem gravação operacional."""
import base64
import json
from pathlib import Path
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from estoque.models import ItemCompra, Produto
from estoque.tests_compra_pre_revisao import CompraPreRevisaoFixture
from estoque import tests_compra_unidade_selecao as selecao
from estoque import tests_decisoes_precos_browser as decisoes


class UnidadeVisualBrowserTests(CompraPreRevisaoFixture, StaticLiveServerTestCase):
    navegador = selecao.UnidadeSelecaoBrowserTests.navegador
    abrir = selecao.UnidadeSelecaoBrowserTests.abrir
    tocar = decisoes.DecisoesBrowserTests.tocar

    def setUp(self):
        super().setUp()
        for n in range(1, 11):
            produto = Produto.objects.create(nome=f'Produto visual {n:02d}', quantidade=10,
                preco_compra=10, preco_vista=15, preco_prazo=16, unidade_compra='UN')
            ItemCompra.objects.create(compra=self.compra, produto=produto, quantidade=9,
                unidade='UN', preco_unitario=10, valor_total=90)
        self.compra.total = self.compra.total_produtos = 990
        self.compra.save(update_fields=['total','total_produtos'])

    def foto(self, t, nome):
        pasta = Path('amostras/unidade-visual')
        pasta.mkdir(parents=True, exist_ok=True)
        t.evaluate("(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()")
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot', {'format':'png'})['data']))

    def verificar(self, largura):
        t = self.navegador()
        itens = list(self.compra.itens.order_by('pk'))
        for posicao in (5,10):
            self.compra.itens.update(unidade='UN')
            self.compra.itens.filter(pk=itens[posicao].pk).update(unidade='2,95')
            antes = list(self.compra.itens.order_by('pk').values())
            self.abrir(t, largura)
            t.evaluate("window.linhaVisual=[...document.querySelectorAll('#tabelaItensCompra tbody .linha-item')].find(l=>l.querySelector('[name=\"produto_id[]\"]').value==="+json.dumps(str(itens[posicao].produto_id))+");window.campoVisual=linhaVisual.querySelector('[name=\"unidade[]\"]');window.avisoVisual=linhaVisual.querySelector('.aviso-unidade-compra');true")
            self.assertFalse(t.evaluate("!!document.querySelector('#avisoUnidadesCompra')"))
            self.assertFalse(t.evaluate("!!linhaVisual.querySelector('.compra-unidade-celula label')"))
            self.assertEqual(t.evaluate('[...campoVisual.options].map(o=>o.value)'), ['', 'UN'])
            if largura < 820:
                t.evaluate("linhaVisual.querySelector('.btn-editar-item-mobile').click();true")
            t.evaluate("linhaVisual.scrollIntoView({block:'center',behavior:'instant'});true")
            t.wait("getComputedStyle(avisoVisual).visibility==='visible'")
            self.assertEqual(t.evaluate('avisoVisual.textContent'), 'Unidade inválida. Selecione uma unidade cadastrada.')
            medidas = t.evaluate("(() => {const r=linhaVisual.getBoundingClientRect();return ['.produto-busca','[name=\"quantidade[]\"]','[name=\"unidade[]\"]','.campo-preco-unitario-compra','.valor-total-item'].map(s=>{const x=linhaVisual.querySelector(s).getBoundingClientRect();return {top:x.top-r.top,height:x.height,width:x.width}})})()")
            if largura >= 820:
                self.assertAlmostEqual(medidas[1]['top'], medidas[2]['top'], delta=1)
                self.assertAlmostEqual(medidas[1]['height'], medidas[2]['height'], delta=.1)
            self.foto(t, f'{largura}-{posicao}-invalida')
            # Fora da tela o aviso não fica perdido sobre outros produtos.
            t.evaluate("window.scrollTo({top:0,behavior:'instant'});true")
            t.wait("getComputedStyle(avisoVisual).visibility==='hidden'")
            self.tocar(t, '#btnSalvarCompra')
            t.wait('document.activeElement===campoVisual')
            self.assertTrue(t.evaluate("getComputedStyle(avisoVisual).visibility==='visible'"))
            self.assertTrue(t.evaluate('(()=>{const c=campoVisual.getBoundingClientRect(),a=avisoVisual.getBoundingClientRect();return c.top>=0&&c.bottom<=innerHeight&&a.top>=0&&a.bottom<=innerHeight})()'))
            self.assertFalse(t.evaluate("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')"))
            # Medidas dentro da linha, independentes da posição de rolagem.
            t.evaluate("window.medidasAntes=[...linhaVisual.children].map(e=>({top:e.getBoundingClientRect().top-linhaVisual.getBoundingClientRect().top,height:e.getBoundingClientRect().height}));window.valoresAntes=[linhaVisual.querySelector('[name=\"quantidade[]\"]').value,linhaVisual.querySelector('.campo-preco-unitario-compra').value,linhaVisual.querySelector('.valor-total-item').textContent];campoVisual.value='UN';campoVisual.dispatchEvent(new Event('change',{bubbles:true}));true")
            self.assertTrue(t.evaluate('avisoVisual.hidden'))
            self.assertEqual(t.evaluate("campoVisual.getAttribute('aria-invalid')"), 'false')
            self.assertTrue(t.evaluate("JSON.stringify(medidasAntes)===JSON.stringify([...linhaVisual.children].map(e=>({top:e.getBoundingClientRect().top-linhaVisual.getBoundingClientRect().top,height:e.getBoundingClientRect().height})))"))
            self.assertTrue(t.evaluate("JSON.stringify(valoresAntes)===JSON.stringify([linhaVisual.querySelector('[name=\"quantidade[]\"]').value,linhaVisual.querySelector('.campo-preco-unitario-compra').value,linhaVisual.querySelector('.valor-total-item').textContent])"))
            self.foto(t, f'{largura}-{posicao}-valida')
            self.assertEqual(list(self.compra.itens.order_by('pk').values()), antes)
            self.assert_sem_efeitos()
            print('UNIDADE_LAYOUT '+json.dumps({'largura':largura,'posicao':posicao,'campos':medidas}), flush=True)

    def test_desktop_meio_e_fim(self):
        self.verificar(1280)

    def test_mobile_meio_e_fim(self):
        self.verificar(390)
