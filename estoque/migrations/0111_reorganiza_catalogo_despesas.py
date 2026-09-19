from django.db import migrations


def reorganizar_catalogo(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    # Os atalhos antigos genericos deixam de ser favoritos quando
    # passamos a ter atalhos operacionais mais especificos.
    CatalogoDespesa.objects.filter(
        nome__in=[
            "Gasolina",
            "Manutencao de veiculo",
            "Funcionarios",
            "Energia da empresa",
            "Casa",
            "Energia da casa",
            "Faculdade",
        ]
    ).update(favorito=False)

    itens = [
        # EMPRESA
        ("Gasolina - Gol", "empresa", "Veiculos", "Combustivel", "", True, 10),
        ("Gasolina - Strada", "empresa", "Veiculos", "Combustivel", "", True, 20),
        ("Manutencao - Gol", "empresa", "Veiculos", "Manutencao", "", True, 30),
        ("Manutencao - Strada", "empresa", "Veiculos", "Manutencao", "", True, 40),
        ("Funcionarios", "empresa", "Pessoal", "Funcionarios", "", True, 50),
        ("Vale funcionario", "empresa", "Pessoal", "Vale funcionario", "", True, 60),
        ("Energia - Deposito", "empresa", "Estrutura", "Energia", "", True, 70),
        ("Aluguel - Deposito", "empresa", "Estrutura", "Aluguel", "", True, 80),
        ("Material de apoio", "empresa", "Operacionais", "Material de apoio", "", False, 90),
        ("Outros - Empresa", "empresa", "Outras", "Outros", "", False, 100),

        # PESSOAL - LINCOLN
        ("Casa", "pessoal", "Casa", "Moradia", "Lincoln", True, 210),
        ("Energia da casa", "pessoal", "Casa", "Energia", "Lincoln", True, 220),
        ("Outros - Lincoln", "pessoal", "Outras", "Outros", "Lincoln", False, 290),

        # PESSOAL - ROSELI
        ("Despesa pessoal - Roseli", "pessoal", "Pessoal", "Despesa pessoal", "Roseli", True, 310),
        ("Outros - Roseli", "pessoal", "Outras", "Outros", "Roseli", False, 390),

        # FAMILIA / FILHAS
        ("Faculdade", "pessoal", "Familia", "Educacao", "Familia", True, 410),
        ("Outros - Familia", "pessoal", "Familia", "Outros", "Familia", False, 490),
    ]

    for nome, tipo, grupo, categoria, pessoa, favorito, ordem in itens:
        obj, _ = CatalogoDespesa.objects.get_or_create(
            nome=nome,
            tipo=tipo,
            defaults={
                "grupo": grupo,
                "categoria": categoria,
                "pessoa": pessoa,
                "favorito": favorito,
                "ativo": True,
                "ordem": ordem,
            },
        )

        obj.grupo = grupo
        obj.categoria = categoria
        obj.pessoa = pessoa
        obj.favorito = favorito
        obj.ativo = True
        obj.ordem = ordem
        obj.save(
            update_fields=[
                "grupo",
                "categoria",
                "pessoa",
                "favorito",
                "ativo",
                "ordem",
                "atualizado_em",
            ]
        )


def reverter(apps, schema_editor):
    # Nao apagamos catalogos na reversao para evitar destruir
    # referencias caso algum lancamento ja esteja ligado a eles.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0110_catalogodespesa_pessoa"),
    ]

    operations = [
        migrations.RunPython(
            reorganizar_catalogo,
            reverter,
        ),
    ]
