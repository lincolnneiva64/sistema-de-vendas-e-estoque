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
COMPROVANTE = ('locacoes', '0018_comprovante_cliente')
CORRECAO = ('locacoes', '0019_correcao_pagamento_direto')
AUTHORIZED = frozenset((RECOLHIMENTO, COMPROVANTE, CORRECAO, TARGET, COMPRA, PRECISAO, COLETA))


class Command(BaseCommand):
    help = 'Valida todas as pendências e aplica somente inventario.0008, locacoes.0017–0019 e estoque.0135–0137 autorizadas.'
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
            executor.migrate([CORRECAO, TARGET, COLETA], plan=plan)
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
        if plan and CORRECAO in executor.loader.applied_migrations:
            with connection.cursor() as cursor:
                self.verify_correction_schema(cursor)
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
        receipt = loader.disk_migrations.get(COMPROVANTE)
        expected_receipt = [
            ('comprovante_token_hash', models.CharField(
                max_length=64, null=True, blank=True, unique=True, editable=False)),
            ('comprovante_expira_em', models.DateTimeField(null=True, blank=True, editable=False)),
            ('comprovante_revogado_em', models.DateTimeField(null=True, blank=True, editable=False)),
        ]
        if (receipt is None or receipt.atomic is not True or receipt.replaces or receipt.run_before
                or receipt.initial is not None or receipt.dependencies != [RECOLHIMENTO]
                or len(receipt.operations) != len(expected_receipt)):
            raise CommandError('Publicação recusada: locacoes.0018 diverge da migration autorizada.')
        for operation, (name, field) in zip(receipt.operations, expected_receipt):
            if (type(operation) is not migrations.AddField
                    or operation.model_name != 'conferenciarecolhimentolocacao'
                    or operation.name != name or operation.preserve_default is not True
                    or operation.elidable is not False
                    or type(operation.field) is not type(field)
                    or operation.field.deconstruct()[1:] != field.deconstruct()[1:]):
                raise CommandError('Publicação recusada: locacoes.0018 deve adicionar somente os três campos opcionais autorizados.')
        Command.validate_correction_definition(loader)
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
    def validate_correction_definition(loader):
        correction = loader.disk_migrations.get(CORRECAO)
        expected = migrations.CreateModel(
            name='CorrecaoPagamentoLocacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('operacao_uuid', models.UUIDField(unique=True, editable=False)),
                ('versao_anterior', models.CharField(max_length=64, editable=False)),
                ('requisicao_hash', models.CharField(max_length=64, editable=False)),
                ('antes', models.JSONField(editable=False)),
                ('depois', models.JSONField(editable=False)),
                ('motivo', models.TextField()),
                ('usuario_nome', models.CharField(max_length=150, editable=False)),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('recibo_anterior_html', models.TextField(editable=False)),
                ('recibo_corrigido_html', models.TextField(editable=False)),
                ('pagamento', models.ForeignKey(on_delete=models.PROTECT, related_name='correcoes', to='locacoes.pagamentolocacao')),
                ('usuario', models.ForeignKey(on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('movimento_original', models.ForeignKey(on_delete=models.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
                ('estorno', models.OneToOneField(on_delete=models.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
                ('movimento_novo', models.OneToOneField(on_delete=models.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
            ],
            options={'ordering': ['criado_em', 'pk'], 'constraints': [
                models.UniqueConstraint(fields=('pagamento', 'versao_anterior'), name='locacao_correcao_versao_unica'),
            ]},
        )
        if (correction is None or correction.atomic is not True or correction.replaces or correction.run_before
                or correction.initial is not None
                or correction.dependencies != [COMPROVANTE, ('estoque', '0056_contafinanceira_movimentofinanceiro'),
                                               migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
                or len(correction.operations) != 1
                or type(correction.operations[0]) is not migrations.CreateModel
                or correction.operations[0].elidable is not False
                or MigrationWriter.serialize(correction.operations)[0] != MigrationWriter.serialize([expected])[0]):
            raise CommandError('Publicação recusada: locacoes.0019 deve criar somente a tabela de auditoria exatamente como autorizada.')

    @staticmethod
    def verify_correction_schema(cursor):
        table = 'locacoes_correcaopagamentolocacao'
        user = apps.get_model(settings.AUTH_USER_MODEL)
        types = {
            'id': 'bigint', 'operacao_uuid': 'uuid', 'versao_anterior': 'character varying(64)',
            'requisicao_hash': 'character varying(64)', 'antes': 'jsonb', 'depois': 'jsonb',
            'motivo': 'text', 'usuario_nome': 'character varying(150)', 'criado_em': 'timestamp with time zone',
            'recibo_anterior_html': 'text', 'recibo_corrigido_html': 'text',
            'pagamento_id': 'bigint', 'usuario_id': user._meta.pk.rel_db_type(connection),
            'movimento_original_id': 'bigint', 'estorno_id': 'bigint', 'movimento_novo_id': 'bigint',
        }
        cursor.execute('SELECT relkind, relpersistence FROM pg_class WHERE oid = to_regclass(%s)', [table])
        if cursor.fetchone() != ('r', 'p'):
            raise CommandError('Publicação recusada: tabela de locacoes.0019 ausente ou incompatível.')
        cursor.execute('''SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped''', [table])
        actual = {name: (kind, required, default, identity, generated)
                  for name, kind, required, default, identity, generated in cursor.fetchall()}
        expected = {name: (kind, True, None, 'd' if name == 'id' else '', '') for name, kind in types.items()}
        if actual != expected:
            raise CommandError('Publicação recusada: campos/tipos/defaults de locacoes.0019 ausentes ou incompatíveis.')

        cursor.execute('''SELECT c.conname, c.contype,
                ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num, ord)
                    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num ORDER BY k.ord),
                CASE WHEN c.contype = 'f' THEN c.confrelid::regclass::text END,
                ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num, ord)
                    JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.num ORDER BY k.ord),
                c.convalidated, c.condeferrable, c.condeferred, c.confupdtype, c.confdeltype, c.confmatchtype,
                i.indisvalid, i.indisready, i.indpred IS NULL, i.indexprs IS NULL
            FROM pg_constraint c LEFT JOIN pg_index i ON i.indexrelid = c.conindid
            WHERE c.conrelid = to_regclass(%s) AND c.contype <> 'n' ''', [table])
        # PostgreSQL 18 also exposes NOT NULL as constraints (type n); those are
        # already checked through attnotnull above. All other constraints are pinned.
        rows = cursor.fetchall()
        expected_keys = {('p', ('id',))} | {('u', columns) for columns in (
            ('operacao_uuid',), ('pagamento_id', 'versao_anterior'), ('estorno_id',), ('movimento_novo_id',))}
        expected_fks = {
            ('pagamento_id',): ('locacoes_pagamentolocacao', ('id',)),
            ('usuario_id',): (user._meta.db_table, (user._meta.pk.column,)),
            ('movimento_original_id',): ('estoque_movimentofinanceiro', ('id',)),
            ('estorno_id',): ('estoque_movimentofinanceiro', ('id',)),
            ('movimento_novo_id',): ('estoque_movimentofinanceiro', ('id',)),
        }
        seen_keys, seen_fks = set(), set()
        for (name, kind, columns, target, target_columns, valid, deferred, initially_deferred,
             on_update, on_delete, match, index_valid, index_ready, full_index, plain_index) in rows:
            columns = tuple(columns)
            if kind in {'p', 'u'}:
                key = (kind, columns)
                good = (key in expected_keys and key not in seen_keys and valid and not deferred
                        and not initially_deferred and index_valid and index_ready and full_index and plain_index)
                if columns == ('pagamento_id', 'versao_anterior'):
                    good = good and name == 'locacao_correcao_versao_unica'
                seen_keys.add(key)
            elif kind == 'f':
                good = (columns in expected_fks and columns not in seen_fks
                        and (target, tuple(target_columns)) == expected_fks.get(columns)
                        and valid and deferred and initially_deferred
                        and on_update == on_delete == 'a' and match == 's')
                seen_fks.add(columns)
            else:
                good = False
            if not good:
                raise CommandError('Publicação recusada: chaves/unicidade/FKs de locacoes.0019 incompatíveis.')
        if seen_keys != expected_keys or seen_fks != set(expected_fks):
            raise CommandError('Publicação recusada: chaves/unicidade/FKs de locacoes.0019 ausentes.')

    @staticmethod
    def verify_schema():
        with connection.cursor() as cursor:
            columns = {c.name: c for c in connection.introspection.get_table_description(cursor, 'inventario_inventario')}
        for name in ('cancelada_em', 'cancelada_por_id'):
            if name not in columns or not columns[name].null_ok:
                raise CommandError('Publicação recusada: estrutura de cancelamento ausente ou incompatível.')
        expected = {
            'locacoes_conferenciarecolhimentolocacao': {
                'comprovante_token_hash': ('character varying(64)', False, None),
                'comprovante_expira_em': ('timestamp with time zone', False, None),
                'comprovante_revogado_em': ('timestamp with time zone', False, None),
            },
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
                    if table == 'locacoes_conferenciarecolhimentolocacao':
                        raise CommandError('Publicação recusada: estrutura/default de locacoes.0018 ausente ou incompatível.')
                    name = ('0136' if table == 'estoque_itemvenda' else
                            '0137' if table == 'estoque_listacomprafornecedor' else '0135')
                    raise CommandError(f'Publicação recusada: estrutura/default de estoque.{name} ausente ou incompatível.')
            cursor.execute('''SELECT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'comprovante_token_hash'
                JOIN pg_index i ON i.indexrelid = c.conindid
                WHERE c.conrelid = to_regclass('locacoes_conferenciarecolhimentolocacao')
                  AND c.contype = 'u' AND c.conkey = ARRAY[a.attnum]
                  AND c.convalidated AND NOT c.condeferrable
                  AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND i.indexprs IS NULL
            )''')
            if not cursor.fetchone()[0]:
                raise CommandError('Publicação recusada: unicidade do hash de locacoes.0018 ausente ou incompatível.')
            constraints = connection.introspection.get_constraints(cursor, 'estoque_conciliacaocompra')
            for column, target in [('compra_id', 'estoque_compra'), ('edicao_id', 'estoque_edicaocompra')]:
                if (not any(c['columns'] == [column] and c['unique'] for c in constraints.values())
                        or not any(c['columns'] == [column] and c['foreign_key'] == (target, 'id') for c in constraints.values())):
                    raise CommandError('Publicação recusada: vínculos de estoque.0135 ausentes ou incompatíveis.')
            if not any(c['columns'] == ['id'] and c['primary_key'] for c in constraints.values()):
                raise CommandError('Publicação recusada: chave de estoque.0135 ausente ou incompatível.')
            Command.verify_correction_schema(cursor)
