from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("locacoes", "0014_tarefa_resolucao_administrativa")]

    operations = [
        migrations.AlterField(
            model_name="tarefaoperacionallocacao",
            name="status",
            field=models.CharField(
                max_length=32,
                default="pendente",
                choices=[
                    ("pendente", "Pendente"),
                    ("parcial", "Parcial"),
                    ("confirmada", "Confirmada"),
                    ("nao_possivel", "Nao foi possivel realizar"),
                    ("resolvida_admin", "Resolvida administrativamente"),
                    ("confirmada_sem_checklist", "Entrega confirmada sem checklist fisico"),
                ],
            ),
        ),
    ]
