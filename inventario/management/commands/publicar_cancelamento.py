"""Release gate for the explicit inventory, pickup and purchase approvals."""
import hashlib
import inspect

from django.apps import apps
from django.conf import settings
from django.contrib.auth.management import create_permissions
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, migrations, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.writer import MigrationWriter
from django.db.migrations.operations.models import CreateModel

from locacoes.management.commands.publicar_recolhimento import (
    Command as RecolhimentoCommand, LOCK, TARGET as RECOLHIMENTO,
)


TARGET = ('inventario', '0008_inventario_cancelamento')
COMPRA = ('estoque', '0135_conciliacao_compra')
PRECISAO = ('estoque', '0136_itemvenda_quantidade_precisao')
COLETA = ('estoque', '0137_lista_coleta_mobile')
AUTHORIZED = frozenset((RECOLHIMENTO, TARGET, COMPRA, PRECISAO, COLETA))


class Command(BaseCommand):
    help = 'Valida todas as pendências e aplica somente inventario.0008, locacoes.0017 e estoque.0135–0137 autorizadas.'
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
            # The final stock target includes exactly the checked 0135 -> 0136 -> 0137 chain.
            executor.migrate([RECOLHIMENTO, TARGET, COLETA], plan=plan)
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
        compra = loader.disk_migrations.get(COMPRA)
        expected_compra = [
            migrations.AddField(model_name='compra', name='conciliacao_automatica',
                field=models.BooleanField(default=False, db_default=False, editable=False)),
            migrations.AddField(model_name='itemcompra', name='estoque_base_registrado',
                field=models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, editable=False)),
            migrations.CreateModel(name='ConciliacaoCompra', fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tipo', models.CharField(max_length=24)),
                ('valor_pago', models.DecimalField(max_digits=12, decimal_places=2)),
                ('valor_devolvido', models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                ('credito_fornecedor', models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                ('saldo_a_pagar', models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                ('efeitos', models.JSONField(default=dict)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
                ('compra', models.OneToOneField(on_delete=models.PROTECT, related_name='conciliacao', to='estoque.compra')),
                ('edicao', models.OneToOneField(on_delete=models.PROTECT, related_name='conciliacao', to='estoque.edicaocompra')),
            ]),
        ]
        if (compra is None or not compra.atomic or compra.replaces
                or compra.dependencies != [('estoque', '0134_edicao_compra')]
                or compra.run_before
                or MigrationWriter.serialize(compra.operations)[0] != MigrationWriter.serialize(expected_compra)[0]):
            raise CommandError('Publicação recusada: estoque.0135 diverge da definição autorizada.')

        precisao = loader.disk_migrations.get(PRECISAO)
        if (precisao is None or not precisao.atomic or precisao.replaces or precisao.run_before
                or precisao.dependencies != [COMPRA] or len(precisao.operations) != 1):
            raise CommandError('Publicação recusada: estoque.0136 diverge da definição autorizada.')
        operation = precisao.operations[0]
        if (type(operation) is not migrations.RunPython or operation.reverse_code is not None
                or operation.atomic is not None or operation.hints or operation.elidable
                or getattr(operation.code, '__module__', '') != 'estoque.migrations.0136_itemvenda_quantidade_precisao'
                or getattr(operation.code, '__name__', '') != 'corrigir_precisao'):
            raise CommandError('Publicação recusada: estoque.0136 deve executar somente a correção de precisão autorizada.')
        # Pin the approved Python body, not just its import name; normalize CRLF only.
        try:
            source = inspect.getsource(operation.code).replace('\r\n', '\n').strip()
            digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        except (OSError, TypeError) as exc:
            raise CommandError('Publicação recusada: não foi possível validar o código de estoque.0136.') from exc
        if digest != '6c78f06e6bdd7f168183bb11bb542881854294c11403542bc3d3e4dd6112abea':
            raise CommandError('Publicação recusada: código de estoque.0136 diverge da correção autorizada.')

        coleta = loader.disk_migrations.get(COLETA)
        expected_coleta = [
            migrations.AddField(model_name='listacomprafornecedor', name='coleta_mobile',
                field=models.JSONField(null=True, blank=True)),
            migrations.AlterField(model_name='listacomprafornecedor', name='coleta_mobile',
                field=models.JSONField(default=dict, null=True, blank=True)),
        ]
        if (coleta is None or not coleta.atomic or coleta.replaces or coleta.run_before
                or coleta.dependencies != [PRECISAO]
                or MigrationWriter.serialize(coleta.operations)[0] != MigrationWriter.serialize(expected_coleta)[0]):
            raise CommandError('Publicação recusada: estoque.0137 diverge da definição autorizada.')

    @staticmethod
    def verify_schema():
        with connection.cursor() as cursor:
            columns = {c.name: c for c in connection.introspection.get_table_description(cursor, 'inventario_inventario')}
        for name in ('cancelada_em', 'cancelada_por_id'):
            if name not in columns or not columns[name].null_ok:
                raise CommandError('Publicação recusada: estrutura de cancelamento ausente ou incompatível.')
        expected = {
            'estoque_itemvenda': {'quantidade': ('numeric(12,3)', True, None)},
            'estoque_listacomprafornecedor': {'coleta_mobile': ('jsonb', False, None)},
            'estoque_compra': {'conciliacao_automatica': ('boolean', True, 'false')},
            'estoque_itemcompra': {'estoque_base_registrado': ('numeric(12,3)', False, None)},
            'estoque_conciliacaocompra': {
                'id': ('bigint', True, None), 'tipo': ('character varying(24)', True, None),
                'valor_pago': ('numeric(12,2)', True, None),
                'valor_devolvido': ('numeric(12,2)', True, None),
                'credito_fornecedor': ('numeric(12,2)', True, None),
                'saldo_a_pagar': ('numeric(12,2)', True, None),
                'efeitos': ('jsonb', True, None),
                'atualizado_em': ('timestamp with time zone', True, None),
                'compra_id': ('bigint', True, None), 'edicao_id': ('bigint', True, None),
            },
        }
        with connection.cursor() as cursor:
            for table, fields in expected.items():
                cursor.execute('''SELECT a.attname, format_type(a.atttypid, a.atttypmod),
                        a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
                    FROM pg_attribute a LEFT JOIN pg_attrdef d
                      ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped''', [table])
                actual = {name: (kind, required, default) for name, kind, required, default in cursor.fetchall()}
                if any(actual.get(name) != definition for name, definition in fields.items()):
                    name = ('0136' if table == 'estoque_itemvenda' else
                            '0137' if table == 'estoque_listacomprafornecedor' else '0135')
                    raise CommandError(f'Publicação recusada: estrutura/default de estoque.{name} ausente ou incompatível.')
            constraints = connection.introspection.get_constraints(cursor, 'estoque_conciliacaocompra')
            for column, target in [('compra_id', 'estoque_compra'), ('edicao_id', 'estoque_edicaocompra')]:
                if (not any(c['columns'] == [column] and c['unique'] for c in constraints.values())
                        or not any(c['columns'] == [column] and c['foreign_key'] == (target, 'id') for c in constraints.values())):
                    raise CommandError('Publicação recusada: vínculos de estoque.0135 ausentes ou incompatíveis.')
            if not any(c['columns'] == ['id'] and c['primary_key'] for c in constraints.values()):
                raise CommandError('Publicação recusada: chave de estoque.0135 ausente ou incompatível.')
