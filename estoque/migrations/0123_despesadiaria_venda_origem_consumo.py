# Generated manually for consumo proprio automatic expenses.

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0122_fechamentorotarecebimento_conferido_por_funcionario"),
    ]

    operations = [
        migrations.AddField(
            model_name="despesadiaria",
            name="venda_origem",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="despesas_automaticas",
                to="estoque.venda",
            ),
        ),
        migrations.AddField(
            model_name="despesadiaria",
            name="origem_automatica",
            field=models.CharField(
                blank=True,
                choices=[("consumo_proprio", "Consumo proprio")],
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="despesadiaria",
            name="chave_automatica",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddConstraint(
            model_name="despesadiaria",
            constraint=models.UniqueConstraint(
                condition=Q(
                    venda_origem__isnull=False,
                    origem_automatica__gt="",
                    chave_automatica__gt="",
                ),
                fields=("venda_origem", "origem_automatica", "chave_automatica"),
                name="uniq_despesa_automatica_venda_chave",
            ),
        ),
    ]
