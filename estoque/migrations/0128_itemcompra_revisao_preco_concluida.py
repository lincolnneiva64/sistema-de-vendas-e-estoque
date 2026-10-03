from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0127_itemcompra_preco_compra_anterior"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemcompra",
            name="revisao_preco_concluida",
            field=models.BooleanField(default=False),
        ),
    ]
