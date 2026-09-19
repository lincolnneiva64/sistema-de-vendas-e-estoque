from django.db import migrations


def criar_catalogo_inicial(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    itens = [
        # Empresa
        {
            "nome": "Gasolina",
            "tipo": "empresa",
            "grupo": "Veiculos",
            "categoria": "Combustivel",
            "favorito": True,
            "ordem": 10,
        },
        {
            "nome": "Manutencao de veiculo",
            "tipo": "empresa",
            "grupo": "Veiculos",
            "categoria": "Manutencao",
            "favorito": True,
            "ordem": 20,
        },
        {
            "nome": "Funcionarios",
            "tipo": "empresa",
            "grupo": "Pessoal",
            "categoria": "Funcionarios",
            "favorito": True,
            "ordem": 30,
        },
        {
            "nome": "Energia da empresa",
            "tipo": "empresa",
            "grupo": "Estrutura",
            "categoria": "Energia",
            "favorito": True,
            "ordem": 40,
        },
        {
            "nome": "Material de apoio",
            "tipo": "empresa",
            "grupo": "Operacionais",
            "categoria": "Material de apoio",
            "favorito": True,
            "ordem": 50,
        },
        {
            "nome": "Outros - Empresa",
            "tipo": "empresa",
            "grupo": "Outras",
            "categoria": "Outros",
            "favorito": False,
            "ordem": 90,
        },

        # Pessoal
        {
            "nome": "Casa",
            "tipo": "pessoal",
            "grupo": "Casa",
            "categoria": "Moradia",
            "favorito": True,
            "ordem": 110,
        },
        {
            "nome": "Energia da casa",
            "tipo": "pessoal",
            "grupo": "Casa",
            "categoria": "Energia",
            "favorito": True,
            "ordem": 120,
        },
        {
            "nome": "Faculdade",
            "tipo": "pessoal",
            "grupo": "Familia",
            "categoria": "Educacao",
            "favorito": True,
            "ordem": 130,
        },
        {
            "nome": "Outros - Pessoal",
            "tipo": "pessoal",
            "grupo": "Outras",
            "categoria": "Outros",
            "favorito": False,
            "ordem": 190,
        },
    ]

    for item in itens:
        CatalogoDespesa.objects.get_or_create(
            nome=item["nome"],
            tipo=item["tipo"],
            defaults={
                "grupo": item["grupo"],
                "categoria": item["categoria"],
                "favorito": item["favorito"],
                "ativo": True,
                "ordem": item["ordem"],
            },
        )


def remover_catalogo_inicial(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")
    nomes = [
        "Gasolina",
        "Manutencao de veiculo",
        "Funcionarios",
        "Energia da empresa",
        "Material de apoio",
        "Outros - Empresa",
        "Casa",
        "Energia da casa",
        "Faculdade",
        "Outros - Pessoal",
    ]
    CatalogoDespesa.objects.filter(nome__in=nomes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0108_despesadiaria_catalogo"),
    ]

    operations = [
        migrations.RunPython(
            criar_catalogo_inicial,
            remover_catalogo_inicial,
        ),
    ]
