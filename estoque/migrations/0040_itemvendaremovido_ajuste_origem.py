from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0039_itemvendaremovido"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemvendaremovido",
            name="ajuste_origem",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="itens_removidos_origem",
                to="estoque.ajusteitemvendaquitada",
            ),
        ),
    ]
