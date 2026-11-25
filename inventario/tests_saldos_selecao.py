from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.template.loader import render_to_string

from .estoque_selecao import apresentar_estoque


def produto(**campos):
    dados = dict(quantidade=Decimal('1.5'), unidade_venda_1='CX', unidade_compra='CX',
                 unidade_venda_2='UN', fator_conversao=Decimal('12'))
    dados.update(campos)
    return SimpleNamespace(**dados)


class SaldosSelecaoTests(SimpleTestCase):
    def test_caixa_e_unidades_sem_consultas(self):
        self.assertEqual(apresentar_estoque(produto()),
                         {'principal': '1 CX + 6 UN', 'equivalencia': '18 UN'})

    def test_fardo_e_unidades(self):
        self.assertEqual(apresentar_estoque(produto(unidade_venda_1='FD', fator_conversao=24)),
                         {'principal': '1 FD + 12 UN', 'equivalencia': '36 UN'})

    def test_peso_em_destaque_sem_mudar_saldo(self):
        p = produto(quantidade=Decimal('1.601'), unidade_venda_1='FD',
                    unidade_venda_2='KG', fator_conversao=30, nome='Acucar 30/1Kg')
        self.assertEqual(apresentar_estoque(p),
                         {'principal': '48 kg', 'equivalencia': '1 FD + 18 kg'})
        self.assertEqual(p.quantidade, Decimal('1.601'))

    def test_base_kg_e_saldo_zero(self):
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('0'),
                         unidade_venda_1='KG', unidade_venda_2='')),
                         {'principal': '0,000 kg', 'equivalencia': ''})

    def test_arredonda_apresentacao_unidades_e_preserva_negativo(self):
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('2.167'))),
                         {'principal': '2 CX + 2 UN', 'equivalencia': '26 UN'})
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('-1.5')))['equivalencia'], '-18 UN')
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('-1.5')))['principal'], '-(1 CX + 6 UN)')

    def test_conversao_invalida_preserva_saldo_original(self):
        for fator in (None, 0, -1, Decimal('NaN')):
            with self.subTest(fator=fator):
                self.assertEqual(apresentar_estoque(produto(fator_conversao=fator)),
                                 {'principal': '1,5 CX', 'equivalencia': ''})

    def test_saldo_ausente_nao_vira_zero(self):
        self.assertEqual(apresentar_estoque(produto(quantidade=None)),
                         {'principal': 'Não informado', 'equivalencia': ''})

    def test_sem_conversao_nao_inventa_quilos(self):
        p = produto(nome='Acucar', unidade_venda_2='')
        self.assertEqual(apresentar_estoque(p), {'principal': '1,5 CX', 'equivalencia': ''})

    def test_template_compartilhado_destaca_peso(self):
        html = render_to_string('inventario/estoque_selecao.html', {'produto': produto(
            unidade_venda_2='KG', fator_conversao=30, nome='Acucar 30/1Kg')})
        self.assertIn('<strong>45 kg</strong>', html)
        self.assertIn('= 1 CX + 15 kg', html)
        self.assertIn('Estoque registrado:', html)

    def test_peso_continuo_preserva_tres_casas_inclusive_zeros(self):
        for valor, esperado in [('2.230', '2,230 kg'), ('5.750', '5,750 kg'), ('10', '10,000 kg')]:
            with self.subTest(valor=valor):
                self.assertEqual(apresentar_estoque(produto(quantidade=Decimal(valor),
                    unidade_venda_1='KG', unidade_venda_2=''))['principal'], esperado)

    def test_unidade_inteira_e_transporte_para_embalagem_completa(self):
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('54.012'),
            unidade_venda_1='UN'))['principal'], '54 UN')
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('.999'),
            unidade_venda_1='PCT')),
            {'principal': '1 PCT + 0 UN', 'equivalencia': '12 UN'})
        self.assertEqual(apresentar_estoque(produto(quantidade=Decimal('54.500'),
            unidade_venda_1='UN'))['principal'], '55 UN')

    def test_embalagem_sem_identificacao_1kg_nao_arredonda_peso_para_inteiro(self):
        self.assertEqual(apresentar_estoque(produto(nome='Saco 1/20Kg',
            unidade_venda_2='KG', fator_conversao=20))['principal'], '30,000 kg')

    def test_pacote_de_1kg_no_nome_nao_substitui_cadastro_de_venda_por_peso(self):
        self.assertEqual(apresentar_estoque(produto(nome='Frango 18/1Kg',
            unidade_venda_1='KG', unidade_venda_2='', quantidade=Decimal('2.230')))['principal'], '2,230 kg')
