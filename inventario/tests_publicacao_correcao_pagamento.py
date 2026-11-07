"""0019 release gate: exact approval, real DDL and failure rollback on disposable PG."""
from copy import deepcopy
from importlib import import_module
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from inventario.management.commands.publicar_cancelamento import (
    AUTHORIZED, CORRECAO, COMPROVANTE, RECOLHIMENTO, TARGET, COMPRA, PRECISAO, COLETA, LOCK, Command,
    CANCELAMENTO, RASTREABILIDADE,
)
from locacoes.tests_saldos_consulta import DadosSaldos

TABLE = 'locacoes_correcaopagamentolocacao'


class CorrecaoDefinitionTests(SimpleTestCase):
    def test_only_0019_is_added_to_existing_authorizations(self):
        self.assertEqual(AUTHORIZED, {CORRECAO, COMPROVANTE, RECOLHIMENTO, CANCELAMENTO, *RASTREABILIDADE, COMPRA, PRECISAO, COLETA})
        Command.validate_definitions(MigrationLoader(None))

    def test_changed_dependencies_metadata_and_extra_operations_are_rejected(self):
        cls = import_module('locacoes.migrations.' + CORRECAO[1]).Migration
        mutations = [
            ('dependencies', [COMPROVANTE]), ('dependencies', [COMPROVANTE, COMPROVANTE]),
            ('dependencies', [*cls.dependencies, COLETA]), ('atomic', False), ('initial', True),
            ('run_before', [('offline', '0005_validate_local_sale_principal')]),
            ('replaces', [COMPROVANTE]), ('operations', []),
            ('operations', [*cls.operations, migrations.RunSQL('DROP TABLE estoque_movimentofinanceiro')]),
            ('operations', [migrations.DeleteModel('PagamentoLocacao')]),
        ]
        for attr, value in mutations:
            loader = MigrationLoader(None)
            with self.subTest(attribute=attr, value=value), patch.object(loader.disk_migrations[CORRECAO], attr, value):
                with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                    Command.validate_definitions(loader)

    def test_every_field_is_pinned_including_types_null_defaults_relations_and_protect(self):
        cls = import_module('locacoes.migrations.' + CORRECAO[1]).Migration
        for index, (name, field) in enumerate(cls.operations[0].fields):
            operations = deepcopy(cls.operations)
            operations[0].fields[index] = (name, models.TextField(null=True))
            with self.subTest(field=name), patch.object(cls, 'operations', operations):
                with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                    Command.validate_definitions(MigrationLoader(None))
        for name, field in [
            ('operacao_uuid', models.UUIDField(editable=False)),
            ('antes', models.JSONField(editable=False, default=dict)),
            ('pagamento', models.ForeignKey(on_delete=models.CASCADE, related_name='correcoes', to='locacoes.pagamentolocacao')),
            ('usuario', models.ForeignKey(on_delete=models.PROTECT, to='estoque.funcionario')),
            ('estorno', models.ForeignKey(on_delete=models.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
        ]:
            operations = deepcopy(cls.operations)
            operations[0].fields = [(key, field if key == name else value) for key, value in operations[0].fields]
            with self.subTest(change=name), patch.object(cls, 'operations', operations):
                with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                    Command.validate_definitions(MigrationLoader(None))

    def test_changed_name_constraints_bases_managers_or_missing_definition_are_rejected(self):
        cls = import_module('locacoes.migrations.' + CORRECAO[1]).Migration
        for change in ('name', 'constraint', 'bases', 'managers', 'elidable'):
            operations = deepcopy(cls.operations)
            if change == 'name':
                operations[0].name = 'OutraTabela'
            elif change == 'constraint':
                operations[0].options['constraints'] = []
            elif change == 'bases':
                operations[0].bases = ('locacoes.pagamentolocacao',)
            elif change == 'managers':
                operations[0].managers = [('outro', models.Manager())]
            else:
                operations[0].elidable = True
            with self.subTest(change=change), patch.object(cls, 'operations', operations):
                with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                    Command.validate_definitions(MigrationLoader(None))
        loader = MigrationLoader(None)
        loader.disk_migrations.pop(CORRECAO)
        with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
            Command.validate_definitions(loader)


class PublicacaoCorrecaoPagamentoTests(DadosSaldos, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'test_inventario_disposable')
        super().setUp()
        self.rental = self.criar('Cliente fictício da publicação', 'entregue', 100)
        self.payment = self.rental.registrar_pagamento(30, 'pix', responsavel=self.operador.nome)
        self.recorder = MigrationRecorder(connection)
        self.addCleanup(self.restore)

    def restore(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def previous(self):
        MigrationExecutor(connection).migrate([COMPROVANTE])

    def gate(self, *args):
        output = StringIO()
        call_command('publicar_cancelamento', *args, stdout=output)
        return output.getvalue()

    def snapshot(self, business=False):
        result = {}
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                if business and table in {TABLE, 'django_migrations'}:
                    continue
                columns = [c.name for c in connection.introspection.get_table_description(cursor, table)]
                cursor.execute('SELECT ' + ', '.join(map(connection.ops.quote_name, columns))
                               + ' FROM ' + connection.ops.quote_name(table))
                result[table] = sorted(map(repr, cursor.fetchall()))
        return result

    def test_apply_only_0019_preserves_all_other_rows_columns_and_explicit_targets(self):
        self.previous()
        before = self.snapshot(business=True)
        applied_before = set(self.recorder.applied_migrations())
        with connection.cursor() as cursor:
            tables_before = set(connection.introspection.table_names(cursor))
            columns_before = {table: connection.introspection.get_table_description(cursor, table) for table in tables_before}
        migrate = MigrationExecutor.migrate
        seen = []
        def checked(executor, targets, **kwargs):
            seen.append(targets)
            self.assertEqual([(m.app_label, m.name, backwards) for m, backwards in kwargs['plan']], [(*CORRECAO, False)])
            return migrate(executor, targets, **kwargs)
        with patch.object(MigrationExecutor, 'migrate', checked):
            self.assertIn('APLICAR locacoes.0019_correcao_pagamento_direto', self.gate())
        self.assertEqual(seen, [[CORRECAO, TARGET, COLETA]])
        self.assertEqual(set(self.recorder.applied_migrations()) - applied_before, {CORRECAO})
        self.assertEqual(before, self.snapshot(business=True))
        with connection.cursor() as cursor:
            self.assertEqual(set(connection.introspection.table_names(cursor)) - tables_before, {TABLE})
            for table in tables_before:
                self.assertEqual(connection.introspection.get_table_description(cursor, table), columns_before[table])
        Command.verify_schema()

    def test_plan_and_already_applied_correct_schema_are_read_only(self):
        self.previous()
        before = self.snapshot()
        self.assertIn('APLICAR locacoes.0019', self.gate('--plan'))
        self.assertEqual(self.snapshot(), before)
        self.gate()
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            self.assertIn('Nenhuma pendência', self.gate())
            self.gate('--plan')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(
            ('ALTER ', 'CREATE ', 'DROP ', 'INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))

    def test_recorded_migration_with_missing_table_is_rejected(self):
        self.previous()
        self.recorder.record_applied(*CORRECAO)
        try:
            before = self.snapshot()
            for options in ((), ('--plan',)):
                with self.assertRaisesMessage(CommandError, 'tabela de locacoes.0019'):
                    self.gate(*options)
                self.assertEqual(self.snapshot(), before)
        finally:
            self.recorder.record_unapplied(*CORRECAO)

    def test_wrong_types_nullability_defaults_extra_columns_and_identity_are_rejected(self):
        changes = [
            ('ALTER COLUMN operacao_uuid TYPE text USING operacao_uuid::text', 'ALTER COLUMN operacao_uuid TYPE uuid USING operacao_uuid::uuid'),
            ('ALTER COLUMN versao_anterior TYPE varchar(63)', 'ALTER COLUMN versao_anterior TYPE varchar(64)'),
            ('ALTER COLUMN requisicao_hash DROP NOT NULL', 'ALTER COLUMN requisicao_hash SET NOT NULL'),
            ('ALTER COLUMN antes TYPE json USING antes::json', 'ALTER COLUMN antes TYPE jsonb USING antes::jsonb'),
            ("ALTER COLUMN motivo SET DEFAULT 'alterado'", 'ALTER COLUMN motivo DROP DEFAULT'),
            ('ALTER COLUMN usuario_nome TYPE varchar(149)', 'ALTER COLUMN usuario_nome TYPE varchar(150)'),
            ('ALTER COLUMN criado_em TYPE timestamp without time zone', 'ALTER COLUMN criado_em TYPE timestamp with time zone'),
            ('ALTER COLUMN recibo_anterior_html DROP NOT NULL', 'ALTER COLUMN recibo_anterior_html SET NOT NULL'),
            ('ALTER COLUMN usuario_id TYPE bigint', 'ALTER COLUMN usuario_id TYPE integer'),
            ('ADD COLUMN inesperada text', 'DROP COLUMN inesperada'),
            ('ALTER COLUMN id DROP IDENTITY', 'ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY'),
        ]
        for change, restore in changes:
            with self.subTest(change=change):
                with connection.cursor() as cursor:
                    cursor.execute(f'ALTER TABLE {TABLE} {change}')
                try:
                    before = self.snapshot()
                    with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                        self.gate()
                    self.assertEqual(self.snapshot(), before)
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(f'ALTER TABLE {TABLE} {restore}')

    def test_already_applied_audit_is_checked_even_with_other_authorized_pending(self):
        MigrationExecutor(connection).migrate([PRECISAO])
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} ALTER COLUMN motivo DROP NOT NULL')
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                self.gate('--plan')
            self.assertEqual(self.snapshot(), before)
            self.assertNotIn(COLETA, self.recorder.applied_migrations())
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'ALTER TABLE {TABLE} ALTER COLUMN motivo SET NOT NULL')

    def constraints(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT conname, pg_get_constraintdef(oid), contype FROM pg_constraint WHERE conrelid=to_regclass(%s)', [TABLE])
            return cursor.fetchall()

    def test_every_missing_primary_unique_or_foreign_key_is_rejected(self):
        for name, definition, kind in self.constraints():
            if kind not in {'p', 'u', 'f'}:
                continue
            quoted = connection.ops.quote_name(name)
            with self.subTest(constraint=name):
                with connection.cursor() as cursor:
                    cursor.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT {quoted}')
                try:
                    with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                        self.gate()
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(f'ALTER TABLE {TABLE} ADD CONSTRAINT {quoted} {definition}')

    def test_partial_unique_index_is_not_a_substitute_for_the_uuid_constraint(self):
        name, definition, _ = next(row for row in self.constraints() if row[1] == 'UNIQUE (operacao_uuid)')
        quoted = connection.ops.quote_name(name)
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT {quoted}')
            cursor.execute(f'CREATE UNIQUE INDEX audit_uuid_partial ON {TABLE} (operacao_uuid) WHERE operacao_uuid IS NOT NULL')
        try:
            with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                self.gate()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP INDEX audit_uuid_partial')
                cursor.execute(f'ALTER TABLE {TABLE} ADD CONSTRAINT {quoted} {definition}')

    def test_wrong_foreign_key_destination_delete_action_or_unvalidated_key_is_rejected(self):
        name, definition, _ = next(row for row in self.constraints() if row[1].startswith('FOREIGN KEY (movimento_original_id)'))
        quoted = connection.ops.quote_name(name)
        for wrong in (
            'FOREIGN KEY (movimento_original_id) REFERENCES estoque_contafinanceira(id) DEFERRABLE INITIALLY DEFERRED',
            'FOREIGN KEY (movimento_original_id) REFERENCES estoque_movimentofinanceiro(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED',
            definition + ' NOT VALID',
        ):
            with self.subTest(definition=wrong):
                with connection.cursor() as cursor:
                    cursor.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT {quoted}')
                    cursor.execute(f'ALTER TABLE {TABLE} ADD CONSTRAINT {quoted} {wrong}')
                try:
                    with self.assertRaisesMessage(CommandError, 'locacoes.0019'):
                        self.gate()
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT {quoted}')
                        cursor.execute(f'ALTER TABLE {TABLE} ADD CONSTRAINT {quoted} {definition}')

    def test_unknown_migration_conflicting_leaves_and_reversal_are_rejected_without_writes(self):
        self.previous()
        original = MigrationLoader.load_disk
        for conflict in (False, True):
            def inject(loader):
                original(loader)
                for name in (['0020_inesperada', '0021_conflito'] if conflict else ['0020_inesperada']):
                    migration = migrations.Migration(name, 'locacoes')
                    migration.dependencies = [CORRECAO]
                    migration.operations = [migrations.RunSQL('CREATE TABLE publication_forbidden (id int)')]
                    loader.disk_migrations[('locacoes', name)] = migration
            before = self.snapshot()
            with self.subTest(conflict=conflict), patch.object(MigrationLoader, 'load_disk', inject):
                with self.assertRaisesMessage(CommandError, 'conflitos' if conflict else 'migrations inesperadas'):
                    self.gate()
            self.assertEqual(self.snapshot(), before)
        migration = MigrationLoader(None).disk_migrations[CORRECAO]
        with patch.object(MigrationExecutor, 'migration_plan', return_value=[(migration, True)]):
            with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
                self.gate()
        self.assertEqual(self.snapshot(), before)

    def test_missing_applied_file_and_inconsistent_history_are_rejected(self):
        self.recorder.record_applied('estoque', '9999_ausente')
        try:
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'ausentes nesta versão'):
                self.gate()
            self.assertEqual(self.snapshot(), before)
        finally:
            self.recorder.record_unapplied('estoque', '9999_ausente')
        self.recorder.record_unapplied(*COMPROVANTE)
        try:
            before = self.snapshot()
            with self.assertRaises(InconsistentMigrationHistory):
                self.gate()
            self.assertEqual(self.snapshot(), before)
        finally:
            self.recorder.record_applied(*COMPROVANTE)

    def test_final_verification_failure_rolls_back_schema_and_recorder(self):
        self.previous()
        before = self.snapshot()
        with patch.object(Command, 'verify_schema', side_effect=CommandError('falha final sintética')):
            with self.assertRaisesMessage(CommandError, 'falha final sintética'):
                self.gate()
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(CORRECAO, self.recorder.applied_migrations())

    def test_failure_after_creating_table_also_rolls_back_all_changes(self):
        self.previous()
        before = self.snapshot()
        original = MigrationExecutor.apply_migration
        def fail(executor, state, migration, *args, **kwargs):
            result = original(executor, state, migration, *args, **kwargs)
            if (migration.app_label, migration.name) == CORRECAO:
                raise RuntimeError('falha após criação da tabela')
            return result
        with patch.object(MigrationExecutor, 'apply_migration', fail):
            with self.assertRaisesRegex(RuntimeError, 'falha após criação'):
                self.gate()
        self.assertEqual(self.snapshot(), before)

    def test_shared_publication_lock_blocks_0019_without_writes(self):
        self.previous()
        other = connection.copy(alias='correction-publication-lock')
        try:
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [LOCK])
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'Outra publicação'):
                self.gate()
            self.assertEqual(self.snapshot(), before)
        finally:
            other.close()
