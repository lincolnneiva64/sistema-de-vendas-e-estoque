from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0043_alter_itempedido_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="funcionario",
            name="pode_operar_sistema",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterModelOptions(
            name="funcionario",
            options={
                "ordering": ["-ativo", "-pode_operar_sistema", "-pode_receber_checklist", "nome"],
                "verbose_name": "Funcionario",
                "verbose_name_plural": "Funcionarios",
            },
        ),
    ]
