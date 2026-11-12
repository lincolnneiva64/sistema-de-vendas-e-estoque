"""Physical display and independent states, with synthetic objects and no DB."""
import unittest
from decimal import Decimal as D
from types import SimpleNamespace as Obj
from django.template import Context

from .apresentacao import comparacao_fisica
from .painel import resumo_painel
from .tests_tabela_visual import TabelaVisualTests


class ClassificacaoVisualTests(unittest.TestCase):
    setUpClass = classmethod(TabelaVisualTests.setUpClass.__func__)
    renderizar = TabelaVisualTests.renderizar

    def preparar(self, fisica='0', decimal='0', normalizacao=False, estado='Aplicado', revisar=False):
        linha = TabelaVisualTests.linha(self, estado=estado)
        linha['item'] = Obj(pk=1, cadastro=dict(base='PCT', continua=False, fracionado=False,
            nome='Produto fictício'), custo_base=D('10'),
            produto=Obj(unidade_venda_1='PCT', quantidade=D('7.083')))
        linha['rodada'] = Obj(pk=2, numero=1, ok_em=True, encerrada_em=None, esperado=D('7.084'))
        linha.update(diferenca=D(decimal), situacao='Revisão necessária' if revisar else 'Falta',
                     decisao=None, bloqueio='Histórico sem conciliação' if revisar else '')
        linha['comparacao_fisica'] = comparacao_fisica(dict(fator='12', base='PCT',
            total_unidades='85', esperado_unidades='85', esperado_canonico=not normalizacao,
            diferenca_fisica=fisica, natureza='normalizacao_decimal' if normalizacao else 'diferenca_fisica'))
        return linha

    def test_igualdade_com_residuo_preserva_aplicacao_e_dados_tecnicos(self):
        linha = self.preparar(fisica=None, decimal='-.001', normalizacao=True)
        evidencia = linha['comparacao_fisica']['dados'].copy()
        resumo = resumo_painel([linha])
        self.assertEqual((linha['resultado'], linha['resultado_label'], linha['diferenca_texto']),
                         ('correto', 'Ajuste de precisão', 'Ajuste de precisão'))
        self.assertEqual(resumo['corretos'], 1)
        self.assertEqual(linha['diferenca'], D('-.001'))
        self.assertEqual(linha['estimativa'], D('-.01'))
        self.assertEqual(linha['comparacao_fisica']['dados'], evidencia)
        _, texto = self.renderizar(linha)
        self.assertIn('Ajuste aplicado', texto)
        self.assertIn('Ajuste de precisão', texto)
        self.assertNotIn('Diferença de arredondamento', texto)

    def test_42_un_iguais_e_falta_sobra_fisicas(self):
        for fisica, decimal, estado, texto in [('0', '0', 'correto', 'Sem diferença'),
                ('-1', '-.083', 'falta', 'Falta 1 UN'), ('2', '.167', 'sobra', 'Sobra 2 UN')]:
            linha = self.preparar(fisica, decimal)
            if fisica == '0':
                linha['comparacao_fisica'] = comparacao_fisica(dict(fator='6', base='PCT',
                    total_unidades='42', esperado_unidades='42', esperado_canonico=True,
                    diferenca_fisica='0', natureza='diferenca_fisica'))
            resumo_painel([linha])
            self.assertEqual(linha['resultado'], estado)
            self.assertEqual(linha['diferenca_texto'], texto)

    def test_revisao_e_atualizacao_independentes_da_classificacao(self):
        for estado, rotulo in [('Aplicado', 'Ajuste aplicado'), ('Sem diferença', 'Sem diferença no OK · revisão pendente'),
                               ('Bloqueado', 'Ajuste bloqueado'), ('Requer prévia', 'Ajuste pendente')]:
            linha = self.preparar(estado=estado, revisar=True)
            resumo_painel([linha])
            _, texto = self.renderizar(linha)
            self.assertIn('Correto', texto)
            self.assertIn('Revisão necessária', texto)
            self.assertIn(rotulo, texto)
            self.assertTrue(linha['revisar'])
            self.assertEqual(linha['atualizacao']['estado'], estado)

    def test_referencia_incerta_nao_vira_correto(self):
        linha = self.preparar(fisica=None, decimal='-.002')
        resumo_painel([linha])
        self.assertEqual(linha['resultado'], 'pendente')

    def test_zero_avulsos_sem_repetir_parcela_preserva_total_e_unidade(self):
        for base in ('PCT', 'DZ', 'CX', 'UN'):
            html = self.engine.get_template('inventario/quantidade_tabela.html').render(
                Context(dict(texto=f'4 {base} + 0 UN = 24 UN')))
            self.assertIn(f'4 {base}', html)
            self.assertIn('24 UN no total', html)
            self.assertNotIn('+ 0 UN', html)
