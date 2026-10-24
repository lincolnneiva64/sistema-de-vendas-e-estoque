from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [("estoque", "0134_edicao_compra")]
    operations = [
        migrations.AddField(model_name="compra", name="conciliacao_automatica", field=models.BooleanField(default=False, db_default=False, editable=False)),
        migrations.AddField(model_name="itemcompra", name="estoque_base_registrado", field=models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, editable=False)),
        migrations.CreateModel(name="ConciliacaoCompra", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("tipo", models.CharField(max_length=24)),
            ("valor_pago", models.DecimalField(max_digits=12, decimal_places=2)),
            ("valor_devolvido", models.DecimalField(max_digits=12, decimal_places=2, default=0)),
            ("credito_fornecedor", models.DecimalField(max_digits=12, decimal_places=2, default=0)),
            ("saldo_a_pagar", models.DecimalField(max_digits=12, decimal_places=2, default=0)),
            ("efeitos", models.JSONField(default=dict)),
            ("atualizado_em", models.DateTimeField(auto_now=True)),
            ("compra", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="conciliacao", to="estoque.compra")),
            ("edicao", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="conciliacao", to="estoque.edicaocompra")),
        ]),
    ]
