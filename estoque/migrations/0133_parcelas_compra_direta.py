import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("estoque", "0132_adocao_precos_por_produto")]

    operations = [
        migrations.AddField(
            model_name="compra", name="valor_nota_boleto",
            field=models.DecimalField(blank=True, null=True, max_digits=12, decimal_places=2),
        ),
        migrations.AddField(
            model_name="compra", name="forma_cobranca_nota",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="compra", name="observacao_pagamento_nota",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="ParcelaCompra",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("numero", models.PositiveIntegerField()),
                ("data_vencimento", models.DateField()),
                ("valor", models.DecimalField(max_digits=12, decimal_places=2)),
                ("observacao", models.CharField(blank=True, default="", max_length=120)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("compra", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="parcelas_nota", to="estoque.compra")),
            ],
            options={
                "ordering": ["numero", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("compra", "numero"), name="parcela_compra_numero_unico"),
                    models.CheckConstraint(condition=models.Q(numero__gte=1, numero__lte=12), name="parcela_compra_numero_valido"),
                    models.CheckConstraint(condition=models.Q(valor__gt=0), name="parcela_compra_valor_positivo"),
                ],
            },
        ),
    ]
