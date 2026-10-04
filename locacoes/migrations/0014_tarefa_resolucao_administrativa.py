from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("locacoes", "0013_pagamentolocacao_operacao_recebimento_cliente")]

    operations = [
        migrations.AlterField(
            model_name="tarefaoperacionallocacao", name="status",
            field=models.CharField(max_length=20, default="pendente", choices=[
                ("pendente", "Pendente"), ("parcial", "Parcial"),
                ("confirmada", "Confirmada"),
                ("nao_possivel", "Nao foi possivel realizar"),
                ("resolvida_admin", "Resolvida administrativamente"),
            ]),
        ),
        migrations.AddField(model_name="tarefaoperacionallocacao", name="resolvida_em", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="tarefaoperacionallocacao", name="resolvida_por", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="tarefaoperacionallocacao", name="motivo_resolucao", field=models.TextField(blank=True)),
    ]
