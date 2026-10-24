"""Real PostgreSQL DDL and old-version INSERTs; disposable database only."""
from io import StringIO
import importlib
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import CircularDependencyError, InconsistentMigrationHistory
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from estoque.tests_correcao_automatica import Fixture
from estoque.models import Compra
from inventario.management.commands.publicar_cancelamento import Command, COMPRA, LOCK

PREVIOUS = ('estoque', '0134_edicao_compra')


class PublicacaoCompraTests(Fixture, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'test_inventario_disposable')
        super().setUp()
        self.recorder = MigrationRecorder(connection)
        self.addCleanup(self.restore)
        MigrationExecutor(connection).migrate([PREVIOUS])

    def restore(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def gate(self, *args):
        output = StringIO()
        call_command('publicar_cancelamento', *args, stdout=output)
        return output.getvalue()

    def snapshot(self, legacy=False):
        result = {}
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                if legacy and table in {'django_migrations', 'estoque_conciliacaocompra'}:
                    continue
                columns = [c.name for c in connection.introspection.get_table_description(cursor, table)]
                if legacy:
                    columns = [c for c in columns if (table, c) not in {
                        ('estoque_compra', 'conciliacao_automatica'),
                        ('estoque_itemcompra', 'estoque_base_registrado')}]
                cursor.execute('SELECT ' + ', '.join(map(connection.ops.quote_name, columns))
                               + ' FROM ' + connection.ops.quote_name(table))
                result[table] = sorted(map(repr, cursor.fetchall()))
        return result

    def test_apply_only_0135_preserves_records_and_old_and_new_inserts(self):
        before = self.snapshot(legacy=True)
        self.assertIn('APLICAR estoque.0135_conciliacao_compra', self.gate())
        self.assertEqual(before, self.snapshot(legacy=True))
        self.assertEqual(self.recorder.migration_qs.filter(app=COMPRA[0], name=COMPRA[1]).count(), 1)
        with connection.cursor() as cursor:
            cursor.execute('SELECT conciliacao_automatica FROM estoque_compra')
            self.assertTrue(all(value is False for value, in cursor.fetchall()))
            cursor.execute('SELECT estoque_base_registrado FROM estoque_itemcompra')
            self.assertTrue(all(value is None for value, in cursor.fetchall()))
            cursor.execute('SELECT count(*) FROM estoque_conciliacaocompra')
            self.assertEqual(cursor.fetchone()[0], 0)
        # Historical 0134 ORM omits the new column, just as aa5fdb6 does.
        old = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps.get_model('estoque', 'Compra')
        old_purchase = old.objects.create(fornecedor_id=self.fornecedor.pk,
            data_compra=self.compra.data_compra, tipo_pagamento='avista')
        self.assertIs(Compra.objects.get(pk=old_purchase.pk).conciliacao_automatica, False)
        new_purchase = Compra.objects.create(fornecedor=self.fornecedor, data_compra=self.compra.data_compra)
        new_purchase.refresh_from_db()
        self.assertIs(new_purchase.conciliacao_automatica, False)
        self.movement.refresh_from_db()
        self.assertEqual(str(self.movement.valor), '442.20')

    def test_second_execution_has_no_schema_or_record_writes(self):
        self.gate()
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            self.assertIn('Nenhuma pendência', self.gate())
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(
            ('ALTER ', 'CREATE ', 'DROP ', 'INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))

    def test_plan_does_not_write(self):
        before = self.snapshot()
        self.assertIn('APLICAR estoque.0135', self.gate('--plan'))
        self.assertEqual(before, self.snapshot())

    def test_unexpected_migration_blocks_0135_without_writes(self):
        other = ('offline', '0005_validate_local_sale_principal')
        self.recorder.record_unapplied(*other)
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
                self.gate()
            self.assertEqual(before, self.snapshot())
            self.assertNotIn(COMPRA, self.recorder.applied_migrations())
        finally:
            self.recorder.record_applied(*other)

    def test_missing_0134_is_not_implicitly_authorized(self):
        self.recorder.record_unapplied(*PREVIOUS)
        try:
            before = self.snapshot()
            with self.assertRaises(InconsistentMigrationHistory):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            self.recorder.record_applied(*PREVIOUS)

    def test_altered_definition_or_dependency_is_rejected(self):
        migration = importlib.import_module('estoque.migrations.0135_conciliacao_compra').Migration
        cases = [('operations', [migrations.RunSQL('SELECT 1')]),
                 ('dependencies', [('estoque', '0133_parcelas_compra_direta')]),
                 ('atomic', False), ('run_before', [('estoque', '0134_edicao_compra')])]
        for attribute, value in cases:
            with self.subTest(attribute=attribute), patch.object(migration, attribute, value):
                before = self.snapshot()
                with self.assertRaises((CommandError, CircularDependencyError)):
                    self.gate()
                self.assertEqual(before, self.snapshot())

    def test_removing_persistent_default_from_definition_is_rejected(self):
        migration = importlib.import_module('estoque.migrations.0135_conciliacao_compra').Migration
        altered = [migrations.AddField(model_name='compra', name='conciliacao_automatica',
            field=models.BooleanField(default=False, editable=False)), *migration.operations[1:]]
        before = self.snapshot()
        with patch.object(migration, 'operations', altered):
            with self.assertRaisesMessage(CommandError, 'estoque.0135 diverge'):
                self.gate()
        self.assertEqual(before, self.snapshot())

    def test_failure_after_ddl_rolls_back_schema_and_recorder(self):
        before = self.snapshot()
        with patch.object(Command, 'verify_schema', side_effect=CommandError('synthetic verification failure')):
            with self.assertRaisesMessage(CommandError, 'synthetic verification failure'):
                self.gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(COMPRA, self.recorder.applied_migrations())
        self.gate()

    def test_noop_detects_missing_persistent_default(self):
        self.gate()
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE estoque_compra ALTER COLUMN conciliacao_automatica DROP DEFAULT')
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'estrutura/default de estoque.0135'):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE estoque_compra ALTER COLUMN conciliacao_automatica SET DEFAULT false')

    def test_failure_creating_table_rolls_back_both_added_columns(self):
        before = self.snapshot()
        original = connection.SchemaEditorClass.create_model

        def fail(editor, model):
            if model._meta.label_lower == 'estoque.conciliacaocompra':
                raise RuntimeError('synthetic table creation failure')
            return original(editor, model)

        with patch.object(connection.SchemaEditorClass, 'create_model', fail):
            with self.assertRaisesMessage(RuntimeError, 'synthetic table creation failure'):
                self.gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(COMPRA, self.recorder.applied_migrations())

    def test_concurrent_publication_uses_existing_shared_lock(self):
        other = connection.copy(alias='compra-publication-lock')
        try:
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [LOCK])
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'Outra publicação'):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            other.close()
