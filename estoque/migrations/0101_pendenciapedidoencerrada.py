from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0100_separacaovenda_sequencia_diaria"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pedido",
            name="status",
            field=models.CharField(
                choices=[
                    ("aberto", "Aberto"),
                    ("cancelado", "Cancelado"),
                    ("parcial", "Parcial"),
                    ("encerrado", "Encerrado"),
                    ("convertido_em_venda", "Convertido em venda"),
                ],
                default="aberto",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PendenciaPedidoEncerrada",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("produto_nome", models.CharField(max_length=120)),
                ("quantidade", models.DecimalField(decimal_places=3, max_digits=12)),
                ("unidade", models.CharField(blank=True, max_length=20)),
                ("preco_unitario", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("valor_total", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("motivo", models.TextField(blank=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                (
                    "item_pedido",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pendencias_encerradas",
                        to="estoque.itempedido",
                    ),
                ),
                (
                    "pedido",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pendencias_encerradas",
                        to="estoque.pedido",
                    ),
                ),
                (
                    "produto",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pendencias_pedido_encerradas",
                        to="estoque.produto",
                    ),
                ),
            ],
            options={
                "ordering": ["-criado_em", "-id"],
            },
        ),
    ]
