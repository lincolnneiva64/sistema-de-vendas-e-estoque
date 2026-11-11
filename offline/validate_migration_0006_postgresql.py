"""Real migrations in a fresh, guarded PostgreSQL cluster. No production settings.

Run: python -B -m offline.validate_migration_0006_postgresql
Optional OFFLINE_PG_BIN points to installed PostgreSQL binaries (read only).
Logs and stopped cluster are preserved in the printed temporary directory.
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def main():
    root = Path(tempfile.mkdtemp(prefix='offline-migration-0006-pg-'))
    data = root / 'data'
    binary = Path(os.environ.get('OFFLINE_PG_BIN', str(Path(tempfile.gettempdir()) / 'offline-phase1-pg' / 'pgsql' / 'bin')))
    suffix = '.exe' if os.name == 'nt' else ''
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    started, result = False, 1
    state = {'directory': str(root)}
    print('Cluster exclusivo: ' + str(root), flush=True)

    def run(args):
        with (root / 'bootstrap.log').open('a', encoding='utf-8') as log:
            subprocess.run([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT, check=True, creationflags=flags)

    try:
        # No inherited libpq destinations, options, services or credentials.
        for key in list(os.environ):
            if key.startswith('PG'):
                os.environ.pop(key)
        run([binary / ('initdb' + suffix), '-D', data, '-U', 'offline_0006_fixture', '-A', 'trust', '--encoding=UTF8', '--locale=C'])
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        run([binary / ('pg_ctl' + suffix), '-D', data, '-l', root / 'server.log', '-w', '-t', '30', '-o', f'-h 127.0.0.1 -p {port}', 'start'])
        started = True
        import psycopg2
        from psycopg2.extensions import parse_dsn
        real_connect = psycopg2.connect

        def guarded_connect(dsn=None, *args, **kwargs):
            params = parse_dsn(dsn or '')
            params.update(kwargs)
            if (params.get('host') != '127.0.0.1' or str(params.get('port')) != str(port)
                    or params.get('user') != 'offline_0006_fixture'
                    or params.get('dbname', params.get('database')) not in ('postgres', 'offline_0006_disposable', 'test_offline_0006_disposable')
                    or any(params.get(k) for k in ('service', 'hostaddr', 'options'))):
                raise RuntimeError('Conexão fora do cluster descartável recusada.')
            conn = real_connect(dsn, *args, **kwargs)
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_setting('data_directory'), inet_server_port()")
                directory, actual_port = cursor.fetchone()
                if Path(directory).resolve() != data.resolve() or actual_port != port:
                    conn.close()
                    raise RuntimeError('Cluster descartável divergente.')
            conn.rollback()
            return conn

        psycopg2.connect = guarded_connect
        conn = guarded_connect(host='127.0.0.1', port=port, dbname='postgres', user='offline_0006_fixture')
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute('CREATE DATABASE offline_0006_disposable')
            cursor.execute('SELECT version()')
            state['version'] = cursor.fetchone()[0]
        conn.close()
        # Import the standalone fixture module only: never sistema.settings/.env.
        from sistema import settings_test_compras as fixture
        from django.conf import settings
        config = {key: getattr(fixture, key) for key in dir(fixture) if key.isupper()}
        config['INSTALLED_APPS'] = [*config['INSTALLED_APPS'], 'inventario']
        config['DATABASES'] = {'default': {
            'ENGINE': 'django.db.backends.postgresql', 'HOST': '127.0.0.1', 'PORT': port,
            'USER': 'offline_0006_fixture', 'NAME': 'offline_0006_disposable',
            'TEST': {'NAME': 'test_offline_0006_disposable'},
        }}
        config['OFFLINE_0006_TEST_DATA_DIRECTORY'] = str(data)
        config['TEST_RUNNER'] = 'django.test.runner.DiscoverRunner'
        settings.configure(**config)
        import django
        django.setup()
        from django.test.runner import DiscoverRunner
        with (root / 'tests.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = DiscoverRunner(verbosity=2, interactive=False).run_tests(sys.argv[1:] or ['offline.tests_publicacao_resolucao'])
        state.update(failures=result, port=port)
    except Exception:
        import traceback
        (root / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        print('Falha: ' + str(root / 'error.log'), flush=True)
        print(traceback.format_exc(), flush=True)
    finally:
        if 'django' in sys.modules:
            from django.db import connections
            connections.close_all()
        if started:
            run([binary / ('pg_ctl' + suffix), '-D', data, '-m', 'fast', '-w', '-t', '30', 'stop'])
        state['stopped'] = started
        (root / 'validation.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        if (root / 'tests.log').exists():
            log = (root / 'tests.log').read_text(encoding='utf-8')
            print(log[-18000:] if result else log[-6000:])
        print('Resultado: ' + str(result) + '; cluster encerrado: ' + str(started), flush=True)
    return bool(result)


if __name__ == '__main__':
    sys.exit(main())
