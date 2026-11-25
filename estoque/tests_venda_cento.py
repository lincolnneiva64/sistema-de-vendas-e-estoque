"""Conversão CT/UN da venda; instâncias em memória, sem conexão ao banco."""
from decimal import Decimal as D

from django.test import SimpleTestCase

from estoque.models import Produto, ItemVenda
from estoque import views
from estoque.services.quantidade_venda import valores


class VendaCentoTests(SimpleTestCase):
    def setUp(self):
        self.produto = Produto(pk=544, nome='Marmitex', unidade_compra='CT',
                               unidade_venda_1='CT', unidade_venda_2='UN',
                               fator_conversao=D('100'), vende_fracionado=True,
                               quantidade=D('3.000'))

    def test_meio_cento_equivale_a_cinquenta_unidades(self):
        self.assertTrue(views._quantidade_valida_para_produto_unidade(self.produto, 'CT', D('.5')))
        for quantidade, unidade in [(D('.5'), 'CT'), (D('50'), 'UN')]:
            with self.subTest(unidade=unidade):
                self.assertEqual(views._quantidade_estoque_para_unidade_base(
                    self.produto, quantidade, unidade), (D('.500'), 'CT'))

    def test_fracoes_de_unidade_fisica_sao_recusadas(self):
        for quantidade, unidade in [(D('.001'), 'CT'), (D('.005'), 'CT'), (D('.5'), 'UN')]:
            with self.subTest(quantidade=quantidade, unidade=unidade):
                with self.assertRaises(ValueError):
                    views._quantidade_estoque_para_unidade_base(self.produto, quantidade, unidade)
        self.assertTrue(views._quantidade_valida_para_produto_unidade(self.produto, 'CT', D('.01')))
        self.assertTrue(views._quantidade_valida_para_produto_unidade(self.produto, 'UN', D('1')))

    def test_sem_conversao_confiavel_nao_adivinha_fator(self):
        for campo, valor in [('vende_fracionado', False), ('fator_conversao', D('0')),
                              ('unidade_venda_2', '')]:
            with self.subTest(campo=campo):
                original = getattr(self.produto, campo)
                setattr(self.produto, campo, valor)
                with self.assertRaises(ValueError):
                    views._quantidade_estoque_para_unidade_base(self.produto, D('.5'), 'CT')
                setattr(self.produto, campo, original)

    def test_snapshot_movimenta_meio_cento_sem_mudar_unidade_do_estoque(self):
        r = views._snapshot_item_venda_quantidade_atual(self.produto, D('.5'), 'CT')
        self.assertEqual(r['estoque_movimentado'], D('.500'))
        self.assertEqual(r['estoque_unidade_snapshot'], 'CT')
        self.assertEqual(r['estoque_antes'], D('3.500'))
        self.assertEqual(r['estoque_depois'], D('3.000'))

    def test_previa_existente_mantem_trinta_e_seis_reais(self):
        item = ItemVenda(produto=self.produto, quantidade=D('1'), unidade='CT',
                         preco_unitario=D('72'), valor_total=D('72'))
        for quantidade, unidade, preco in [('.5', 'CT', D('72.00')), ('50', 'UN', D('.72'))]:
            r = valores(item, quantidade, unidade)
            self.assertEqual((r[2], r[3]), (preco, D('36.00')))

    def test_embalagens_e_sacos_existentes_preservados(self):
        for unidade in ['PCT', 'SC']:
            self.produto.unidade_venda_1 = unidade
            self.assertTrue(views._quantidade_valida_para_produto_unidade(self.produto, unidade, D('.5')))
            self.assertFalse(views._quantidade_valida_para_produto_unidade(self.produto, unidade, D('.001')))
