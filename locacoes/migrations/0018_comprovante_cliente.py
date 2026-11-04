from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('locacoes', '0017_autorizacao_recolhimento')]
    operations = [
        migrations.AddField('conferenciarecolhimentolocacao', 'comprovante_token_hash',
                            models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)),
        migrations.AddField('conferenciarecolhimentolocacao', 'comprovante_expira_em',
                            models.DateTimeField(null=True, blank=True, editable=False)),
        migrations.AddField('conferenciarecolhimentolocacao', 'comprovante_revogado_em',
                            models.DateTimeField(null=True, blank=True, editable=False)),
    ]
