import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import MovimentacaoEstoqueManual, Produto
from .services.estoque_manual import conferir_ou_ajustar_estoque


class EstoqueManualEndpointTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username="operador-endpoint",
            password="senha-teste",
        )
        self.client.force_login(self.usuario)
        self.produto = self._produto("Produto Endpoint", quantidade=Decimal("4.000"))

    def _produto(self, nome, quantidade=Decimal("1.000"), ativo=True, excluido=False, conferido=False):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=quantidade,
            ativo=ativo,
            excluido=excluido,
            estoque_conferido=conferido,
        )

    def _url_consulta(self, produto=None):
        return reverse("estoque:conferencia_estoque_produto", args=[(produto or self.produto).id])

    def _url_confirmar(self, produto=None):
        return reverse("estoque:conferencia_estoque_confirmar", args=[(produto or self.produto).id])

    def _url_corrigir(self, produto=None):
        return reverse("estoque:conferencia_estoque_corrigir", args=[(produto or self.produto).id])

    def _assert_produto_sem_conferencia(self, produto, quantidade):
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, quantidade)
        self.assertFalse(produto.estoque_conferido)
        self.assertIsNone(produto.estoque_conferido_em)

    def test_get_consulta_produto(self):
        resposta = self.client.get(self._url_consulta())

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertEqual(dados["produto"]["id"], self.produto.id)
        self.assertEqual(dados["produto"]["nome"], "Produto Endpoint")
        self.assertEqual(dados["produto"]["estoque_atual"], "4.000")
        self.assertFalse(dados["produto"]["estoque_conferido"])
        self.assertIsNone(dados["produto"]["estoque_conferido_em"])

    def test_consulta_retorna_estoque_real_do_banco(self):
        produto_memoria = Produto.objects.get(pk=self.produto.pk)
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=Decimal("7.125"))

        resposta = self.client.get(self._url_consulta(produto_memoria))

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["produto"]["estoque_atual"], "7.125")

    def test_consulta_retorna_ultima_movimentacao(self):
        conferir_ou_ajustar_estoque(
            self.produto.id,
            MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=Decimal("6.500"),
            motivo="Contagem inicial",
            observacao="Achou sobra",
            usuario=self.usuario,
        )

        resposta = self.client.get(self._url_consulta())

        self.assertEqual(resposta.status_code, 200)
        movimentacao = resposta.json()["produto"]["ultima_movimentacao"]
        self.assertEqual(movimentacao["tipo"], "ajuste")
        self.assertEqual(movimentacao["estoque_antes"], "4.000")
        self.assertEqual(movimentacao["estoque_depois"], "6.500")
        self.assertEqual(movimentacao["diferenca"], "2.500")
        self.assertEqual(movimentacao["motivo"], "Contagem inicial")
        self.assertEqual(movimentacao["observacao"], "Achou sobra")
        self.assertEqual(movimentacao["operador"], "operador-endpoint")
        self.assertTrue(movimentacao["criado_em"])

    def test_consulta_retorna_contador_correto(self):
        self._produto("Produto Ja Conferido", conferido=True)
        self._produto("Produto Inativo", ativo=False, conferido=True)
        self._produto("Produto Excluido", excluido=True, conferido=True)

        resposta = self.client.get(self._url_consulta())

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta.json()["conferencia"],
            {
                "total_produtos": 2,
                "total_conferidos": 1,
                "total_faltantes": 1,
            },
        )

    def test_post_confirmacao(self):
        resposta = self.client.post(self._url_confirmar())

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertTrue(dados["produto"]["estoque_conferido"])
        self.assertEqual(dados["produto"]["ultima_movimentacao"]["tipo"], "conferencia")
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)

    def test_post_ajuste_positivo(self):
        resposta = self.client.post(
            self._url_corrigir(),
            data=json.dumps({"novo_estoque": "8.250", "motivo": "Contagem fisica"}),
            content_type="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("8.250"))
        self.assertEqual(resposta.json()["produto"]["estoque_atual"], "8.250")
        self.assertEqual(resposta.json()["produto"]["ultima_movimentacao"]["diferenca"], "4.250")

    def test_post_ajuste_negativo_de_diferenca_sem_permitir_estoque_final_negativo(self):
        resposta = self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "1.250", "motivo": "Perda identificada"},
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["produto"]["ultima_movimentacao"]["diferenca"], "-2.750")

        resposta_negativa = self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "-0.001", "motivo": "Erro de contagem"},
        )

        self.assertEqual(resposta_negativa.status_code, 400)
        self.assertFalse(resposta_negativa.json()["sucesso"])
        self.assertIn("nao pode ser negativo", resposta_negativa.json()["mensagem"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("1.250"))

    def test_motivo_obrigatorio(self):
        resposta = self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "5.000", "motivo": ""},
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["sucesso"])
        self.assertIn("Informe o motivo", resposta.json()["mensagem"])
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_corrigir_rejeita_array_json(self):
        resposta = self.client.post(
            self._url_corrigir(),
            data=json.dumps([]),
            content_type="application/json",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["sucesso"])
        self.assertIn("objeto JSON", resposta.json()["mensagem"])
        self._assert_produto_sem_conferencia(self.produto, Decimal("4.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_corrigir_rejeita_string_json(self):
        resposta = self.client.post(
            self._url_corrigir(),
            data=json.dumps("texto"),
            content_type="application/json",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["sucesso"])
        self.assertIn("objeto JSON", resposta.json()["mensagem"])
        self._assert_produto_sem_conferencia(self.produto, Decimal("4.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_produto_inexistente(self):
        resposta = self.client.get(reverse("estoque:conferencia_estoque_produto", args=[999999]))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])

    def test_post_confirmar_produto_inexistente_nao_cria_historico(self):
        resposta = self.client.post(reverse("estoque:conferencia_estoque_confirmar", args=[999999]))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(self.produto, Decimal("4.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_post_corrigir_produto_inexistente_nao_cria_historico(self):
        resposta = self.client.post(
            reverse("estoque:conferencia_estoque_corrigir", args=[999999]),
            data={"novo_estoque": "5.000", "motivo": "Contagem"},
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(self.produto, Decimal("4.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_produto_inativo(self):
        produto = self._produto("Produto Inativo Consulta", ativo=False)

        resposta = self.client.get(self._url_consulta(produto))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])

    def test_post_confirmar_produto_inativo_nao_altera_estoque_nem_conferencia(self):
        produto = self._produto("Produto Inativo Confirmar", quantidade=Decimal("2.000"), ativo=False)

        resposta = self.client.post(self._url_confirmar(produto))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(produto, Decimal("2.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_post_corrigir_produto_inativo_nao_altera_estoque_nem_conferencia(self):
        produto = self._produto("Produto Inativo Corrigir", quantidade=Decimal("2.000"), ativo=False)

        resposta = self.client.post(
            self._url_corrigir(produto),
            data={"novo_estoque": "5.000", "motivo": "Contagem"},
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(produto, Decimal("2.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_produto_excluido(self):
        produto = self._produto("Produto Excluido Consulta", excluido=True)

        resposta = self.client.get(self._url_consulta(produto))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])

    def test_post_confirmar_produto_excluido_nao_altera_estoque_nem_conferencia(self):
        produto = self._produto("Produto Excluido Confirmar", quantidade=Decimal("2.000"), excluido=True)

        resposta = self.client.post(self._url_confirmar(produto))

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(produto, Decimal("2.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_post_corrigir_produto_excluido_nao_altera_estoque_nem_conferencia(self):
        produto = self._produto("Produto Excluido Corrigir", quantidade=Decimal("2.000"), excluido=True)

        resposta = self.client.post(
            self._url_corrigir(produto),
            data={"novo_estoque": "5.000", "motivo": "Contagem"},
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["sucesso"])
        self._assert_produto_sem_conferencia(produto, Decimal("2.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_historico_criado(self):
        self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "3.000", "motivo": "Contagem final"},
        )

        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(historico.produto, self.produto)
        self.assertEqual(historico.tipo, MovimentacaoEstoqueManual.TIPO_AJUSTE)
        self.assertEqual(historico.motivo, "Contagem final")
        self.assertEqual(historico.usuario, self.usuario)

    def test_confirmacao_nao_altera_quantidade(self):
        self.client.post(self._url_confirmar())

        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("4.000"))
        historico = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(historico.estoque_antes, Decimal("4.000"))
        self.assertEqual(historico.estoque_depois, Decimal("4.000"))

    def test_contador_aumenta_depois_da_primeira_conferencia(self):
        resposta_antes = self.client.get(self._url_consulta())
        self.assertEqual(resposta_antes.json()["conferencia"]["total_conferidos"], 0)

        resposta_depois = self.client.post(self._url_confirmar())

        self.assertEqual(resposta_depois.status_code, 200)
        self.assertEqual(resposta_depois.json()["conferencia"]["total_conferidos"], 1)

    def test_segunda_correcao_do_mesmo_produto_nao_aumenta_total_conferidos(self):
        primeira = self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "5.000", "motivo": "Primeira contagem"},
        )
        segunda = self.client.post(
            self._url_corrigir(),
            data={"novo_estoque": "6.000", "motivo": "Segunda contagem"},
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(primeira.json()["conferencia"]["total_conferidos"], 1)
        self.assertEqual(segunda.json()["conferencia"]["total_conferidos"], 1)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 2)

    def test_posts_sao_protegidos_por_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.usuario)

        resposta = csrf_client.post(self._url_confirmar())

        self.assertEqual(resposta.status_code, 403)
