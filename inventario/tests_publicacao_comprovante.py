"""Release gate for locacoes.0018, exercised only in disposable PostgreSQL."""
from copy import deepcopy
from importlib import import_module
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, IntegrityError, migrations, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from inventario.management.commands.publicar_cancelamento import (
    AUTHORIZED, COLETA, COMPRA, COMPROVANTE, CORRECAO, LOCK, PRECISAO, RECOLHIMENTO, TARGET, Command,
    CANCELAMENTO, RASTREABILIDADE,
)
from offline import test_pickup_checklist as pickup_tests


TABLE = 'locacoes_conferenciarecolhimentolocacao'
FIELDS = ('comprovante_token_hash', 'comprovante_expira_em', 'comprovante_revogado_em')


class ComprovanteDefinitionTests(SimpleTestCase):
    def test_allowlist_adds_only_0018_and_exact_definition_is_accepted(self):
        self.assertEqual(AUTHORIZED, {RECOLHIMENTO, COMPROVANTE, CORRECAO, CANCELAMENTO, *RASTREABILIDADE, COMPRA, PRECISAO, COLETA})
        Command.validate_definitions(MigrationLoader(None))

    def test_all_definition_changes_are_refused(self):
        mutations = [
            ('dependencies', [('locacoes', '0016_locacao_horario_previsto_devolucao')]),
            ('dependencies', [RECOLHIMENTO, RECOLHIMENTO]),
            ('dependencies', [RECOLHIMENTO, COLETA]),
            ('atomic', False), ('initial', True),
            ('run_before', [('offline', '0005_validate_local_sale_principal')]),
            ('replaces', [('locacoes', '0016_locacao_horario_previsto_devolucao')]),
            ('operations', []), ('operations', [migrations.RunSQL('SELECT 1')]),
        ]
        for attribute, value in mutations:
            with self.subTest(attribute=attribute, value=value):
                loader = MigrationLoader(None)
                setattr(loader.disk_migrations[COMPROVANTE], attribute, value)
                with self.assertRaisesMessage(CommandError, 'locacoes.0018'):
                    Command.validate_definitions(loader)

        changes = [
            (0, 'model_name', 'locacao'), (0, 'name', 'outro_campo'),
            (0, 'preserve_default', False), (0, 'elidable', True),
            (0, 'field.max_length', 63), (0, 'field._unique', False),
            (0, 'field.null', False), (0, 'field.blank', False),
            (0, 'field.editable', True), (0, 'field.default', 'nao-autorizado'),
            (0, 'field.db_default', 'nao-autorizado'), (0, 'field.db_index', True),
            (1, 'field', models.DateField(null=True, blank=True, editable=False)),
            (1, 'field.null', False), (1, 'field.blank', False),
            (1, 'field.editable', True), (1, 'field.auto_now', True),
            (2, 'field', models.CharField(max_length=64, null=True, blank=True, editable=False)),
            (2, 'field.null', False), (2, 'field.blank', False), (2, 'field.editable', True),
        ]
        for index, attribute, value in changes:
            with self.subTest(index=index, attribute=attribute):
                loader = MigrationLoader(None)
                receipt = loader.disk_migrations[COMPROVANTE]
                receipt.operations = deepcopy(receipt.operations)
                operation = receipt.operations[index]
                if attribute.startswith('field.'):
                    setattr(operation.field, attribute.split('.')[1], value)
                else:
                    setattr(operation, attribute, value)
                with self.assertRaisesMessage(CommandError, 'locacoes.0018'):
                    Command.validate_definitions(loader)
        loader = MigrationLoader(None)
        loader.disk_migrations[COMPROVANTE].operations = [
            *loader.disk_migrations[COMPROVANTE].operations, migrations.RunSQL('SELECT 1')]
        with self.assertRaisesMessage(CommandError, 'locacoes.0018'):
            Command.validate_definitions(loader)
        loader = MigrationLoader(None)
        loader.disk_migrations.pop(COMPROVANTE)
        with self.assertRaisesMessage(CommandError, 'locacoes.0018'):
            Command.validate_definitions(loader)


