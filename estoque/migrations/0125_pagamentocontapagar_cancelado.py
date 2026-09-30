from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0124_corrige_venda_tipo_pagamento_tamanho"),
    ]

    operations = [
        migrations.AddField(
            model_name="pagamentocontapagar",
            name="cancelado",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="pagamentocontapagar",
            name="cancelado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
