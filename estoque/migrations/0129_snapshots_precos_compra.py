from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("estoque", "0128_itemcompra_revisao_preco_concluida")]

    operations = [
        migrations.AddField(
            model_name="produto", name="autoria_precos",
            field=models.JSONField(default=dict, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="itemcompra", name="alteracoes_precos",
            field=models.JSONField(default=dict, blank=True, editable=False),
        ),
    ]
