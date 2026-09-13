from django.db import migrations, models


def preencher_totais_produtos(apps, schema_editor):
    Compra = apps.get_model("estoque", "Compra")
    Compra.objects.all().update(total_produtos=models.F("total"), ajuste_total=0)


def limpar_totais_produtos(apps, schema_editor):
    Compra = apps.get_model("estoque", "Compra")
    Compra.objects.all().update(total_produtos=0, ajuste_total=0)


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0096_despesadiaria_data_rota_recebimento_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="compra",
            name="total_produtos",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="compra",
            name="ajuste_total",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.RunPython(preencher_totais_produtos, limpar_totais_produtos),
    ]
