from unittest.mock import patch
from django.db.migrations.operations.special import RunSQL
from django.test.runner import DiscoverRunner


class OfflineTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        original = RunSQL.database_forwards
        def portable(operation, app_label, editor, from_state, to_state):
            # Existing migration 0124 only corrects PostgreSQL varchar size; SQLite does not enforce it.
            # All other migrations, including offline.0001_initial, execute normally.
            if editor.connection.vendor == 'sqlite' and operation.sql == 'ALTER TABLE estoque_venda ALTER COLUMN tipo_pagamento TYPE varchar(40)':
                return
            return original(operation, app_label, editor, from_state, to_state)
        with patch.object(RunSQL, 'database_forwards', portable):
            return super().setup_databases(**kwargs)
