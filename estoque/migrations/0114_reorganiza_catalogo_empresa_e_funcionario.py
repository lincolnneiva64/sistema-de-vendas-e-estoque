from django.db import migrations, models
import django.db.models.deletion


CATALOGO_EMPRESA = [
    ("Veículos", "Combustível", "Gol", 10),
    ("Veículos", "Combustível", "Strada", 20),
    ("Veículos", "Manutenção", "Gol", 30),
    ("Veículos", "Manutenção", "Strada", 40),
    ("Funcionários", "Pagamento", "Pagamento", 50),
    ("Funcionários", "Vale", "Vale", 60),
    ("Funcionários", "Adiantamento", "Adiantamento", 70),
    ("Depósito", "Aluguel", "Aluguel", 80),
    ("Depósito", "Energia", "Energia", 90),
    ("Depósito", "Manutenção", "Manutenção", 100),
    ("Alimentação", "Café da manhã", "Café da manhã", 110),
    ("Alimentação", "Café", "Café", 120),
    ("Alimentação", "Água", "Água", 130),
    ("Alimentação", "Lanche", "Lanche", 140),
    ("Terreno", "Capina", "Capina", 150),
    ("Terreno", "Limpeza", "Limpeza", 160),
    ("Terreno", "Manutenção", "Manutenção", 170),
    ("Outros", "Outros", "Outros", 180),
]


def reorganizar_catalogo_empresa(apps, schema_editor):
    CatalogoDespesa = apps.get_model("estoque", "CatalogoDespesa")

    # Preserva todos os registros antigos e seus vínculos históricos.
    CatalogoDespesa.objects.filter(tipo="empresa").update(
        ativo=False,
        favorito=False,
    )

    for grupo, categoria, nome, ordem in CATALOGO_EMPRESA:
        CatalogoDespesa.objects.create(
            tipo="empresa",
            grupo=grupo,
            categoria=categoria,
            nome=nome,
            pessoa="",
            favorito=False,
            ativo=True,
            ordem=ordem,
        )


def reverso_preservando_historico(apps, schema_editor):
    # Não removemos catálogos porque podem estar ligados a despesas reais.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0113_reorganiza_catalogo_pessoal"),
    ]

    operations = [
        migrations.AddField(
            model_name="despesadiaria",
            name="funcionario",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="despesas_recebidas",
                to="estoque.funcionario",
            ),
        ),
        migrations.RunPython(
            reorganizar_catalogo_empresa,
            reverso_preservando_historico,
        ),
    ]
