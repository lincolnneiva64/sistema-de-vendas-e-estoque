"""One-release migration gate; never migrate unrelated pending changes."""
from django.apps import apps
from django.contrib.auth.management import create_permissions
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.models import CreateModel


TARGET = ('locacoes', '0017_autorizacao_recolhimento')
LOCK = 5161650017


class Command(BaseCommand):
    help = 'Consulta todas as pendências e aplica somente a autorização de recolhimento 0017.'
    requires_system_checks = []

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('Publicação recusada: PostgreSQL configurado é obrigatório.')
        # A refused preflight cannot even create django_migrations or permission rows.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                self.timeouts(cursor)
            if not self.checked_plan(MigrationExecutor(connection)):
                return
        # Transaction-scoped locks work with transaction-pooling connections as well.
        # Recheck under the lock; another build may have finished since preflight.
        with transaction.atomic():
            with connection.cursor() as cursor:
                self.timeouts(cursor)
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [LOCK])
                if not cursor.fetchone()[0]:
                    raise CommandError('Outra publicação do recolhimento está em andamento. Tente novamente depois.')
            executor = MigrationExecutor(connection)
            plan = self.checked_plan(executor)
            if not plan:
                return
            self.stdout.write('Somente locacoes.0017 pendente; dependências aplicadas. Executando autorização aprovada.')
            # Explicit approved plan only. DDL, record and permissions commit together.
            executor.migrate([TARGET], plan=plan)
            create_permissions(apps.get_app_config('locacoes'), verbosity=0, interactive=False)
        self.stdout.write('Migration 0017 aplicada. Permissões criadas, sem concedê-las a usuários. Build liberado.')

    @staticmethod
    def timeouts(cursor):
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '60s'")

    def checked_plan(self, executor):
        executor.loader.check_consistent_history(connection)
        if executor.loader.detect_conflicts():
            raise CommandError('Publicação recusada: há conflitos no grafo de migrations.')
        migration = executor.loader.disk_migrations.get(TARGET)
        if (migration is None or len(migration.operations) != 1
                or not isinstance(migration.operations[0], CreateModel)
                or migration.operations[0].name != 'AutorizacaoRecolhimento'):
            raise CommandError('Publicação recusada: a migration 0017 não corresponde à criação autorizada.')
        if set(executor.loader.applied_migrations) - set(executor.loader.disk_migrations):
            raise CommandError('Publicação recusada: o histórico contém migrations ausentes nesta versão.')
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self.stdout.write('Plano completo de migrations pendentes:')
        for item, backwards in plan:
            direction = 'REVERTER' if backwards else 'APLICAR'
            self.stdout.write(f'  {direction} {item.app_label}.{item.name}')
        if not plan:
            self.stdout.write('Nenhuma pendência. Migration 0017 já aplicada; build liberado.')
            return plan
        if len(plan) != 1 or plan[0][1] or (plan[0][0].app_label, plan[0][0].name) != TARGET:
            raise CommandError('Publicação bloqueada: há migrations inesperadas. Nenhuma migration foi executada.')
        return plan
