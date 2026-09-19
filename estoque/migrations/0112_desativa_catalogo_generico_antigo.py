from django.db import migrations


def desativar_genericos(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    CatalogoDespesa.objects.filter(
        nome__in=[
            "Gasolina",
            "Manutencao de veiculo",
            "Energia da empresa",
            "Outros - Pessoal",
        ]
    ).update(
        ativo=False,
        favorito=False,
    )


def reverter(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    CatalogoDespesa.objects.filter(
        nome__in=[
            "Gasolina",
            "Manutencao de veiculo",
            "Energia da empresa",
            "Outros - Pessoal",
        ]
    ).update(ativo=True)


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0111_reorganiza_catalogo_despesas"),
    ]

    operations = [
        migrations.RunPython(
            desativar_genericos,
            reverter,
        ),
    ]
