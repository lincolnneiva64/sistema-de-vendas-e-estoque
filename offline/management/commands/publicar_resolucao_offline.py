"""Opt-in, one-migration release gate. Never called by the Render build."""
import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, migrations, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.writer import MigrationWriter

from locacoes.management.commands.publicar_recolhimento import LOCK, Command as ExistingGate


TARGET = ('offline', '0006_resolucao_venda_offline')
APPROVAL = '.'.join(TARGET)
SOURCE_SHA256 = '37b81bca1f8fc67bf9ff429e1feb3f1274ab9315866168b28adf39e297fbbbb3'
TABLES = ('offline_controleresolucaovendaoffline', 'offline_abandonovendaoffline')


def refuse(message):
    raise CommandError('Aplicação da 0006 recusada: ' + message)


def validate_source():
    if getattr(settings, 'MIGRATION_MODULES', {}).get('offline', 'offline.migrations') != 'offline.migrations':
        refuse('módulo de migrations substituído.')
    source = Path(__file__).resolve().parents[2] / 'migrations' / (TARGET[1] + '.py')
    digest = hashlib.sha256(source.read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip().encode()).hexdigest()
    if digest != SOURCE_SHA256:
        refuse('definição do arquivo diverge da revisão autorizada.')


def validate_definition(loader):
    """Also check the loaded object: source hash alone cannot detect runtime patches."""
    expected = [
        migrations.CreateModel(name='ControleResolucaoVendaOffline', fields=[
            ('actor', models.OneToOneField(primary_key=True, serialize=False, on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='AbandonoVendaOffline', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('operation_id', models.UUIDField(unique=True)), ('closure_id', models.UUIDField(unique=True)),
            ('environment_id', models.CharField(max_length=160)), ('device_id', models.UUIDField()),
            ('payload_hash', models.CharField(max_length=64)), ('comando', models.JSONField()),
            ('motivo', models.TextField()), ('evidencia', models.JSONField()),
            ('encerrado_em', models.DateTimeField(auto_now_add=True)),
            ('actor', models.ForeignKey(on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL)),
        ]),
    ]
    item = loader.disk_migrations.get(TARGET)
    if (item is None or item.dependencies != [
            ('offline', '0005_validate_local_sale_principal'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
            or not item.atomic or item.initial is not None or item.replaces or item.run_before
            or MigrationWriter.serialize(item.operations)[0] != MigrationWriter.serialize(expected)[0]
            or any(type(op) is not migrations.CreateModel or op.elidable for op in item.operations)):
        refuse('definição carregada diverge da revisão autorizada.')


def verify_schema(cursor):
    """Exact public tables, columns, constraints and indexes; never repair or fake."""
    user = get_user_model()._meta
    actor_type = user.pk.db_type(connection)
    expected_columns = [
        {'actor_id': (actor_type, True, None, '', '')},
        {
            'id': ('bigint', True, None, 'd', ''),
            'operation_id': ('uuid', True, None, '', ''), 'closure_id': ('uuid', True, None, '', ''),
            'environment_id': ('character varying(160)', True, None, '', ''),
            'device_id': ('uuid', True, None, '', ''),
            'payload_hash': ('character varying(64)', True, None, '', ''),
            'comando': ('jsonb', True, None, '', ''), 'motivo': ('text', True, None, '', ''),
            'evidencia': ('jsonb', True, None, '', ''),
            'encerrado_em': ('timestamp with time zone', True, None, '', ''),
            'actor_id': (actor_type, True, None, '', ''),
        },
    ]
    for table, columns in zip(TABLES, expected_columns):
        cursor.execute("""SELECT c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname=%s""", [table])
        if cursor.fetchone() != ('r', 'p', False, False):
            refuse('schema inesperado: tabela ' + table)
        cursor.execute("""SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid), a.attidentity, a.attgenerated
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped""", ['public.' + table])
        if {row[0]: tuple(row[1:]) for row in cursor.fetchall()} != columns:
            refuse('schema inesperado: colunas de ' + table)
        cursor.execute("""SELECT c.contype,
            ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.ord),
            c.convalidated,c.condeferrable,c.condeferred,
            CASE WHEN c.contype='f' THEN c.confrelid::regclass::text END,
            ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.ord),
            c.confupdtype,c.confdeltype,c.confmatchtype
            FROM pg_constraint c WHERE c.conrelid=%s::regclass AND c.contype <> 'n'""", ['public.' + table])
        actual = set()
        for kind, cols, valid, defer, deferred, target, target_cols, update, delete, match in cursor.fetchall():
            actual.add((kind, tuple(cols), valid, defer, deferred, target, tuple(target_cols),
                        update if kind == 'f' else '', delete if kind == 'f' else '', match if kind == 'f' else ''))
        primary = 'actor_id' if table == TABLES[0] else 'id'
        expected = {('p', (primary,), True, False, False, None, (), '', '', ''),
                    ('f', ('actor_id',), True, True, True, user.db_table, (user.pk.column,), 'a', 'a', 's')}
        if table == TABLES[1]:
            expected |= {('u', (col,), True, False, False, None, (), '', '', '') for col in ('operation_id', 'closure_id')}
        if actual != expected:
            refuse('schema inesperado: restrições de ' + table)
        cursor.execute("""SELECT ARRAY(SELECT a.attname FROM unnest(i.indkey::smallint[]) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num ORDER BY k.ord),
            i.indisunique,i.indisprimary,i.indisvalid,i.indisready,i.indpred IS NULL,
            i.indexprs IS NULL,am.amname,i.indnkeyatts=i.indnatts
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam
            WHERE i.indrelid=%s::regclass""", ['public.' + table])
        indexes = [tuple([tuple(row[0]), *row[1:]]) for row in cursor.fetchall()]
        expected_indexes = [((primary,), True, True, True, True, True, True, 'btree', True)]
        if table == TABLES[1]:
            expected_indexes += [((col,), True, False, True, True, True, True, 'btree', True) for col in ('operation_id', 'closure_id')]
            expected_indexes += [(('actor_id',), False, False, True, True, True, True, 'btree', True)]
        if sorted(indexes) != sorted(expected_indexes):
            refuse('schema inesperado: índices de ' + table)
        cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal)
            OR EXISTS(SELECT 1 FROM pg_rewrite WHERE ev_class=%s::regclass)""", ['public.' + table] * 2)
        if cursor.fetchone()[0]:
            refuse('schema inesperado: triggers ou regras de ' + table)


class Command(BaseCommand):
    help = 'Plano ou aplicação controlada exclusivamente de offline.0006 (fora do build).'
    requires_system_checks = []

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument('--plan', action='store_true')
        action.add_argument('--apply', action='store_true')
        parser.add_argument('--environment', choices=('isolated', 'production'), required=True)
        for name in ('host', 'port', 'database', 'user'):
            parser.add_argument('--expected-' + name, required=True)
        parser.add_argument('--expected-data-directory')
        parser.add_argument('--authorize-production')
        parser.add_argument('--approval-reference')

    def check_destination(self, options):
        if connection.vendor != 'postgresql' or connection.in_atomic_block:
            refuse('PostgreSQL e transação externa livre são obrigatórios.')
        if options['environment'] == 'production':
            if options.get('authorize_production') != APPROVAL or not (options.get('approval_reference') or '').strip():
                refuse('produção exige autorização explícita e referência da aprovação, inclusive para consulta.')
        elif (options['expected_host'] != '127.0.0.1'
              or not options['expected_database'].startswith('test_offline_0006_')
              or not options.get('expected_data_directory')):
            refuse('destino isolado exige loopback, banco de teste e diretório descartável explícito.')
        params = connection.get_connection_params()
        expected = {k: str(options['expected_' + opt]) for k, opt in
                    [('host', 'host'), ('port', 'port'), ('dbname', 'database'), ('user', 'user')]}
        if any(str(params.get(k, '')) != value or not value for k, value in expected.items()):
            refuse('destino configurado difere da identidade autorizada.')
        if (any(params.get(k) for k in ('service', 'hostaddr', 'options'))
                or any(os.environ.get(k) for k in ('PGSERVICE', 'PGSERVICEFILE', 'PGHOSTADDR', 'PGOPTIONS'))
                or any(c in expected['host'] for c in (',', '/', '\\', ' '))):
            refuse('redirecionamento de conexão não autorizado.')
        if options['environment'] == 'production' and (
                expected['host'] in ('localhost', '127.0.0.1', '::1')
                or params.get('sslmode') not in ('require', 'verify-ca', 'verify-full')):
            refuse('produção exige endpoint remoto explícito e TLS obrigatório.')
        if not connection.get_autocommit():
            refuse('conexão já possui transação aberta.')

    def verify_identity(self, cursor, options):
        info = connection.connection.info
        if any(str(getattr(info, key)) != str(options['expected_' + opt]) for key, opt in
               [('host', 'host'), ('port', 'port'), ('dbname', 'database'), ('user', 'user')]):
            refuse('identidade da conexão diverge do destino autorizado.')
        cursor.execute('SELECT current_database(), current_user, session_user, pg_is_in_recovery()')
        if cursor.fetchone() != (options['expected_database'], options['expected_user'], options['expected_user'], False):
            refuse('identidade do servidor divergente ou servidor somente leitura.')
        if options['environment'] == 'isolated':
            cursor.execute("SELECT current_setting('data_directory'), host(inet_server_addr()), inet_server_port()")
            directory, host, port = cursor.fetchone()
            expected = Path(options['expected_data_directory']).resolve()
            if (Path(directory).resolve() != expected or host != '127.0.0.1'
                    or port != int(options['expected_port'])
                    or not expected.parent.name.startswith('offline-migration-0006-pg-') or expected.name != 'data'):
                refuse('cluster descartável não confirmado.')
        elif not info.ssl_in_use:
            refuse('conexão de produção sem TLS.')

    def checked_plan(self, executor, cursor):
        validate_source()
        validate_definition(executor.loader)
        executor.loader.check_consistent_history(connection)
        if executor.loader.detect_conflicts():
            refuse('grafo com conflitos.')
        applied = set(executor.loader.applied_migrations)
        if applied - set(executor.loader.disk_migrations):
            refuse('histórico contém migrations ausentes nesta versão.')
        dependencies = set(executor.loader.graph.forwards_plan(TARGET)) - {TARGET}
        if not dependencies <= applied:
            refuse('dependência ausente; aplique-a em procedimento separado e autorizado.')
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if [(m.app_label, m.name, backwards) for m, backwards in plan] not in (
                [], [(TARGET[0], TARGET[1], False)]):
            refuse('outra migration pendente no plano completo.')
        if TARGET in applied:
            if plan:
                refuse('estado indeterminado do histórico.')
            verify_schema(cursor)
        else:
            if not plan:
                refuse('estado indeterminado do plano.')
            for table in TABLES:
                cursor.execute('SELECT to_regclass(%s)', ['public.' + table])
                if cursor.fetchone()[0] is not None:
                    refuse('schema inesperado antes da aplicação; não usar --fake.')
        return plan, applied

    def inspect(self, cursor, options):
        ExistingGate.timeouts(cursor)
        cursor.execute('SET LOCAL search_path = public, pg_catalog')
        self.verify_identity(cursor, options)
        validate_source()  # Before the loader imports executable migration modules.
        return self.checked_plan(MigrationExecutor(connection), cursor)

    def handle(self, *args, **options):
        # No database connection before explicit production authorization and identity checks.
        validate_source()
        # get_autocommit opens a connection, so check production consent first.
        if options['environment'] == 'production' and (
                options.get('authorize_production') != APPROVAL or not (options.get('approval_reference') or '').strip()):
            refuse('produção exige autorização explícita e referência da aprovação, inclusive para consulta.')
        self.check_destination(options)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                plan, _ = self.inspect(cursor, options)
        if not options['apply']:
            self.stdout.write('Plano validado: somente offline.0006 pendente.' if plan else 'Já aplicada; schema validado. Nenhuma escrita.')
            return
        with transaction.atomic():
            with connection.cursor() as cursor:
                ExistingGate.timeouts(cursor)
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [LOCK])
                if not cursor.fetchone()[0]:
                    refuse('trava de publicação ocupada; tente depois.')
                plan, before = self.inspect(cursor, options)
                if plan:
                    MigrationExecutor(connection).migrate([TARGET], plan=plan)
                verify_schema(cursor)
                executor = MigrationExecutor(connection)
                after = set(executor.loader.applied_migrations)
                if after != before | {TARGET}:
                    refuse('histórico alterado de forma inesperada.')
                self.checked_plan(executor, cursor)
        self.stdout.write('0006 aplicada e schema verificado.' if plan else '0006 já aplicada; schema verificado sob trava. Nenhuma escrita.')
