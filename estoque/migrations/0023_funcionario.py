# Generated manually for the Sistema de Vendas project.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0022_entregachecklistitem_flags"),
    ]

    operations = [
        migrations.CreateModel(
            name="Funcionario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nome", models.CharField(max_length=140)),
                ("telefone_whatsapp", models.CharField(blank=True, max_length=30, null=True)),
                ("telefone_whatsapp_normalizado", models.CharField(blank=True, max_length=20, null=True)),
                ("ativo", models.BooleanField(default=True)),
                ("pode_receber_checklist", models.BooleanField(default=False)),
                ("observacoes", models.TextField(blank=True, null=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Funcionario",
                "verbose_name_plural": "Funcionarios",
                "ordering": ["-ativo", "-pode_receber_checklist", "nome"],
            },
        ),
    ]
