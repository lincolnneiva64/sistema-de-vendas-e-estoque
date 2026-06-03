from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0038_alter_ajusteitemvendaquitada_resolucao_financeira"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemVendaRemovido",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("produto_nome_snapshot", models.CharField(max_length=120)),
                ("quantidade_snapshot", models.DecimalField(decimal_places=3, max_digits=12)),
                ("unidade_snapshot", models.CharField(blank=True, max_length=20)),
                ("preco_unitario_snapshot", models.DecimalField(decimal_places=2, max_digits=12)),
                ("valor_total_snapshot", models.DecimalField(decimal_places=2, max_digits=12)),
                ("item_venda_original_id", models.PositiveIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("removido", "Removido"), ("revertido", "Revertido")], default="removido", max_length=20)),
                ("operador", models.CharField(blank=True, max_length=120)),
                ("observacao", models.TextField(blank=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("revertido_em", models.DateTimeField(blank=True, null=True)),
                ("credito_gerado", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="itens_removidos_origem", to="estoque.creditocliente")),
                ("produto", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="itens_venda_removidos", to="estoque.produto")),
                ("venda", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="itens_removidos", to="estoque.venda")),
            ],
            options={
                "ordering": ["-criado_em", "-id"],
            },
        ),
    ]
