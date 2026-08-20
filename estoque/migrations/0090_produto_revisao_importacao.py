from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('estoque', '0089_produto_ativo'),
    ]

    operations = [
        migrations.AddField(
            model_name='produto',
            name='revisado_importacao',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='produto',
            name='revisado_importacao_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
