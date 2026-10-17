import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.db import transaction

from estoque.models import Cliente, Funcionario, Produto, ContaPagar
from estoque.tests_compra_pre_revisao import CompraPreRevisaoFixture
from estoque.views import _devolver_estoque_produto
from . import services
from .tests import produto


class VendasInventarioTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-integracao", "", "teste")
        self.produto = produto(unidade_compra="UN", vende_fracionado=False, codigo_legado="INV-FICTICIO")
        self.cliente = Cliente.objects.create(nome="Cliente fictício inventário")
        self.funcionario = Funcionario.objects.create(nome="Contador integração")
        inventario = services.criar_inventario(self.usuario, "Integração", "manual", [self.produto.pk])
        self.rodada = inventario.itens.get().rodadas.get()
        self.acesso, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def vender(self, quantidade=1, tipo="A prazo"):
        response = self.client.post("/vendas/gravar/", json.dumps({
            "cliente_id": self.cliente.pk, "data_venda": "2026-10-17", "data_vencimento": "2026-11-17",
            "tipo_pagamento": tipo, "operador": "Teste inventário", "itens": [{
                "produto_id": self.produto.pk, "produto_nome": self.produto.nome,
                "quantidade": str(quantidade), "unidade": "UN", "preco_unitario": "15"}]}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_vendas_antes_e_depois_ok_e_devolucao(self):
        self.vender()
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "", True)
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (9, 1))
        self.vender()
        with transaction.atomic():
            _devolver_estoque_produto(self.produto.pk, 1, unidade="UN")
        movimentos = list(services.posteriores(rodada))
        self.assertEqual([(m.antes, m.depois) for m in movimentos], [(9, 8), (8, 9)])
        self.assertEqual([m.origem for m in movimentos], ["venda", "devolucao_venda"])
        self.assertTrue(movimentos[0].referencia.startswith("Venda #"))
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Sem comprovação física")

    def test_sincronizacao_motor_real_com_fonte_ficticia_e_observada(self):
        # Only the write engine is called. Never extracts or connects to Firebird.
        import atualizar_estoque_firebird
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "", True)
        atualizar_estoque_firebird.aplicar({"bloqueados": [], "alterados": [{"produto": self.produto, "quantidade_firebird": Decimal("12")} ]})
        movimento = services.posteriores(rodada).get()
        self.assertEqual((movimento.antes, movimento.depois), (10, 12))
        self.assertEqual(movimento.origem, "sincronizacao_firebird")
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Sincronização exige revisão física")

    def test_consumo_proprio_antes_e_depois_do_ok(self):
        from estoque.models import Venda
        self.cliente.nome = "Lincoln Neiva"
        self.cliente.save()
        self.vender(tipo=Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "", True)
        self.vender(tipo=Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        posteriores = list(services.posteriores(rodada))
        self.assertEqual(rodada.sequencia_ok, 1)
        self.assertEqual(len(posteriores), 1)
        self.assertEqual(posteriores[0].origem, "consumo_proprio")
        self.assertTrue(posteriores[0].referencia.startswith("Venda #"))


class ComprasInventarioTests(CompraPreRevisaoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.gestor = get_user_model().objects.create_superuser("gestor-compra-inv", "", "teste")
        funcionario = Funcionario.objects.create(nome="Contador compra")
        inventario = services.criar_inventario(self.gestor, "Compra inventário", "manual", [self.produto.pk])
        self.rodada = inventario.itens.get().rodadas.get()
        self.acesso, _ = services.gerar_acesso(self.gestor, inventario.pk, funcionario.pk, [self.rodada.pk])

    def finalizar_compra(self):
        self.client.post(self.salvar_url, self.payload())
        self.client.post(self.editar_url, self.payload())
        self.client.post(self.finalizar_url, self.payload(acao_compra="confirmar_financeiro"))
        self.compra.refresh_from_db()
        self.assertTrue(self.compra.estoque_entrada_realizada)

    def test_compra_posterior_ok_e_registrada_sem_alterar_contagem(self):
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "", True)
        self.finalizar_compra()
        movimento = services.posteriores(rodada).get()
        self.assertEqual((movimento.antes, movimento.depois), (10, 19))
        self.assertEqual(movimento.origem, "compra")
        self.assertEqual(movimento.referencia, f"Compra #{self.compra.pk}")
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)
        rodada.refresh_from_db()
        self.assertEqual(rodada.contado_base, 8)
        with self.assertRaises(ValueError):
            services.revisar(self.gestor, rodada.pk, "Recebimento sem conciliação")

    def test_compra_anterior_ok_compoe_saldo_de_referencia(self):
        self.finalizar_compra()
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "19", "", True)
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (19, 1))
        self.assertFalse(services.posteriores(rodada).exists())
