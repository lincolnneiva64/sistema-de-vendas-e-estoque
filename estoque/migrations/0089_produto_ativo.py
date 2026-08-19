from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0088_registrocobrancacliente"),
    ]

    operations = [
        migrations.AddField(
            model_name="produto",
            name="ativo",
            field=models.BooleanField(default=True),
        ),
    ]
