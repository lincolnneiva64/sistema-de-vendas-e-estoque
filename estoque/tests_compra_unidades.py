"""Synthetic 15-line purchase; no operational records or settings."""
import ast
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, LiveServerTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque import views
from estoque.models import (Compra, ContaPagar, Fornecedor, ItemCompra, ItemListaCompraFornecedor,
                            ListaCompraFornecedor, MovimentoFinanceiro, Produto, ProdutoFornecedor)
from estoque.tests_precos_vinculados import autorizar_operador
from inventario.models import MovimentoObservado
from offline.browser_support import Chrome


def publicado(nome):
    source = subprocess.check_output(["git", "show", "d2348fd:estoque/views.py"]).decode("utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == nome)
    namespace = dict(views.__dict__)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "d2348fd:estoque/views.py", "exec"), namespace)
    return namespace[nome]


class UnidadesFixture:
    def setUp(self):
        self.user = autorizar_operador(get_user_model().objects.create_user(username="unidades-sinteticas"))
        self.client.force_login(self.user)
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor sintetico")
        self.lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,
            data_lista=date(2026, 10, 22), data_inicio_periodo=date(2026, 10, 1),
            data_fim_periodo=date(2026, 10, 22), total_lista=2180)
        self.examples = [
            ("Micos Guarana sintetico 12/200Ml", "PCT", 12, "3", "100", "36"),
            ("Tixan Primavera sintetico 24/400G", "FD", 24, "1", "120", "24"),
            ("Tixan Maciez sintetico 24/400G", "FD", 24, "0.5", "140", "12"),
            ("Oleo Siol sintetico 20/500Ml", "CX", 20, "1", "150", "20"),
            ("Acucar Itamarati sintetico 30/1Kg", "FD", 30, "3", "80", "90"),
        ]
        self.produtos = []
        for nome, unidade, fator, qtd, preco, _ in self.examples + [
            (f"Controle sintetico {i}", "UN", 1, "1", "130", "1") for i in range(10)
        ]:
            p = Produto.objects.create(nome=nome, unidade_compra=unidade, unidade_venda_2="UN" if fator > 1 else "",
                vende_fracionado=fator > 1, fator_conversao=fator, quantidade=10,
                preco_compra=Decimal(preco) / 2, preco_compra_fracionado=1,
                preco_vista=300, preco_prazo=310, preco_vista_fracionado=50, preco_prazo_fracionado=60)
            self.produtos.append(p)
            ItemListaCompraFornecedor.objects.create(lista=self.lista, produto=p,
                quantidade_sugerida=Decimal(qtd), quantidade_final=Decimal(qtd), unidade=unidade,
                preco_compra=Decimal(preco), preco_unitario=(Decimal(preco) / fator).quantize(Decimal("0.01")),
                total=Decimal(qtd) * Decimal(preco))
        from django.test import RequestFactory
        request = RequestFactory().post("/")
        request.user = self.user
        with transaction.atomic():
            self.compra = views._criar_compra_rascunho_da_lista_fornecedor(self.lista, request)

    def payload(self):
        itens = list(self.compra.itens.order_by("id"))
        return {"acao_compra": "confirmar_financeiro", "fornecedor_id": str(self.fornecedor.pk),
            "data_compra": "2026-10-22", "tipo_pagamento": "aprazo", "data_vencimento": "2026-11-22",
            "valor_cobrado": "2153,11", "produto_id[]": [str(i.produto_id) for i in itens],
            "quantidade[]": [str(i.quantidade) for i in itens], "unidade[]": [i.unidade for i in itens],
            "preco_unitario[]": [str(i.preco_unitario) for i in itens],
            "atualizar_custo_produto_ids[]": [str(i.produto_id) for i in itens]}

    def assert_sem_efeitos(self):
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_RASCUNHO)
        self.assertFalse(self.compra.estoque_entrada_realizada)
        self.assertEqual(Produto.objects.filter(quantidade=10).count(), 15)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(ProdutoFornecedor.objects.exists())
        self.assertFalse(self.compra.itens.exclude(alteracoes_precos={}).exists())
        for item in self.lista.itens.select_related("produto"):
            self.assertEqual(item.produto.preco_compra, item.preco_compra / 2)


