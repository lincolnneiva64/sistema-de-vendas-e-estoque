from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('locacoes', '0016_locacao_horario_previsto_devolucao'),
        ('estoque', '0023_funcionario'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name='AutorizacaoRecolhimento',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token_hash', models.CharField(editable=False, max_length=64, unique=True)),
            ('criada_em', models.DateTimeField(auto_now_add=True)),
            ('expira_em', models.DateTimeField()),
            ('revogada_em', models.DateTimeField(blank=True, null=True)),
            ('tarefa', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='autorizacoes_recolhimento', to='locacoes.tarefaoperacionallocacao')),
            ('funcionario', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='estoque.funcionario')),
            ('emitida_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('conferencia', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='locacoes.conferenciarecolhimentolocacao')),
        ],
        options={'permissions': [('autorizar_recolhimento', 'Pode autorizar links individuais de recolhimento')]},
    )]
