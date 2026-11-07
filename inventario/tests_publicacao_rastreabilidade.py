"""Strict release approval and real DDL only in guarded disposable PostgreSQL."""
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from .management.commands.publicar_cancelamento import (
    AUTHORIZED, CANCELAMENTO, RASTREABILIDADE, TARGET, Command,
)


class DefinicoesRastreabilidadeTests(SimpleTestCase):
    def test_definicoes_aprovadas_e_alvo(self):
        self.assertEqual(TARGET, RASTREABILIDADE[-1])
        self.assertTrue({CANCELAMENTO, *RASTREABILIDADE} <= AUTHORIZED)
        self.assertEqual(len(AUTHORIZED), 11)
        Command.validate_definitions(MigrationLoader(None))

    def test_cada_migration_ausente_ou_adulterada_e_recusada(self):
        for key in RASTREABILIDADE:
            for attribute, value in (
                ('dependencies', []), ('atomic', False), ('initial', True),
                ('run_before', [CANCELAMENTO]), ('replaces', [CANCELAMENTO]),
                ('operations', [migrations.RunSQL('SELECT 1')]),
            ):
                loader = MigrationLoader(None)
                with self.subTest(key=key, attribute=attribute), patch.object(
                        loader.disk_migrations[key], attribute, value):
                    with self.assertRaisesMessage(CommandError, key[1]):
                        Command.validate_definitions(loader)
            loader = MigrationLoader(None)
            loader.disk_migrations.pop(key)
            with self.assertRaisesMessage(CommandError, key[1]):
                Command.validate_definitions(loader)

    def test_elidable_alterado_recusado(self):
        for key in RASTREABILIDADE:
            loader = MigrationLoader(None)
            operations = deepcopy(loader.disk_migrations[key].operations)
            operations[0].elidable = True
            with self.subTest(key=key), patch.object(loader.disk_migrations[key], 'operations', operations):
                with self.assertRaisesMessage(CommandError, key[1]):
                    Command.validate_definitions(loader)

    def test_campos_e_constraint_alterados_recusados(self):
        for index, key in enumerate(RASTREABILIDADE):
            loader = MigrationLoader(None)
            operations = deepcopy(loader.disk_migrations[key].operations)
            if index == 0:
                operations[0].options['constraints'] = []
            elif index == 1:
                operations[1].field.max_length = 33
            elif index == 2:
                operations[2].field.max_digits = 26
            else:
                operations[0].field.db_default = {'extra': True}
            with self.subTest(key=key), patch.object(loader.disk_migrations[key], 'operations', operations):
                with self.assertRaisesMessage(CommandError, key[1]):
                    Command.validate_definitions(loader)

    def test_corpo_runpython_alterado_recusado(self):
        with patch('inventario.management.commands.publicar_cancelamento.inspect.getsource', return_value='pass'):
            with self.assertRaisesMessage(CommandError, 'não autorizada'):
                Command.validate_traceability_definitions(MigrationLoader(None))


class PublicacaoRastreabilidadeTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'test_inventario_disposable')
        self.addCleanup(self.restore)
        MigrationExecutor(connection).migrate([CANCELAMENTO])

    def restore(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def gate(self, *args):
        output = StringIO()
        call_command('publicar_cancelamento', *args, stdout=output)
        return output.getvalue()

    def assert_baseline(self):
        self.assertFalse(set(RASTREABILIDADE) & set(MigrationRecorder(connection).applied_migrations()))
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('inventario_detalhemovimentoestoque'), to_regprocedure('inventario_detalhe_imutavel_fn()')")
            self.assertEqual(cursor.fetchone(), (None, None))

    def test_aplica_cadeia_e_segunda_execucao_sem_escritas(self):
        output = self.gate()
        positions = [output.index('APLICAR ' + '.'.join(key)) for key in RASTREABILIDADE]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(set(RASTREABILIDADE) <= set(MigrationRecorder(connection).applied_migrations()))
        with CaptureQueriesContext(connection) as queries:
            self.assertIn('Nenhuma pendência', self.gate())
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in
                             {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP'} for q in queries))

    def test_plan_nao_aplica(self):
        self.assertIn('APLICAR inventario.0012', self.gate('--plan'))
        self.assert_baseline()

    def test_extra_recusada_antes_de_ddl(self):
        original = MigrationLoader.load_disk
        def extra(loader):
            original(loader)
            migration = migrations.Migration('0013_inesperada', 'inventario')
            migration.dependencies = [TARGET]
            migration.operations = [migrations.RunSQL('CREATE TABLE nunca_autorizada (id integer)')]
            loader.disk_migrations[('inventario', migration.name)] = migration
        with patch.object(MigrationLoader, 'load_disk', extra):
            with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
                self.gate()
        self.assert_baseline()

    def test_adulteracao_recusada_antes_de_ddl(self):
        original = Command.validate_traceability_definitions
        def changed(loader):
            loader.disk_migrations[RASTREABILIDADE[-1]].dependencies = [CANCELAMENTO]
            original(loader)
        with patch.object(Command, 'validate_traceability_definitions', changed):
            with self.assertRaisesMessage(CommandError, '0012'):
                self.gate()
        self.assert_baseline()

    def test_falha_ddl_0012_reverte_toda_cadeia_funcao_e_recorder(self):
        original = connection.SchemaEditorClass.add_field
        def fail(editor, model, field):
            result = original(editor, model, field)
            if model._meta.label_lower == 'inventario.detalhemovimentoestoque' and field.name == 'evidencia':
                raise RuntimeError('falha depois do DDL 0012')
            return result
        with patch.object(connection.SchemaEditorClass, 'add_field', fail):
            with self.assertRaisesMessage(RuntimeError, 'falha depois do DDL'):
                self.gate()
        self.assert_baseline()
        self.gate()

    def test_falha_verificacao_final_reverte_cadeia(self):
        with patch.object(Command, 'verify_traceability_schema', side_effect=CommandError('schema inválido')):
            with self.assertRaisesMessage(CommandError, 'schema inválido'):
                self.gate()
        self.assert_baseline()

    def test_noop_recusa_coluna_ou_trigger_ausente(self):
        self.gate()
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE inventario_detalhemovimentoestoque RENAME COLUMN evidencia TO evidencia_ausente')
        try:
            with self.assertRaisesMessage(CommandError, 'colunas/default'):
                self.gate()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE inventario_detalhemovimentoestoque RENAME COLUMN evidencia_ausente TO evidencia')
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE inventario_detalhemovimentoestoque DISABLE TRIGGER inventario_detalhe_imutavel')
        try:
            with self.assertRaisesMessage(CommandError, 'imutabilidade'):
                self.gate()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE inventario_detalhemovimentoestoque ENABLE TRIGGER inventario_detalhe_imutavel')
