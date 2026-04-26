from django.db import migrations


def corrigir_refigerante(apps, schema_editor):
    Categoria = apps.get_model("estoque", "Categoria")
    Produto = apps.get_model("estoque", "Produto")

    Categoria.objects.update_or_create(
        nome="Refrigerantes e Afins",
        defaults={"ativa": True},
    )

    Produto.objects.filter(categoria__iexact="Refigerante").update(
        categoria="Refrigerantes e Afins"
    )
    Produto.objects.filter(categoria__iexact="Refigerante,").update(
        categoria="Refrigerantes e Afins"
    )

    Categoria.objects.filter(nome__iexact="Refigerante").delete()
    Categoria.objects.filter(nome__iexact="Refigerante,").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0014_limpar_categorias_duplicadas"),
    ]

    operations = [
        migrations.RunPython(corrigir_refigerante, migrations.RunPython.noop),
    ]
