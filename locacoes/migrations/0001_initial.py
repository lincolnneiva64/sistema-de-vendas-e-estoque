from decimal import Decimal

from django.db import migrations, models


def criar_configuracoes_iniciais(apps, schema_editor):
    ConfiguracaoLocacao = apps.get_model("locacoes", "ConfiguracaoLocacao")
    FaixaPrecoLocacao = apps.get_model("locacoes", "FaixaPrecoLocacao")

    ConfiguracaoLocacao.objects.get_or_create(
        defaults={
            "valor_reposicao_cadeira": Decimal("40.00"),
            "valor_reposicao_mesa": Decimal("80.00"),
        }
    )

    faixas = [
        ("centro_perto", "Centro/perto", Decimal("8.00"), 1),
        ("mais_distante", "Mais distante", Decimal("10.00"), 2),
        ("muito_distante", "Muito distante", Decimal("15.00"), 3),
    ]
    for codigo, nome, preco, ordem in faixas:
        FaixaPrecoLocacao.objects.get_or_create(
            codigo=codigo,
            defaults={
                "nome": nome,
                "preco_jogo_diaria": preco,
                "ordem": ordem,
                "ativa": True,
            },
        )


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ConfiguracaoLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("total_mesas", models.PositiveIntegerField(blank=True, null=True)),
                ("total_cadeiras", models.PositiveIntegerField(blank=True, null=True)),
                ("preco_mesa_avulsa_diaria", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("preco_cadeira_avulsa_diaria", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("valor_reposicao_mesa", models.DecimalField(decimal_places=2, default=Decimal("80.00"), max_digits=10)),
                ("valor_reposicao_cadeira", models.DecimalField(decimal_places=2, default=Decimal("40.00"), max_digits=10)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Configuracao de locacao",
                "verbose_name_plural": "Configuracoes de locacao",
            },
        ),
        migrations.CreateModel(
            name="FaixaPrecoLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("codigo", models.CharField(choices=[("centro_perto", "Centro/perto"), ("mais_distante", "Mais distante"), ("muito_distante", "Muito distante")], max_length=30, unique=True)),
                ("nome", models.CharField(max_length=80)),
                ("preco_jogo_diaria", models.DecimalField(decimal_places=2, max_digits=10)),
                ("ordem", models.PositiveSmallIntegerField(default=0)),
                ("ativa", models.BooleanField(default=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Faixa de preco de locacao",
                "verbose_name_plural": "Faixas de preco de locacao",
                "ordering": ["ordem", "id"],
            },
        ),
        migrations.RunPython(criar_configuracoes_iniciais, migrations.RunPython.noop),
    ]
