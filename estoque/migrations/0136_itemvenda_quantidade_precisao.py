"""Repair physical drift only; Django state has been (12, 3) since 0017."""
from django.db import migrations


def corrigir_precisao(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "sqlite":
        # SQLite has no numeric typmod; Django already owns the correct state.
        return
    if connection.vendor != "postgresql":
        raise RuntimeError("Precisao de ItemVenda: backend nao suportado.")
    model = apps.get_model("estoque", "ItemVenda")
    table = schema_editor.quote_name(model._meta.db_table)
    column = schema_editor.quote_name(model._meta.get_field("quantidade").column)
    with connection.cursor() as cursor:
        # Bound the lock wait and hold the lock through inspection and alteration.
        cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
        previous = cursor.fetchone()
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '60s'")
        cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        cursor.execute("""
            SELECT format_type(atttypid, atttypmod)
            FROM pg_attribute
            WHERE attrelid = to_regclass(%s) AND attname = %s
              AND attnum > 0 AND NOT attisdropped
        """, [model._meta.db_table, model._meta.get_field("quantidade").column])
        row = cursor.fetchone()
        actual = row[0] if row else None
        if actual not in ("numeric(10,2)", "numeric(12,3)"):
            raise RuntimeError(
                f"ItemVenda.quantidade: formato inesperado {actual!r}; "
                "esperado numeric(10,2) ou numeric(12,3). Nenhum valor corrigido."
            )
        if actual == "numeric(10,2)":
            cursor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE numeric(12,3)")
        for setting, value in zip(("lock_timeout", "statement_timeout"), previous):
            cursor.execute("SELECT set_config(%s, %s, true)", [setting, value])


class Migration(migrations.Migration):
    atomic = True
    dependencies = [("estoque", "0135_conciliacao_compra")]
    # Deliberately irreversible: returning to two decimals would destroy data.
    operations = [migrations.RunPython(corrigir_precisao)]
