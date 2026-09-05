import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Compra, ItemCompra, MovimentacaoEstoqueManual, Produto
from .services.estoque_manual import conferir_ou_ajustar_estoque


class EstoqueManualServiceTests(TestCase):
    def setUp(self):
        self.produto = Produto.objects.create(
            nome="Produto Conferencia",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("4.000"),
        )
        self.usuario = get_user_model().objects.create_user(
            username="operador-estoque",
            password="senha-teste",
        )

    def test_conferencia_preserva_saldo_cria_historico_e_marca_produto(self):
        resultado = conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
            usuario=self.usuario,
        )

        self.produto.refresh_from_db()
        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(self.produto.quantidade, Decimal("4.000"))
        self.assertTrue(self.produto.estoque_conferido)
        self.assertIsNotNone(self.produto.estoque_conferido_em)
        self.assertEqual(self.produto.estoque_conferido_por, self.usuario)
        self.assertEqual(historico.tipo, MovimentacaoEstoqueManual.TIPO_CONFERENCIA)
        self.assertEqual(historico.estoque_antes, Decimal("4.000"))
        self.assertEqual(historico.estoque_depois, Decimal("4.000"))
        self.assertEqual(historico.diferenca, Decimal("0.000"))
        self.assertEqual(historico.usuario, self.usuario)
        self.assertEqual(historico.operador_nome, "operador-estoque")
        self.assertEqual(resultado["diferenca"], Decimal("0.000"))

    def test_conferencia_de_saldo_nulo_persiste_zero_com_historico_coerente(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=None)

        resultado = conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
        )

        self.produto.refresh_from_db()
        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(self.produto.quantidade, Decimal("0.000"))
        self.assertEqual(resultado["estoque_depois"], Decimal("0.000"))
        self.assertEqual(historico.estoque_antes, Decimal("0.000"))
        self.assertEqual(historico.estoque_depois, Decimal("0.000"))
        self.assertEqual(historico.diferenca, Decimal("0.000"))
        self.assertTrue(self.produto.estoque_conferido)

    def test_ajuste_reduz_estoque_registra_diferenca_e_motivo(self):
        resultado = conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=Decimal("1.250"),
            motivo="Contagem fisica",
            observacao="Divergencia encontrada",
            usuario=self.usuario,
        )

        self.produto.refresh_from_db()
        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(self.produto.quantidade, Decimal("1.250"))
        self.assertTrue(self.produto.estoque_conferido)
        self.assertEqual(historico.estoque_antes, Decimal("4.000"))
        self.assertEqual(historico.estoque_depois, Decimal("1.250"))
        self.assertEqual(historico.diferenca, Decimal("-2.750"))
        self.assertEqual(historico.motivo, "Contagem fisica")
        self.assertEqual(historico.observacao, "Divergencia encontrada")
        self.assertEqual(resultado["estoque_depois"], Decimal("1.250"))

    def test_ajuste_aumenta_estoque_e_registra_diferenca_positiva(self):
        conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque="6.125",
            motivo="Produto encontrado",
        )

        self.produto.refresh_from_db()
        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(self.produto.quantidade, Decimal("6.125"))
        self.assertEqual(historico.diferenca, Decimal("2.125"))

    def test_ajuste_exige_motivo_e_rejeita_estoque_negativo(self):
        with self.assertRaisesMessage(ValueError, "Informe o motivo"):
            conferir_ou_ajustar_estoque(
                self.produto.id,
                MovimentacaoEstoqueManual.TIPO_AJUSTE,
                novo_estoque=Decimal("3.000"),
            )

        with self.assertRaisesMessage(ValueError, "nao pode ser negativo"):
            conferir_ou_ajustar_estoque(
                self.produto.id,
                MovimentacaoEstoqueManual.TIPO_AJUSTE,
                novo_estoque=Decimal("-1.000"),
                motivo="Correcao",
            )

        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("4.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_historico_anterior_permanece_apos_ajuste_e_nova_conferencia(self):
        primeira = conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
        )["historico"]
        conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=Decimal("5.000"),
            motivo="Ajuste inicial",
        )
        conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
        )

        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 3)
        self.assertTrue(MovimentacaoEstoqueManual.objects.filter(pk=primeira.pk).exists())
        self.assertEqual(
            list(MovimentacaoEstoqueManual.objects.values_list("tipo", flat=True)),
            ["conferencia", "ajuste", "conferencia"],
        )

    def test_produto_inativo_ou_excluido_nao_pode_ser_conferido(self):
        self.produto.ativo = False
        Produto.objects.filter(pk=self.produto.pk).update(ativo=False)

        with self.assertRaisesMessage(ValueError, "inativo ou excluido"):
            conferir_ou_ajustar_estoque(
                self.produto.id,
                MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
            )

    def test_entrada_normal_de_compra_nao_cria_historico_manual(self):
        compra = Compra.objects.create(
            data_compra=date(2026, 9, 5),
            tipo_pagamento="pix",
            total=Decimal("0.00"),
            status=Compra.STATUS_RASCUNHO,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=self.produto,
            quantidade=Decimal("2.000"),
            unidade="UN",
            preco_unitario=Decimal("0.00"),
            valor_total=Decimal("0.00"),
        )

        from . import views

        views._finalizar_compra_com_financeiro(compra)

        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("6.000"))
        self.assertFalse(self.produto.estoque_conferido)

    def test_venda_normal_baixa_estoque_sem_historico_manual(self):
        produto = Produto.objects.create(
            nome="Produto Venda Normal",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("5.000"),
        )
        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps({
                "cliente_id": "",
                "data_venda": timezone.localdate().isoformat(),
                "data_vencimento": "",
                "tipo_pagamento": "A vista",
                "operador": "Operador Teste",
                "itens": [{
                    "produto_nome": produto.nome,
                    "quantidade": "2",
                    "unidade": "un",
                    "preco_unitario": "2.00",
                }],
            }),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, Decimal("3.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)
