from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL), ("offline", "0001_initial")]

    operations = [migrations.CreateModel(
        name="RevisaoVendaOffline",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("relacao", models.CharField(default="revisao_de_conflito", max_length=40)),
            ("original_hash", models.CharField(max_length=64)),
            ("environment_id", models.CharField(max_length=160)),
            ("motivo_original", models.TextField()),
            ("revisada_em", models.DateTimeField()),
            ("registrado_em", models.DateTimeField(auto_now_add=True)),
            ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ("original", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="substituicao", to="offline.operacaosincronizacao")),
            ("substituta", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="revisao_origem", to="offline.operacaosincronizacao")),
        ],
    )]
