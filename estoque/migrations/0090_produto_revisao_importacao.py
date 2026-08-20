# Generated migration for adding revisao_importacao fields
# This migration adds two fields to Produto model for tracking review status of imported products

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('estoque', '0089_produto_ativo'),
    ]

    operations = [
        migrations.AddField(
            model_name='produto',
            name='revisado_importacao',
            field=models.BooleanField(db_index=True, default=False, help_text='Indica se o produto foi revisado pela importação'),
        ),
        migrations.AddField(
            model_name='produto',
            name='revisado_importacao_em',
            field=models.DateTimeField(blank=True, db_index=True, null=True, help_text='Data/hora quando foi revisado'),
        ),
    ]
