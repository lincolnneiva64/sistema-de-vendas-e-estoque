"""Ensaios adicionais de publicação. Exclusivamente banco descartável de testes."""
import ast
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction, DatabaseError
from django.test import TransactionTestCase, Client
from django.urls import reverse

from estoque import views
from estoque.models import Compra, ItemCompra, ContaPagar, MovimentacaoEstoqueManual
from . import services
from .models import MovimentoObservado, DecisaoAjuste
from . import tests_postgresql


@skipUnless(connection.vendor == "postgresql", "Exige PostgreSQL descartável")
class PublicacaoTests(TransactionTestCase):
    # Reutiliza somente a preparação, sem repetir os testes de venda existentes.
    setUp = tests_postgresql.ConcorrenciaPostgresqlTests.setUp
    executar = tests_postgresql.ConcorrenciaPostgresqlTests.executar

    def compra(self):
        compra = Compra.objects.create(data_compra=date(2026, 10, 17), total=20,
            tipo_pagamento="aprazo", data_vencimento=date(2026, 11, 17), status=Compra.STATUS_RASCUNHO)
        ItemCompra.objects.create(compra=compra, produto=self.produto,
            quantidade=2, preco_unitario=10, valor_total=20, unidade="CX")
        return compra

    def conferir_compra(self, compra):
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 12)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        conta = ContaPagar.objects.get(compra=compra)
        self.assertEqual((conta.valor_original, conta.valor_em_aberto), (20, 20))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 1)
        views._finalizar_compra_com_financeiro(compra)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 1)

    def test_compra_em_curso_antes_ok_e_retry_preservam_financeiro(self):
        compra = self.compra()
        travado, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        original = views.identificar
        def observar(*args, **kwargs):
            original(*args, **kwargs)
            travado.set()
            self.assertTrue(liberar.wait(10))
        def ok():
            iniciou.set()
            resultado = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "12", "0", True)
            terminou.set()
            return resultado
        with patch("estoque.views.identificar", side_effect=observar), ThreadPoolExecutor(2) as pool:
            primeiro = pool.submit(self.executar, lambda: views._finalizar_compra_com_financeiro(compra))
            self.assertTrue(travado.wait(10))
            segundo = pool.submit(self.executar, ok)
            try:
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.25))
            finally:
                liberar.set()
            primeiro.result(15)
            rodada = segundo.result(15)
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (12, 1))
        self.assertFalse(services.posteriores(rodada).exists())
        self.conferir_compra(compra)

    def test_compra_aguarda_ok_e_fica_posterior(self):
        compra = self.compra()
        travado, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        original = services.evento
        def observar(*args, **kwargs):
            if args[1] == "produto_ok":
                travado.set()
                self.assertTrue(liberar.wait(10))
            return original(*args, **kwargs)
        def comprar():
            iniciou.set()
            views._finalizar_compra_com_financeiro(compra)
            terminou.set()
        with patch("inventario.services.evento", side_effect=observar), ThreadPoolExecutor(2) as pool:
            primeiro = pool.submit(self.executar, lambda: services.salvar_contagem(
                self.acesso.pk, self.rodada.pk, 0, "10", "0", True))
            self.assertTrue(travado.wait(10))
            segundo = pool.submit(self.executar, comprar)
            try:
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.25))
            finally:
                liberar.set()
            rodada = primeiro.result(15)
            segundo.result(15)
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (10, 0))
        self.assertEqual(services.posteriores(rodada).get().origem, "compra")
        self.conferir_compra(compra)

    def test_escritor_antigo_nao_precisa_grants_nos_objetos_do_trigger(self):
        role = "inventario_legacy_" + uuid4().hex
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE ROLE "{role}"')
            try:
                cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
                cursor.execute(f'GRANT SELECT, UPDATE ON estoque_produto TO "{role}"')
                cursor.execute(f'SET ROLE "{role}"')
                try:
                    cursor.execute("UPDATE estoque_produto SET quantidade=11 WHERE id=%s", [self.produto.pk])
                    with self.assertRaises(DatabaseError):
                        cursor.execute("SELECT * FROM inventario_movimentoobservado")
                finally:
                    cursor.execute("RESET ROLE")
                self.produto.refresh_from_db()
                self.assertEqual(self.produto.quantidade, 11)
                self.assertEqual(MovimentoObservado.objects.get().origem, "nao_identificada")
            finally:
                cursor.execute("RESET ROLE")
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE "{role}"')

    def test_helpers_originais_f07eba0_operam_no_schema_novo(self):
        # Compila as funções originais obtidas do Git. Modelos de estoque não mudaram.
        source = subprocess.check_output(["git", "show", "f07eba0:estoque/views.py"], encoding="utf-8")
        nomes = {"_baixar_estoque_produto", "_devolver_estoque_produto", "_finalizar_compra_com_financeiro"}
        arvore = ast.parse(source)
        modulo = ast.Module(body=[n for n in arvore.body if isinstance(n, ast.FunctionDef) and n.name in nomes], type_ignores=[])
        namespace = dict(vars(views))
        exec(compile(modulo, "git:f07eba0:estoque/views.py", "exec"), namespace)
        with transaction.atomic():
            namespace["_baixar_estoque_produto"](self.produto.pk, 1, unidade="CX")
            namespace["_devolver_estoque_produto"](self.produto.pk, 1, unidade="CX")
        compra = self.compra()
        namespace["_finalizar_compra_com_financeiro"](compra)
        namespace["_finalizar_compra_com_financeiro"](compra)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 12)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(list(MovimentoObservado.objects.values_list("origem", flat=True)), ["nao_identificada"] * 3)

    def test_post_aprovar_inventario_bloqueado_mesmo_com_permissao_e_csrf(self):
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "0", True)
        services.revisar(self.usuario, rodada.pk, "Contagem fictícia verificada")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.usuario)
        url = reverse("inventario:detalhe", args=[self.inventario.pk])
        self.assertEqual(client.get(url).status_code, 200)
        resposta = client.post(url, {"acao": "aprovar", "rodada": rodada.pk, "motivo": "Teste",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertContains(resposta, "desabilitada")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(DecisaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_rota_legada_recusa_operador_apenas_identificado_por_nome(self):
        self.funcionario.pode_operar_sistema = True
        self.funcionario.save(update_fields=["pode_operar_sistema"])
        client = Client()
        url = reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk])
        resposta = client.get(url, {"operador": self.funcionario.nome})
        self.assertEqual(resposta.status_code, 403)
        self.assertNotIn("produto", resposta.json())
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
