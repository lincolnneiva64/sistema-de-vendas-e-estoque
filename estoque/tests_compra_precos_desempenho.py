"""Complete synthetic POST benchmarks, including the sale-price path in the trace."""
from collections import Counter
from datetime import date
from decimal import Decimal
import json
import os
from time import perf_counter, sleep
from unittest.mock import patch
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.models.fields.related_descriptors import ReverseOneToOneDescriptor
from django.db.backends.utils import CursorWrapper
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque import views
from estoque.models import (Compra, Fornecedor, Produto, ItemCompra, ContaPagar,
                            GrupoProdutoVinculado, MembroGrupoProduto, AlteracaoPrecoVinculado)
from estoque.services.precos_compra import aplicar_precos_compra
from estoque.services.precos_vinculados import diagnosticar_grupo
from estoque.tests_precos_vinculados import autorizar_operador

if os.environ.get("PRECO_TIMEOUT_BASELINE") == "1":
    import ast
    import subprocess
    from estoque.services import precos_compra
    for module, path, names in [
        (views, "estoque/views.py", {"_finalizar_compra_com_financeiro", "_atualizar_custos_produtos_compra", "_atualizar_precos_venda_produtos_compra"}),
        (precos_compra, "estoque/services/precos_compra.py", {"aplicar_precos_compra"}),
    ]:
        source = subprocess.check_output(["git", "show", "4b8953c:" + path]).decode("utf-8")
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "4b8953c:" + path, "exec"), module.__dict__)
    aplicar_precos_compra = precos_compra.aplicar_precos_compra


