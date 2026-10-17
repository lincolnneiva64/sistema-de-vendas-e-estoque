from django.contrib.postgres.operations import ValidateConstraint
from django.db import migrations


def bound_lock_wait(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET LOCAL lock_timeout = '2s'")


class ValidatePrincipalConstraint(ValidateConstraint):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    # A separate transaction releases the expansion's locks before the scan.
    dependencies = [("offline", "0004_local_sale_principal")]
    operations = [
        migrations.RunPython(bound_lock_wait, migrations.RunPython.noop),
        ValidatePrincipalConstraint(
            model_name="operacaosincronizacao", name="offline_operation_principal_valid"
        ),
    ]
