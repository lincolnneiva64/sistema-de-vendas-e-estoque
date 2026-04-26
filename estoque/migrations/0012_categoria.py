from django.db import migrations, models


CATEGORIAS_INICIAIS = [
    "Bebidas",
    "Cervejas",
    "Refrigerantes e Afins",
    "Bebidas Quentes",
    "Hortifruti",
    "Granjeiro",
    "Estivas",
    "Mercearia",
    "Frios e Embutidos",
    "Enlatados",
    "Massas",
    "Milho e Derivados",
    "Limpeza",
    "Higiene",
    "Descartáveis",
    "Diversos",
]


def criar_categorias_iniciais(apps, schema_editor):
    Categoria = apps.get_model("estoque", "Categoria")

    nomes_existentes = {
        categoria.nome.casefold()
        for categoria in Categoria.objects.all()
    }

    for nome in CATEGORIAS_INICIAIS:
        nome_limpo = " ".join(str(nome).strip().split())
        if not nome_limpo:
            continue

        nome_normalizado = nome_limpo
        chave = nome_normalizado.casefold()
        if chave in nomes_existentes:
            continue

        Categoria.objects.create(nome=nome_normalizado, ativa=True)
        nomes_existentes.add(chave)


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0011_unidade"),
    ]

    operations = [
        migrations.CreateModel(
            name="Categoria",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nome", models.CharField(max_length=60, unique=True)),
                ("descricao", models.CharField(blank=True, max_length=255, null=True)),
                ("ativa", models.BooleanField(default=True)),
            ],
        ),
        migrations.RunPython(criar_categorias_iniciais, migrations.RunPython.noop),
    ]