class CompraUnidadesTests(UnidadesFixture, TestCase):
    def test_lista_compra_post_15_itens_custos_desconto_financeiro(self):
        self.assertEqual(self.compra.itens.count(), 15)
        self.assertEqual(self.compra.total, Decimal("2180"))
        for item in self.lista.itens.all():
            compra_item = self.compra.itens.get(produto_id=item.produto_id)
            self.assertEqual((compra_item.unidade, compra_item.quantidade, compra_item.preco_unitario),
                             (item.unidade, item.quantidade_final, item.preco_compra))
        with CaptureQueriesContext(connection) as queries:
            start = perf_counter()
            response = self.client.post(reverse("estoque:compra_finalizar", args=[self.compra.pk]), self.payload())
            elapsed = perf_counter() - start
        self.assertEqual(response.status_code, 302)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.compra.total, Decimal("2153.11"))
        self.assertEqual(self.compra.total_produtos, Decimal("2180"))
        self.assertEqual(self.compra.ajuste_total, Decimal("-26.89"))
        self.assertEqual(ContaPagar.objects.get(compra=self.compra).valor_original, Decimal("2153.11"))
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        for p, (_, unidade, fator, qtd, preco, internas) in zip(self.produtos, self.examples):
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10 + Decimal(qtd))
            self.assertEqual((p.quantidade - 10) * fator, Decimal(internas))
            self.assertEqual(views._estoque_disponivel_na_unidade(p, "UN"), (10 + Decimal(qtd)) * fator)
            self.assertEqual(p.preco_compra, Decimal(preco))
            self.assertEqual(p.preco_compra_fracionado, (Decimal(preco) / fator).quantize(Decimal("0.01")))
            self.assertEqual((p.preco_vista, p.preco_prazo, p.fator_conversao), (300, 310, fator))
            observado = MovimentoObservado.objects.get(produto=p)
            self.assertEqual((observado.antes, observado.depois, observado.origem), (10, p.quantidade, "compra"))
            print("UNIDADES_ESTOQUE " + json.dumps({"produto": p.nome, "delta_base": qtd,
                "unidade_base": unidade, "fator": fator, "equivalente_un": internas}), flush=True)
        print(f"UNIDADES_POST queries={len(queries)} seconds={elapsed:.4f}", flush=True)
        self.assertLessEqual(len(queries), 26 * 15 + 65)
        self.client.post(reverse("estoque:compra_finalizar", args=[self.compra.pk]), self.payload())
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 1)
        self.assertEqual(MovimentoObservado.objects.count(), 15)

    def test_publicado_ignora_rotulo_un_sem_multiplicar(self):
        self.compra.itens.update(unidade="UN")
        self.compra.tipo_pagamento = "aprazo"
        self.compra.data_vencimento = date(2026, 11, 22)
        self.compra.save()
        publicado("_finalizar_compra_com_financeiro")(self.compra)
        for p, (_, _, fator, qtd, _, internas) in zip(self.produtos, self.examples):
            p.refresh_from_db()
            self.assertEqual(p.quantidade - 10, Decimal(qtd))
            self.assertEqual((p.quantidade - 10) * fator, Decimal(internas))

    def test_validacao_unidade_nao_adiciona_consultas(self):
        self.compra.tipo_pagamento = "aprazo"
        self.compra.data_vencimento = date(2026, 11, 22)
        self.compra.save()
        original = publicado("_finalizar_compra_com_financeiro")
        results = []
        for label, callback in [("d2348fd", original), ("corrigido", views._finalizar_compra_com_financeiro)]:
            with transaction.atomic():
                with CaptureQueriesContext(connection) as queries:
                    start = perf_counter()
                    callback(self.compra, atualizar_custo_produto_ids={p.pk for p in self.produtos})
                    elapsed = perf_counter() - start
                results.append(len(queries))
                print(f"UNIDADES_PERFORMANCE version={label} queries={len(queries)} seconds={elapsed:.4f}", flush=True)
                transaction.set_rollback(True)  # Restore this synthetic fixture for a fair comparison.
        self.assertEqual(results[0], results[1])
        self.assert_sem_efeitos()

    def test_un_gravado_divergente_bloqueia_post_sem_efeitos(self):
        self.compra.itens.update(unidade="UN")
        antes = list(self.compra.itens.values_list("id", "unidade", "preco_unitario"))
        response = self.client.post(reverse("estoque:compra_finalizar", args=[self.compra.pk]), self.payload())
        self.assertEqual(response.status_code, 302)
        self.assert_sem_efeitos()
        self.assertEqual(list(self.compra.itens.values_list("id", "unidade", "preco_unitario")), antes)
        self.assertEqual(self.compra.total, Decimal("2180"))
        self.assertTrue(any("diverge do estoque" in str(m) for m in response.wsgi_request._messages))

    def test_service_bloqueia_antes_de_atualizar_qualquer_item(self):
        item = self.compra.itens.last()
        item.unidade = "FD"
        item.save(update_fields=["unidade"])
        with self.assertRaisesMessage(ValueError, "diverge do estoque"):
            views._finalizar_compra_com_financeiro(self.compra,
                atualizar_custo_produto_ids={p.pk for p in self.produtos})
        self.assert_sem_efeitos()

    def test_rotulo_vazio_nao_exibe_un_ficticio_e_get_nao_altera_dados(self):
        self.compra.itens.update(unidade="")
        linhas_antes = publicado("_linhas_item_compra")(self.compra)
        self.assertTrue(all(l["unidade"] == "" for l in linhas_antes))
        response = self.client.get(reverse("estoque:compra_editar", args=[self.compra.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["item_linhas"][0]["unidade"], "PCT")
        self.assertNotContains(response, 'placeholder="UN"')
        self.assertEqual(self.compra.itens.filter(unidade="").count(), 15)

    def test_rotulo_un_salvo_nao_e_corrigido_silenciosamente(self):
        self.compra.itens.update(unidade="UN")
        response = self.client.get(reverse("estoque:compra_editar", args=[self.compra.pk]))
        linha = response.context["item_linhas"][0]
        self.assertEqual(linha["unidade"], "UN")
        self.assertEqual(linha["unidade_estoque"], "PCT")
        self.assertTrue(linha["unidade_divergente"])
        self.assertContains(response, "Unidade divergente")
        self.assertEqual(self.compra.itens.filter(unidade="UN").count(), 15)

    def test_payload_lista_manual_usa_campo_existente_e_preserva_snapshot(self):
        p = self.produtos[0]
        self.assertEqual(publicado("_lista_fornecedor_produto_payload")(p)["unidade"], "")
        self.assertEqual(views._lista_fornecedor_produto_payload(p)["unidade"], "PCT")
        item = self.lista.itens.get(produto=p)
        item.unidade = "UN"
        item.save(update_fields=["unidade"])
        self.assertEqual(views._lista_fornecedor_produto_payload(p, item=item)["unidade"], "UN")

    def test_lista_sem_rotulo_gera_compra_com_base_sem_alterar_lista(self):
        self.lista.itens.update(unidade="")
        self.compra.delete()  # Only this test's synthetic draft; one purchase per list.
        from django.test import RequestFactory
        request = RequestFactory().post("/")
        request.user = self.user
        compra = views._criar_compra_rascunho_da_lista_fornecedor(self.lista, request)
        self.assertEqual(compra.itens.first().unidade, "PCT")
        self.assertEqual(self.lista.itens.filter(unidade="").count(), 15)
        self.assertEqual(compra.total, Decimal("2180"))

    def test_post_cria_lista_e_compra_sem_inventar_un_e_sem_mudar_rotulo_explicito(self):
        payload = {"fornecedorId": str(self.fornecedor.pk), "dataInicio": "2026-10-01",
                   "dataFim": "2026-10-22", "linhas": []}
        for i, item in enumerate(self.lista.itens.all()):
            payload["linhas"].append({"produtoId": str(item.produto_id), "sugestao": str(item.quantidade_final),
                "precoCompra": str(item.preco_compra), "precoUnitario": str(item.preco_unitario),
                "total": str(item.total), "unidade": "UN" if i == 1 else ""})
        response = self.client.post(reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload), "gerar_compra": "1"})
        self.assertEqual(response.status_code, 302)
        compra = Compra.objects.exclude(pk=self.compra.pk).get()
        self.assertEqual(compra.itens.get(produto=self.produtos[0]).unidade, "PCT")
        self.assertEqual(compra.itens.get(produto=self.produtos[1]).unidade, "UN")
        self.assertEqual(compra.lista_fornecedor.itens.get(produto=self.produtos[0]).unidade, "PCT")
        self.assertEqual(compra.total, Decimal("2180"))
        self.assert_sem_efeitos()

    def test_cadastro_base_un_nao_permite_inferir_embalagem_pelo_nome(self):
        p = self.produtos[0]
        # A package-like name alone cannot authorize multiplying the quantity.
        Produto.objects.filter(pk=p.pk).update(unidade_compra="UN", unidade_venda_1="UN")
        self.compra.itens.update(unidade="UN")
        with self.assertRaises(ValueError):  # Other package products remain divergent.
            views._finalizar_compra_com_financeiro(self.compra)
        p.refresh_from_db()
        self.assertEqual(p.quantidade, 10)
        self.assertEqual(views._quantidade_estoque_para_unidade_base(p, 3, "UN")[0], 3)


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@skipUnless(Path(CHROME).exists(), "Chrome unavailable")
class CompraUnidadesBrowserTests(UnidadesFixture, LiveServerTestCase):
    def test_reabrir_desktop_mobile_preserva_rotulos_e_distingue_un_gravado(self):
        self.compra.itens.filter(produto=self.produtos[0]).update(unidade="")
        self.compra.itens.filter(produto=self.produtos[1]).update(unidade="UN")
        with TemporaryDirectory(prefix="unidades-compra-", ignore_cleanup_errors=True) as profile:
            browser = Chrome(CHROME, profile).start()
            try:
                tab = browser.tab()
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                for width, mobile in [(1280, False), (390, True)]:
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 1, "mobile": mobile})
                    tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:compra_editar", args=[self.compra.pk])})
                    tab.wait("document.readyState === 'complete' && !!document.getElementById('formCompra')")
                    units = tab.evaluate("Array.from(document.querySelectorAll('#tabelaItensCompra tbody [name=\"unidade[]\"]')).map(e=>({value:e.value,placeholder:e.placeholder}))")
                    self.assertEqual(len(units), 15)
                    self.assertEqual([u["value"] for u in units[:5]], ["PCT", "UN", "FD", "CX", "FD"])
                    self.assertTrue(all(u["placeholder"] != "UN" for u in units))
                    self.assertIn("Unidade divergente", tab.evaluate("document.body.textContent"))
            finally:
                browser.stop()
        self.assertEqual(self.compra.itens.get(produto=self.produtos[0]).unidade, "")
        self.assertEqual(self.compra.itens.get(produto=self.produtos[1]).unidade, "UN")
