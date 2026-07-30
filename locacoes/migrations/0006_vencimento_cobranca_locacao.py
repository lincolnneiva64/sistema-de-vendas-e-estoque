import django.db.models.deletion
from django.db import migrations, models


def preencher_vencimento_existente(apps, schema_editor):
    Locacao = apps.get_model("locacoes", "Locacao")
    for locacao in Locacao.objects.filter(data_vencimento_saldo__isnull=True).only("id", "data_entrega"):
        locacao.data_vencimento_saldo = locacao.data_entrega
        locacao.save(update_fields=["data_vencimento_saldo"])


class Migration(migrations.Migration):
    dependencies = [
        ("locacoes", "0005_pagamentos_recibos_termo"),
    ]

    operations = [
        migrations.AddField(
            model_name="locacao",
            name="data_vencimento_saldo",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(preencher_vencimento_existente, migrations.RunPython.noop),
        migrations.CreateModel(
            name="RegistroCobrancaLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo", models.CharField(choices=[("whatsapp", "WhatsApp"), ("telefone", "Ligacao"), ("visita", "Visita"), ("outro", "Outro")], max_length=20)),
                ("status", models.CharField(choices=[("pendente", "Pendente"), ("contatado", "Contatado"), ("sem_resposta", "Nao atendeu"), ("promessa_pagamento", "Promessa de pagamento"), ("resolvido", "Resolvido"), ("outro", "Outro")], max_length=30)),
                ("observacao", models.TextField(blank=True)),
                ("criado_por_nome", models.CharField(blank=True, max_length=150)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("locacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="registros_cobranca", to="locacoes.locacao")),
            ],
            options={
                "verbose_name": "Registro de cobranca de locacao",
                "verbose_name_plural": "Registros de cobranca de locacoes",
                "ordering": ["-criado_em", "-id"],
            },
        ),
    ]
