"""Synthetic purchases only; run via validar_compra_timeout_pg."""
from datetime import date
from decimal import Decimal
import json
import os
from time import perf_counter, sleep
from unittest.mock import patch

from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.contrib.auth import get_user_model
from django.urls import reverse
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from estoque import views
from estoque.models import Compra, ContaPagar, Fornecedor, ItemCompra, Produto, ProdutoFornecedor
from inventario.models import MovimentoObservado

# Compare the identical fixture against the actual published implementation.
# No checkout changes, operational settings or database imports are involved.
if os.environ.get("COMPRA_TIMEOUT_BASELINE") == "1":
    import ast
    import subprocess
    source = subprocess.check_output(["git", "show", "ed16f09:estoque/views.py"]).decode("utf-8")
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "_finalizar_compra_com_financeiro")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "ed16f09:estoque/views.py", "exec"), views.__dict__)


class CompraTimeoutTests(TransactionTestCase):
    def fixture(self, n=30):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor sintetico 219")
        produtos = Produto.objects.bulk_create([
            Produto(nome=f"Sintetico {i}", quantidade=10, preco_compra=5,
                    preco_vista=100, preco_prazo=110, unidade_compra="CX",
                    vende_fracionado=True, fator_conversao=10,
                    preco_compra_fracionado=Decimal("0.50"),
                    preco_vista_fracionado=10, preco_prazo_fracionado=11)
            for i in range(n)
        ])
        compra = Compra.objects.create(fornecedor=fornecedor, data_compra=date(2026, 10, 22),
            data_vencimento=date(2026, 11, 22), tipo_pagamento="aprazo",
            total=Decimal("2153.11"), total_produtos=n * 10,
            ajuste_total=Decimal("2153.11") - n * 10, status=Compra.STATUS_RASCUNHO)
        ItemCompra.objects.bulk_create([ItemCompra(compra=compra, produto=p, quantidade=1,
            preco_unitario=10, valor_total=10, unidade="CX") for p in produtos])
        return compra, produtos

    def test_desempenho_post_completo(self):
        from estoque.tests_precos_vinculados import autorizar_operador
        user = autorizar_operador(get_user_model().objects.create_user(username="timeout-sintetico"))
        self.client.force_login(user)
        for n, custos in [(30, False), (100, True)]:
            compra, produtos = self.fixture(n)
            payload = {
                "acao_compra": "confirmar_financeiro", "fornecedor_id": str(compra.fornecedor_id),
                "data_compra": "2026-10-22", "tipo_pagamento": "aprazo",
                "data_vencimento": "2026-11-22", "valor_cobrado": "2153,11",
                "produto_id[]": [str(p.pk) for p in produtos],
                "quantidade[]": ["1"] * n, "preco_unitario[]": ["10"] * n,
                "unidade[]": ["CX"] * n,
                "atualizar_custo_produto_ids[]": [str(p.pk) for p in produtos] if custos else [],
            }
            with CaptureQueriesContext(connection) as captured:
                start = perf_counter()
                response = self.client.post(reverse("estoque:compra_finalizar", args=[compra.pk]), payload)
                elapsed = perf_counter() - start
            self.assertEqual(response.status_code, 302)
            compra.refresh_from_db()
            self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
            self.assertEqual(compra.total, Decimal("2153.11"))
            print("COMPRA_POST_METRIC " + json.dumps({"items": n, "costs": custos,
                "queries": len(captured), "seconds": round(elapsed, 4)}), flush=True)

    def test_desempenho(self):
        for n, custos in [(1, False), (30, False), (100, False), (30, True), (100, True)]:
            compra, produtos = self.fixture(n)
            with CaptureQueriesContext(connection) as captured:
                start = perf_counter()
                views._finalizar_compra_com_financeiro(compra,
                    atualizar_custo_produto_ids={p.pk for p in produtos} if custos else set())
                elapsed = perf_counter() - start
            print("COMPRA_METRIC " + json.dumps({"items": n, "costs": custos,
                "queries": len(captured), "seconds": round(elapsed, 4),
                "sql_seconds": round(sum(float(q["time"]) for q in captured), 4)}), flush=True)
            if os.environ.get("COMPRA_TIMEOUT_BASELINE") != "1":
                # A real query budget; time is reported, not asserted across hosts.
                self.assertLessEqual(len(captured), (26 if custos else 9) * n + 25)
            compra.refresh_from_db()
            self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
            self.assertEqual(ContaPagar.objects.get(compra=compra).valor_original, Decimal("2153.11"))
            self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in produtos], quantidade=11).count(), n)
            self.assertEqual(MovimentoObservado.objects.filter(produto_id__in=[p.pk for p in produtos], origem="compra").count(), n)
            if custos:
                self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in produtos], preco_compra_fracionado=1).count(), n)

    def test_systemexit_reverte_inclusive_financeiro_e_trigger(self):
        compra, produtos = self.fixture()
        original = views._criar_contas_pagar_compra
        def falhar(compra):
            original(compra)
            raise SystemExit(1)
        with patch.object(views, "_criar_contas_pagar_compra", side_effect=falhar):
            with self.assertRaises(SystemExit):
                views._finalizar_compra_com_financeiro(compra, atualizar_custo_produto_ids={p.pk for p in produtos})
        compra.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertFalse(compra.estoque_entrada_realizada)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(ProdutoFornecedor.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(ItemCompra.objects.exclude(alteracoes_precos={}).exists())
        self.assertEqual(Produto.objects.filter(quantidade=10, preco_compra=5).count(), 30)

    def test_duas_finalizacoes_concorrentes(self):
        compra, produtos = self.fixture()
        barrier = Barrier(2)
        def finalizar():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                views._finalizar_compra_com_financeiro(compra)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: finalizar(), range(2)))
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(Produto.objects.filter(quantidade=11).count(), 30)
        self.assertEqual(MovimentoObservado.objects.count(), 30)

    def test_upsert_preserva_metadados_fornecedor_e_precos(self):
        compra, produtos = self.fixture(1)
        vinculo = ProdutoFornecedor.objects.create(produto=produtos[0], fornecedor=compra.fornecedor,
            ativo=False, codigo_produto_fornecedor="PRESERVAR", observacao="Historico")
        criado = vinculo.criado_em
        views._finalizar_compra_com_financeiro(compra)
        vinculo.refresh_from_db()
        self.assertFalse(vinculo.ativo)
        self.assertEqual(vinculo.codigo_produto_fornecedor, "PRESERVAR")
        self.assertEqual(vinculo.observacao, "Historico")
        self.assertEqual(vinculo.criado_em, criado)
        self.assertEqual(vinculo.ultimo_preco_compra, 10)
        produtos[0].refresh_from_db()
        self.assertEqual(produtos[0].preco_compra, 5)
        self.assertEqual(produtos[0].preco_vista, 100)
        self.assertEqual(produtos[0].fator_conversao, 10)

    def test_mede_espera_no_mutex_catalogo(self):
        from estoque.models import ControlePrecosVinculados
        from estoque.services.precos_vinculados import bloquear_catalogo
        ControlePrecosVinculados.objects.get_or_create(pk=1)
        compra, _ = self.fixture(1)
        locked, release = Event(), Event()
        def segurar():
            close_old_connections()
            try:
                with bloquear_catalogo():
                    locked.set()
                    release.wait(timeout=10)
            finally:
                close_old_connections()
        def finalizar():
            close_old_connections()
            try:
                start = perf_counter()
                views._finalizar_compra_com_financeiro(compra)
                return perf_counter() - start
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(segurar)
            self.assertTrue(locked.wait(timeout=10))
            worker = pool.submit(finalizar)
            try:
                sleep(0.4)
                self.assertFalse(worker.done())
            finally:
                release.set()
            holder.result(timeout=10)
            elapsed = worker.result(timeout=10)
        print(f"COMPRA_LOCK_WAIT seconds={elapsed:.4f}", flush=True)
        self.assertGreaterEqual(elapsed, 0.35)
