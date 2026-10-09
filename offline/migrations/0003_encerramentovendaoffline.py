from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('offline', '0002_revisaovendaoffline'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name='EncerramentoVendaOffline',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('closure_id', models.UUIDField(unique=True)),
            ('original_hash', models.CharField(max_length=64)),
            ('environment_id', models.CharField(max_length=160)),
            ('device_id', models.UUIDField()),
            ('motivo', models.TextField()),
            ('evidencia', models.JSONField()),
            ('encerrado_em', models.DateTimeField(auto_now_add=True)),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('original', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                related_name='encerramento_administrativo', to='offline.operacaosincronizacao')),
        ],
    )]
