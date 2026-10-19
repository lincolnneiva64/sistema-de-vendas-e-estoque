"""Release rehearsal: apply/reverse ONLY the grant DDL on disposable PostgreSQL."""
import importlib
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase
from .test_pickup_checklist import PickupFixture
from locacoes.models import ConferenciaRecolhimentoLocacao, AutorizacaoRecolhimento


class PickupMigrationReleaseTests(PickupFixture, TransactionTestCase):
    def setUp(self):
        self.seed()
        self.client.force_login(self.user)
        self.payload['confirmacao_token'] = self.client.get(self.url).context['confirmacao_token']
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 302)

    def business_snapshot(self):
        tables = [name for name in connection.introspection.table_names()
                  if name.startswith(('locacoes_', 'estoque_', 'offline_', 'inventario_'))
                  and name != 'locacoes_autorizacaorecolhimento']
        result = {}
        with connection.cursor() as cursor:
            for name in tables:
                cursor.execute('SELECT * FROM ' + connection.ops.quote_name(name))
                result[name] = sorted(repr(row) for row in cursor.fetchall())
        return result

    def test_actual_migration_forward_and_reverse_preserve_all_commercial_rows(self):
        self.assertEqual(connection.vendor, 'postgresql')
        # The launcher refuses connections outside its new private cluster.
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'pickup_disposable')
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 0)
        before = self.business_snapshot()
        migration = importlib.import_module('locacoes.migrations.0017_autorizacao_recolhimento').Migration(
            '0017_autorizacao_recolhimento', 'locacoes')
        loader = MigrationLoader(None)
        state = loader.project_state()
        state.remove_model('locacoes', 'autorizacaorecolhimento')
        self.assertEqual(len(migration.operations), 1)
        self.assertEqual(type(migration.operations[0]).__name__, 'CreateModel')
        # Start with the existing current-schema fixture and remove only the new table.
        # Exercise the migration's real reversible DDL, never migrate the application graph.
        with connection.schema_editor() as editor:
            migration.unapply(state.clone(), editor)
        self.assertNotIn('locacoes_autorizacaorecolhimento', connection.introspection.table_names())
        self.assertEqual(self.business_snapshot(), before)
        try:
            with connection.schema_editor() as editor:
                migration.apply(state.clone(), editor)
            self.assertIn('locacoes_autorizacaorecolhimento', connection.introspection.table_names())
            self.assertEqual(self.business_snapshot(), before)
            with connection.schema_editor() as editor:
                migration.unapply(state.clone(), editor)
            self.assertEqual(self.business_snapshot(), before)
        finally:
            if 'locacoes_autorizacaorecolhimento' not in connection.introspection.table_names():
                with connection.schema_editor() as editor:
                    migration.apply(state.clone(), editor)
        self.assertEqual(self.business_snapshot(), before)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
