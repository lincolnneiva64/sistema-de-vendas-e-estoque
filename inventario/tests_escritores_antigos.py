"""Roles sem superuser e escritores de duas versões no mesmo banco descartável."""
import ast
from contextlib import contextmanager
import subprocess
import sys
import types
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction, DatabaseError
from django.test import TransactionTestCase
from estoque import views
from estoque.models import Cliente, ContaReceber, ContaPagar
from estoque.services.vendas import criar_ou_atualizar_venda
from . import tests_auditoria_publicacao
from .models import MovimentoObservado


@skipUnless(connection.vendor == "postgresql", "Exige PostgreSQL descartável")
class EscritoresAntigosTests(TransactionTestCase):
    setUp = tests_auditoria_publicacao.PublicacaoTests.setUp
    compra = tests_auditoria_publicacao.PublicacaoTests.compra

    @contextmanager
    def roles(self):
        sufixo = uuid4().hex[:12]
        antigo, novo, observador = [prefixo + sufixo for prefixo in ("inv_antigo_", "inv_novo_", "inv_diario_")]
        with connection.cursor() as cursor:
            for role in (antigo, novo, observador):
                cursor.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT')
                cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            cursor.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'")
            for (table,) in cursor.fetchall():
                qtable = connection.ops.quote_name(table)
                if not table.startswith("inventario_"):
                    cursor.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON public.{qtable} TO "{antigo}", "{novo}"')
                else:
                    cursor.execute(f'GRANT SELECT ON public.{qtable} TO "{novo}"')
                    if table not in {"inventario_sequenciaestoque", "inventario_movimentoobservado"}:
                        cursor.execute(f'GRANT INSERT, UPDATE, DELETE ON public.{qtable} TO "{novo}"')
            cursor.execute(f'GRANT UPDATE (origem, referencia) ON inventario_movimentoobservado TO "{novo}"')
            cursor.execute("SELECT sequencename FROM pg_catalog.pg_sequences WHERE schemaname='public'")
            for (sequence,) in cursor.fetchall():
                qsequence = connection.ops.quote_name(sequence)
                if not sequence.startswith("inventario_"):
                    cursor.execute(f'GRANT USAGE, SELECT ON SEQUENCE public.{qsequence} TO "{antigo}", "{novo}"')
                elif sequence != "inventario_movimentoobservado_id_seq":
                    cursor.execute(f'GRANT USAGE, SELECT ON SEQUENCE public.{qsequence} TO "{novo}"')
            cursor.execute(f'GRANT SELECT, INSERT, UPDATE ON inventario_sequenciaestoque, inventario_movimentoobservado TO "{observador}"')
            cursor.execute(f'GRANT USAGE, SELECT ON SEQUENCE inventario_movimentoobservado_id_seq TO "{observador}"')
            cursor.execute('ALTER FUNCTION public.inventario_observar_quantidade_fn() OWNER TO "' + observador + '"')
            try:
                yield antigo, novo, observador
            finally:
                cursor.execute("RESET ROLE")
                cursor.execute('ALTER FUNCTION public.inventario_observar_quantidade_fn() OWNER TO CURRENT_USER')
                for role in (antigo, novo, observador):
                    cursor.execute(f'DROP OWNED BY "{role}"')
                    cursor.execute(f'DROP ROLE "{role}"')

    @contextmanager
    def role(self, role):
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{role}"')
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")

    def anteriores(self):
        # Compila o serviço inteiro e as funções originais de views, sem hooks.
        fonte = subprocess.check_output(["git", "show", "f07eba0:estoque/views.py"], encoding="utf-8")
        arvore = ast.parse(fonte)
        namespace = dict(vars(views))
        funcoes = [n for n in arvore.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        exec(compile(ast.Module(body=funcoes, type_ignores=[]), "git:f07eba0:views", "exec"), namespace)
        antigos = types.ModuleType("estoque.views")
        antigos.__dict__.update(namespace)
        nome = "estoque.services._inventario_teste_vendas_antigas"
        vendas = types.ModuleType(nome)
        vendas.__package__ = "estoque.services"
        fonte = subprocess.check_output(["git", "show", "f07eba0:estoque/services/vendas.py"], encoding="utf-8")
        with patch.dict(sys.modules, {nome: vendas}):
            exec(compile(fonte, "git:f07eba0:vendas", "exec"), vendas.__dict__)
        return antigos, vendas

    def test_venda_compra_antigas_e_novas_com_roles_restritas(self):
        antigos, vendas = self.anteriores()
        cliente = Cliente.objects.create(nome="Cliente fictício entre versões")
        self.produto.unidade_compra = "UN"
        self.produto.vende_fracionado = False
        self.produto.save()
        dados = {"cliente_id": cliente.pk, "data_venda": "2026-10-17", "data_vencimento": "2026-11-17",
            "tipo_pagamento": "A prazo", "operador": "Operador fictício", "itens": [{
                "produto_id": self.produto.pk, "produto_nome": self.produto.nome,
                "quantidade": "1", "unidade": "UN", "preco_unitario": "15"}]}
        compra_antiga, compra_nova = self.compra(), self.compra()
        with self.roles() as (antigo, novo, _):
            with self.role(antigo), patch.dict(sys.modules, {"estoque.views": antigos}):
                vendas.criar_ou_atualizar_venda(usuario=None, dados=dados)
                antigos._finalizar_compra_com_financeiro(compra_antiga)
                antigos._finalizar_compra_com_financeiro(compra_antiga)
            with self.role(novo):
                criar_ou_atualizar_venda(usuario=None, dados=dados)
                views._finalizar_compra_com_financeiro(compra_nova)
                views._finalizar_compra_com_financeiro(compra_nova)
                with self.assertRaises(DatabaseError):
                    with connection.cursor() as cursor:
                        cursor.execute("UPDATE inventario_movimentoobservado SET antes=0")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 12)
        self.assertEqual(ContaReceber.objects.filter(venda__cliente=cliente).count(), 2)
        self.assertEqual(list(ContaReceber.objects.filter(venda__cliente=cliente).order_by("pk").values_list("valor_original", flat=True)), [15, 15])
        self.assertEqual(list(ContaPagar.objects.order_by("pk").values_list("valor_original", flat=True)), [20, 20])
        self.assertEqual(list(MovimentoObservado.objects.order_by("sequencia").values_list("origem", flat=True)),
            ["nao_identificada", "nao_identificada", "venda", "compra"])

    def test_falha_do_diario_reverte_compra_sem_sucesso_silencioso(self):
        compra = self.compra()
        with self.roles() as (antigo, _, observador):
            with connection.cursor() as cursor:
                cursor.execute(f'REVOKE INSERT ON inventario_movimentoobservado FROM "{observador}"')
            with self.role(antigo), self.assertRaises(DatabaseError):
                views._finalizar_compra_com_financeiro(compra)
        self.produto.refresh_from_db()
        compra.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(compra.estoque_entrada_realizada)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())

    def test_search_path_temporario_nao_desvia_observacao(self):
        with self.roles() as (antigo, _, __), self.role(antigo):
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE inventario_movimentoobservado (produto_id bigint)")
                try:
                    cursor.execute("SET search_path=pg_temp,public")
                    cursor.execute("UPDATE public.estoque_produto SET quantidade=11 WHERE id=%s", [self.produto.pk])
                    cursor.execute("SELECT count(*) FROM pg_temp.inventario_movimentoobservado")
                    self.assertEqual(cursor.fetchone()[0], 0)
                finally:
                    cursor.execute("SET search_path=public")
                    cursor.execute("DROP TABLE pg_temp.inventario_movimentoobservado")
        self.assertEqual(MovimentoObservado.objects.get().depois, 11)

    def test_funcao_nao_e_executavel_pelo_publico_e_fixa_search_path(self):
        with self.roles() as (antigo, _, __), connection.cursor() as cursor:
            cursor.execute("SELECT prosecdef, proconfig, has_function_privilege(%s, oid, 'EXECUTE') FROM pg_catalog.pg_proc WHERE oid='public.inventario_observar_quantidade_fn()'::regprocedure", [antigo])
            definidor, config, pode_executar = cursor.fetchone()
            self.assertTrue(definidor)
            self.assertIn("search_path=pg_catalog", config)
            self.assertFalse(pode_executar)
