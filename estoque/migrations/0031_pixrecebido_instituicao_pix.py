from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0030_pixrecebido"),
    ]

    operations = [
        migrations.AddField(
            model_name="pixrecebido",
            name="instituicao_pix",
            field=models.CharField(blank=True, max_length=80),
        ),
    ]
