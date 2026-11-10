from decimal import Decimal
from django.test import TestCase
from django.contrib.messages import get_messages

from estoque.models import Compra, ContaPagar, Produto
from estoque.tests_edicao_compra import EdicaoFixture


class CorrecaoBoletoRegressaoTests(EdicaoFixture, TestCase):

    def test_corrigir_apenas_financeiro_preserva_estoque(self):
        self.item.observacao = None
        self.item.save(update_fields=["observacao"])
        dados = self.payload(
            acao_compra="salvar_correcao_simples",
            valor_cobrado="95,00",
            compra_valor_nota_boleto="95,00",
            compra_parcela_valor_1="95,00",
        )

        dados["observacao_item[]"] = [""]
        estoque_antes = self.produto.quantidade
        resposta = self.client.post(self.url, dados)

        self.compra.refresh_from_db()
        self.conta.refresh_from_db()
        self.produto.refresh_from_db()

        print("DIAGNOSTICO SALVAMENTO:", [
            str(m) for m in get_messages(resposta.wsgi_request)
        ])
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self.compra.total, Decimal("95.00"))
        self.assertEqual(self.compra.total_produtos, Decimal("100.00"))
        self.assertEqual(self.compra.ajuste_total, Decimal("-5.00"))
        self.assertEqual(self.conta.valor_original, Decimal("95.00"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("95.00"))
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 1)

    def test_corrigir_preco_do_item_e_boleto_sem_mudar_estoque(self):
        self.item.quantidade = Decimal("2")
        self.item.preco_unitario = Decimal("41.74")
        self.item.valor_total = Decimal("83.48")
        self.item.observacao = None
        self.item.save()

        self.compra.total_produtos = Decimal("83.48")
        self.compra.total = Decimal("83.48")
        self.compra.ajuste_total = Decimal("0")
        self.compra.save()

        self.conta.valor_original = Decimal("83.48")
        self.conta.valor_em_aberto = Decimal("83.48")
        self.conta.save()

        dados = self.payload(**{
            "quantidade[]": ["2"],
            "preco_unitario[]": ["43,99"],
            "observacao_item[]": [""],
            "valor_cobrado": "87,98",
            "compra_valor_nota_boleto": "87,98",
            "compra_parcela_valor_1": "87,98",
            "acao_compra": "salvar_correcao_simples",
        })

        estoque_antes = self.produto.quantidade
        resposta = self.client.post(self.url, dados)

        self.item.refresh_from_db()
        self.compra.refresh_from_db()
        self.conta.refresh_from_db()
        self.produto.refresh_from_db()

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self.item.preco_unitario, Decimal("43.99"))
        self.assertEqual(self.item.quantidade, Decimal("2"))
        self.assertEqual(self.compra.total_produtos, Decimal("87.98"))
        self.assertEqual(self.compra.total, Decimal("87.98"))
        self.assertEqual(self.conta.valor_original, Decimal("87.98"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("87.98"))
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_itens_e_boletos_juntos_continuam_protegidos(self):
        self.item.observacao = None
        self.item.save(update_fields=["observacao"])
        dados = self.payload(
            acao_compra="salvar_correcao_simples",
            **{
                "quantidade[]": ["11"],
                "valor_cobrado": "105,00",
                "compra_valor_nota_boleto": "105,00",
                "compra_parcela_valor_1": "105,00",
            }
        )

        dados["observacao_item[]"] = [""]
        estoque_antes = self.produto.quantidade
        resposta = self.client.post(self.url, dados)

        self.compra.refresh_from_db()
        self.conta.refresh_from_db()
        self.produto.refresh_from_db()

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self.compra.total, Decimal("100.00"))
        self.assertEqual(self.conta.valor_original, Decimal("100.00"))
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertIn(
            "Alterar itens e boletos juntos exige",
            " ".join(str(m) for m in get_messages(resposta.wsgi_request)),
        )
