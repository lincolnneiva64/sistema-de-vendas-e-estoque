# Corrige divergencia do schema real: o estado Django ja define max_length=40.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0123_despesadiaria_venda_origem_consumo"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE estoque_venda "
                "ALTER COLUMN tipo_pagamento TYPE varchar(40)"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
