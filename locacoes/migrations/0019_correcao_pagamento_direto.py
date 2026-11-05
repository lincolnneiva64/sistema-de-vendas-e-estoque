from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('locacoes', '0018_comprovante_cliente'),
        ('estoque', '0056_contafinanceira_movimentofinanceiro'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name='CorrecaoPagamentoLocacao',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('operacao_uuid', models.UUIDField(unique=True, editable=False)),
            ('versao_anterior', models.CharField(max_length=64, editable=False)),
            ('requisicao_hash', models.CharField(max_length=64, editable=False)),
            ('antes', models.JSONField(editable=False)),
            ('depois', models.JSONField(editable=False)),
            ('motivo', models.TextField()),
            ('usuario_nome', models.CharField(max_length=150, editable=False)),
            ('criado_em', models.DateTimeField(auto_now_add=True)),
            ('recibo_anterior_html', models.TextField(editable=False)),
            ('recibo_corrigido_html', models.TextField(editable=False)),
            ('pagamento', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='correcoes', to='locacoes.pagamentolocacao')),
            ('usuario', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('movimento_original', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
            ('estorno', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
            ('movimento_novo', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='estoque.movimentofinanceiro')),
        ],
        options={
            'ordering': ['criado_em', 'pk'],
            'constraints': [models.UniqueConstraint(fields=('pagamento', 'versao_anterior'), name='locacao_correcao_versao_unica')],
        },
    )]
