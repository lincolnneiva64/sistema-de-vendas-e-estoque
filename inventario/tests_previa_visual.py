"""Presentation only: synthetic contexts, no database or adjustment execution."""
import copy
import unittest
from decimal import Decimal as D
from types import SimpleNamespace as Obj

from django.template import Context
from django.test import override_settings
from django.urls import include, path

from .apresentacao import comparacao_fisica
from .tests_tabela_visual import TabelaVisualTests, destino

urlpatterns = [path('', include(([
    path('modal/<int:pk>/', destino, name='ajustes_modal'),
    path('detalhe/<int:pk>/', destino, name='detalhe'),
    path('lista/', destino, name='lista'),
], 'inventario'), namespace='inventario'))]


class PreviaVisualTests(unittest.TestCase):
    setUpClass = classmethod(TabelaVisualTests.setUpClass.__func__)

    def linha(self, base='PCT', diferenca='-0.167', fisico=True, natureza='diferenca_fisica'):
        cadastro = dict(nome='Produto fictício', base=base, continua=False, fracionado=False)
        linha = dict(rodada=Obj(item=Obj(cadastro=cadastro), contado_base=D('3.333')),
                     atual=D('3.5'), resultante=D('3.333'), unidade=base, diferenca=D(diferenca))
        if fisico:
            linha['comparacao_fisica'] = comparacao_fisica(dict(
                fator='6', base=base, total_unidades='20', esperado_unidades='21',
                esperado_canonico=True, diferenca_fisica='-1', natureza=natureza))
        return linha

    def renderizar(self, linha, completo=False):
        original = copy.deepcopy(linha)
        with override_settings(ROOT_URLCONF=__name__, LANGUAGE_CODE='pt-br'):
            html = self.engine.get_template('inventario/' + (
                'previa_ajustes_modal.html' if completo else 'previa_produtos_ajuste.html')).render(Context(dict(
                    atualizacao=dict(aptas=[linha]), inventario=dict(pk=25), previa_lote='token-ficticio',
                    revisao_historica='acesso-ficticio', csrf_token='csrf-ficticio',
                    ajustes_habilitados=True)))
        self.assertEqual(linha, original)
        return html

    def test_composicao_reutilizada_e_saldos_preservados(self):
        for base in ('PCT', 'DZ', 'CX'):
            html = self.renderizar(self.linha(base))
            for texto in ('Produto fictício', f'3,500 {base}', f'3,333 {base}',
                          'Contagem física:', f'3 {base}', '2 UN', '20 UN no total', 'Falta 1 UN'):
                self.assertIn(texto, html)
            for texto in ('Projeção decimal', 'Resíduo da projeção', 'Equivalência aproximada', ' = ', '· diferença'):
                self.assertNotIn(texto, html)

    def test_residuo_sem_falta_fisica_e_referencia_incerta_nao_inventa_diferenca(self):
        linha = self.linha(natureza='normalizacao_decimal')
        linha['comparacao_fisica']['diferenca'] = 'Equivalência aproximada — resíduo decimal; sem falta física comprovada'
        self.assertIn('Sem diferença física', self.renderizar(linha))
        linha['comparacao_fisica'].update(normalizacao=False,
            diferenca='Diferença física não comprovada — referência aproximada')
        linha['comparacao_fisica']['dados']['diferenca_fisica'] = None
        self.assertIn('Conferir diferença', self.renderizar(linha))

    def test_unidades_simples_falta_sobra_zero(self):
        for valor, esperado in (('-1', 'Falta 1 UN'), ('2', 'Sobra 2 UN'), ('0', 'Sem diferença física')):
            self.assertIn(esperado, self.renderizar(self.linha('UN', valor, fisico=False)))

    def test_confirmacao_mantem_saldos_tokens_e_botoes(self):
        html = self.renderizar(self.linha(), completo=True)
        for texto in ('3,500 PCT', '3,333 PCT', 'Falta 1 UN', 'token-ficticio',
                      'acesso-ficticio', 'csrf-ficticio', 'Confirmar atualização', 'Cancelar',
                      'name="confirmacao" value="sim"', 'movimentos posteriores ao OK'):
            self.assertIn(texto, html)

    def test_titulo_uma_identificacao_e_escape(self):
        template = self.engine.from_string('{% load conferencia_visual %}{{ titulo|titulo_conferencia:pk }}')
        for titulo in ('Coca Cola', 'Conferência nº 25 — Coca Cola'):
            self.assertEqual(template.render(Context(dict(titulo=titulo, pk=25))),
                             'Conferência nº 25 — Coca Cola')
        self.assertNotIn('<script>', template.render(Context(dict(titulo='<script>', pk=25))))
        with override_settings(ROOT_URLCONF=__name__):
            html = self.engine.get_template('inventario/ajustes_diretos.html').render(Context(dict(
                pode_aprovar=True, inventario=dict(pk=25, titulo='Conferência nº 25 — Coca Cola'))))
        self.assertIn('data-conferencia-titulo="Conferência nº 25 — Coca Cola"', html)

    def test_nome_produto_escapado(self):
        linha = self.linha()
        linha['rodada'].item.cadastro['nome'] = '<script>teste</script>'
        self.assertNotIn('<script>', self.renderizar(linha))