class PublicacaoComprovanteTests(pickup_tests.PickupFixture, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'test_inventario_disposable')
        self.seed()
        self.client.force_login(self.user)
        token = self.client.get(self.url).context['confirmacao_token']
        self.assertEqual(self.client.post(self.url, {**self.payload, 'confirmacao_token': token}).status_code, 302)
        self.recorder = MigrationRecorder(connection)
        self.addCleanup(self.restore)

    def restore(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def previous(self):
        MigrationExecutor(connection).migrate([RECOLHIMENTO])

    def gate(self, *args):
        output = StringIO()
        call_command('publicar_cancelamento', *args, stdout=output)
        return output.getvalue()

    def snapshot(self, business=False):
        result = {}
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                if business and table in {'django_migrations', 'locacoes_correcaopagamentolocacao'}:
                    continue
                columns = [c.name for c in connection.introspection.get_table_description(cursor, table)]
                if business and table == TABLE:
                    columns = [c for c in columns if c not in FIELDS]
                cursor.execute('SELECT ' + ', '.join(map(connection.ops.quote_name, columns))
                    + ' FROM ' + connection.ops.quote_name(table))
                result[table] = sorted(map(repr, cursor.fetchall()))
        return result

    def test_apply_0018_and_0019_preserves_records_and_targets_the_new_leaf(self):
        self.previous()
        before = self.snapshot(business=True)
        migrate = MigrationExecutor.migrate
        targets_seen = []

        def checked(executor, targets, **kwargs):
            targets_seen.append(targets)
            self.assertEqual([(m.app_label, m.name, backwards) for m, backwards in kwargs['plan']],
                             [(*COMPROVANTE, False), (*CORRECAO, False)])
            return migrate(executor, targets, **kwargs)

        with patch.object(MigrationExecutor, 'migrate', checked):
            self.assertIn('APLICAR locacoes.0018_comprovante_cliente', self.gate())
        self.assertEqual(targets_seen, [[CORRECAO, TARGET, COLETA]])
        self.assertEqual(before, self.snapshot(business=True))
        self.assertIn(COMPROVANTE, self.recorder.applied_migrations())
        self.assertEqual(self.recorder.migration_qs.filter(app=COMPROVANTE[0], name=COMPROVANTE[1]).count(), 1)
        with connection.cursor() as cursor:
            cursor.execute('SELECT ' + ', '.join(FIELDS) + ' FROM ' + TABLE)
            self.assertEqual(cursor.fetchall(), [(None, None, None)])
        Command.verify_schema()

    def test_plan_and_noop_never_write(self):
        self.previous()
        before = self.snapshot()
        self.assertIn('APLICAR locacoes.0018', self.gate('--plan'))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(COMPROVANTE, self.recorder.applied_migrations())
        self.gate()
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            self.assertIn('Nenhuma pendência', self.gate())
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(
            ('ALTER ', 'CREATE ', 'DROP ', 'INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))

    def test_hash_uniqueness_is_enforced_by_postgresql(self):
        self.previous()
        self.gate()
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + TABLE + ' SET comprovante_token_hash = %s', ['a' * 64])
        # Clone only a fictitious conference through SQL, retaining valid foreign keys.
        with connection.cursor() as cursor:
            columns = [c.name for c in connection.introspection.get_table_description(cursor, TABLE) if c.name != 'id']
        names = ', '.join(map(connection.ops.quote_name, columns))
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f'INSERT INTO {TABLE} ({names}) SELECT {names} FROM {TABLE} LIMIT 1')
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + TABLE + ' SET comprovante_token_hash = NULL')
            cursor.execute(f'INSERT INTO {TABLE} ({names}) SELECT {names} FROM {TABLE} LIMIT 1')
            cursor.execute('SELECT count(*) FROM ' + TABLE)
            self.assertEqual(cursor.fetchone()[0], 2)

    def test_unknown_pending_and_conflicting_migrations_are_refused(self):
        self.previous()
        original = MigrationLoader.load_disk
        for conflict in (False, True):
            def inject(loader):
                original(loader)
                for name in (['0020_inesperada', '0021_conflito'] if conflict else ['0020_inesperada']):
                    migration = migrations.Migration(name, 'locacoes')
                    migration.dependencies = [CORRECAO]
                    migration.operations = [migrations.RunSQL('CREATE TABLE publicacao_nao_autorizada (id int)')]
                    loader.disk_migrations[('locacoes', name)] = migration
            with self.subTest(conflict=conflict):
                before = self.snapshot()
                with patch.object(MigrationLoader, 'load_disk', inject):
                    with self.assertRaisesMessage(CommandError, 'conflitos' if conflict else 'migrations inesperadas'):
                        self.gate()
                self.assertEqual(before, self.snapshot())

    def test_unexpected_reversal_is_refused(self):
        self.previous()
        before = self.snapshot()
        migration = MigrationLoader(None).disk_migrations[COMPROVANTE]
        with patch.object(MigrationExecutor, 'migration_plan', return_value=[(migration, True)]):
            with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
                self.gate()
        self.assertEqual(before, self.snapshot())

    def test_bad_definition_is_refused_before_any_ddl(self):
        self.previous()
        before = self.snapshot()
        migration = import_module('locacoes.migrations.0018_comprovante_cliente').Migration
        altered = deepcopy(migration.operations)
        altered[0].field._unique = False
        with patch.object(migration, 'operations', altered):
            with self.assertRaisesMessage(CommandError, 'locacoes.0018'):
                self.gate()
        self.assertEqual(before, self.snapshot())

    def test_unknown_applied_and_inconsistent_history_are_refused(self):
        unknown = ('locacoes', '9999_desconhecida')
        self.recorder.record_applied(*unknown)
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'ausentes nesta versão'):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            self.recorder.record_unapplied(*unknown)
        self.recorder.record_unapplied(*RECOLHIMENTO)
        try:
            before = self.snapshot()
            with self.assertRaises(InconsistentMigrationHistory):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            self.recorder.record_applied(*RECOLHIMENTO)

    def test_ddl_and_recorder_roll_back_if_a_field_fails(self):
        self.previous()
        before = self.snapshot()
        original = connection.SchemaEditorClass.add_field
        def fail(editor, model, field):
            if model._meta.db_table == TABLE and field.name == FIELDS[1]:
                raise RuntimeError('falha DDL sintética')
            return original(editor, model, field)
        with patch.object(connection.SchemaEditorClass, 'add_field', fail):
            with self.assertRaisesMessage(RuntimeError, 'falha DDL sintética'):
                self.gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(COMPROVANTE, self.recorder.applied_migrations())
        self.gate()

    def test_final_schema_failure_rolls_back_0018(self):
        self.previous()
        before = self.snapshot()
        with patch.object(Command, 'verify_schema', side_effect=CommandError('falha final sintética')):
            with self.assertRaisesMessage(CommandError, 'falha final sintética'):
                self.gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(COMPROVANTE, self.recorder.applied_migrations())

    def test_noop_detects_missing_columns_despite_recorder(self):
        self.previous()
        self.recorder.record_applied(*COMPROVANTE)
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'estrutura/default de locacoes.0018'):
                self.gate()
            self.assertEqual(before, self.snapshot())
        finally:
            self.recorder.record_unapplied(*COMPROVANTE)

    def test_noop_refuses_incompatible_types_nullability_and_defaults(self):
        mutations = [
            ('comprovante_token_hash TYPE varchar(63)', 'comprovante_token_hash TYPE varchar(64)'),
            ('comprovante_token_hash SET DEFAULT \'alterado\'', 'comprovante_token_hash DROP DEFAULT'),
            ('comprovante_expira_em TYPE timestamp without time zone', 'comprovante_expira_em TYPE timestamp with time zone'),
            ('comprovante_revogado_em TYPE date', 'comprovante_revogado_em TYPE timestamp with time zone'),
            ('comprovante_expira_em SET DEFAULT now()', 'comprovante_expira_em DROP DEFAULT'),
            ('comprovante_revogado_em SET DEFAULT now()', 'comprovante_revogado_em DROP DEFAULT'),
        ]
        for change, restore in mutations:
            with self.subTest(change=change):
                with connection.cursor() as cursor:
                    cursor.execute('ALTER TABLE ' + TABLE + ' ALTER COLUMN ' + change)
                try:
                    before = self.snapshot()
                    with self.assertRaisesMessage(CommandError, 'estrutura/default de locacoes.0018'):
                        self.gate()
                    self.assertEqual(before, self.snapshot())
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute('ALTER TABLE ' + TABLE + ' ALTER COLUMN ' + restore)
        for column, value in [(FIELDS[0], 'a' * 64), (FIELDS[1], timezone.now()), (FIELDS[2], timezone.now())]:
            with self.subTest(required_column=column):
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE {TABLE} SET {column} = %s', [value])
                    cursor.execute(f'ALTER TABLE {TABLE} ALTER COLUMN {column} SET NOT NULL')
                try:
                    with self.assertRaisesMessage(CommandError, 'estrutura/default de locacoes.0018'):
                        self.gate()
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(f'ALTER TABLE {TABLE} ALTER COLUMN {column} DROP NOT NULL')

    def test_noop_refuses_missing_or_partial_hash_uniqueness(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, TABLE)
        name = next(name for name, info in constraints.items() if info['unique'] and info['columns'] == [FIELDS[0]])
        quoted = connection.ops.quote_name(name)
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT {quoted}')
        try:
            with self.assertRaisesMessage(CommandError, 'unicidade do hash'):
                self.gate()
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE UNIQUE INDEX comprovante_unico_parcial ON {TABLE} (comprovante_token_hash) WHERE comprovante_token_hash LIKE 'a%'")
            try:
                with self.assertRaisesMessage(CommandError, 'unicidade do hash'):
                    self.gate()
            finally:
                with connection.cursor() as cursor:
                    cursor.execute('DROP INDEX comprovante_unico_parcial')
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'ALTER TABLE {TABLE} ADD CONSTRAINT {quoted} UNIQUE (comprovante_token_hash)')

    def test_shared_lock_prevents_concurrent_publication(self):
        self.previous()
        other = connection.copy(alias='comprovante-publication-lock')
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
