from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('offline','0005_validate_local_sale_principal'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='ControleResolucaoVendaOffline',fields=[
            ('actor',models.OneToOneField(primary_key=True,serialize=False,on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='AbandonoVendaOffline',fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
            ('operation_id',models.UUIDField(unique=True)), ('closure_id',models.UUIDField(unique=True)),
            ('environment_id',models.CharField(max_length=160)), ('device_id',models.UUIDField()),
            ('payload_hash',models.CharField(max_length=64)), ('comando',models.JSONField()),
            ('motivo',models.TextField()), ('evidencia',models.JSONField()),
            ('encerrado_em',models.DateTimeField(auto_now_add=True)),
            ('actor',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
        ]),
    ]
