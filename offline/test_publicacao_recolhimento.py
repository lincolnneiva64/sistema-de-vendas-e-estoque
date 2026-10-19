"""Exercise the build gate against the launcher's isolated PostgreSQL only."""
import importlib
from io import StringIO
from unittest.mock import patch
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from locacoes.management.commands.publicar_recolhimento import TARGET, LOCK
from .test_pickup_checklist import PickupFixture


class PublicationGateTests(PickupFixture, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'pickup_disposable')
        self.seed()
        self.recorder = MigrationRecorder(connection)
        self.recorder.ensure_schema()
        self.loader = MigrationLoader(None)
        self.recorder.Migration.objects.bulk_create([
            self.recorder.Migration(app=app, name=name) for app, name in self.loader.disk_migrations
        ])

    def previous_schema(self):
        migration = importlib.import_module('locacoes.migrations.0017_autorizacao_recolhimento').Migration(*TARGET[::-1])
        state = self.loader.project_state()
        state.remove_model('locacoes', 'autorizacaorecolhimento')
        with connection.schema_editor() as editor:
            migration.unapply(state, editor)
        self.recorder.record_unapplied(*TARGET)
        Permission.objects.filter(content_type__app_label='locacoes', codename='autorizar_recolhimento').delete()
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        # Keep the launcher's current-model schema available for flush and other tests.
        if 'locacoes_autorizacaorecolhimento' not in connection.introspection.table_names():
            migration = importlib.import_module('locacoes.migrations.0017_autorizacao_recolhimento').Migration(*TARGET[::-1])
            state = self.loader.project_state()
            state.remove_model('locacoes', 'autorizacaorecolhimento')
            with connection.schema_editor() as editor:
                migration.apply(state, editor)

    def snapshot(self, business_only=False):
        result = {}
        with connection.cursor() as cursor:
            for name in connection.introspection.table_names():
                if business_only and not name.startswith(('locacoes_', 'estoque_', 'offline_', 'inventario_')):
                    continue
                if business_only and name == 'locacoes_autorizacaorecolhimento':
                    continue
                cursor.execute('SELECT * FROM ' + connection.ops.quote_name(name))
                result[name] = sorted(repr(row) for row in cursor.fetchall())
        return result

    def run_gate(self):
        output = StringIO()
        call_command('publicar_recolhimento', stdout=output)
        return output.getvalue()

    def test_only_0017_applies_and_retry_is_noop(self):
        self.previous_schema()
        before = self.snapshot(business_only=True)
        output = self.run_gate()
        self.assertIn('Migration 0017 aplicada', output)
        self.assertEqual(self.snapshot(business_only=True), before)
        self.assertIn(TARGET, self.recorder.applied_migrations())
        self.assertTrue(Permission.objects.filter(content_type__app_label='locacoes',codename='autorizar_recolhimento').exists())
        self.assertFalse(self.user.has_perm('locacoes.autorizar_recolhimento'))
        all_rows = self.snapshot()
        self.assertIn('já aplicada', self.run_gate())
        self.assertEqual(self.snapshot(), all_rows)

    def test_unrelated_pending_migration_blocks_without_any_database_change(self):
        self.previous_schema()
        unexpected = self.loader.graph.leaf_nodes('inventario')[0]
        self.recorder.record_unapplied(*unexpected)
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(self.snapshot(), before)

    def test_missing_0016_dependency_blocks_without_any_database_change(self):
        self.previous_schema()
        for key in self.loader.graph.backwards_plan(('locacoes','0016_locacao_horario_previsto_devolucao')):
            self.recorder.record_unapplied(*key)
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(self.snapshot(), before)

    def test_already_applied_does_not_write_anything(self):
        before = self.snapshot()
        self.assertIn('Nenhuma pendência', self.run_gate())
        self.assertEqual(self.snapshot(), before)

    def test_unexpected_pending_also_blocks_when_0017_is_already_applied(self):
        self.recorder.record_unapplied(*self.loader.graph.leaf_nodes('inventario')[0])
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(self.snapshot(), before)

    def test_failure_after_ddl_rolls_back_schema_and_migration_record(self):
        self.previous_schema()
        before = self.snapshot()
        with patch('locacoes.management.commands.publicar_recolhimento.create_permissions', side_effect=RuntimeError('test rollback')):
            with self.assertRaisesMessage(RuntimeError,'test rollback'):
                self.run_gate()
        self.assertNotIn('locacoes_autorizacaorecolhimento', connection.introspection.table_names())
        self.assertNotIn(TARGET, self.recorder.applied_migrations())
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_build_is_refused_without_database_changes(self):
        self.previous_schema()
        other = connection.copy(alias='publication-lock-probe')
        try:
            other.ensure_connection()
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)',[LOCK])
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError,'Outra publicação'):
                self.run_gate()
            self.assertEqual(self.snapshot(), before)
        finally:
            other.close()
