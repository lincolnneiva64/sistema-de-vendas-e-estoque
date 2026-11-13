"""Unidade selecionável: banco descartável e navegador, sem dados operacionais."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from offline.browser_support import Chrome
from estoque.models import Produto
from estoque.tests_compra_pre_revisao import CompraPreRevisaoFixture


class UnidadeSelecaoDjangoTests(CompraPreRevisaoFixture, TestCase):
    def test_post_adulterado_recusado_e_rascunho_preservado(self):
        item = self.compra.itens.get()
        self.compra.itens.update(unidade='2,95')
        for invalida in ('2,95', ''):
            with self.subTest(unidade=invalida):
                resposta = self.client.post(self.salvar_url, self.payload(**{'unidade[]':[invalida]}))
                self.assertEqual(resposta.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.unidade, '2,95')
        self.assertEqual(item.quantidade, 9)
        self.assertEqual(item.preco_unitario, 10)
        resposta = self.client.post(self.salvar_url, self.payload(**{'unidade[]':['UN']}))
        self.assertEqual(resposta.status_code, 200)
        item = self.compra.itens.get()
        self.assertEqual(item.unidade, 'UN')
        self.assertEqual(item.quantidade, 9)
        self.assertEqual(item.preco_unitario, 10)
        self.assert_sem_efeitos()

    def test_codigo_numerico_cadastrado_aceito(self):
        self.produto.unidade_compra = 'CX12'
        self.produto.unidade_venda_1 = 'CX12'
        self.produto.save()
        resposta = self.client.post(self.salvar_url, self.payload(**{'unidade[]':['CX12']}))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.compra.itens.get().unidade, 'CX12')
        self.assert_sem_efeitos()


CHROME = os.environ.get('COMPRA_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class UnidadeSelecaoBrowserTests(CompraPreRevisaoFixture, StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        self.outro = Produto.objects.create(nome='Produto caixa teste', quantidade=0,
            preco_compra=12, preco_vista=20, preco_prazo=21, unidade_compra='CX12',
            unidade_venda_1='CX12', unidade_venda_2='UN', vende_fracionado=True, fator_conversao=12)

    def navegador(self):
        perfil = TemporaryDirectory(prefix='compra-unidade-select-', ignore_cleanup_errors=True)
        self.addCleanup(perfil.cleanup)
        browser = Chrome(CHROME, perfil.name).start()
        self.addCleanup(browser.stop)
        t = browser.tab()
        t.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        return t

    def abrir(self, t, width):
        t.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 820))
        t.evaluate('window.documentoAnteriorSelect=true;true')
        t.call('Page.navigate', {'url':self.live_server_url+self.editar_url})
        t.wait("!window.documentoAnteriorSelect && document.readyState==='complete' && !!document.querySelector('#tabelaItensCompra select[name=\"unidade[]\"]')")

    def test_select_recusa_digitacao_corrige_legado_sem_mudar_valores(self):
        self.compra.itens.update(unidade='2,95')
        t = self.navegador()
        for width in (1280, 390):
            self.abrir(t, width)
            t.evaluate("window.linhaTeste=document.querySelector('#tabelaItensCompra tbody .linha-item');window.unidadeTeste=linhaTeste.querySelector('[name=\"unidade[]\"]');true")
            self.assertEqual(t.evaluate('unidadeTeste.tagName'), 'SELECT')
            if width < 820:
                t.evaluate("linhaTeste.querySelector('.btn-editar-item-mobile').click();true")
            self.assertGreater(t.evaluate('unidadeTeste.getBoundingClientRect().width'), 0)
            self.assertNotEqual(t.evaluate('getComputedStyle(unidadeTeste).pointerEvents'), 'none')

            self.assertEqual(t.evaluate('unidadeTeste.value'), '')
            self.assertNotIn('2,95', t.evaluate('[...unidadeTeste.options].map(o=>o.value)'))
            self.assertEqual(t.evaluate('[...unidadeTeste.options].filter(o=>!o.disabled).map(o=>o.value)'), ['', 'UN'])
            t.evaluate("window.valoresTeste=[linhaTeste.querySelector('[name=\"quantidade[]\"]').value,linhaTeste.querySelector('.campo-preco-unitario-compra').value];true")
            # Atribuição inválida também não cria opção selecionável.
            t.evaluate("unidadeTeste.value='2,95';unidadeTeste.dispatchEvent(new Event('change',{bubbles:true}));true")
            self.assertFalse(t.evaluate('unidadeTeste.checkValidity()'))
            t.evaluate("unidadeTeste.value='UN';unidadeTeste.dispatchEvent(new Event('change',{bubbles:true}));true")
            self.assertTrue(t.evaluate('unidadeTeste.checkValidity()'))
            self.assertTrue(t.evaluate("linhaTeste.querySelector('.aviso-unidade-compra').hidden"))
            self.assertTrue(t.evaluate("JSON.stringify(valoresTeste)===JSON.stringify([linhaTeste.querySelector('[name=\"quantidade[]\"]').value,linhaTeste.querySelector('.campo-preco-unitario-compra').value])"))
            # Digitação real no select nativo não insere texto nem cria opções.
            t.evaluate('unidadeTeste.focus();true')
            t.call('Input.insertText', {'text':'2,95'})
            self.assertEqual(t.evaluate('unidadeTeste.value'), 'UN')
            self.assertEqual(t.evaluate('[...unidadeTeste.options].filter(o=>!o.disabled).map(o=>o.value)'), ['', 'UN'])
            if width < 820:
                t.evaluate("linhaTeste.querySelector('.btn-cancelar-edicao-item-mobile').click();true")
                self.assertEqual(t.evaluate('unidadeTeste.value'), '')
                self.assertFalse(t.evaluate('unidadeTeste.checkValidity()'))
        self.assertEqual(self.compra.itens.get().unidade, '2,95')  # GET/edição visual não grava.
        self.assert_sem_efeitos()

    def test_linha_dinamica_e_mobile_somente_unidade_suportada(self):
        t = self.navegador()
        for width in (1280, 390):
            self.abrir(t, width)
            t.evaluate("window.novaTeste=window.compraCriarLinhaItem();novaTeste.dataset.teste='nova';novaTeste.querySelector('[name=\"produto_busca[]\"]').focus();true")
            t.call('Input.insertText', {'text':'Produto caixa teste'})
            t.wait("!!document.querySelector('[data-teste=\"nova\"] .produto-sugestao')")
            t.evaluate("document.querySelector('[data-teste=\"nova\"] .produto-sugestao').click();true")
            t.wait("novaTeste.querySelector('[name=\"produto_id[]\"]').value==="+json.dumps(str(self.outro.pk)))
            self.assertEqual(t.evaluate("novaTeste.querySelector('[name=\"unidade[]\"]').tagName"), 'SELECT')
            self.assertEqual(t.evaluate("[...novaTeste.querySelector('[name=\"unidade[]\"]').options].filter(o=>!o.disabled).map(o=>o.value)"), ['CX12'])
            self.assertEqual(t.evaluate("novaTeste.querySelector('[name=\"unidade[]\"]').value"), 'CX12')
            self.assertTrue(t.evaluate("novaTeste.querySelector('[name=\"unidade[]\"]').checkValidity()"))
        self.assertEqual(self.compra.itens.count(), 1)
        self.assert_sem_efeitos()
