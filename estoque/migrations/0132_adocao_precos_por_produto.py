from django.db import migrations, models


def preservar_adocao_existente(apps, schema_editor):
    # Preserve the effective price source already used by active groups.
    # No price, stock, cost, history or pending group is modified.
    Produto = apps.get_model('estoque', 'Produto')
    Produto.objects.using(schema_editor.connection.alias).filter(
        vinculo_grupo__grupo__precos_regularizados=True,
    ).update(precos_canonicos_adotados=True)


class Migration(migrations.Migration):
    dependencies = [('estoque', '0131_precos_vinculados_auditoria')]
    operations = [
        migrations.AddField(
            model_name='produto', name='precos_canonicos_adotados',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(preservar_adocao_existente, migrations.RunPython.noop),
    ]
