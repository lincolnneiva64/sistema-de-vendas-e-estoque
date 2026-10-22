from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("inventario", "0007_auditoria_ajuste_imutavel"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="inventario", name="cancelada_em", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="inventario", name="cancelada_por", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventarios_cancelados", to=settings.AUTH_USER_MODEL)),
    ]
