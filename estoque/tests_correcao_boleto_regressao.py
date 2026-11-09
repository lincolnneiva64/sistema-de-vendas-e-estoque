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
