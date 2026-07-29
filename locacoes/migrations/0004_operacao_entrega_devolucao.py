import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("locacoes", "0003_locacao_itemlocacao_eventolocacao"),
    ]

    operations = [
        migrations.AlterField(
            model_name="locacao",
            name="status",
            field=models.CharField(
                choices=[
                    ("reservada", "Reservada"),
                    ("saiu_para_entrega", "Saiu para entrega"),
                    ("entregue", "Entregue"),
                    ("devolvida", "Devolvida"),
                    ("devolvida_com_avaria", "Devolvida com avaria"),
                    ("pendente_devolucao", "Pendente de devolucao"),
                    ("cancelada", "Cancelada"),
                ],
                default="reservada",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="itemlocacao",
            name="devolvida_boa",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="itemlocacao",
            name="quebrada",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="itemlocacao",
            name="perdida",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="itemlocacao",
            name="descartada",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="movimentoestoquelocacao",
            name="item_locacao",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="movimentos_estoque",
                to="locacoes.itemlocacao",
            ),
        ),
        migrations.AddField(
            model_name="movimentoestoquelocacao",
            name="locacao",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="movimentos_estoque",
                to="locacoes.locacao",
            ),
        ),
    ]
