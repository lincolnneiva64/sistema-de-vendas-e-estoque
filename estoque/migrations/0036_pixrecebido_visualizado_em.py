from django.db import migrations, models
from django.utils import timezone


def marcar_pix_antigos_como_visualizados(apps, schema_editor):
    PixRecebido = apps.get_model("estoque", "PixRecebido")
    agora = timezone.now()
    PixRecebido.objects.filter(visualizado_em__isnull=True).update(visualizado_em=agora)


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0035_pixrecebido_status_duplicado"),
    ]

    operations = [
        migrations.AddField(
            model_name="pixrecebido",
            name="visualizado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(marcar_pix_antigos_como_visualizados, migrations.RunPython.noop),
    ]
