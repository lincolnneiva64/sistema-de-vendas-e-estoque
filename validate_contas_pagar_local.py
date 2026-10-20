"""Run synthetic sale tests in a new guarded loopback cluster.

Create the test model schema directly; block all migration execution.

Usage: python -m offline.validate_gelo_postgresql [test labels...]
GELO_PG_BIN may name an existing PostgreSQL binary directory (read only).
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch


def main():
    root = Path(tempfile.mkdtemp(prefix="gelo-disposable-pg-"))
    data = root / "data"
    binary = Path(os.environ.get("GELO_PG_BIN", str(Path(tempfile.gettempdir()) / "offline-phase1-pg" / "pgsql" / "bin")))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    estado = {"directory": str(root), "state": "created"}
    started, result = False, 1
    print("Cluster exclusivo: " + str(root), flush=True)

    def command(args):
        with (root / "bootstrap.log").open("a", encoding="utf-8") as log:
            subprocess.run([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT, creationflags=flags, check=True)

    try:
        command([binary / "initdb.exe", "-D", data, "-U", "gelo_fixture", "-A", "trust", "--encoding=UTF8", "--locale=C"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command([binary / "pg_ctl.exe", "-D", data, "-l", root / "server.log", "-w", "-t", "30", "-o", f"-h 127.0.0.1 -p {port}", "start"])
        started = True
        import psycopg2
        from psycopg2.extensions import parse_dsn
        real_connect = psycopg2.connect
        def guarded_connect(dsn=None, *args, **kwargs):
            params = parse_dsn(dsn or "")
            params.update(kwargs)
            if (params.get("host") != "127.0.0.1" or str(params.get("port")) != str(port)
                    or params.get("dbname", params.get("database")) not in {"postgres", "gelo_disposable", "test_gelo_disposable"}
                    or params.get("user") != "gelo_fixture" or any(params.get(k) for k in ("service", "hostaddr"))):
                raise RuntimeError("Conexão fora do cluster descartável recusada.")
            conn = real_connect(dsn, *args, **kwargs)
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_setting('data_directory'), inet_server_port()")
                directory, actual_port = cursor.fetchone()
                if Path(directory).resolve() != data.resolve() or actual_port != port:
                    conn.close()
                    raise RuntimeError("Cluster descartável divergente.")
            conn.rollback()
            return conn
        psycopg2.connect = guarded_connect
        for name in list(os.environ):
            if name.startswith("PG"):
                os.environ.pop(name)
        conn = guarded_connect(host="127.0.0.1", port=port, dbname="postgres", user="gelo_fixture")
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE DATABASE gelo_disposable")
            cursor.execute("SELECT version()")
            estado["version"] = cursor.fetchone()[0]
        conn.close()
        os.environ["OFFLINE_TEST_DATABASE_URL"] = f"postgresql://gelo_fixture@127.0.0.1:{port}/gelo_disposable"
        os.environ["DJANGO_SETTINGS_MODULE"] = "offline.test_settings"
        os.environ["DATABASE_URL"] = os.environ["OFFLINE_TEST_DATABASE_URL"]
        os.environ["SECRET_KEY"] = "disposable-gelo-test-key"
        import django
        django.setup()
        from django.apps import apps
        from django.db import connection
        from django.contrib.auth.management import create_permissions
        from django.test.runner import DiscoverRunner
        class OfflineTestRunner(DiscoverRunner):
            def setup_databases(self, **kwargs):
                # Create the current model schema directly. No migration executor or command.
                with connection.schema_editor() as editor:
                    for model in apps.get_models():
                        if model._meta.managed and not model._meta.proxy:
                            editor.create_model(model)
                for app in apps.get_app_configs():
                    create_permissions(app, verbosity=0)
                return None
            def teardown_databases(self, old_config, **kwargs):
                pass
        labels = sys.argv[1:] or ["estoque.tests_totais_contas_pagar", "estoque.tests_periodo_contas_pagar", "estoque.tests_encargos_conta_pagar", "estoque.tests_filtros_contas_pagar"]
        from django.db.migrations.executor import MigrationExecutor
        def restricted_migrate(*args, **kwargs):
            raise RuntimeError('Migrations proibidas nesta validacao descartavel.')
        with (root / "tests.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            with patch('django.db.migrations.executor.MigrationExecutor.migrate',
                       autospec=True, side_effect=restricted_migrate):
                result = OfflineTestRunner(verbosity=2, interactive=False).run_tests(labels)
        estado.update(failures=result, port=port)
    except Exception:
        import traceback
        (root / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        print("Falha: detalhes em " + str(root / "error.log"), flush=True)
    finally:
        if "django" in sys.modules:
            from django.db import connections
            connections.close_all()
        if started:
            command([binary / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "-t", "30", "stop"])
            estado["state"] = "stopped"
        (root / "validation.json").write_text(json.dumps(estado, indent=2), encoding="utf-8")
        evidence = Path(__file__).resolve().parent / 'diagnostics' / 'contas-pagar-rodape'
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'validation.json').write_text(json.dumps(estado, indent=2), encoding='utf-8')
        if (root / "tests.log").exists():
            texto = (root / "tests.log").read_text(encoding="utf-8")
            (evidence / 'tests.log').write_text(texto, encoding='utf-8')
            print(texto[-16000:] if result else texto[-2200:])
        print("Resultado: " + str(result) + "; cluster encerrado: " + str(started), flush=True)
    return bool(result)


if __name__ == "__main__":
    sys.exit(main())
