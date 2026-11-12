"""Presentation only, fictitious contexts and no database configuration."""
from decimal import Decimal as D
import unittest

from .tests_tabela_visual import TabelaVisualTests


class SituacaoApresentacaoTests(unittest.TestCase):
    setUpClass = classmethod(TabelaVisualTests.setUpClass.__func__)
    linha = TabelaVisualTests.linha
    def renderizar(self, linha):
        from types import SimpleNamespace
        from .confirmacao_estoque import classificar
        linha['rodada'] = SimpleNamespace(**linha['rodada'], encerrada_em=None)
        classificar([linha])
        return TabelaVisualTests.renderizar(self, linha)
    def caso(self, diferenca, estado='Requer prévia', marco=None):
        linha = self.linha(estado=estado)
        linha.update(marco=marco, revisar=False)
        linha['rodada']['ok_em'] = True
        linha['comparacao_atual'] = dict(diferenca=diferenca,
            contado=D('1') if diferenca is not None else None,
            contado_texto='1 MIL' if diferenca is not None else 'Não comparável',
            diferenca_texto='0 MIL' if diferenca == 0 else 'Não comparável',
            destaque='neutro', limite='' if diferenca is not None else 'Documento não comprovado')
        return linha

    def test_zero_marco_pendente_inclusive_bloqueio_documental(self):
        for estado in ('Requer prévia', 'Bloqueado', 'Sem diferença'):
            html, texto = self.renderizar(self.caso(D('0'), estado))
            self.assertIn('Aguardando confirmação', texto)
            self.assertNotIn('Ajuste pendente', texto)
            self.assertNotIn('estado-bloqueado', html)

    def test_zero_marco_aplicado(self):
        _, texto = self.renderizar(self.caso(D('0'), 'Aplicado', {'diferenca': D('0')}))
        self.assertIn('Confirmado', texto)
        self.assertNotIn('pendente', texto)

    def test_correcao_pendente(self):
        for diferenca in (D('-1'), D('1')):
            _, texto = self.renderizar(self.caso(diferenca, 'Bloqueado'))
            self.assertIn('Aguardando confirmação', texto)
            self.assertNotIn('Contagem OK', texto)

    def test_correcao_aplicada_preservada_mesmo_com_diferenca_atual_zero(self):
        _, texto = self.renderizar(self.caso(D('0'), 'Aplicado', {'diferenca': D('-1')}))
        self.assertIn('Confirmado', texto)
        self.assertNotIn('Marco pendente', texto)

    def test_projecao_desconhecida_preserva_contagem_original_e_unidade(self):
        linha = self.caso(None, 'Bloqueado')
        linha['contado_texto'] = '3 PCT + 2 UN (20 UN)'
        linha['comparacao_atual']['contado_texto'] = '3,333 PCT'
        html, texto = self.renderizar(linha)
        contado = html.split('data-label="Contado"', 1)[1].split('</td>', 1)[0]
        self.assertIn('3,333 PCT', contado)
        self.assertIn('20 UN', contado)
        self.assertIn('Contagem original no OK', contado)
        self.assertNotIn('Não comparável', contado)
        self.assertNotIn('estado-bloqueado', html)
        self.assertNotIn('Correção pendente', texto)
        self.assertEqual(html.count('<td '), 6)
        self.assertIn('popover role="dialog"', html)
        self.assertNotIn(' open', html)
