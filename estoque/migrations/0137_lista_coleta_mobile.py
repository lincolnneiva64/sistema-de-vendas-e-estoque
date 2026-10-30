from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('estoque', '0136_itemvenda_quantidade_precisao')]

    operations = [
        # Existing rows stay NULL: their collection history cannot be inferred.
        migrations.AddField(
            model_name='listacomprafornecedor', name='coleta_mobile',
            field=models.JSONField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='listacomprafornecedor', name='coleta_mobile',
            field=models.JSONField(default=dict, null=True, blank=True),
        ),
    ]
