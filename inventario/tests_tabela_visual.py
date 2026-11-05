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
        self.assertIn('24 UN no total', texto)
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

    def test_formatacao_escapa_html(self):
        from .templatetags.conferencia_visual import parcelas_quantidade
        self.assertNotIn('<script>', str(parcelas_quantidade('<script>alert(1)</script>')))


if __name__ == '__main__':
    unittest.main()
