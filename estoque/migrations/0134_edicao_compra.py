import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("estoque", "0133_parcelas_compra_direta"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name="EdicaoCompra",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("operacao", models.UUIDField(unique=True)),
            ("motivo", models.TextField()),
            ("payload_hash", models.CharField(max_length=64)),
            ("antes", models.JSONField()),
            ("depois", models.JSONField()),
            ("criado_em", models.DateTimeField(auto_now_add=True)),
            ("compra", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="edicoes", to="estoque.compra")),
            ("usuario", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        ],
    )]
