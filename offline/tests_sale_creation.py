"""Criação idempotente: protocolo, efeitos, rollback e concorrência PostgreSQL."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from estoque.models import (
    CatalogoDespesa, Cliente, ContaFinanceira, ContaReceber, DespesaDiaria, EventoVenda,
    ItemVenda, MovimentoFinanceiro, Produto, Venda,
)
from .models import OperacaoSincronizacao
from .services import command_hash, process_operation, validate_command


def fixtures():
    user = get_user_model().objects.create_user(username="sale-offline", password="test")
    user.user_permissions.add(Permission.objects.get(
        content_type__app_label="offline", codename="registrar_observacao"))
    customer = Cliente.objects.create(nome="Lincoln Neiva")
    product = Produto.objects.create(
        nome="Produto offline", categoria="Estivas", quantidade=Decimal("10"),
        preco_compra=Decimal("5"), preco_vista=Decimal("10"),
        preco_prazo=Decimal("10"), unidade_venda_1="UN",
    )
    operation_id = str(uuid4())
    command = {
        "operation_id": operation_id, "aggregate_id": operation_id,
        "device_id": str(uuid4()), "actor_id": str(user.pk),
        "environment_id": "offline-isolated-tests", "type": "criar_venda",
        "schema_version": 1, "sequence": 1, "created_at": timezone.now().isoformat(),
        "payload": {
            "schema_version": 1, "cliente_id": str(customer.pk),
            "data_venda": "2026-10-07", "data_vencimento": "2026-11-07",
            "tipo_pagamento": "A prazo", "operador": "Teste",
            "itens": [{"produto_id": str(product.pk), "quantidade": "2",
                       "unidade": "UN", "preco_unitario": "10"}],
        },
    }
    return user, customer, product, command


class SaleCreationTests(TestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.command = fixtures()
        self.client.force_login(self.user)
        self.catalogs_before = list(CatalogoDespesa.objects.order_by("pk").values())

    def send(self, command=None, *, client=None, digest=None):
        command = self.command if command is None else command
        body = {**command, "payload_hash": command_hash(command) if digest is None else digest}
        return (client or self.client).post(
            "/api/offline/observations/", json.dumps(body), content_type="application/json")

    def effects(self):
        models = (Venda, ItemVenda, EventoVenda, ContaReceber, CatalogoDespesa,
                  MovimentoFinanceiro, ContaFinanceira, DespesaDiaria)
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in models} | {
            "estoque": list(Produto.objects.order_by("pk").values("pk", "quantidade")),
        }

    def assert_no_sale(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("10"))
        for model in (Venda, ItemVenda, EventoVenda, ContaReceber, MovimentoFinanceiro,
                      DespesaDiaria, ContaFinanceira):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(list(CatalogoDespesa.objects.order_by("pk").values()), self.catalogs_before)

    def assert_replay(self, payment):
        self.command["payload"]["tipo_pagamento"] = payment
        if payment == "A vista":
            self.command["payload"]["origem_recebimento"] = {"caixa": "8", "banco": "12"}
        first = self.send()
        self.assertEqual(first.status_code, 200, first.content)
        receipt = first.json()
        self.assertEqual(set(receipt), {"operation_id", "hash", "status", "record_id", "completed_at"})
        self.assertEqual(receipt["status"], "confirmada")
        self.assertEqual(receipt["hash"], command_hash(self.command))
        sale = Venda.objects.get(pk=receipt["record_id"])
        self.assertEqual(sale.total, Decimal("20"))
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(EventoVenda.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("8"))
        operation = OperacaoSincronizacao.objects.get()
        self.assertEqual(operation.resultado, receipt)
        self.assertEqual(operation.concluido_em.isoformat(), receipt["completed_at"])
        before = self.effects()
        # The client loses the first response after the server completed processing.
        with patch("offline.sale_commands.criar_ou_atualizar_venda", side_effect=AssertionError("Replay executou venda")):
            for _ in range(3):
                replay = self.send()
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), receipt)
        self.assertEqual(self.effects(), before)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        return sale

    def test_prazo_resposta_perdida_replay_sem_efeitos(self):
        sale = self.assert_replay("A prazo")
        self.assertEqual(ContaReceber.objects.get(venda=sale).valor_em_aberto, Decimal("20"))
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_vista_resposta_perdida_nao_duplica_origens_financeiras(self):
        self.assert_replay("A vista")
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        self.assertEqual(sorted(MovimentoFinanceiro.objects.values_list("valor", flat=True)), [Decimal("8"), Decimal("12")])
        self.assertFalse(ContaReceber.objects.exists())

    def test_consumo_resposta_perdida_nao_duplica_despesa(self):
        sale = self.assert_replay("consumo_proprio")
        self.assertEqual(DespesaDiaria.objects.get(venda_origem=sale).valor, Decimal("20"))
        self.assertFalse(ContaReceber.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_adaptador_chama_servico_oficial_sem_campos_calculados(self):
        from estoque.services.vendas import criar_ou_atualizar_venda
        for target in (self.command["payload"], self.command["payload"]["itens"][0]):
            target.update(total=999999, subtotal=999999, estoque=999999, custo=0, saldo=999999)
        with patch("offline.sale_commands.criar_ou_atualizar_venda", wraps=criar_ou_atualizar_venda) as service:
            response = self.send()
        self.assertEqual(response.status_code, 200)
        service.assert_called_once()
        data = service.call_args.kwargs["dados"]
        self.assertNotIn("total", data)
        self.assertNotIn("custo", data["itens"][0])
        sale = Venda.objects.get()
        self.assertEqual(sale.total, Decimal("20"))
        item = ItemVenda.objects.get()
        self.assertEqual(item.valor_total, Decimal("20"))
        self.assertEqual(item.custo_total_snapshot, Decimal("10"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("8"))

    def test_uuid_confirmado_com_outro_payload_preserva_operacao_original(self):
        self.send()
        before = self.effects()
        original = OperacaoSincronizacao.objects.values().get()
        changed = copy.deepcopy(self.command)
        changed["payload"]["itens"][0]["quantidade"] = "3"
        self.assertEqual(self.send(changed).status_code, 409)
        self.assertEqual(self.effects(), before)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(), original)

    def test_ordem_json_nao_muda_hash_nem_receipt(self):
        first = self.send().json()
        command = {key: self.command[key] for key in reversed(list(self.command))}
        command["payload"] = {key: self.command["payload"][key] for key in reversed(list(self.command["payload"]))}
        self.assertEqual(command_hash(command), first["hash"])
        self.assertEqual(self.send(command).json(), first)

    def test_replay_confirmado_nao_revalida_venda_apos_mudanca_comercial(self):
        receipt = self.send().json()
        Produto.objects.filter(pk=self.product.pk).update(
            ativo=False, quantidade=0, preco_compra=Decimal("15"))
        before = self.effects()
        self.assertEqual(self.send().json(), receipt)
        self.assertEqual(self.effects(), before)

    def test_snapshot_preco_antigo_rejeitado_sem_substituir_preco(self):
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=Decimal("15"))
        receipt = self.assert_business_conflict()
        self.assertIn("abaixo do custo", receipt["erro"])
        self.assertEqual(OperacaoSincronizacao.objects.get().payload["itens"][0]["preco_unitario"], "10")

    def test_dois_itens_receipt_perdido_preserva_todas_as_contagens(self):
        second = Produto.objects.create(
            nome="Segundo produto", quantidade=Decimal("5"), preco_compra=Decimal("2"),
            preco_vista=Decimal("4"), preco_prazo=Decimal("4"), unidade_venda_1="UN")
        self.command["payload"]["itens"].append({
            "produto_id": str(second.pk), "quantidade": "3", "unidade": "UN", "preco_unitario": "4"})
        first = self.send()
        self.assertEqual(first.status_code, 200)
        before = self.effects()
        self.assertEqual(ItemVenda.objects.count(), 2)
        self.assertEqual(Venda.objects.get().total, Decimal("32"))
        second.refresh_from_db()
        self.assertEqual(second.quantidade, Decimal("2"))
        self.assertEqual(self.send().json(), first.json())
        self.assertEqual(self.effects(), before)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)

    def test_identidade_versao_sequence_hash_invalidos_sem_operacao(self):
        for field, value in (("operation_id", "nao-uuid"), ("device_id", "nao-uuid"),
                             ("actor_id", "999"), ("environment_id", "outro"),
                             ("aggregate_id", "1"), ("schema_version", 2),
                             ("sequence", 0), ("sequence", True), ("created_at", "2026-10-07")):
            with self.subTest(field=field):
                command = copy.deepcopy(self.command)
                command[field] = value
                self.assertEqual(self.send(command).status_code, 400)
        self.assertEqual(self.send(digest="0" * 64).status_code, 400)
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        self.assert_no_sale()

    def test_reuso_ator_device_ambiente_sequence_preserva_original(self):
        self.send()
        original = OperacaoSincronizacao.objects.values().get()
        before = self.effects()
        for field, value in (("device_id", str(uuid4())), ("sequence", 2)):
            command = {**self.command, field: value}
            self.assertEqual(self.send(command).status_code, 409)
        other = get_user_model().objects.create_user(username="outro")
        other.user_permissions.add(Permission.objects.get(codename="registrar_observacao", content_type__app_label="offline"))
        self.client.force_login(other)
        self.assertEqual(self.send().status_code, 400)
        self.assertEqual(self.send({**self.command, "actor_id": str(other.pk)}).status_code, 409)
        self.client.force_login(self.user)
        with self.settings(OFFLINE_ENVIRONMENT_ID="outro"):
            self.assertEqual(self.send().status_code, 400)
            self.assertEqual(self.send({**self.command, "environment_id": "outro"}).status_code, 409)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(), original)
        self.assertEqual(self.effects(), before)

    def test_nao_permite_edicao_pedido_ou_nome_substituindo_id(self):
        for field in ("venda_id", "next", "ajuste_separacao_id", "pedido_id"):
            command = copy.deepcopy(self.command)
            command["payload"][field] = ""
            self.assertEqual(self.send(command).status_code, 400)
        for field in ("item_id", "produto_nome"):
            command = copy.deepcopy(self.command)
            command["payload"]["itens"][0][field] = str(self.product.pk)
            self.assertEqual(self.send(command).status_code, 400)
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        self.assert_no_sale()

    def assert_business_conflict(self, command=None):
        response = self.send(command)
        self.assertEqual(response.status_code, 409, response.content)
        receipt = response.json()
        self.assertEqual(receipt["status"], "conflito")
        self.assertIsNone(receipt["record_id"])
        self.assertTrue(receipt["erro"])
        self.assertEqual(OperacaoSincronizacao.objects.get().status, "conflito")
        self.assert_no_sale()
        return receipt

    def test_estoque_insuficiente_conflito_persistido_mesmo_apos_reposicao(self):
        self.command["payload"]["itens"][0]["quantidade"] = "11"
        receipt = self.assert_business_conflict()
        Produto.objects.filter(pk=self.product.pk).update(quantidade=20)
        self.assertEqual(self.send().json(), receipt)
        self.assertFalse(Venda.objects.exists())
        changed = copy.deepcopy(self.command)
        changed["payload"]["itens"][0]["quantidade"] = "1"
        self.assertEqual(self.send(changed).status_code, 409)

    def test_cliente_invalido_inativo(self):
        for value in ("999999", str(self.customer.pk)):
            self.command["payload"]["cliente_id"] = value
            if value == str(self.customer.pk):
                self.customer.ativo = False
                self.customer.save(update_fields=["ativo"])
            self.command["operation_id"] = self.command["aggregate_id"] = str(uuid4())
            response = self.send()
            self.assertEqual(response.status_code, 409)
            self.assert_no_sale()

    def test_produto_invalido_nao_faz_fallback_por_nome(self):
        self.command["payload"]["itens"][0]["produto_id"] = "999999"
        self.assert_business_conflict()

    def test_produto_inativo(self):
        self.product.ativo = False
        self.product.save(update_fields=["ativo"])
        self.assert_business_conflict()

    def test_quantidade_preco_unidade_data_rejeitados_por_regra_oficial(self):
        for field, value in (("quantidade", "0"), ("quantidade", "-1"),
                             ("quantidade", "0.5"), ("preco_unitario", "4.99"),
                             ("preco_unitario", "0"), ("unidade", "INVALIDA")):
            with self.subTest(field=field, value=value):
                command = copy.deepcopy(self.command)
                command["operation_id"] = command["aggregate_id"] = str(uuid4())
                command["payload"]["itens"][0][field] = value
                self.assertEqual(self.send(command).status_code, 409)
                self.assert_no_sale()
        command = copy.deepcopy(self.command)
        command["payload"]["data_venda"] = "invalida"
        self.assertEqual(self.send(command).status_code, 409)
        self.assert_no_sale()

    def test_shape_numeros_nao_finitos_e_versao_payload(self):
        for field, value in (("quantidade", "NaN"), ("preco_unitario", "Infinity"),
                             ("produto_id", 1), ("quantidade", {})):
            command = copy.deepcopy(self.command)
            command["payload"]["itens"][0][field] = value
            self.assertEqual(self.send(command).status_code, 400)
        command = copy.deepcopy(self.command)
        command["payload"]["schema_version"] = 2
        self.assertEqual(self.send(command).status_code, 400)
        self.assertFalse(OperacaoSincronizacao.objects.exists())

    def test_conversao_custo_e_estoque_revalidados(self):
        self.product.vende_fracionado = True
        self.product.unidade_venda_1 = "CX"
        self.product.unidade_venda_2 = "UN"
        self.product.fator_conversao = Decimal("10")
        self.product.save()
        self.command["payload"]["itens"][0]["quantidade"] = "5"
        self.assertEqual(self.send().status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("9.5"))
        self.assertEqual(ItemVenda.objects.get().estoque_movimentado, Decimal("0.5"))

    def test_origem_financeira_incompativel_conflito_sem_movimento(self):
        self.command["payload"].update(tipo_pagamento="A vista", origem_recebimento={"caixa": "1", "banco": "0"})
        self.assert_business_conflict()

    def test_erro_obrigatorio_apos_baixa_itens_evento_conta_rollback_e_conflito(self):
        with patch("estoque.views._sincronizar_despesas_consumo_proprio", side_effect=ValueError("Falha de negocio")):
            receipt = self.assert_business_conflict()
        self.assertEqual(receipt["erro"], "Falha de negocio")
        self.assertEqual(self.send().json(), receipt)

    def test_erro_tecnico_apos_financeiro_rollback_permite_retry(self):
        self.command["payload"]["tipo_pagamento"] = "A vista"
        with patch("estoque.views.MovimentoFinanceiro.objects.create", side_effect=RuntimeError("Transitorio")):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assert_no_sale()
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 1)
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(Venda.objects.count(), 1)

    def fail_completion_then_retry(self, payment):
        self.command["payload"]["tipo_pagamento"] = payment
        original_save = OperacaoSincronizacao.save
        def save(instance, *args, **kwargs):
            if kwargs.get("update_fields"):
                raise RuntimeError("Falhou apos criar venda")
            return original_save(instance, *args, **kwargs)
        with patch.object(OperacaoSincronizacao, "save", save):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assert_no_sale()
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        first = self.send().json()
        self.assertEqual(self.send().json(), first)
        self.assertEqual(Venda.objects.count(), 1)

    def test_falha_ao_confirmar_operacao_desfaz_venda_e_conta_receber(self):
        self.fail_completion_then_retry("A prazo")
        self.assertEqual(ContaReceber.objects.count(), 1)

    def test_falha_ao_confirmar_operacao_desfaz_movimentos_ja_criados(self):
        self.fail_completion_then_retry("A vista")
        self.assertEqual(MovimentoFinanceiro.objects.count(), 1)

    def test_falha_ao_confirmar_operacao_desfaz_despesa_e_catalogo(self):
        # Remove seeded catalogs only in this isolated test to exercise creation.
        CatalogoDespesa.objects.all().delete()
        self.catalogs_before = []
        self.fail_completion_then_retry("consumo_proprio")
        self.assertEqual(DespesaDiaria.objects.count(), 1)
        self.assertEqual(CatalogoDespesa.objects.count(), 1)

    def test_falha_no_segundo_item_desfaz_baixa_do_primeiro(self):
        second = Produto.objects.create(
            nome="Sem saldo", quantidade=Decimal("1"), preco_compra=Decimal("5"),
            preco_vista=Decimal("10"), preco_prazo=Decimal("10"), unidade_venda_1="UN")
        self.command["payload"]["itens"].append({
            "produto_id": str(second.pk), "quantidade": "2", "unidade": "UN", "preco_unitario": "10"})
        self.assert_business_conflict()
        second.refresh_from_db()
        self.assertEqual(second.quantidade, Decimal("1"))

    def test_nova_operacao_usa_sequence_original_sem_sequence_especifica(self):
        self.send()
        self.assertEqual(OperacaoSincronizacao.objects.get().comando["sequence"], 1)
        # Protocol has no global sequence uniqueness: same sequence on another UUID
        # remains a separate operation, just as existing observation commands.
        command = copy.deepcopy(self.command)
        command["operation_id"] = command["aggregate_id"] = str(uuid4())
        self.assertEqual(self.send(command).status_code, 200)
        self.assertEqual(Venda.objects.count(), 2)

    def test_body_limit_and_no_store(self):
        response = self.send()
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.post(
            "/api/offline/observations/", "x" * 20001,
            content_type="application/json").status_code, 413)

    def test_autenticacao_permissao_csrf_e_no_store(self):
        self.assertEqual(self.send(client=Client()).status_code, 401)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(self.send(client=csrf_client).status_code, 403)
        self.user.user_permissions.clear()
        self.assertEqual(self.send().status_code, 403)
        self.assertFalse(OperacaoSincronizacao.objects.exists())


class SaleCreationCommitTests(TransactionTestCase):
    def test_resposta_perdida_apos_commit_real_e_retry_sem_efeitos(self):
        user, _, product, command = fixtures()
        command["payload"].update(tipo_pagamento="A vista", origem_recebimento={"caixa": "8", "banco": "12"})
        self.client.force_login(user)
        body = json.dumps({**command, "payload_hash": command_hash(command)})
        # JsonResponse is built after process_operation has committed. Simulate
        # loss at that boundary, outside TestCase's surrounding transaction.
        with patch("offline.views.JsonResponse", side_effect=RuntimeError("Resposta perdida apos commit")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/offline/observations/", body, content_type="application/json")
        self.assertFalse(connection.in_atomic_block)
        operation = OperacaoSincronizacao.objects.get(status="confirmada")
        receipt = operation.resultado
        models = (Venda, ItemVenda, EventoVenda, MovimentoFinanceiro, ContaReceber, ContaFinanceira, DespesaDiaria)
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(EventoVenda.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        product.refresh_from_db()
        self.assertEqual(product.quantidade, Decimal("8"))
        replay = self.client.post("/api/offline/observations/", body, content_type="application/json")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), receipt)
        self.assertEqual({model: list(model.objects.order_by("pk").values()) for model in models}, before)
        product.refresh_from_db()
        self.assertEqual(product.quantidade, Decimal("8"))
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL: set OFFLINE_TEST_DATABASE_URL")
class SaleCreationPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.command = fixtures()

    def concurrent(self, commands):
        barrier = Barrier(2)
        def submit(command):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                validated = validate_command({**command, "payload_hash": command_hash(command)}, user, "offline-isolated-tests")
                barrier.wait(timeout=10)
                return process_operation(validated, user)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, command) for command in commands]
            return [future.result(timeout=30) for future in futures]

    def test_mesmo_uuid_concorrente_cria_uma_venda_e_um_financeiro(self):
        self.command["payload"]["tipo_pagamento"] = "A vista"
        results = self.concurrent([self.command, copy.deepcopy(self.command)])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][1], 200)
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(EventoVenda.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 1)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("8"))

    def test_mesmo_uuid_concorrente_conteudo_diferente_preserva_vencedor(self):
        changed = copy.deepcopy(self.command)
        changed["payload"]["itens"][0]["quantidade"] = "3"
        results = self.concurrent([self.command, changed])
        self.assertEqual(sorted(result[1] for result in results), [200, 409])
        winner = next(result[0] for result in results if result[1] == 200)
        self.assertEqual(OperacaoSincronizacao.objects.get().resultado, winner)
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(ContaReceber.objects.count(), 1)
        self.assertEqual(EventoVenda.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal("10") - ItemVenda.objects.get().quantidade)
