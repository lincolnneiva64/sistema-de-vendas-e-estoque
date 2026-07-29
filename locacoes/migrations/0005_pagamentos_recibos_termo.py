from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def congelar_reposicao_existente(apps, schema_editor):
    ConfiguracaoLocacao = apps.get_model("locacoes", "ConfiguracaoLocacao")
    Locacao = apps.get_model("locacoes", "Locacao")
    configuracao = ConfiguracaoLocacao.objects.order_by("id").first()
    mesa = Decimal("0.00")
    cadeira = Decimal("0.00")
    if configuracao:
        mesa = configuracao.valor_reposicao_mesa or Decimal("0.00")
        cadeira = configuracao.valor_reposicao_cadeira or Decimal("0.00")
    Locacao.objects.update(
        valor_reposicao_mesa_snapshot=mesa,
        valor_reposicao_cadeira_snapshot=cadeira,
        saldo_devedor=models.F("total"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("estoque", "0088_registrocobrancacliente"),
        ("locacoes", "0004_operacao_entrega_devolucao"),
    ]

    operations = [
        migrations.AddField(
            model_name="locacao",
            name="total_pago",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12),
        ),
        migrations.AddField(
            model_name="locacao",
            name="saldo_devedor",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12),
        ),
        migrations.AddField(
            model_name="locacao",
            name="status_financeiro",
            field=models.CharField(
                choices=[("pendente", "Pendente"), ("parcial", "Parcialmente pago"), ("quitada", "Quitada")],
                default="pendente",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="locacao",
            name="valor_reposicao_mesa_snapshot",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="locacao",
            name="valor_reposicao_cadeira_snapshot",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="locacao",
            name="termo_gerado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="locacao",
            name="termo_gerado_por",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.CreateModel(
            name="PagamentoLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("valor", models.DecimalField(decimal_places=2, max_digits=12)),
                ("data_hora", models.DateTimeField(default=django.utils.timezone.now)),
                ("forma_pagamento", models.CharField(choices=[("dinheiro", "Dinheiro"), ("pix", "Pix"), ("cartao", "Cartao"), ("outro", "Outro")], max_length=20)),
                ("observacao", models.TextField(blank=True)),
                ("responsavel", models.CharField(blank=True, max_length=120)),
                ("recibo_status", models.CharField(choices=[("pendente", "Pendente"), ("enviado", "Enviado"), ("dispensado", "Dispensado / Nao enviado")], default="pendente", max_length=20)),
                ("recibo_enviado_em", models.DateTimeField(blank=True, null=True)),
                ("recibo_enviado_por", models.CharField(blank=True, max_length=120)),
                ("recibo_dispensado_em", models.DateTimeField(blank=True, null=True)),
                ("recibo_dispensado_por", models.CharField(blank=True, max_length=120)),
                ("recibo_dispensa_observacao", models.TextField(blank=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("locacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pagamentos", to="locacoes.locacao")),
                ("movimento_financeiro", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pagamento_locacao", to="estoque.movimentofinanceiro")),
            ],
            options={
                "verbose_name": "Pagamento de locacao",
                "verbose_name_plural": "Pagamentos de locacao",
                "ordering": ["-data_hora", "-id"],
            },
        ),
        migrations.RunPython(congelar_reposicao_existente, migrations.RunPython.noop),
    ]
