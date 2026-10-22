"""Only disposable PostgreSQL: real DDL, recorder, rollback and shared locks."""
from io import StringIO
from pathlib import Path
import importlib
import shutil
import subprocess
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase

from inventario.management.commands.publicar_cancelamento import Command, TARGET, RECOLHIMENTO, LOCK
from inventario.tests_cancelamento import Fixture


class PublicacaoCancelamentoTests(Fixture, TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.assertEqual(connection.settings_dict['HOST'], '127.0.0.1')
        self.assertEqual(connection.settings_dict['NAME'], 'test_inventario_disposable')
        super().setUp()
        self.acesso()
        self.recorder = MigrationRecorder(connection)
        self.addCleanup(self.restore)

    def restore(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def previous(self, inventory=True, pickup=False):
        targets = [TARGET, RECOLHIMENTO]
        if inventory:
            targets[0] = ('inventario', '0007_auditoria_ajuste_imutavel')
        if pickup:
            targets[1] = ('locacoes', '0016_locacao_horario_previsto_devolucao')
        MigrationExecutor(connection).migrate(targets)

    def snapshot(self, business=False):
        result = {}
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                if business and not table.startswith(('estoque_', 'inventario_', 'offline_', 'locacoes_')):
                    continue
                if business and table == 'locacoes_autorizacaorecolhimento':
                    continue
                columns = [c.name for c in connection.introspection.get_table_description(cursor, table)]
                if business and table == 'inventario_inventario':
                    columns = [c for c in columns if c not in ('cancelada_em', 'cancelada_por_id')]
                cursor.execute('SELECT ' + ', '.join(map(connection.ops.quote_name, columns))
                               + ' FROM ' + connection.ops.quote_name(table))
                result[table] = sorted(map(repr, cursor.fetchall()))
        return result

    def run_gate(self, *args):
        out = StringIO()
        call_command('publicar_cancelamento', *args, stdout=out)
        return out.getvalue()

    def test_somente_0008_preserva_todos_registros_e_retry_sem_escrita(self):
        self.previous()
        before = self.snapshot(business=True)
        self.assertIn('APLICAR inventario.0008', self.run_gate())
        self.assertEqual(before, self.snapshot(business=True))
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.cancelada_em)
        self.assertIsNone(self.inv.cancelada_por_id)
        self.assertTrue(self.inv.acessos.filter(revogado_em__isnull=True).exists())
        self.assertFalse(self.inv.eventos.filter(acao='conferencia_cancelada').exists())
        before = self.snapshot()
        self.assertIn('Nenhuma pendência', self.run_gate())
        self.assertEqual(before, self.snapshot())

    def test_0008_e_0017_pendentes_aplicadas_sem_outros_dados(self):
        self.previous(pickup=True)
        before = self.snapshot(business=True)
        output = self.run_gate()
        self.assertIn('APLICAR inventario.0008', output)
        self.assertIn('APLICAR locacoes.0017', output)
        self.assertEqual(before, self.snapshot(business=True))
        self.assertIn(TARGET, self.recorder.applied_migrations())
        self.assertIn(RECOLHIMENTO, self.recorder.applied_migrations())

    def test_somente_0017_pendente_continua_autorizada(self):
        self.previous(inventory=False, pickup=True)
        self.run_gate()
        self.assertIn(RECOLHIMENTO, self.recorder.applied_migrations())
        self.assertIn(TARGET, self.recorder.applied_migrations())

    def test_plan_e_somente_leitura(self):
        self.previous(pickup=True)
        before = self.snapshot()
        self.assertIn('APLICAR inventario.0008', self.run_gate('--plan'))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(TARGET, self.recorder.applied_migrations())

    def test_comando_original_recusa_0008_e_mantem_regra_0017(self):
        self.previous(pickup=True)
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            call_command('publicar_recolhimento', stdout=StringIO())
        self.assertEqual(before, self.snapshot())
        self.run_gate()
        self.previous(inventory=False, pickup=True)
        call_command('publicar_recolhimento', stdout=StringIO())
        self.assertIn(RECOLHIMENTO, self.recorder.applied_migrations())

    def unexpected(self, target):
        self.recorder.record_unapplied(*target)
        self.addCleanup(self.recorder.record_applied, *target)

    def test_dependencia_anterior_inventario_nao_autorizada(self):
        self.previous()
        self.unexpected(('inventario', '0007_auditoria_ajuste_imutavel'))
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_dependencia_anterior_locacoes_nao_autorizada(self):
        self.previous(pickup=True)
        self.unexpected(('locacoes', '0016_locacao_horario_previsto_devolucao'))
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_inesperada_bloqueia_mesmo_0008_ja_aplicada(self):
        self.unexpected(('offline', '0005_validate_local_sale_principal'))
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
            self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_historico_inconsistente_interrompe_sem_escrita(self):
        self.unexpected(('estoque', '0134_edicao_compra'))
        before = self.snapshot()
        from django.db.migrations.exceptions import InconsistentMigrationHistory
        with self.assertRaises(InconsistentMigrationHistory):
            self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_plano_revalidado_dentro_do_lock(self):
        self.previous()
        before = self.snapshot()
        original = Command.checked_plan
        calls = []

        def changed(command, executor):
            calls.append(True)
            if len(calls) == 2:
                self.recorder.record_unapplied('offline', '0005_validate_local_sale_principal')
                # Recreate the executor to observe the changed recorder in this transaction.
                executor = MigrationExecutor(connection)
            return original(command, executor)

        with patch.object(Command, 'checked_plan', changed):
            with self.assertRaisesMessage(CommandError, 'migrations inesperadas'):
                self.run_gate()
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, self.snapshot())

    def test_historico_com_migration_desconhecida_recusado(self):
        self.recorder.record_applied('inventario', '9999_desconhecida')
        self.addCleanup(self.recorder.record_unapplied, 'inventario', '9999_desconhecida')
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'ausentes nesta versão'):
            self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_falha_no_segundo_campo_desfaz_primeiro_campo_e_recorder(self):
        self.previous()
        before = self.snapshot()
        original = connection.SchemaEditorClass.add_field

        def failing(editor, model, field):
            if model._meta.label_lower == 'inventario.inventario' and field.name == 'cancelada_por':
                raise RuntimeError('falha DDL sintetica')
            return original(editor, model, field)

        with patch.object(connection.SchemaEditorClass, 'add_field', failing):
            with self.assertRaisesMessage(RuntimeError, 'falha DDL sintetica'):
                self.run_gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(TARGET, self.recorder.applied_migrations())
        self.run_gate()

    def test_falha_apos_migrations_desfaz_ddl_das_duas_e_recorder(self):
        self.previous(pickup=True)
        before = self.snapshot()
        with patch('inventario.management.commands.publicar_cancelamento.create_permissions',
                   side_effect=RuntimeError('falha permissoes sintetica')):
            with self.assertRaisesMessage(RuntimeError, 'falha permissoes sintetica'):
                self.run_gate()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(TARGET, self.recorder.applied_migrations())
        self.assertNotIn(RECOLHIMENTO, self.recorder.applied_migrations())

    def test_falha_verificacao_final_desfaz_0008(self):
        self.previous()
        before = self.snapshot()
        with patch.object(Command, 'verify_schema', side_effect=CommandError('verificacao sintetica')):
            with self.assertRaisesMessage(CommandError, 'verificacao sintetica'):
                self.run_gate()
        self.assertEqual(before, self.snapshot())

    def test_build_concorrente_compartilha_lock_com_recolhimento(self):
        self.previous()
        other = connection.copy(alias='cancelamento-publication-lock')
        try:
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [LOCK])
            before = self.snapshot()
            with self.assertRaisesMessage(CommandError, 'Outra publicação'):
                self.run_gate()
            self.assertEqual(before, self.snapshot())
        finally:
            other.close()

    def test_lock_ddl_tem_timeout_sem_mudar_dados(self):
        self.previous()
        other = connection.copy(alias='cancelamento-table-lock')
        try:
            other.set_autocommit(False)
            with other.cursor() as cursor:
                cursor.execute('LOCK TABLE inventario_inventario IN ACCESS SHARE MODE')
            before = self.snapshot()

            def short_timeouts(cursor):
                cursor.execute("SET LOCAL lock_timeout = '150ms'")
                cursor.execute("SET LOCAL statement_timeout = '2s'")

            from django.db import OperationalError
            with patch('inventario.management.commands.publicar_cancelamento.RecolhimentoCommand.timeouts', short_timeouts):
                with self.assertRaises(OperationalError):
                    self.run_gate()
            self.assertEqual(before, self.snapshot())
        finally:
            other.close()

    def test_noop_detecta_recorder_com_colunas_ausentes(self):
        self.previous()
        self.recorder.record_applied(*TARGET)
        self.addCleanup(self.recorder.record_unapplied, *TARGET)
        with self.assertRaisesMessage(CommandError, 'estrutura de cancelamento'):
            self.run_gate()

    def test_definicao_modificada_recusada_antes_de_ddl(self):
        self.previous()
        before = self.snapshot()
        migration = importlib.import_module('inventario.migrations.0008_inventario_cancelamento').Migration
        with patch.object(migration, 'operations', [migrations.RunSQL('SELECT 1')]):
            with self.assertRaisesMessage(CommandError, 'diverge'):
                self.run_gate()
        self.assertEqual(before, self.snapshot())


class BuildCancelamentoTests(SimpleTestCase):
    def test_build_interrompe_e_nao_autoriza_start_em_falha(self):
        bash = shutil.which('bash')
        git_bash = Path('C:/Program Files/Git/bin/bash.exe')
        if git_bash.is_file():
            bash = str(git_bash)
        self.assertTrue(bash, 'Bash necessário para validar o build real')
        root = Path(__file__).resolve().parent.parent
        for code in (0, 23):
            script = '''
pip() { echo DEPENDENCIAS; }
python() {
  case "$*" in
    *collectstatic*) echo ESTATICOS ;;
    *publicar_cancelamento*) echo MIGRATION; return CODE ;;
    *) echo COMANDO_INESPERADO; return 99 ;;
  esac
}
source build.sh
echo START_LIBERADO
'''.replace('CODE', str(code))
            result = subprocess.run([bash, '-c', script], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual('START_LIBERADO' in result.stdout, code == 0)
            self.assertLess(result.stdout.index('ESTATICOS'), result.stdout.index('MIGRATION'))
        self.assertIn('set -o errexit', (root / 'build.sh').read_text())
