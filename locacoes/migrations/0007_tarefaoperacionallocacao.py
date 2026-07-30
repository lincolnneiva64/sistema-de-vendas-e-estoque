import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("locacoes", "0006_vencimento_cobranca_locacao"),
    ]

    operations = [
        migrations.CreateModel(
            name="TarefaOperacionalLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo", models.CharField(choices=[("entrega", "Entrega"), ("recolhimento", "Recolhimento")], max_length=20)),
                ("status", models.CharField(choices=[("pendente", "Pendente"), ("confirmada", "Confirmada"), ("nao_possivel", "Nao foi possivel realizar")], default="pendente", max_length=20)),
                ("data_agendada", models.DateField()),
                ("horario_agendado", models.TimeField(blank=True, null=True)),
                ("confirmado_em", models.DateTimeField(blank=True, null=True)),
                ("confirmado_por", models.CharField(blank=True, max_length=120)),
                ("tentativa_em", models.DateTimeField(blank=True, null=True)),
                ("tentativa_por", models.CharField(blank=True, max_length=120)),
                ("motivo_nao_realizado", models.TextField(blank=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("locacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tarefas_operacionais", to="locacoes.locacao")),
            ],
            options={
                "verbose_name": "Tarefa operacional de locacao",
                "verbose_name_plural": "Tarefas operacionais de locacao",
                "ordering": ["data_agendada", "horario_agendado", "id"],
                "unique_together": {("locacao", "tipo")},
            },
        ),
    ]
