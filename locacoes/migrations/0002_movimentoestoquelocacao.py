from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("locacoes", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MovimentoEstoqueLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("item", models.CharField(choices=[("mesa", "Mesa"), ("cadeira", "Cadeira")], max_length=20)),
                ("tipo", models.CharField(choices=[("entrada", "Entrada/compra ou aquisicao"), ("baixa_quebra", "Baixa definitiva por quebra"), ("baixa_perda", "Baixa definitiva por perda"), ("baixa_descarte", "Baixa definitiva por descarte"), ("ajuste_inventario", "Ajuste de inventario")], max_length=30)),
                ("quantidade", models.PositiveIntegerField()),
                ("saldo_anterior", models.PositiveIntegerField()),
                ("saldo_posterior", models.PositiveIntegerField()),
                ("responsavel", models.CharField(max_length=120)),
                ("observacao", models.TextField(blank=True)),
                ("data_hora", models.DateTimeField(default=django.utils.timezone.now)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Movimento de estoque de locacao",
                "verbose_name_plural": "Movimentos de estoque de locacao",
                "ordering": ["-data_hora", "-id"],
            },
        ),
    ]
