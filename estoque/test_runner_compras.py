"""Recusa bancos externos e adapta somente o SQL legado incompativel com SQLite."""
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db.migrations.operations.special import RunSQL
from django.test.runner import DiscoverRunner


class CompraTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        if settings.SETTINGS_MODULE != "sistema.settings_test_compras":
            raise RuntimeError("Use exclusivamente sistema.settings_test_compras.")
        if set(settings.DATABASES) != {"default"}:
            raise RuntimeError("Alias de banco inesperado.")
        config = settings.DATABASES["default"]
        test_path = Path(config["TEST"]["NAME"]).resolve()
        temp_root = Path(settings.COMPRAS_TEST_DIRECTORY.name).resolve()
        if config["ENGINE"] != "django.db.backends.sqlite3" or config["NAME"] != ":memory:" or test_path.parent != temp_root:
            raise RuntimeError("Banco de testes nao isolado.")
        print(f"Banco comprovadamente isolado: SQLite descartavel em {test_path}")
        original = RunSQL.database_forwards

        def portable(operation, app_label, editor, from_state, to_state):
            if editor.connection.vendor == "sqlite" and operation.sql == "ALTER TABLE estoque_venda ALTER COLUMN tipo_pagamento TYPE varchar(40)":
                return  # SQLite nao limita varchar; nenhuma migration foi alterada.
            return original(operation, app_label, editor, from_state, to_state)

        with patch.object(RunSQL, "database_forwards", portable):
            return super().setup_databases(**kwargs)
