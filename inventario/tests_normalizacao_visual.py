"""Presentation checks with fictitious objects; no database or endpoints."""
import copy
import unittest
from decimal import Decimal as D
from types import SimpleNamespace as N

from django.conf import settings

if not settings.configured:
    settings.configure(USE_I18N=False, USE_TZ=False, DATABASES={},
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', 'estoque', 'inventario'],
        ROOT_URLCONF='inventario.tests_tabela_visual', SECRET_KEY='visual-sem-banco')
    import django
    django.setup()

from .tests_tabela_visual import TabelaVisualTests
from .conversao import converter
from .conversao_fisica import representacao_fisica, exigir_precisao_ajuste
from .apresentacao import comparacao_fisica
from .painel import resumo_painel


class NormalizacaoVisualTests(TabelaVisualTests):
    def preparar(self, esperado='3.501', principal='3', avulsos='6', base='DZ', fator='12'):
        cadastro = dict(nome='Produto fictício', base=base, secundaria='UN', fator=fator,
            fracionado=True, continua=False, secundaria_continua=False)
        p, a, contado, projecao = converter(cadastro, principal, avulsos)
        item = N(pk=1, cadastro=cadastro, custo_base=D('40'), produto=N(
            quantidade=D(esperado), unidade_venda_1=base, unidade_compra=base))
        rodada = N(pk=2, numero=1, item=item, embalagens=p, avulsos=a, contado=contado,
            contado_base=projecao, esperado=D(esperado), ok_em=True, encerrada_em=None)
        fisica = comparacao_fisica(representacao_fisica(rodada))
        linha = dict(item=item, rodada=rodada, diferenca=projecao-D(esperado),
            situacao='Correto', comparacao_fisica=fisica, atual=D(esperado),
            resultante=projecao, unidade=base, atualizacao=dict(estado='Requer prévia'))
        original = copy.deepcopy(vars(rodada))
        resumo_painel([linha])
        exigir_precisao_ajuste(rodada)
        self.assertEqual(vars(rodada), original)
        return linha

    def test_normalizacao_contagem_residuo_custo_e_previa(self):
        linha = self.preparar()
        self.assertEqual(linha['estimativa'], D('-.04'))
        self.assertEqual(linha['diferenca'], D('-.001'))
        self.assertEqual(linha['resultado_label'], 'Ajuste de precisão')
        self.assertIsNone(linha['percentual_quantidade'])
        _, texto = self.renderizar(linha)
        for esperado in ['Ajuste de precisão', '42 UN no total', '-0.001 DZ',
                         'Não há perda física comprovada', 'Ajuste pendente']:
            self.assertIn(esperado, texto)
        self.assertNotIn('R$ -0.04', texto)
        from django.template import Context
        html = self.engine.get_template('inventario/previa_produtos_ajuste.html').render(
            Context({'atualizacao': {'aptas': [linha]}}))
        for esperado in ['Normalização de precisão', '3.501 DZ', '3.500 DZ', '-0.001 DZ', '42 UN']:
            self.assertIn(esperado, html)
        self.assertEqual(linha['rodada'].esperado, D('3.501'))
        self.assertEqual(linha['rodada'].contado_base, D('3.500'))

    def test_igualdade_falta_sobra_e_embalagens_discretas(self):
        for base in ['DZ', 'PCT', 'FD']:
            for esperado, avulso, resultado in [('3.500', '6', 'correto'),
                    ('3.500', '5', 'falta'), ('3.500', '7', 'sobra')]:
                with self.subTest(base=base, avulso=avulso):
                    linha = self.preparar(esperado=esperado, avulsos=avulso, base=base)
                    self.assertFalse(linha['comparacao_fisica']['normalizacao'])
                    self.assertEqual(linha['resultado'], resultado)
                    _, texto = self.renderizar(linha)
                    self.assertNotIn('Não há perda física comprovada', texto)
                    self.assertNotIn('Ajuste de precisão', texto)
            self.assertEqual(self.preparar(base=base)['resultado_label'], 'Ajuste de precisão')

    def test_kg_continuo_preserva_fracao_e_estimativa(self):
        cadastro = dict(base='KG', secundaria='', fator='1', fracionado=False,
            continua=True, secundaria_continua=False)
        _, _, contado, base = converter(cadastro, '3.500', None)
        item = N(cadastro=cadastro, _relacao_fisica=None, custo_base=D('40'), produto=N(
            quantidade=D('3.501'), unidade_venda_1='KG', unidade_compra='KG'))
        rodada = N(item=item, esperado=D('3.501'), contado_base=base, contado=contado,
            embalagens=D('3.500'), avulsos=None, ok_em=True, encerrada_em=None)
        self.assertEqual(representacao_fisica(rodada), {})
        exigir_precisao_ajuste(rodada)
        linha = dict(item=item, rodada=rodada, diferenca=base-rodada.esperado, situacao='Falta')
        resumo_painel([linha])
        self.assertEqual(linha['resultado'], 'falta')
        self.assertEqual(linha['estimativa'], D('-.04'))
        self.assertEqual(base, D('3.500'))


if __name__ == '__main__':
    unittest.main()
