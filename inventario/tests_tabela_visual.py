"""Template-only checks. No Django app, database, users or browser required."""
import copy
import unittest
from decimal import Decimal
from pathlib import Path
from html.parser import HTMLParser

from django.conf import settings
from django.template import Engine, Context
from django.urls import path, include


def destino(request, *args):
    raise AssertionError('Estes testes não executam endpoints.')


urlpatterns = [path('', include(([
    path('historico/<int:a>/<int:b>/', destino, name='historico_produto'),
    path('contagem/<int:a>/<int:b>/', destino, name='editar_contagem_admin'),
    path('relacao/<int:a>/<int:b>/', destino, name='confirmar_conversao_fisica'),
], 'inventario'), namespace='inventario'))]


class TextoPrincipal(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ocultos = 0
        self.pilha = []
        self.texto = []

    def handle_starttag(self, tag, attrs):
        escondido = ('popover', None) in attrs or tag == 'dialog'
        if tag not in {'input', 'br', 'hr', 'meta', 'link'}:
            self.pilha.append((tag, escondido))
        self.ocultos += escondido

    def handle_endtag(self, tag):
        if self.pilha and self.pilha[-1][0] == tag:
            _, oculto = self.pilha.pop()
            self.ocultos -= oculto

    def handle_data(self, texto):
        if not self.ocultos:
            self.texto.append(texto)


class TabelaVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not settings.configured:
            settings.configure(USE_I18N=False, USE_TZ=False, DATABASES={},
                               ROOT_URLCONF=__name__, INSTALLED_APPS=[], SECRET_KEY='teste-sem-banco')
            import django
            django.setup()
        cls.root = Path(__file__).resolve().parents[1]
        cls.engine = Engine(dirs=[cls.root/'inventario/templates'], libraries={
            'conferencia_visual': 'inventario.templatetags.conferencia_visual',
            'l10n': 'django.templatetags.l10n',
        })

    def linha(self, base='PCT', nome='Fanta 6/2L', estado='Requer prévia'):
        return dict(item=dict(pk=1, cadastro=dict(nome=nome, base=base, continua=False)),
            rodada=dict(pk=2, numero=1), comparacao_fisica=dict(
                esperado=f'3 {base} + 3 UN = 21 UN', encontrado=f'3 {base} + 2 UN = 20 UN',
                dados=dict(esperado_base='3.500', projecao='3.333', diferenca_decimal='-.167',
                    base=base, residuo_unidades='.002', residuo_base_numerador='.002',
                    residuo_base_denominador='6', esperado_canonico=True)),
            atualizacao=dict(estado=estado, preliminar=True), resultado='falta',
            resultado_label='Falta', diferenca=Decimal('-.167'), diferenca_texto='Falta 1 UN',
            percentual_quantidade=Decimal('-4.76'), estoque_atual_texto=f'3,5 {base}',
            edicao_admin=True, esperado_texto=f'3,5 {base}', contado_texto=f'3,333 {base}')

    def renderizar(self, linha):
        original = copy.deepcopy(linha)
        html = self.engine.get_template('inventario/linha_tabela.html').render(Context({
            'linha': linha, 'inventario': {'pk': 3}, 'csrf_token': 'ficticio-sem-servidor'}))
        self.assertEqual(linha, original)
        principal = TextoPrincipal()
        principal.feed(html)
        return html, ' '.join(principal.texto)

    def test_fanta_coca_cx_dz_preservam_quantidades_e_acoes(self):
        for nome, base in [('Fanta 6/2L', 'PCT'), ('Coca-Cola 6/1L', 'PCT'),
                           ('Caixa fictícia', 'CX'), ('Dúzia fictícia', 'DZ')]:
            with self.subTest(nome=nome):
                html, texto = self.renderizar(self.linha(base, nome))
                self.assertIn(f'3 {base}', texto)
                self.assertIn('20 UN no total', texto)
                self.assertIn('Falta 1 UN', texto)
                self.assertIn('Editar contagem', texto)
                self.assertIn('Ver detalhes', texto)
                self.assertIn('Ajuste pendente', texto)
                self.assertNotIn('−1,002 UN', texto)
                self.assertIn('class="parcela-quantidade"', html)

    def test_amstel_sem_jargao_na_linha_e_evidencias_preservadas(self):
        linha = self.linha(nome='Amstel 12/350ml')
        linha['diferenca_texto'] = 'Equivalência aproximada — resíduo decimal; sem falta física comprovada'
        linha['resultado_label'] = 'Resíduo decimal'
        linha['comparacao_fisica'].update(esperado='≈ 7 PCT + 1 UN = 85 UN', encontrado='7 PCT + 1 UN = 85 UN')
        linha['comparacao_fisica']['dados'].update(esperado_base='7.084', projecao='7.083',
            diferenca_decimal='-.001', esperado_canonico=False, residuo_unidades='.004')
        html, texto = self.renderizar(linha)
        self.assertIn('Diferença de arredondamento', texto)
        self.assertIn('85 UN no total', texto)
        for termo in ['Equivalência', 'Projeção', 'Resíduo', 'aplicação não comprovada']:
            self.assertNotIn(termo, texto)
        for termo in ['7.084', '7.083', '.004', 'referência esperada', 'não comprova falta']:
            self.assertIn(termo, html)

    def test_estados_sem_confundir_pendencia_e_bloqueio(self):
        for estado, rotulo in [('Aplicado', 'Ajuste aplicado'), ('Bloqueado', 'Ajuste bloqueado'),
                ('Sem ajuste', 'Sem ajuste necessário'), ('Sem diferença', 'Sem ajuste necessário'),
                ('Pendente de revisão', 'Ajuste pendente'), ('Requer prévia', 'Ajuste pendente')]:
            linha = self.linha(estado=estado)
            linha['atualizacao'].update(bloqueio='Motivo preservado', saldo_aplicado=Decimal('3.333'), unidade_aplicada='PCT')
            html, texto = self.renderizar(linha)
            self.assertIn(rotulo, texto)
            self.assertNotIn('Motivo preservado', texto)
            self.assertIn('Motivo preservado', html)
            if estado == 'Aplicado':
                self.assertIn('Saldo registrado após o ajuste', html)

    def test_sem_diferenca_e_sobra_usam_rotulos_simples(self):
        for original, simples in [('Equivalência física', 'Sem diferença'), ('Sobra 1 UN', 'Sobra 1 UN')]:
            linha = self.linha()
            linha['diferenca_texto'] = original
            _, texto = self.renderizar(linha)
            self.assertIn(simples, texto)

    def test_coca_separa_composicao_e_total(self):
        linha = self.linha(nome='Coca-Cola 6/1L')
        linha['comparacao_fisica'].update(esperado='4 PCT + 0 UN = 24 UN',
                                        encontrado='3 PCT + 5 UN = 23 UN')
        html, texto = self.renderizar(linha)
        self.assertNotIn('24 UN no total', texto)
        self.assertIn('23 UN no total', texto)
        self.assertNotIn(' = ', texto)
        self.assertIn('Falta 1 UN', texto)

    def test_pacotes_sem_comparacao_mantem_apresentacao_simples(self):
        linha = self.linha(base='DZ', estado='Sem diferença')
        linha.pop('comparacao_fisica')
        linha.update(esperado_texto='3 DZ', contado_texto='3 DZ', diferenca_texto='0 DZ')
        html, texto = self.renderizar(linha)
        self.assertIn('3 DZ', texto)
        self.assertNotIn('no total', texto)
        self.assertIn('Sem ajuste necessário', texto)

    def test_popover_acessivel_com_abertura_e_fechamento_nativos(self):
        html, _ = self.renderizar(self.linha())
        self.assertIn('popovertarget="detalhes-conferencia-1"', html)
        self.assertIn('popovertargetaction="hide"', html)
        self.assertIn('popover role="dialog"', html)
        self.assertIn('aria-labelledby="titulo-detalhes-1"', html)
        self.assertIn('aria-label="Ver detalhes de Fanta 6/2L"', html)
        css = (self.root/'static/inventario/revisao.css').read_text(encoding='utf-8')
        self.assertIn('max-height:min(75vh,650px)', css)
        self.assertIn('@media(max-width:1100px)', css)
        self.assertIn('white-space:nowrap', css)

    def test_colunas_alinhadas_sem_esperado_e_sem_banco(self):
        class Celulas(HTMLParser):
            def __init__(self):
                super().__init__()
                self.celulas = []
            def handle_starttag(self, tag, attrs):
                if tag in ('th', 'td'):
                    self.celulas.append((tag, dict(attrs)))

        for fisica in (True, False):
            linha = self.linha()
            if not fisica:
                linha.pop('comparacao_fisica')
            html, _ = self.renderizar(linha)
            parser = Celulas()
            parser.feed(html)
            self.assertEqual(len(parser.celulas), 7)
            self.assertEqual([a.get('data-label') for t, a in parser.celulas[1:]], [
                'Estoque atual', 'Contado', 'Diferença / %', 'Estimativa ao custo',
                'Situação', 'Atualização do estoque'])
            self.assertNotIn('data-label="Esperado no OK"', html)

        # Render the actual table fragment, including its empty state, without
        # loading the page's unrelated forms or executing any endpoint.
        fonte = (self.root/'inventario/templates/inventario/detalhe.html').read_text(encoding='utf-8')
        inicio = fonte.index('<div class="scroll tabela-resumo">')
        fim = fonte.index('</tbody></table></div>', inicio) + len('</tbody></table></div>')
        template = self.engine.from_string(fonte[inicio:fim])
        for linhas in ([], [self.linha()]):
            html = template.render(Context({'linhas': linhas, 'inventario': {'pk': 3}}))
            cabecalho = html.split('<thead>')[1].split('</thead>')[0]
            self.assertEqual(cabecalho.count('scope="col"'), 7)
            self.assertNotIn('Esperado no OK', cabecalho)
            if not linhas:
                self.assertIn('colspan="7"', html)

    def test_formatacao_escapa_html(self):
        from .templatetags.conferencia_visual import parcelas_quantidade
        self.assertNotIn('<script>', str(parcelas_quantidade('<script>alert(1)</script>')))


    def linha_peso(self, avulsos='14', fator_atual='30'):
        from types import SimpleNamespace as Objeto
        from .painel import resumo_painel
        cadastro = dict(nome='Açúcar Itamarati 30/1Kg', base='FD', secundaria='KG',
                        fator='30', fracionado=True, continua=False, secundaria_continua=True)
        produto = Objeto(nome=cadastro['nome'], quantidade=Decimal('1.335'),
            unidade_compra='FD', unidade_venda_1='FD', unidade_venda_2='KG',
            fator_conversao=Decimal(fator_atual))
        item = Objeto(pk=1, cadastro=cadastro, produto=produto, custo_base=Decimal(10))
        rodada = Objeto(pk=2, numero=1, ok_em=True, encerrada_em=None,
            esperado=Decimal('1.335'), contado_base=Decimal('1.467'), avulsos=Decimal(avulsos))
        linha = self.linha(base='FD', nome=cadastro['nome'], estado='Bloqueado')
        linha.update(item=item, rodada=rodada, comparacao_fisica={},
            situacao='Revisão necessária', diferenca=Decimal('.132'),
            contado_texto='1,467 FD', esperado_texto='1,335 FD')
        resumo_painel([linha])
        return linha

    def test_equivalencias_kg_inteiros_so_na_apresentacao(self):
        linha = self.linha_peso()
        html, texto = self.renderizar(linha)
        for valor in ('1,335 FD', '1,467 FD', '≈ 40 KG', '≈ 44 KG', 'Editar contagem'):
            self.assertIn(valor, texto)
        self.assertIn('Equivalência exata: 40,05 KG', html)
        self.assertIn('Equivalência exata: 44,01 KG', html)
        self.assertEqual(linha['item'].produto.quantidade, Decimal('1.335'))
        self.assertEqual(linha['rodada'].contado_base, Decimal('1.467'))
        self.assertEqual(linha['diferenca'], Decimal('.132'))
        self.assertEqual(linha['estimativa'], Decimal('1.32'))
        self.assertEqual(linha['item'].cadastro['fator'], '30')
        self.assertEqual(linha['item'].produto.fator_conversao, Decimal(30))

    def test_avulsos_em_peso_fracionado_nao_sao_arredondados(self):
        linha = self.linha_peso(avulsos='0.250')
        _, texto = self.renderizar(linha)
        self.assertIn('40,05 KG', texto)
        self.assertIn('44,01 KG', texto)
        self.assertNotIn('≈ 40 KG', texto)
        self.assertNotIn('≈ 44 KG', texto)

    def test_composicao_fator_e_unidades_devem_corresponder(self):
        from .apresentacao import equivalencia_visual, quantidade
        cadastro = dict(nome='Produto 30/1Kg', base='FD', secundaria='KG', fator='30')
        self.assertEqual(equivalencia_visual(Decimal('1.350'), cadastro)['texto'], '≈ 41 KG')
        self.assertEqual(equivalencia_visual(Decimal('1.335'), dict(cadastro, fator='10'))['texto'], '13,35 KG')
        self.assertEqual(equivalencia_visual(Decimal('0.1'), dict(cadastro, nome='Produto 30/1,5Kg', fator='45'))['texto'], '4,5 KG')
        self.assertEqual(equivalencia_visual(Decimal('0.2'), dict(cadastro, nome='Produto 30/0,250Kg', fator='7.5'))['texto'], '1,5 KG')
        self.assertEqual(equivalencia_visual(Decimal('1.335'), dict(cadastro, nome='Produto a granel'))['texto'], '40,05 KG')
        self.assertEqual(quantidade(Decimal('1.5'), dict(base='KG', continua=True)), '1,500 KG')
        self.assertEqual(quantidade(Decimal('0.250'), dict(base='KG', continua=True)), '0,250 KG')

    def test_conversao_invalida_ou_unidade_igual_nao_cria_equivalencia(self):
        from .apresentacao import equivalencia_visual
        cadastro = dict(nome='Produto 30/1Kg', base='FD', secundaria='KG', fator='30')
        for fator in ('0', '-1', 'NaN', 'Infinity', 'incorreto', None):
            self.assertEqual(equivalencia_visual(Decimal(1), dict(cadastro, fator=fator)), {})
        self.assertEqual(equivalencia_visual(Decimal(1), dict(cadastro, base='KG')), {})
        self.assertEqual(equivalencia_visual(None, cadastro), {})

    def test_estoque_atual_e_contado_usam_seus_proprios_fatores(self):
        linha = self.linha_peso(fator_atual='10')
        _, texto = self.renderizar(linha)
        self.assertIn('13,35 KG', texto)
        self.assertIn('≈ 44 KG', texto)
        self.assertNotIn('≈ 40 KG', texto)

    def test_composicao_un_existente_nao_duplica_equivalencia_contada(self):
        from .painel import resumo_painel
        linha = self.linha_peso()
        linha['comparacao_fisica'] = self.linha()['comparacao_fisica']
        # Pending state avoids changing classifications unrelated to this test.
        linha['rodada'].ok_em = None
        resumo_painel([linha])
        self.assertEqual(linha['contado_equivalencia'], {})

    def test_saldo_visivel_apenas_aplicado_com_valor_e_unidade(self):
        for estado in ('Aplicado', 'Bloqueado', 'Requer prévia', 'Pendente de revisão', 'Sem diferença'):
            linha = self.linha(estado=estado)
            linha['atualizacao'].update(saldo_aplicado=Decimal(0), unidade_aplicada='UN')
            _, texto = self.renderizar(linha)
            if estado == 'Aplicado':
                self.assertIn('Saldo aplicado: 0 UN', texto)
            else:
                self.assertNotIn('Saldo aplicado:', texto)
        for saldo, unidade in ((None, 'UN'), (Decimal(1), '')):
            linha = self.linha(estado='Aplicado')
            linha['atualizacao'].update(saldo_aplicado=saldo, unidade_aplicada=unidade)
            _, texto = self.renderizar(linha)
            self.assertNotIn('Saldo aplicado:', texto)


    def coluna_atualizacao(self, linha):
        import re
        html, _ = self.renderizar(linha)
        coluna = re.search(r'<td[^>]*class="situacao-atualizacao"[^>]*>(.*?)</td>', html, re.S).group(1)
        principal = TextoPrincipal()
        principal.feed(coluna)
        return coluna, ' '.join(principal.texto)

    def test_bloqueado_e_pendente_mostram_contagem_e_equivalencia(self):
        for estado in ('Bloqueado', 'Requer prévia', 'Pendente de revisão', 'Rejeitado'):
            linha = self.linha_peso()
            linha['atualizacao'].update(estado=estado, saldo_aplicado=Decimal(9),
                                       unidade_aplicada='FD', bloqueio='Motivo preservado')
            html, texto = self.coluna_atualizacao(linha)
            self.assertIn('1,467 FD', texto)
            self.assertNotIn('Contagem encontrada:', texto)
            self.assertIn('≈ 44 KG', texto)
            self.assertNotIn('Saldo aplicado:', texto)
            self.assertIn('Ver detalhes', texto)
            self.assertIn('Motivo preservado', html)
            self.assertIn('Ajuste bloqueado' if estado == 'Bloqueado' else 'Ajuste pendente', texto)

    def test_aplicado_usa_saldo_efetivo_e_nao_equivalencia_da_contagem(self):
        linha = self.linha_peso()
        linha['atualizacao'].update(estado='Aplicado', saldo_aplicado=Decimal('1.400'),
                                   unidade_aplicada='FD')
        _, texto = self.coluna_atualizacao(linha)
        self.assertRegex(texto, r'Saldo aplicado: 1[.,]400 FD')
        self.assertIn('42 KG', texto)
        self.assertNotIn('44 KG', texto)
        self.assertNotIn('Contagem encontrada:', texto)
        linha['atualizacao'].update(saldo_aplicado=Decimal(44), unidade_aplicada='KG')
        _, texto = self.coluna_atualizacao(linha)
        self.assertIn('Saldo aplicado: 44 KG', texto)
        self.assertEqual(texto.count('KG'), 1)

    def test_sem_contagem_nao_inventa_e_contagem_zero_e_preservada(self):
        for ausente in (None, '', '—'):
            linha = self.linha(estado='Bloqueado')
            linha['contado_texto'] = ausente
            _, texto = self.coluna_atualizacao(linha)
            self.assertNotIn('Contagem encontrada:', texto)
            self.assertNotIn('Saldo aplicado:', texto)
        linha = self.linha_peso()
        linha.update(contado_texto='0 FD', contado_equivalencia=dict(texto='0 KG', exato='0 KG'))
        _, texto = self.coluna_atualizacao(linha)
        self.assertIn('0 FD', texto)
        self.assertNotIn('Contagem encontrada:', texto)
        self.assertIn('0 KG', texto)

    def test_contagem_composta_preserva_total_e_nao_depende_de_nome_ou_id(self):
        for nome, pk in [('Nome livre', 123), ('Outro nome', 987)]:
            linha = self.linha(nome=nome, estado='Bloqueado')
            linha['item']['pk'] = pk
            html, texto = self.coluna_atualizacao(linha)
            self.assertIn('3 PCT + 2 UN', texto)
            self.assertNotIn('Contagem encontrada:', texto)
            self.assertEqual(texto.count('20 UN'), 1)
            self.assertIn(f'popovertarget="detalhes-conferencia-{pk}"', html)
            self.assertNotIn('Saldo aplicado:', texto)


    def test_todos_estados_compartilham_container_e_popover_fora_do_fluxo(self):
        import re
        for estado, classe in [('Aplicado', 'saldo-aplicado'), ('Bloqueado', 'contagem-encontrada'),
                               ('Requer prévia', 'contagem-encontrada')]:
            linha = self.linha_peso()
            linha['atualizacao'].update(estado=estado, saldo_aplicado=Decimal(2), unidade_aplicada='UN')
            html, _ = self.coluna_atualizacao(linha)
            conteudo = re.search(r'<div class="conteudo-atualizacao">(.*?)</div>', html, re.S).group(1)
            self.assertIn('estado-atualizacao', conteudo)
            self.assertIn(f'class="{classe}"', conteudo)
            self.assertIn('class="ver-detalhes-conferencia"', conteudo)
            self.assertNotIn('popover role="dialog"', conteudo)
            if estado != 'Aplicado':
                self.assertIn('class="quantidade-total"', conteudo)
            self.assertIn('popover role="dialog"', html)


if __name__ == '__main__':
    unittest.main()
