from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0126_compra_revisao_precos_pendente"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemcompra",
            name="preco_compra_anterior",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=12,
                null=True,
            ),
        ),
    ]
