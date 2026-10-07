"""Caracterização do contrato online e regressão da extração da etapa 2.4."""
import copy
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from .models import (
    Cliente, ContaFinanceira, ContaReceber, DespesaDiaria, EventoVenda,
    ItemVenda, MovimentoFinanceiro, Produto, Venda,
)


class ContratoGravarVendaTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nome="Cliente contrato")
        self.produto = Produto.objects.create(
            nome="Produto contrato", quantidade=Decimal("10.000"),
            preco_compra=Decimal("5.00"), preco_vista=Decimal("10.00"),
            preco_prazo=Decimal("10.00"), unidade_venda_1="UN",
        )
        self.dados = {
            "cliente_id": self.cliente.pk, "data_venda": "2026-10-07",
            "data_vencimento": "2026-11-07", "tipo_pagamento": "A prazo",
            "operador": "Teste", "total": "99999", "itens": [{
                "produto_id": self.produto.pk, "produto_nome": self.produto.nome,
                "quantidade": "2", "unidade": "UN", "preco_unitario": "10",
                "valor_total": "99999", "custo": "0", "estoque": "99999",
            }],
        }

    def postar(self, dados=None):
        return self.client.post(
            "/vendas/gravar/", json.dumps(dados or self.dados),
            content_type="application/json", secure=True,
        )

    def assert_sem_efeitos(self):
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("10.000"))
        for modelo in (Venda, ItemVenda, EventoVenda, ContaReceber,
                       MovimentoFinanceiro, DespesaDiaria, ContaFinanceira):
            self.assertFalse(modelo.objects.exists(), modelo.__name__)

    def test_nova_prazo_recalcula_valores_e_preserva_contrato(self):
        resposta = self.postar()
        self.assertEqual(resposta.status_code, 200)
        retorno = resposta.json()
        self.assertEqual(set(retorno), {
            "sucesso", "mensagem", "venda_id", "visualizar_url",
            "separacao", "produtos_estoque_atualizados",
        })
        venda = Venda.objects.get(pk=retorno["venda_id"])
        self.assertEqual(venda.total, Decimal("20"))
        item = ItemVenda.objects.get(venda=venda)
        self.assertEqual(item.valor_total, Decimal("20"))
        self.assertEqual(item.custo_total_snapshot, Decimal("10"))
        self.assertEqual(item.estoque_movimentado, Decimal("2"))
        self.assertEqual(ContaReceber.objects.get(venda=venda).valor_original, Decimal("20"))
        self.assertEqual(EventoVenda.objects.filter(venda=venda).count(), 1)
        self.assertIsNone(retorno["separacao"])
        self.assertTrue(retorno["produtos_estoque_atualizados"])

    def test_validacoes_sem_efeitos(self):
        casos = [
            ("cliente_id", 999999), ("data_venda", "invalida"),
            ("itens", []), ("venda_id", "invalida"),
        ]
        for campo, valor in casos:
            with self.subTest(campo=campo):
                dados = copy.deepcopy(self.dados)
                dados[campo] = valor
                self.assertEqual(self.postar(dados).status_code, 400)
                self.assert_sem_efeitos()

    def test_validacoes_item_sem_efeitos(self):
        for campo, valor in (("quantidade", "0"), ("quantidade", "-1"),
                             ("quantidade", "erro"), ("quantidade", "11"),
                             ("quantidade", "0.5"), ("preco_unitario", "4.99")):
            with self.subTest(campo=campo, valor=valor):
                dados = copy.deepcopy(self.dados)
                dados["itens"][0][campo] = valor
                self.assertEqual(self.postar(dados).status_code, 400)
                self.assert_sem_efeitos()

    def test_produto_invalido_e_inativo(self):
        dados = copy.deepcopy(self.dados)
        dados["itens"][0].update(produto_id=999999, produto_nome="Inexistente")
        self.assertEqual(self.postar(dados).status_code, 400)
        self.produto.ativo = False
        self.produto.save(update_fields=["ativo"])
        self.assertEqual(self.postar().status_code, 400)
        self.assert_sem_efeitos()

    def test_operador_vazio_e_vencimento_ausente_preservam_regra_atual(self):
        self.dados["operador"] = ""
        self.dados["data_vencimento"] = "invalido"
        resposta = self.postar()
        self.assertEqual(resposta.status_code, 200)
        venda = Venda.objects.get()
        self.assertEqual(venda.operador, "")
        self.assertIsNone(venda.data_vencimento)
        self.assertIsNone(ContaReceber.objects.get().data_vencimento)

    def test_produto_id_invalido_com_nome_existente_usa_fallback_atual(self):
        self.dados["itens"][0]["produto_id"] = 999999
        self.assertEqual(self.postar().status_code, 200)
        self.assertEqual(ItemVenda.objects.get().produto_id, self.produto.pk)

    def test_rollback_nova_venda_apos_itens_evento_conta(self):
        with patch("estoque.views._sincronizar_despesas_consumo_proprio",
                   side_effect=ValueError("Falha obrigatoria")):
            resposta = self.postar()
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.json()["mensagem"], "Falha obrigatoria")
        self.assert_sem_efeitos()

    def test_rollback_financeiro_inesperado_inclui_contas_padrao(self):
        self.dados["tipo_pagamento"] = "A vista"
        with patch("estoque.views.MovimentoFinanceiro.objects.create",
                   side_effect=RuntimeError("Falha financeira")):
            with self.assertRaises(RuntimeError):
                self.postar()
        self.assert_sem_efeitos()

    def test_json_ilegivel_e_metodo_http(self):
        resposta = self.client.post("/vendas/gravar/", "{", content_type="application/json", secure=True)
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.json(), {
            "sucesso": False, "mensagem": "Nao foi possivel ler os dados da venda.",
        })
        self.assertEqual(self.client.get("/vendas/gravar/", secure=True).status_code, 405)
        self.assert_sem_efeitos()

    def test_http_chama_servico_real_e_preserva_resultado_oficial(self):
        from .services.vendas import criar_ou_atualizar_venda

        with patch("estoque.views.criar_ou_atualizar_venda",
                   wraps=criar_ou_atualizar_venda) as servico:
            resposta = self.postar()
        self.assertEqual(resposta.status_code, 200)
        servico.assert_called_once()
        self.assertEqual(servico.call_args.kwargs["dados"], self.dados)
        self.assertIn("usuario", servico.call_args.kwargs)
        venda = Venda.objects.get(pk=resposta.json()["venda_id"])
        self.assertEqual(venda.total, Decimal("20"))
        self.assertEqual(ItemVenda.objects.filter(venda=venda).count(), 1)
        self.assertEqual(EventoVenda.objects.filter(venda=venda).count(), 1)
        self.assertEqual(ContaReceber.objects.filter(venda=venda).count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("8"))

    def test_servico_direto_sem_http_e_edicao_sem_duplicar_efeitos(self):
        from .services.vendas import criar_ou_atualizar_venda

        dados = copy.deepcopy(self.dados)
        resultado = criar_ou_atualizar_venda(usuario=None, dados=dados)
        self.assertFalse(resultado.edicao)
        self.assertEqual(resultado.venda.total, Decimal("20"))
        self.assertEqual(resultado.produtos_estoque_atualizados_ids, {self.produto.pk})
        item = ItemVenda.objects.get(venda=resultado.venda)
        dados["venda_id"] = resultado.venda.pk
        dados["itens"][0]["item_id"] = item.pk
        dados["itens"][0]["quantidade"] = "3"
        editada = criar_ou_atualizar_venda(usuario=None, dados=dados)
        self.assertTrue(editada.edicao)
        self.assertEqual(editada.venda.pk, resultado.venda.pk)
        self.assertEqual(editada.venda.total, Decimal("30"))
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(ContaReceber.objects.count(), 1)
        self.assertEqual(ContaReceber.objects.get().valor_original, Decimal("30"))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("7"))

    def test_servico_direto_rejeicao_e_rollback(self):
        from .services.vendas import ErroGravarVenda, criar_ou_atualizar_venda

        with patch("estoque.views._sincronizar_despesas_consumo_proprio",
                   side_effect=ValueError("Falha obrigatoria")):
            with self.assertRaises(ErroGravarVenda) as erro:
                criar_ou_atualizar_venda(usuario=None, dados=self.dados)
        self.assertEqual(erro.exception.status, 400)
        self.assertEqual(erro.exception.mensagem, "Falha obrigatoria")
        self.assert_sem_efeitos()

    def test_edicao_next_seguro_e_bloqueio_url_externa(self):
        venda_id = self.postar().json()["venda_id"]
        self.dados["venda_id"] = venda_id
        self.dados["itens"][0]["item_id"] = ItemVenda.objects.get().pk
        for destino, esperado in (
            ("/vendas/", "/vendas/"),
            ("https://externo.example/", f"/vendas/{venda_id}/?nota_atualizada=1"),
        ):
            with self.subTest(destino=destino):
                self.dados["next"] = destino
                resposta = self.postar()
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.json()["visualizar_url"], esperado)