@override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=10000)
class CompraPrecosDesempenhoTests(TransactionTestCase):
    def setUp(self):
        self.user = autorizar_operador(get_user_model().objects.create_user(username="benchmark-precos"))
        self.client.force_login(self.user)

    def fixture(self, n, mode):
        supplier = Fornecedor.objects.create(nome="Fornecedor sintetico precos")
        products = Produto.objects.bulk_create([Produto(nome=f"Produto sintetico {i}",
            quantidade=10, preco_compra=5, preco_vista=100, preco_prazo=110,
            unidade_compra="CX", unidade_venda_1="CX", unidade_venda_2="UN",
            vende_fracionado=True, fator_conversao=10, preco_compra_fracionado=Decimal("0.50"),
            preco_vista_fracionado=10, preco_prazo_fracionado=11) for i in range(n)])
        purchase = Compra.objects.create(fornecedor=supplier, data_compra=date(2026, 10, 22),
            status=Compra.STATUS_RASCUNHO, total=Decimal("2153.11"))
        ItemCompra.objects.bulk_create([ItemCompra(compra=purchase, produto=p, quantidade=1,
            unidade="CX", preco_unitario=10, valor_total=10) for p in products])
        payload = {"acao_compra": "confirmar_financeiro", "fornecedor_id": str(supplier.pk),
            "data_compra": "2026-10-22", "tipo_pagamento": "aprazo", "data_vencimento": "2026-11-22",
            "valor_cobrado": "2153,11", "produto_id[]": [str(p.pk) for p in products],
            "quantidade[]": ["1"] * n, "unidade[]": ["CX"] * n, "preco_unitario[]": ["10"] * n}
        if mode != "none":
            payload["atualizar_custo_produto_ids[]"] = [str(p.pk) for p in products]
        if mode in {"sale", "linked"}:
            payload["atualizar_preco_venda_produto_ids[]"] = [str(p.pk) for p in products]
            payload["atualizar_preco_venda_nomes[]"] = ["preco_vista"] * n
            payload["atualizar_preco_venda_valores[]"] = ["120"] * n
        if mode == "linked":
            for start in range(0, n, 5):
                members = products[start:start + 5]
                group = GrupoProdutoVinculado.objects.create(nome=f"Grupo sintetico {start}", precos_regularizados=True)
                MembroGrupoProduto.objects.bulk_create([MembroGrupoProduto(grupo=group, produto=p) for p in members])
                signature = diagnosticar_grupo(members, validar_precos=False)["assinatura_precos"]
                for p in members:
                    payload[f"versao_grupo_produto_{p.pk}"] = "0"
                    payload[f"assinatura_precos_produto_{p.pk}"] = signature
                    payload[f"confirmar_precos_produto_{p.pk}"] = "1"
                    payload[f"destinatarios_precos_produto_{p.pk}"] = [str(m.pk) for m in members]
        return purchase, products, payload

    def measure(self, n, mode, latency=0):
        purchase, products, payload = self.fixture(n, mode)
        sql_seconds = 0
        def observe(execute, sql, params, many, context):
            nonlocal sql_seconds
            start = perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                sql_seconds += perf_counter() - start
        connection.queries_log.clear()  # Keep each case below Django's 9000-query ring buffer.
        original_execute = CursorWrapper._execute
        def delayed_execute(cursor, sql, params, *args, **kwargs):
            if latency:
                sleep(latency)  # Inside driver wrappers, so diagnostics count the simulated round trip.
            return original_execute(cursor, sql, params, *args, **kwargs)
        with CaptureQueriesContext(connection) as queries, connection.execute_wrapper(observe), patch.object(CursorWrapper, '_execute', delayed_execute):
            start = perf_counter()
            response = self.client.post(reverse("estoque:compra_finalizar", args=[purchase.pk]), payload)
            total = perf_counter() - start
        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(purchase.status, Compra.STATUS_FINALIZADA)
        ids = [p.pk for p in products]
        self.assertEqual(Produto.objects.filter(pk__in=ids, quantidade=11).count(), n)
        self.assertEqual(ContaPagar.objects.get(compra=purchase).valor_original, Decimal("2153.11"))
        if mode != "none":
            self.assertEqual(Produto.objects.filter(pk__in=ids, preco_compra=10, preco_compra_fracionado=1).count(), n)
        if mode in {"sale", "linked"}:
            self.assertEqual(Produto.objects.filter(pk__in=ids, preco_vista=120).count(), n)
        if mode == "linked":
            self.assertEqual(len(purchase.alteracoes_precos_vinculados), n // 5)
        result = {"items": n, "mode": mode, "simulated_rtt_ms": latency * 1000,
            "queries": len(queries), "total_seconds": round(total, 4), "driver_seconds": round(sql_seconds, 4),
            "outside_driver_seconds": round(total - sql_seconds, 4),
            "catalog_selects": sum('FROM "estoque_controleprecosvinculados"' in q["sql"] for q in queries),
            "membership_selects": sum('FROM "estoque_membrogrupoproduto"' in q["sql"] for q in queries),
            "commands": dict(Counter(q["sql"].split()[0] for q in queries))}
        print("PRECO_POST_METRIC " + json.dumps(result), flush=True)
        if os.environ.get('PRECO_TIMEOUT_BASELINE') != '1':
            slope, fixed = {'none': (9, 60), 'cost': (11, 80), 'sale': (13, 90), 'linked': (15, 95)}[mode]
            self.assertLessEqual(len(queries), slope * n + fixed)
        return result

    def test_post_15_30_100_sem_preco_com_custo_com_venda_e_vinculados(self):
        for n in (15, 30, 100):
            for mode in ("none", "cost", "sale", "linked"):
                with self.subTest(n=n, mode=mode):
                    self.measure(n, mode)

    def test_15_itens_venda_latencia_sintetica_explicita(self):
        self.measure(15, "sale", latency=0.005)

    @skipUnless(os.environ.get('PRECO_TIMEOUT_RTT_50MS') == '1', 'Run explicit 50ms round-trip experiment separately')
    def test_15_itens_50ms_por_comando_pode_exceder_30s(self):
        self.measure(15, 'sale', latency=0.05)

    def test_consulta_implicita_no_getattr_do_traceback(self):
        purchase, _, _ = self.fixture(15, "sale")
        item = purchase.itens.first()
        counts = []
        original = ReverseOneToOneDescriptor.__get__
        def observe(descriptor, instance, cls=None):
            if instance is not None and descriptor.related.get_accessor_name() == "vinculo_grupo":
                with CaptureQueriesContext(connection) as queries:
                    try:
                        return original(descriptor, instance, cls)
                    finally:
                        counts.append(len(queries))
            return original(descriptor, instance, cls)
        with patch.object(ReverseOneToOneDescriptor, "__get__", observe):
            aplicar_precos_compra(item, {"preco_vista": Decimal("120")}, operador=self.user)
        print("PRECO_TRACEBACK_GETATTR " + json.dumps(counts), flush=True)
        self.assertTrue(counts)
        if os.environ.get("PRECO_TIMEOUT_BASELINE") != "1":
            self.assertEqual(sum(counts), 0)
