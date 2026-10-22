"""Release gate for exactly inventory cancellation and the previous pickup approval."""
from django.apps import apps
from django.conf import settings
from django.contrib.auth.management import create_permissions
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, migrations, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.models import CreateModel

from locacoes.management.commands.publicar_recolhimento import (
    Command as RecolhimentoCommand, LOCK, TARGET as RECOLHIMENTO,
)


TARGET = ('inventario', '0008_inventario_cancelamento')
AUTHORIZED = frozenset((RECOLHIMENTO, TARGET))


class Command(BaseCommand):
    help = 'Valida todas as pendências e aplica exclusivamente inventario.0008 e, se pendente, locacoes.0017.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--plan', action='store_true', help='Valida e mostra o plano, sem gravar no banco.')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('Publicação recusada: PostgreSQL é obrigatório.')
        # A refused preflight cannot create schema, records or permissions.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                RecolhimentoCommand.timeouts(cursor)
            plan = self.checked_plan(MigrationExecutor(connection))
        if options['plan'] or not plan:
            return

        # Same lock as publicar_recolhimento: both entry points exclude each other.
        with transaction.atomic():
            with connection.cursor() as cursor:
                RecolhimentoCommand.timeouts(cursor)
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [LOCK])
                if not cursor.fetchone()[0]:
                    raise CommandError('Outra publicação autorizada está em andamento. Tente novamente depois.')
            executor = MigrationExecutor(connection)
            plan = self.checked_plan(executor)
            if not plan:
                return
            executor.migrate([RECOLHIMENTO, TARGET], plan=plan)
            if any((m.app_label, m.name) == RECOLHIMENTO for m, _ in plan):
                create_permissions(apps.get_app_config('locacoes'), verbosity=0, interactive=False)
            # A later failure here also rolls back both DDL and migration records.
            self.verify_schema()
        self.stdout.write('Migrations autorizadas aplicadas. Nenhuma conferência cancelada; build liberado.')

    def checked_plan(self, executor):
        executor.loader.check_consistent_history(connection)
        if executor.loader.detect_conflicts():
            raise CommandError('Publicação recusada: conflitos no grafo de migrations.')
        if set(executor.loader.applied_migrations) - set(executor.loader.disk_migrations):
            raise CommandError('Publicação recusada: migrations aplicadas ausentes nesta versão.')
        self.validate_definitions(executor.loader)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self.stdout.write('Plano completo de migrations pendentes:')
        for migration, backwards in plan:
            self.stdout.write(f'  {"REVERTER" if backwards else "APLICAR"} {migration.app_label}.{migration.name}')
        if any(backwards or (migration.app_label, migration.name) not in AUTHORIZED
               for migration, backwards in plan):
            raise CommandError('Publicação bloqueada: migrations inesperadas. Nenhuma migration foi executada.')
        if not plan:
            self.verify_schema()
            self.stdout.write('Nenhuma pendência; estrutura de cancelamento conferida. Build liberado.')
        return plan

    @staticmethod
    def validate_definitions(loader):
        legacy = loader.disk_migrations.get(RECOLHIMENTO)
        # Preserve exactly the original pickup gate's definition check and target.
        if (legacy is None or not legacy.atomic or len(legacy.operations) != 1
                or not isinstance(legacy.operations[0], CreateModel)
                or legacy.operations[0].name != 'AutorizacaoRecolhimento'):
            raise CommandError('Publicação recusada: locacoes.0017 não corresponde à criação autorizada.')
        current = loader.disk_migrations.get(TARGET)
        expected = [
            ('cancelada_em', models.DateTimeField(blank=True, null=True)),
            ('cancelada_por', models.ForeignKey(blank=True, null=True,
                on_delete=models.PROTECT, related_name='inventarios_cancelados', to=settings.AUTH_USER_MODEL)),
        ]
        dependencies = {('inventario', '0007_auditoria_ajuste_imutavel'),
                        migrations.swappable_dependency(settings.AUTH_USER_MODEL)}
        if (current is None or not current.atomic or current.replaces
                or set(current.dependencies) != dependencies or len(current.operations) != len(expected)):
            raise CommandError('Publicação recusada: inventario.0008 diverge da migration autorizada.')
        for operation, (name, field) in zip(current.operations, expected):
            if (type(operation) is not migrations.AddField or operation.model_name != 'inventario'
                    or operation.name != name or not operation.preserve_default
                    or operation.field.deconstruct()[1:] != field.deconstruct()[1:]):
                raise CommandError('Publicação recusada: inventario.0008 deve adicionar somente os campos opcionais autorizados.')

    @staticmethod
    def verify_schema():
        with connection.cursor() as cursor:
            columns = {c.name: c for c in connection.introspection.get_table_description(cursor, 'inventario_inventario')}
        for name in ('cancelada_em', 'cancelada_por_id'):
            if name not in columns or not columns[name].null_ok:
                raise CommandError('Publicação recusada: estrutura de cancelamento ausente ou incompatível.')
