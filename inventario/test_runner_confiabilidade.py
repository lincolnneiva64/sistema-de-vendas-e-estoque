"""Create only disposable model tables; never invoke the migration executor."""
from pathlib import Path
import os

from django.apps import apps
from django.db import connection
from django.test.runner import DiscoverRunner


class SemMigrationsRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        name = Path(connection.settings_dict["NAME"])
        if connection.vendor != "sqlite" or not name.parent.name.startswith("inventario-confiabilidade-"):
            raise RuntimeError("Este runner aceita apenas o SQLite temporário fictício.")
        with connection.schema_editor() as editor:
            for model in apps.get_models():
                if model._meta.managed and not model._meta.proxy:
                    editor.create_model(model)
            # Test fixture for the stock observer, created directly without migrations.
            editor.execute("""
                CREATE TRIGGER inventario_observar_quantidade AFTER UPDATE OF quantidade ON estoque_produto
                WHEN OLD.quantidade IS NOT NEW.quantidade
                BEGIN
                    INSERT INTO inventario_sequenciaestoque (produto_id, sequencia) VALUES (NEW.id, 1)
                    ON CONFLICT(produto_id) DO UPDATE SET sequencia = sequencia + 1;
                    INSERT INTO inventario_movimentoobservado
                        (produto_id, sequencia, antes, depois, registrado_em)
                    SELECT NEW.id, sequencia, OLD.quantidade, NEW.quantidade,
                           STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    FROM inventario_sequenciaestoque WHERE produto_id = NEW.id;
                END;
            """, params=None)
        from django.core.management.sql import emit_post_migrate_signal
        emit_post_migrate_signal(verbosity=0, interactive=False, db="default")
        return []

    def teardown_databases(self, old_config, **kwargs):
        connection.close()


class PostgreSQLSemMigrationsRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        config = connection.settings_dict
        root = Path(os.environ["INVENTARIO_CONFIABILIDADE_PG_DATA"]).resolve()
        if (connection.vendor != "postgresql" or config["HOST"] != "127.0.0.1"
                or str(config["PORT"]) != os.environ["INVENTARIO_CONFIABILIDADE_PG_PORT"]
                or config["NAME"] != "inventario_confiabilidade"
                or not root.parent.name.startswith("inventario-confiabilidade-pg-")):
            raise RuntimeError("Somente o cluster PostgreSQL descartável é aceito.")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('data_directory'), inet_server_port()")
            directory, port = cursor.fetchone()
            if Path(directory).resolve() != root or port != int(config["PORT"]):
                raise RuntimeError("Identidade do cluster descartável divergente.")
        with connection.schema_editor() as editor:
            for model in apps.get_models():
                if model._meta.managed and not model._meta.proxy:
                    editor.create_model(model)
            # Observer fixture equivalent to the stock journal; no migration executor.
            editor.execute("""
                CREATE FUNCTION public.inventario_observar_quantidade_fn() RETURNS trigger
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
                DECLARE versao bigint;
                BEGIN
                    INSERT INTO public.inventario_sequenciaestoque AS contador (produto_id, sequencia)
                    VALUES (NEW.id, 1)
                    ON CONFLICT(produto_id) DO UPDATE SET sequencia = contador.sequencia + 1
                    RETURNING sequencia INTO versao;
                    INSERT INTO public.inventario_movimentoobservado
                        (produto_id, sequencia, antes, depois, registrado_em)
                    VALUES (NEW.id, versao, OLD.quantidade, NEW.quantidade, clock_timestamp());
                    RETURN NEW;
                END;
                $$;
                REVOKE ALL ON FUNCTION public.inventario_observar_quantidade_fn() FROM PUBLIC;
                CREATE TRIGGER inventario_observar_quantidade AFTER UPDATE OF quantidade
                ON public.estoque_produto FOR EACH ROW
                WHEN (OLD.quantidade IS DISTINCT FROM NEW.quantidade)
                EXECUTE FUNCTION public.inventario_observar_quantidade_fn();
            """)
        # Same immutable audit constraints as the pending migration, installed
        # directly only on this identity-checked disposable database.
        from importlib import import_module
        with connection.cursor() as cursor:
            cursor.execute(import_module('inventario.migrations.0007_auditoria_ajuste_imutavel').SQL)
        # Baseline fixtures normally supplied by locacoes.0001 RunPython. The
        # guarded schema-only bootstrap intentionally does not run that migration.
        import_module('locacoes.migrations.0001_initial').criar_configuracoes_iniciais(apps, None)
        from django.core.management.sql import emit_post_migrate_signal
        emit_post_migrate_signal(verbosity=0, interactive=False, db="default")
        return []

    def teardown_databases(self, old_config, **kwargs):
        from django.db import connections
        connections.close_all()
