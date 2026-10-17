from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from estoque.models import Produto, Fornecedor, ProdutoFornecedor
from .conversao import converter, snapshot
from .models import Inventario
from .services import criar_inventario


def produto(**kwargs):
    dados = dict(nome="Produto inventário", preco_compra=Decimal("10"), preco_vista=Decimal("15"),
                 preco_prazo=Decimal("16"), quantidade=Decimal("10"), categoria="Bebidas", unidade_compra="CX",
                 vende_fracionado=True, unidade_venda_2="UN", fator_conversao=Decimal("12"))
    dados.update(kwargs)
    return Produto.objects.create(**dados)


class SelecaoConversaoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor", "", "teste")
        self.produto = produto()

    def test_selecao_manual_congela_cadastro_sem_alterar_saldo(self):
        inventario = criar_inventario(self.usuario, "Teste", "manual", [self.produto.pk, self.produto.pk])
        self.assertEqual(inventario.itens.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertEqual(inventario.itens.get().cadastro["fator"], "12.00")

    def test_categoria(self):
        inventario = criar_inventario(self.usuario, "Teste", "categoria", self.produto.categoria)
        self.assertEqual(inventario.itens.get().produto_id, self.produto.pk)

    def test_fornecedor_usa_vinculo_ativo(self):
        fornecedor = Fornecedor.objects.create(nome="Comercial")
        ProdutoFornecedor.objects.create(fornecedor=fornecedor, produto=self.produto)
        self.assertEqual(criar_inventario(self.usuario, "Teste", "fornecedor", fornecedor.pk).itens.count(), 1)
        ProdutoFornecedor.objects.update(ativo=False)
        with self.assertRaises(ValueError):
            criar_inventario(self.usuario, "Teste", "fornecedor", fornecedor.pk)

    def test_embalagens_avulsos_e_arredondamento(self):
        cadastro = snapshot(self.produto)
        self.assertEqual(converter(cadastro, "3", "6")[3], Decimal("3.500"))
        valores = converter(cadastro, "0", "1")
        self.assertEqual(valores[2], Decimal("0.083333333"))
        self.assertEqual(valores[3], Decimal("0.083"))

    def test_kg(self):
        p = produto(nome="Peso", unidade_compra="KG", vende_fracionado=False)
        self.assertEqual(converter(snapshot(p), "2,375", "")[3], Decimal("2.375"))

    def test_vazio_zero_e_entradas_invalidas(self):
        cadastro = snapshot(self.produto)
        self.assertEqual(converter(cadastro, "0", "0")[3], 0)
        for principal, avulso in [("", "0"), ("0", ""), ("1.5", "0"), ("0", "0.5"), ("NaN", "0"), ("-1", "0"), ("1.0001", "0")]:
            with self.subTest(principal=principal, avulso=avulso), self.assertRaises(ValueError):
                converter(cadastro, principal, avulso)

    def test_erro_de_cadastro_reverte_sessao(self):
        Produto.objects.filter(pk=self.produto.pk).update(unidade_compra="", unidade_venda_1="")
        with self.assertRaises(ValueError):
            criar_inventario(self.usuario, "Teste", "manual", [self.produto.pk])
        self.assertFalse(Inventario.objects.exists())
