"""Exercise ONLY pending 0006/0007 on the guarded disposable PostgreSQL schema."""
from importlib import import_module
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from estoque.models import Compra, ItemCompra, Venda, ItemVenda
from .tests_ajustes import AjustesFixture


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL descartável obrigatório')
class MigrationsAjustesTests(AjustesFixture, TransactionTestCase):
    def test_migrations_reais_preservam_dados_e_recuperam_falhas_atomicas(self):
        # Refuse to remove even test tables outside the existing guarded runner.
        from pathlib import Path
        import os
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('data_directory'), host(inet_server_addr())")
            directory, address = cursor.fetchone()
        esperado = os.environ.get('INVENTARIO_CONFIABILIDADE_PG_DATA')
        self.assertTrue(esperado and Path(directory).resolve() == Path(esperado).resolve())
        self.assertEqual(address, '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'inventario_confiabilidade')
        compra = Compra.objects.create(data_compra='2026-10-01')
        ItemCompra.objects.create(compra=compra, produto=self.produto, quantidade=2,
                                  unidade='CX', preco_unitario=0, valor_total=0)
        venda = Venda.objects.create(data_venda='2026-10-02')
        ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=1,
                                 unidade='CX', preco_unitario=0, valor_total=0)
        novas = {'inventario_propostaajuste', 'inventario_eventoajuste', 'inventario_aplicacaoajuste'}
        recorder = MigrationRecorder(connection)
        recorder.ensure_schema()
        antigas = sorted(set(connection.introspection.table_names()) - novas - {'django_migrations'})

        def dados():
            resultado = {}
            with connection.cursor() as cursor:
                for tabela in antigas:
                    # Only hashes are retained: no credentials or operational fixture dump.
                    cursor.execute(f"SELECT count(*), md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY to_jsonb(t)::text), '')) FROM {connection.ops.quote_name(tabela)} t")
                    resultado[tabela] = cursor.fetchone()
            return resultado

        def esquema():
            with connection.cursor() as cursor:
                cursor.execute("""SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                    a.attnotnull FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='public'
                    AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1, a.attnum""", [antigas])
                colunas = cursor.fetchall()
                cursor.execute("""SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
                    FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
                    AND c.relname=ANY(%s) ORDER BY 1,2""", [antigas])
                restricoes = cursor.fetchall()
                cursor.execute("""SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t
                    JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND NOT t.tgisinternal AND c.relname=ANY(%s) ORDER BY 1,2""", [antigas])
                return colunas, restricoes, cursor.fetchall()

        antes, estrutura = dados(), esquema()
        protecoes = import_module('inventario.migrations.0007_auditoria_ajuste_imutavel')
        # Runner created these NEW tables from current models. Remove only these
        # empty fixture tables to reproduce the published pre-0006 baseline.
        with connection.cursor() as cursor:
            cursor.execute(protecoes.REVERSE)
            cursor.execute('DROP TABLE inventario_eventoajuste, inventario_aplicacaoajuste, inventario_propostaajuste')
        executor = MigrationExecutor(connection)
        estado = executor.loader.project_state([('inventario', '0005_acessoinventario_enviada_em_and_more')])
        m6 = executor.loader.get_migration('inventario', '0006_propostas_aplicacoes_ajuste')
        m7 = executor.loader.get_migration('inventario', '0007_auditoria_ajuste_imutavel')
        self.assertTrue(m6.atomic and m7.atomic)
        original = connection.SchemaEditorClass.execute

        def falhar_tabela(editor, sql, params=()):
            if str(sql).startswith('CREATE TABLE "inventario_aplicacaoajuste"'):
                raise RuntimeError('Falha DDL simulada na 0006')
            return original(editor, sql, params)

        try:
            with patch.object(connection.SchemaEditorClass, 'execute', falhar_tabela):
                with self.assertRaisesMessage(RuntimeError, 'Falha DDL simulada'):
                    executor.apply_migration(estado.clone(), m6)
            self.assertFalse(novas & set(connection.introspection.table_names()))
            self.assertNotIn(('inventario', m6.name), recorder.applied_migrations())
            self.assertEqual(dados(), antes)
            estado6 = executor.apply_migration(estado.clone(), m6)
            self.assertTrue(novas <= set(connection.introspection.table_names()))

            def falhar_protecao(editor, sql, params=()):
                resultado = original(editor, sql, params)
                if str(sql).strip().startswith('CREATE FUNCTION public.inventario_auditoria_ajuste_fn'):
                    raise RuntimeError('Falha DDL simulada na 0007 após CREATE')
                return resultado

            with patch.object(connection.SchemaEditorClass, 'execute', falhar_protecao):
                with self.assertRaisesMessage(RuntimeError, 'Falha DDL simulada'):
                    executor.apply_migration(estado6.clone(), m7)
            self.assertNotIn(('inventario', m7.name), recorder.applied_migrations())
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regprocedure('public.inventario_auditoria_ajuste_fn()')")
                self.assertIsNone(cursor.fetchone()[0])
            executor.apply_migration(estado6.clone(), m7)
            self.assertEqual(dados(), antes)
            self.assertEqual(esquema(), estrutura)
            from .ajustes import verificar_execucao
            from django.test import override_settings
            with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
                verificar_execucao()
            with connection.cursor() as cursor:
                cursor.execute("""SELECT tablename, indexname, indexdef FROM pg_indexes
                    WHERE schemaname='public' AND tablename=ANY(%s) ORDER BY tablename,indexname""", [list(novas)])
                indices = cursor.fetchall()
            self.assertEqual(len(indices), 12)
            self.assertEqual(sum('UNIQUE INDEX' in r[2] for r in indices), 6)
            with connection.cursor() as cursor:
                cursor.execute("""SELECT con.contype, count(*) FROM pg_constraint con
                    JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname=ANY(%s)
                    GROUP BY con.contype ORDER BY con.contype""", [list(novas)])
                self.assertEqual(cursor.fetchall(), [('f',8),('p',3),('u',3)])
                cursor.execute("""SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
                    AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped""", [list(novas)])
                self.assertEqual(cursor.fetchone()[0], 26)
            self.assertEqual(self.fresca().esperado, 10)
            self.assertEqual(self.fresca().item.produto.quantidade, 10)
        finally:
            # On failure recover ONLY the disposable fixture schema for teardown.
            existentes = set(connection.introspection.table_names())
            if not novas <= existentes:
                executor.apply_migration(estado.clone(), m6)
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regprocedure('public.inventario_auditoria_ajuste_fn()')")
                if cursor.fetchone()[0] is None:
                    cursor.execute(protecoes.SQL)
