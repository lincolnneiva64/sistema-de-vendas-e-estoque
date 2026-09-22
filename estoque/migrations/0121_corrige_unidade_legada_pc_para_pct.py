from django.db import migrations


def corrigir_pc_para_pct(apps, schema_editor):
    Produto = apps.get_model("estoque", "Produto")

    Produto.objects.filter(unidade_compra__iexact="PC").update(
        unidade_compra="PCT"
    )
    Produto.objects.filter(unidade_venda_1__iexact="PC").update(
        unidade_venda_1="PCT"
    )
    Produto.objects.filter(unidade_venda_2__iexact="PC").update(
        unidade_venda_2="PCT"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0120_movimentofinanceiro_pagamento_conta_pagar"),
    ]

    operations = [
        migrations.RunPython(
            corrigir_pc_para_pct,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
