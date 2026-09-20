from django.db import migrations


def reorganizar_catalogo_pessoal(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    # Os lancamentos antigos continuam ligados aos catalogos antigos.
    # Apenas retiramos esses atalhos da interface nova.
    CatalogoDespesa.objects.filter(
        tipo="pessoal",
    ).update(
        ativo=False,
        favorito=False,
    )

    categorias = {
        "Casa": [
            "Pagamento da casa",
            "Manutenção da casa",
            "Capina",
            "Faxineira",
        ],
        "Contas": [
            "Energia",
            "Internet",
            "Água",
            "Telefone",
        ],
        "Alimentação": [
            "Mercado",
            "Feira",
            "Pão / café da manhã",
            "Lanche",
            "Refeição fora",
        ],
        "Transporte": [
            "Combustivel",
            "Uber / Taxi",
            "Ônibus",
            "Estacionamento",
        ],
        "Saúde": [
            "Remédio",
            "Consulta",
            "Exame",
        ],
        "Lazer": [
            "Cerveja / Bar",
            "Cinema",
            "Passeio",
        ],
        "Pessoal": [
            "Roupa",
            "Calçado",
            "Cabelo / Barbeiro",
            "Higiene",
        ],
        "Educação": [
            "Faculdade",
            "Curso",
            "Material de estudo",
        ],
        "Serviços": [
            "Serviço eventual",
        ],
        "Outros": [
            "Outros",
        ],
    }

    pessoas = [
        ("Lincoln", 200),
        ("Roseli", 500),
        ("Familia", 800),
    ]

    ordem_categoria = {
        "Casa": 10,
        "Contas": 20,
        "Alimentação": 30,
        "Transporte": 40,
        "Saúde": 50,
        "Lazer": 60,
        "Pessoal": 70,
        "Educação": 80,
        "Serviços": 90,
        "Outros": 100,
    }

    for pessoa, base in pessoas:
        for grupo, subcategorias in categorias.items():
            for indice, subcategoria in enumerate(subcategorias, start=1):
                nome = subcategoria

                obj, _ = CatalogoDespesa.objects.get_or_create(
                    tipo="pessoal",
                    pessoa=pessoa,
                    grupo=grupo,
                    categoria=subcategoria,
                    defaults={
                        "nome": nome,
                        "favorito": False,
                        "ativo": True,
                        "ordem": base + ordem_categoria[grupo] + indice,
                    },
                )

                obj.grupo = grupo
                obj.categoria = subcategoria
                obj.pessoa = pessoa
                obj.favorito = False
                obj.ativo = True
                obj.ordem = base + ordem_categoria[grupo] + indice
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
    # Nao apagamos catalogos para preservar qualquer lancamento
    # que eventualmente tenha sido ligado a eles.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0112_desativa_catalogo_generico_antigo"),
    ]

    operations = [
        migrations.RunPython(
            reorganizar_catalogo_pessoal,
            reverter,
        ),
    ]
