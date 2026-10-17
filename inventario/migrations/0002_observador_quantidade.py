"""Observe every changed stock quantity, including QuerySet.update and raw SQL.

The product update already serializes PostgreSQL writers on that product row.
The cursor is advanced in the same transaction and rolled back with the stock.
This records digital changes only; it deliberately makes no physical timing claim.
"""
from django.db import migrations


def instalar(apps, editor):
    if editor.connection.vendor == "sqlite":
        editor.execute("""
            CREATE TRIGGER inventario_observar_quantidade AFTER UPDATE OF quantidade ON estoque_produto
            WHEN OLD.quantidade IS NOT NEW.quantidade
            BEGIN
                INSERT INTO inventario_sequenciaestoque (produto_id, sequencia) VALUES (NEW.id, 1)
                ON CONFLICT(produto_id) DO UPDATE SET sequencia = sequencia + 1;
                INSERT INTO inventario_movimentoobservado (produto_id, sequencia, antes, depois, registrado_em)
                SELECT NEW.id, sequencia, OLD.quantidade, NEW.quantidade, STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                FROM inventario_sequenciaestoque WHERE produto_id = NEW.id;
            END;
        """)
    elif editor.connection.vendor == "postgresql":
        # This migration has never been published. Harden the first installation,
        # including the interval before 0003, rather than leaving old writers
        # dependent on grants on the new journal. All identifiers are resolved at
        # installation and quoted; runtime search_path cannot redirect the writes.
        def qualified(table):
            with editor.connection.cursor() as cursor:
                cursor.execute("SELECT n.nspname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s::pg_catalog.regclass", [table])
                schema = cursor.fetchone()[0]
            return editor.quote_name(schema) + "." + editor.quote_name(table)
        produto = qualified("estoque_produto")
        sequencia = qualified("inventario_sequenciaestoque")
        movimento = qualified("inventario_movimentoobservado")
        funcao = produto.rsplit(".", 1)[0] + "." + editor.quote_name("inventario_observar_quantidade_fn")
        editor.execute(f"""
            CREATE FUNCTION {funcao}() RETURNS trigger
            LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
            DECLARE versao bigint;
            BEGIN
                INSERT INTO {sequencia} AS contador (produto_id, sequencia) VALUES (NEW.id, 1)
                ON CONFLICT(produto_id) DO UPDATE SET sequencia = contador.sequencia + 1
                RETURNING sequencia INTO versao;
                INSERT INTO {movimento} (produto_id, sequencia, antes, depois, registrado_em)
                VALUES (NEW.id, versao, OLD.quantidade, NEW.quantidade, clock_timestamp());
                RETURN NEW;
            END;
            $$;
            REVOKE ALL ON FUNCTION {funcao}() FROM PUBLIC;
            CREATE TRIGGER inventario_observar_quantidade AFTER UPDATE OF quantidade ON {produto}
            FOR EACH ROW WHEN (OLD.quantidade IS DISTINCT FROM NEW.quantidade)
            EXECUTE FUNCTION {funcao}();
        """)
    else:
        raise RuntimeError("Inventário exige SQLite ou PostgreSQL; observador não instalado.")


def remover(apps, editor):
    if editor.connection.vendor == "sqlite":
        editor.execute("DROP TRIGGER IF EXISTS inventario_observar_quantidade")
    elif editor.connection.vendor == "postgresql":
        with editor.connection.cursor() as cursor:
            cursor.execute("SELECT n.nspname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE c.oid='estoque_produto'::pg_catalog.regclass")
            schema = editor.quote_name(cursor.fetchone()[0])
        editor.execute(f"DROP TRIGGER IF EXISTS inventario_observar_quantidade ON {schema}.estoque_produto")
        editor.execute(f"DROP FUNCTION IF EXISTS {schema}.inventario_observar_quantidade_fn()")


class Migration(migrations.Migration):
    dependencies = [("inventario", "0001_initial")]
    operations = [migrations.RunPython(instalar, remover)]
