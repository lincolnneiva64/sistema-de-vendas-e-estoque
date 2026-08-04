# Generated manually for checklist de entrega por material contratado

from django.db import migrations, models


def preencher_snapshots_entrega(apps, schema_editor):
    ItemLocacao = apps.get_model("locacoes", "ItemLocacao")
    ConferenciaEntregaLocacao = apps.get_model(
        "locacoes",
        "ConferenciaEntregaLocacao",
    )

    tipo_jogo = "jogo"
    tipo_mesa_avulsa = "mesa_avulsa"
    tipo_cadeira_avulsa = "cadeira_avulsa"

    for locacao_id in (
        ConferenciaEntregaLocacao.objects
        .values_list("locacao_id", flat=True)
        .distinct()
    ):
        itens = ItemLocacao.objects.filter(locacao_id=locacao_id)
        previsto = {
            "jogos": 0,
            "mesas_avulsas": 0,
            "cadeiras_avulsas": 0,
        }
        for item in itens:
            if item.tipo == tipo_jogo:
                previsto["jogos"] += int(item.quantidade or 0)
            elif item.tipo == tipo_mesa_avulsa:
                previsto["mesas_avulsas"] += int(item.quantidade or 0)
            elif item.tipo == tipo_cadeira_avulsa:
                previsto["cadeiras_avulsas"] += int(item.quantidade or 0)

        acumulado = {
            "jogos": 0,
            "mesas_avulsas": 0,
            "cadeiras_avulsas": 0,
        }

        conferencias = (
            ConferenciaEntregaLocacao.objects
            .filter(locacao_id=locacao_id)
            .order_by("criado_em", "id")
        )
        for conferencia in conferencias:
            pendente_antes = {
                chave: max(previsto[chave] - acumulado[chave], 0)
                for chave in previsto
            }
            mesas_restantes = int(conferencia.entregue_mesas or 0)
            cadeiras_restantes = int(conferencia.entregue_cadeiras or 0)

            entregue_jogos = min(
                pendente_antes["jogos"],
                mesas_restantes,
                cadeiras_restantes // 4,
            )
            mesas_restantes -= entregue_jogos
            cadeiras_restantes -= entregue_jogos * 4

            entregue_mesas_avulsas = min(
                pendente_antes["mesas_avulsas"],
                mesas_restantes,
            )
            mesas_restantes -= entregue_mesas_avulsas

            entregue_cadeiras_avulsas = min(
                pendente_antes["cadeiras_avulsas"],
                cadeiras_restantes,
            )

            entregues = {
                "jogos": entregue_jogos,
                "mesas_avulsas": entregue_mesas_avulsas,
                "cadeiras_avulsas": entregue_cadeiras_avulsas,
            }
            for chave in acumulado:
                acumulado[chave] += entregues[chave]

            pendente_depois = {
                chave: max(previsto[chave] - acumulado[chave], 0)
                for chave in previsto
            }

            conferencia.previsto_jogos = previsto["jogos"]
            conferencia.previsto_mesas_avulsas = previsto["mesas_avulsas"]
            conferencia.previsto_cadeiras_avulsas = (
                previsto["cadeiras_avulsas"]
            )
            conferencia.entregue_jogos = entregues["jogos"]
            conferencia.entregue_mesas_avulsas = (
                entregues["mesas_avulsas"]
            )
            conferencia.entregue_cadeiras_avulsas = (
                entregues["cadeiras_avulsas"]
            )
            conferencia.acumulado_jogos = acumulado["jogos"]
            conferencia.acumulado_mesas_avulsas = (
                acumulado["mesas_avulsas"]
            )
            conferencia.acumulado_cadeiras_avulsas = (
                acumulado["cadeiras_avulsas"]
            )
            conferencia.pendente_jogos = pendente_depois["jogos"]
            conferencia.pendente_mesas_avulsas = (
                pendente_depois["mesas_avulsas"]
            )
            conferencia.pendente_cadeiras_avulsas = (
                pendente_depois["cadeiras_avulsas"]
            )
            conferencia.save(update_fields=[
                "previsto_jogos",
                "previsto_mesas_avulsas",
                "previsto_cadeiras_avulsas",
                "entregue_jogos",
                "entregue_mesas_avulsas",
                "entregue_cadeiras_avulsas",
                "acumulado_jogos",
                "acumulado_mesas_avulsas",
                "acumulado_cadeiras_avulsas",
                "pendente_jogos",
                "pendente_mesas_avulsas",
                "pendente_cadeiras_avulsas",
            ])


class Migration(migrations.Migration):

    dependencies = [
        ("locacoes", "0011_alter_pagamentolocacao_recibo_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="previsto_jogos",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="previsto_mesas_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="previsto_cadeiras_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="entregue_jogos",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="entregue_mesas_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="entregue_cadeiras_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="acumulado_jogos",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="acumulado_mesas_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="acumulado_cadeiras_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="pendente_jogos",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="pendente_mesas_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conferenciaentregalocacao",
            name="pendente_cadeiras_avulsas",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            preencher_snapshots_entrega,
            migrations.RunPython.noop,
        ),
    ]
