"""Diagnóstico dirigido; somente produtos sintéticos em PostgreSQL descartável."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse
from estoque.forms import ProdutoForm
from estoque.models import Produto, Categoria, Unidade, MembroGrupoProduto
from estoque.grupos_produtos import criar_grupo
from estoque.services.precos_vinculados import diagnosticar_grupo, regularizar_grupo
from estoque.tests_frisco_vinculo import CHROME
from offline.browser_support import Chrome


class LegadoFixture:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('diagnostico', '', 'teste')
        self.client.force_login(self.user)
        Categoria.objects.get_or_create(nome='Bebidas')
        for sigla in ('CX', 'UN'):
            Unidade.objects.get_or_create(sigla=sigla, defaults={'nome': sigla})
        self.produtos = [Produto.objects.create(nome='Frisco sintético '+sabor,
            categoria='Bebidas', unidade_compra='CX', unidade_venda_1='CX',
            vende_fracionado=False, unidade_venda_2='', fator_conversao=0,
            quantidade=4, preco_compra=Decimal('11.70'), preco_vista=Decimal('15.80'),
            preco_prazo=16, preco_vista_fracionado=1, preco_prazo_fracionado=Decimal('1.10'))
            for sabor in ('Laranja', 'Maracujá', 'Morango')]
        self.grupo = criar_grupo([p.pk for p in self.produtos], 'Frisco sintético 18/18G')
        regularizar_grupo(self.grupo.pk, diagnosticar_grupo(self.produtos)['versao_observada'],
            confirmar=True, operador=self.user)

    def url(self, p):
        return reverse('estoque:produto_editar', args=[p.pk])

    def estado(self):
        return list(Produto.objects.order_by('pk').values())

    def payload(self, p):
        form = self.client.get(self.url(p)).context['form']
        dados = {k:v for k,v in form.initial.items() if v is not None}
        dados.pop('fornecedores', None)
        return dados


class DivergenciaTests(LegadoFixture, TestCase):
    def test_resposta_recusada_mostra_proposta_mas_observa_banco(self):
        p = self.produtos[0]
        antes = self.estado()
        dados = self.payload(p)
        dados.update(vende_fracionado='True', unidade_venda_2='UN', fator_conversao='18',
            preco_compra_fracionado='.65', preco_vista_fracionado='1.20')
        resposta = self.client.post(self.url(p), dados)
        form = resposta.context['form']
        self.assertEqual(resposta.status_code, 200)
        self.assertIn('Configuração alterada', str(form.errors))
        self.assertFalse(form.apresentacao_observada['vende_fracionado'])
        self.assertEqual(form.apresentacao_observada['unidade_venda_2'], '')
        self.assertEqual(Decimal(form.apresentacao_observada['fator_conversao']), 0)
        self.assertEqual(form['vende_fracionado'].value(), 'True')
        self.assertEqual(form['unidade_venda_2'].value(), 'UN')
        self.assertEqual(Decimal(form['fator_conversao'].value()), 18)
        self.assertEqual(self.estado(), antes)
        print('POST recusado: observado Não/vazio/0; campos Sim/UN/18; banco intacto')

    def test_apresentacao_sem_reajuste_tambem_bloqueada_nos_tres(self):
        antes = self.estado()
        vinculos = list(MembroGrupoProduto.objects.values_list('produto_id', 'grupo_id'))
        for p in self.produtos:
            dados = self.payload(p)
            dados.update(vende_fracionado='True', unidade_venda_2='UN', fator_conversao='18',
                preco_compra_fracionado='.65')
            resposta = self.client.post(self.url(p), dados)
            self.assertEqual(resposta.status_code, 200)
            self.assertIn('Unidade fracionada', str(resposta.context['form'].errors))
            self.assertIn('grupo ativo', str(resposta.context['form'].errors))
            self.assertEqual(self.estado(), antes)
        self.assertEqual(list(MembroGrupoProduto.objects.values_list('produto_id', 'grupo_id')), vinculos)
        print('Laranja/Maracujá/Morango: proteção estrutural impede regularização individual sem reajuste')


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class DivergenciaBrowserTests(LegadoFixture, StaticLiveServerTestCase):
    def test_get_nao_deriva_apresentacao_e_reproduz_aviso(self):
        antes = self.estado()
        with TemporaryDirectory(prefix='frisco-divergencia-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(CHROME, perfil).start()
            try:
                t = browser.tab()
                t.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
                t.call('Page.navigate', {'url':self.live_server_url+self.url(self.produtos[0])})
                t.wait("document.readyState==='complete' && !!document.querySelector('#fator_visual')")
                dados = t.evaluate("({observada:JSON.parse(document.querySelector('#produto-apresentacao-observada').textContent),fracionado:document.querySelector('#vende_fracionado_visual').value,unidade:document.querySelector('#unidade_fracionada_visual').value,fator:document.querySelector('#fator_visual').value})")
                self.assertEqual((dados['fracionado'], dados['unidade'], Decimal(dados['fator'].replace(',', '.'))), ('False', '', 0))
                t.evaluate("document.querySelector('#vende_fracionado_visual').value='True';document.querySelector('#vende_fracionado_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#unidade_fracionada_visual').value='UN';document.querySelector('#unidade_fracionada_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#fator_visual').value='18';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[name=preco_vista_fracionado]').value='1.20';document.querySelector('#form-produto').requestSubmit();true")
                # The old blocked state now opens the coordinated preview;
                # observation/proposed values remain distinct until confirmation.
                t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                aviso = t.evaluate("document.querySelector('#confirmarApresentacao').textContent")
                self.assertIn('Venda fracionada: Não → Sim', aviso)
                self.assertIn('Fator de conversão: 0.00 → 18', aviso)
                self.assertIn('Unidade fracionada: — → UN', aviso)
                self.assertIn('Nenhum dado foi salvo ainda', aviso)
                self.assertEqual(self.estado(), antes)
                print('GET '+json.dumps(dados, ensure_ascii=False)+'; AVISO '+aviso)
            finally:
                browser.stop()
