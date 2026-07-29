from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("estoque", "0088_registrocobrancacliente"),
        ("locacoes", "0002_movimentoestoquelocacao"),
    ]

    operations = [
        migrations.CreateModel(
            name="Locacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo_pessoa", models.CharField(choices=[("cliente", "Cliente cadastrado"), ("avulsa", "Pessoa avulsa")], max_length=20)),
                ("pessoa_avulsa_nome", models.CharField(blank=True, max_length=160)),
                ("pessoa_avulsa_telefone", models.CharField(blank=True, max_length=40)),
                ("pessoa_avulsa_endereco", models.TextField(blank=True)),
                ("endereco_entrega", models.TextField()),
                ("data_entrega", models.DateField()),
                ("horario_entrega", models.TimeField()),
                ("data_evento", models.DateField()),
                ("horario_evento", models.TimeField()),
                ("data_prevista_devolucao", models.DateField()),
                ("faixa_preco_nome_snapshot", models.CharField(max_length=80)),
                ("status", models.CharField(choices=[("reservada", "Reservada"), ("cancelada", "Cancelada")], default="reservada", max_length=20)),
                ("observacao", models.TextField(blank=True)),
                ("motivo_cancelamento", models.TextField(blank=True)),
                ("total", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("cancelada_em", models.DateTimeField(blank=True, null=True)),
                ("cliente", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="locacoes", to="estoque.cliente")),
                ("faixa_preco", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="locacoes", to="locacoes.faixaprecolocacao")),
            ],
            options={
                "verbose_name": "Locacao",
                "verbose_name_plural": "Locacoes",
                "ordering": ["-data_entrega", "-id"],
            },
        ),
        migrations.CreateModel(
            name="EventoLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo", models.CharField(max_length=40)),
                ("descricao", models.TextField(blank=True)),
                ("responsavel", models.CharField(blank=True, max_length=120)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("locacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="eventos", to="locacoes.locacao")),
            ],
            options={
                "verbose_name": "Evento de locacao",
                "verbose_name_plural": "Eventos de locacao",
                "ordering": ["-criado_em", "-id"],
            },
        ),
        migrations.CreateModel(
            name="ItemLocacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo", models.CharField(choices=[("jogo", "Jogo"), ("mesa_avulsa", "Mesa avulsa"), ("cadeira_avulsa", "Cadeira avulsa")], max_length=30)),
                ("quantidade", models.PositiveIntegerField()),
                ("preco_diaria_snapshot", models.DecimalField(decimal_places=2, max_digits=10)),
                ("diarias", models.PositiveIntegerField()),
                ("valor_total", models.DecimalField(decimal_places=2, max_digits=12)),
                ("ajuste_manual", models.BooleanField(default=False)),
                ("locacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="itens", to="locacoes.locacao")),
            ],
            options={
                "verbose_name": "Item de locacao",
                "verbose_name_plural": "Itens de locacao",
                "ordering": ["id"],
            },
        ),
    ]
