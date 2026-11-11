"""Controlled 0006 gate: actual PostgreSQL DDL, no production settings or fixtures."""
from importlib import import_module
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from offline.management.commands import publicar_resolucao_offline as gate


class PublicacaoResolucaoTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        if (connection.vendor != 'postgresql' or connection.settings_dict['HOST'] != '127.0.0.1'
                or not connection.settings_dict['NAME'].startswith('test_offline_0006_')
                or not hasattr(settings, 'OFFLINE_0006_TEST_DATA_DIRECTORY')):
            raise RuntimeError('Execute exclusivamente pelo runner PostgreSQL descartável da 0006.')
        self.leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self.addCleanup(self.restore)
        MigrationExecutor(connection).migrate([('offline', '0005_validate_local_sale_principal')])
        self.options = dict(
            environment='isolated', expected_host='127.0.0.1',
            expected_port=str(connection.settings_dict['PORT']),
            expected_database=connection.settings_dict['NAME'],
            expected_user=connection.settings_dict['USER'],
            expected_data_directory=settings.OFFLINE_0006_TEST_DATA_DIRECTORY,
        )

    def restore(self):
        MigrationExecutor(connection).migrate(self.leaves)

    def command(self, apply=True, **overrides):
        options = {**self.options, **overrides, 'stdout': StringIO()}
        options['apply' if apply else 'plan'] = True
        return call_command('publicar_resolucao_offline', **options)

    def snapshot(self, business=False):
        with connection.cursor() as cursor:
            cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            tables = [row[0] for row in cursor.fetchall()]
            if business:
                tables = [table for table in tables if table not in (*gate.TABLES, 'django_migrations')]
            result = {}
            for table in tables:
                cursor.execute('SELECT * FROM ' + connection.ops.quote_name(table))
                result[table] = (tuple((col.name, col.type_code) for col in cursor.description), sorted(map(repr, cursor.fetchall())))
            return result

    def assert_refused_unchanged(self, message, **options):
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, message):
            self.command(**options)
        self.assertEqual(self.snapshot(), before)

    def test_first_application_only_target_and_business_data_unchanged(self):
        get_user_model().objects.create_user(username='operador-isolado')
        before = self.snapshot(business=True)
        recorded = set(MigrationRecorder(connection).applied_migrations())
        real = MigrationExecutor.migrate
        calls = []

        def migrate(executor, targets, **kwargs):
            calls.append((targets, [(m.app_label, m.name, backwards) for m, backwards in kwargs['plan']]))
            return real(executor, targets, **kwargs)

        with patch.object(MigrationExecutor, 'migrate', migrate):
            self.command()
        self.assertEqual(calls, [([gate.TARGET], [(*gate.TARGET, False)])])
        self.assertEqual(set(MigrationRecorder(connection).applied_migrations()) - recorded, {gate.TARGET})
        self.assertEqual(self.snapshot(business=True), before)
        with connection.cursor() as cursor:
            gate.verify_schema(cursor)

    def test_plan_is_read_only_and_does_not_create_tables(self):
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            self.command(apply=False)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in ('CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP') for q in queries))

    def test_repeated_application_preserves_administrative_history_without_writes(self):
        self.command()
        from offline.models import AbandonoVendaOffline
        from uuid import uuid4
        actor = get_user_model().objects.create_user(username='historico-isolado')
        AbandonoVendaOffline.objects.create(
            actor=actor, operation_id=uuid4(), closure_id=uuid4(), device_id=uuid4(),
            environment_id='isolated', payload_hash='a' * 64, comando={'fixture': True},
            motivo='teste descartável', evidencia={'confirmado': True},
        )
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            self.command()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in ('CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP') for q in queries))
        self.assertTrue(any('pg_try_advisory_xact_lock' in q['sql'] for q in queries))

    def test_lost_success_response_is_recovered_by_read_only_plan_and_idempotent_retry(self):
        class LostResponse(StringIO):
            def write(self, value):
                raise OSError('resposta perdida depois do commit')

        before = self.snapshot(business=True)
        with self.assertRaisesMessage(OSError, 'depois do commit'):
            call_command('publicar_resolucao_offline', apply=True, stdout=LostResponse(), **self.options)
        self.assertIn(gate.TARGET, MigrationRecorder(connection).applied_migrations())
        committed = self.snapshot()
        self.command(apply=False)
        self.command()
        self.assertEqual(self.snapshot(), committed)
        self.assertEqual(self.snapshot(business=True), before)

    def test_changed_source_is_rejected_before_loader_or_connection(self):
        with patch.object(gate, 'SOURCE_SHA256', '0' * 64), patch.object(connection, 'ensure_connection', side_effect=AssertionError('não conectar')):
            with self.assertRaisesMessage(CommandError, 'definição do arquivo'):
                self.command()

    def test_changed_loaded_definition_and_metadata_are_rejected(self):
        cls = import_module('offline.migrations.' + gate.TARGET[1]).Migration
        for attr, value in (
            ('atomic', False), ('initial', True), ('dependencies', []),
            ('run_before', [('offline', '0005_validate_local_sale_principal')]),
            ('replaces', [('offline', '0005_validate_local_sale_principal')]),
            ('operations', [*cls.operations, migrations.RunSQL('SELECT 1')]),
        ):
            with self.subTest(attribute=attr), patch.object(cls, attr, value):
                # Metadata modifying the graph can fail during loader construction too.
                with self.assertRaises(Exception):
                    self.command()
        self.assertNotIn(gate.TARGET, MigrationRecorder(connection).applied_migrations())

    def test_other_pending_migration_is_rejected(self):
        recorder = MigrationRecorder(connection)
        leaves = [node for node in self.leaves if node[0] == 'locacoes']
        self.assertEqual(len(leaves), 1)
        other = leaves[0]
        recorder.record_unapplied(*other)
        try:
            self.assert_refused_unchanged('outra migration pendente')
        finally:
            recorder.record_applied(*other)

    def test_missing_dependency_is_rejected(self):
        recorder = MigrationRecorder(connection)
        dependency = ('offline', '0005_validate_local_sale_principal')
        recorder.record_unapplied(*dependency)
        try:
            self.assert_refused_unchanged('dependência ausente')
        finally:
            recorder.record_applied(*dependency)

    def test_busy_shared_publication_lock_refuses_without_changes(self):
        other = connection.copy(alias='gate-lock-fixture')
        try:
            with other.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_lock(%s)', [gate.LOCK])
            self.assert_refused_unchanged('trava de publicação ocupada')
        finally:
            other.close()

    def test_busy_lock_also_refuses_already_applied_operation(self):
        self.command()
        self.test_busy_shared_publication_lock_refuses_without_changes()

    def test_failure_after_ddl_rolls_back_tables_and_recorder_then_retry_succeeds(self):
        before = self.snapshot()
        with patch.object(gate, 'verify_schema', side_effect=CommandError('falha simulada após DDL')):
            with self.assertRaisesMessage(CommandError, 'falha simulada'):
                self.command()
        self.assertEqual(self.snapshot(), before)
        self.command()
        self.assertIn(gate.TARGET, MigrationRecorder(connection).applied_migrations())

    def test_actual_ddl_failure_rolls_back_partial_creation_and_retry_succeeds(self):
        before = self.snapshot()
        real = connection.SchemaEditorClass.create_model
        created = []

        def create(editor, model):
            created.append(model._meta.db_table)
            if model._meta.db_table == gate.TABLES[1]:
                editor.execute('SELECT 1 / 0')
            return real(editor, model)

        with patch.object(connection.SchemaEditorClass, 'create_model', create):
            with self.assertRaises(Exception):
                self.command()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(created, list(gate.TABLES))
        self.command()

    def test_wrong_destination_refused_before_connection(self):
        for key, value in (('expected_host', 'wrong.invalid'), ('expected_port', '1'),
                           ('expected_database', 'test_offline_0006_wrong'), ('expected_user', 'wrong')):
            with self.subTest(key=key), patch.object(connection, 'ensure_connection', side_effect=AssertionError('não conectar')):
                with self.assertRaises(CommandError):
                    self.command(**{key: value})

    def test_wrong_cluster_directory_is_rejected(self):
        self.assert_refused_unchanged('cluster descartável não confirmado', expected_data_directory=settings.OFFLINE_0006_TEST_DATA_DIRECTORY + '-wrong')

    def test_production_requires_explicit_consent_before_connection(self):
        with patch.object(connection, 'ensure_connection', side_effect=AssertionError('não conectar')):
            with self.assertRaisesMessage(CommandError, 'autorização explícita'):
                self.command(environment='production')

    def test_orphan_table_without_migration_is_rejected(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE ' + gate.TABLES[0] + ' (unexpected integer)')
        try:
            self.assert_refused_unchanged('schema inesperado antes')
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP TABLE ' + gate.TABLES[0])

    def test_applied_record_without_schema_is_rejected(self):
        recorder = MigrationRecorder(connection)
        recorder.record_applied(*gate.TARGET)
        try:
            self.assert_refused_unchanged('schema inesperado: tabela')
        finally:
            recorder.record_unapplied(*gate.TARGET)

    def test_divergent_column_constraint_index_and_rls_are_rejected(self):
        self.command()
        changes = [
            ('ALTER TABLE offline_abandonovendaoffline ALTER COLUMN motivo DROP NOT NULL',
             'ALTER TABLE offline_abandonovendaoffline ALTER COLUMN motivo SET NOT NULL'),
            ("ALTER TABLE offline_abandonovendaoffline ALTER COLUMN motivo SET DEFAULT 'indevido'",
             'ALTER TABLE offline_abandonovendaoffline ALTER COLUMN motivo DROP DEFAULT'),
            ('ALTER TABLE offline_abandonovendaoffline ADD CONSTRAINT inesperada CHECK (id > 0)',
             'ALTER TABLE offline_abandonovendaoffline DROP CONSTRAINT inesperada'),
            ('CREATE INDEX inesperado ON offline_abandonovendaoffline (motivo)', 'DROP INDEX inesperado'),
            ('ALTER TABLE offline_abandonovendaoffline ENABLE ROW LEVEL SECURITY',
             'ALTER TABLE offline_abandonovendaoffline DISABLE ROW LEVEL SECURITY'),
            ('ALTER TABLE offline_abandonovendaoffline ALTER COLUMN id SET GENERATED ALWAYS',
             'ALTER TABLE offline_abandonovendaoffline ALTER COLUMN id SET GENERATED BY DEFAULT'),
            ('ALTER TABLE offline_abandonovendaoffline ALTER COLUMN environment_id TYPE varchar(161)',
             'ALTER TABLE offline_abandonovendaoffline ALTER COLUMN environment_id TYPE varchar(160)'),
        ]
        for create, undo in changes:
            with self.subTest(sql=create):
                with connection.cursor() as cursor:
                    cursor.execute(create)
                try:
                    self.assert_refused_unchanged('schema inesperado')
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(undo)

    def test_every_loaded_field_and_delete_operation_are_pinned(self):
        cls = import_module('offline.migrations.' + gate.TARGET[1]).Migration
        for model_index, operation in enumerate(cls.operations):
            for field_index, (name, field) in enumerate(operation.fields):
                changed = deepcopy(cls.operations)
                changed[model_index].fields[field_index] = (name, models.TextField(null=True))
                with self.subTest(field=name), patch.object(cls, 'operations', changed):
                    loader = MigrationLoader(None, load=False)
                    loader.load_disk()
                    with self.assertRaisesMessage(CommandError, 'definição carregada'):
                        gate.validate_definition(loader)
        with patch.object(cls, 'operations', [migrations.DeleteModel('AbandonoVendaOffline')]):
            self.assert_refused_unchanged('definição carregada')

    def test_unknown_applied_migration_is_rejected(self):
        recorder = MigrationRecorder(connection)
        unknown = ('offline', '9999_nao_revisada')
        recorder.record_applied(*unknown)
        try:
            self.assert_refused_unchanged('migrations ausentes nesta versão')
        finally:
            recorder.record_unapplied(*unknown)

    def test_unexpected_trigger_is_rejected(self):
        self.command()
        with connection.cursor() as cursor:
            cursor.execute("CREATE FUNCTION public.gate_fixture_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            cursor.execute('CREATE TRIGGER gate_fixture BEFORE INSERT ON offline_abandonovendaoffline FOR EACH ROW EXECUTE FUNCTION public.gate_fixture_trigger()')
        try:
            self.assert_refused_unchanged('triggers ou regras')
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP TRIGGER gate_fixture ON offline_abandonovendaoffline')
                cursor.execute('DROP FUNCTION public.gate_fixture_trigger()')

    def test_unvalidated_fk_is_rejected(self):
        self.command()
        with connection.cursor() as cursor:
            cursor.execute("SELECT conname FROM pg_constraint WHERE conrelid='offline_abandonovendaoffline'::regclass AND contype='f'")
            name = connection.ops.quote_name(cursor.fetchone()[0])
            cursor.execute('ALTER TABLE offline_abandonovendaoffline DROP CONSTRAINT ' + name)
            cursor.execute('ALTER TABLE offline_abandonovendaoffline ADD CONSTRAINT ' + name +
                           ' FOREIGN KEY (actor_id) REFERENCES auth_user(id) DEFERRABLE INITIALLY DEFERRED NOT VALID')
        try:
            self.assert_refused_unchanged('restrições')
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE offline_abandonovendaoffline VALIDATE CONSTRAINT ' + name)

    def test_libpq_redirection_is_rejected_before_connection(self):
        with patch.dict('os.environ', {'PGHOSTADDR': '127.0.0.1'}), patch.object(connection, 'ensure_connection', side_effect=AssertionError('não conectar')):
            with self.assertRaisesMessage(CommandError, 'redirecionamento'):
                self.command()

    def test_production_without_tls_configuration_is_rejected_before_connection(self):
        params = {**connection.get_connection_params(), 'host': 'approved.invalid'}
        params.pop('sslmode', None)
        with patch.object(connection, 'get_connection_params', return_value=params), patch.object(connection, 'ensure_connection', side_effect=AssertionError('não conectar')):
            with self.assertRaisesMessage(CommandError, 'TLS obrigatório'):
                self.command(environment='production', expected_host='approved.invalid',
                             authorize_production=gate.APPROVAL, approval_reference='fixture-only')

    def test_conditions_are_revalidated_under_shared_lock(self):
        before = self.snapshot()
        original = gate.Command.checked_plan
        calls = []

        def recheck(command, executor, cursor):
            calls.append(True)
            if len(calls) == 2:
                raise CommandError('condição mudou sob trava')
            return original(command, executor, cursor)

        with patch.object(gate.Command, 'checked_plan', recheck):
            with self.assertRaisesMessage(CommandError, 'mudou sob trava'):
                self.command()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.snapshot(), before)

    def test_existing_publication_lock_and_timeouts_are_reused(self):
        from inventario.management.commands.publicar_cancelamento import LOCK
        self.assertEqual(gate.LOCK, LOCK)
        with CaptureQueriesContext(connection) as queries:
            self.command()
        sql = '\n'.join(q['sql'] for q in queries)
        self.assertIn("SET LOCAL lock_timeout = '5s'", sql)
        self.assertIn("SET LOCAL statement_timeout = '60s'", sql)

    def test_existing_build_gate_refuses_pending_0006_and_accepts_after_controlled_application(self):
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            call_command('publicar_cancelamento', plan=True, stdout=StringIO())
        self.assertEqual(self.snapshot(), before)
        self.command()
        after = self.snapshot()
        output = StringIO()
        call_command('publicar_cancelamento', plan=True, stdout=output)
        self.assertIn('Build liberado', output.getvalue())
        self.assertEqual(self.snapshot(), after)
