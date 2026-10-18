"""Run reliability tests on a NEW local cluster, without migrations or production access."""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def main():
    root = Path(tempfile.mkdtemp(prefix="inventario-confiabilidade-pg-"))
    data = (root / "data").resolve()
    binary = Path(tempfile.gettempdir()) / "offline-phase1-pg" / "pgsql" / "bin"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    state = {"directory": str(root), "migrations_executed": False, "state": "created"}
    result, started = 1, False
    print("Cluster exclusivo: " + str(root), flush=True)

    def command(args):
        with (root / "bootstrap.log").open("a", encoding="utf-8") as log:
            subprocess.run([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT,
                           creationflags=flags, check=True)

    try:
        for name in list(os.environ):
            if name.startswith("PG"):
                os.environ.pop(name)
        command([binary / "initdb.exe", "-D", data, "-U", "inventario_fixture", "-A", "trust",
                 "--encoding=UTF8", "--locale=C"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command([binary / "pg_ctl.exe", "-D", data, "-l", root / "server.log", "-w", "-t", "30",
                 "-o", f"-h 127.0.0.1 -p {port}", "start"])
        started = True
        import psycopg2
        from psycopg2.extensions import parse_dsn
        real_connect = psycopg2.connect

        def guarded_connect(dsn=None, *args, **kwargs):
            params = parse_dsn(dsn or "")
            params.update(kwargs)
            if (params.get("host") != "127.0.0.1" or str(params.get("port")) != str(port)
                    or params.get("dbname", params.get("database")) not in {"postgres", "inventario_confiabilidade"}
                    or params.get("user") != "inventario_fixture"
                    or any(params.get(k) for k in ("service", "hostaddr"))):
                raise RuntimeError("Conexão fora do cluster descartável recusada.")
            conn = real_connect(dsn, *args, **kwargs)
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_setting('data_directory'), inet_server_port()")
                directory, actual_port = cursor.fetchone()
                if Path(directory).resolve() != data or actual_port != port:
                    conn.close()
                    raise RuntimeError("Identidade do cluster descartável divergente.")
            conn.rollback()
            return conn

        psycopg2.connect = guarded_connect
        conn = guarded_connect(host="127.0.0.1", port=port, dbname="postgres", user="inventario_fixture")
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE DATABASE inventario_confiabilidade")
            cursor.execute("SELECT version()")
            state["version"] = cursor.fetchone()[0]
        conn.close()
        os.environ["INVENTARIO_CONFIABILIDADE_PG_PORT"] = str(port)
        os.environ["INVENTARIO_CONFIABILIDADE_PG_DATA"] = str(data)
        os.environ["DJANGO_SETTINGS_MODULE"] = "inventario.test_settings_confiabilidade_pg"
        import django
        django.setup()
        from .test_runner_confiabilidade import PostgreSQLSemMigrationsRunner
        labels = sys.argv[1:] or ["inventario.tests_ciclo_vida.CicloConcorrenciaTests",
                                 "inventario.tests_confiabilidade"]
        with (root / "tests.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = PostgreSQLSemMigrationsRunner(verbosity=2, interactive=False).run_tests(labels)
        state.update(failures=result, port=port)
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
            state["state"] = "stopped"
        (root / "validation.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        if (root / "tests.log").exists():
            print((root / "tests.log").read_text(encoding="utf-8")[-18000:])
        print("Resultado: " + str(result) + "; estado: " + state["state"], flush=True)
    return bool(result)


if __name__ == "__main__":
    sys.exit(main())
