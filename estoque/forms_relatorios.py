"""Períodos dos relatórios; datas comerciais no fuso de Belém."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django import forms
from django.utils import timezone


BELEM = ZoneInfo('America/Belem')
PERIODOS = (
    ('hoje', 'Hoje'), ('dias_7', 'Últimos 7 dias'),
    ('dias_30', 'Últimos 30 dias'), ('dias_60', 'Últimos 60 dias'),
    ('dias_90', 'Últimos 90 dias'), ('mes_atual', 'Mês atual'),
    ('mes_anterior', 'Mês anterior'),
    ('meses_2', 'Últimos 2 meses (calendário)'),
    ('meses_3', 'Últimos 3 meses (calendário)'),
    ('personalizado', 'Intervalo personalizado'),
)


class EvolucaoRelatorioForm(forms.Form):
    modo = forms.ChoiceField(label='Evolução', choices=(('diario', 'Diário'), ('semanal', 'Semanal'), ('mensal', 'Mensal')))
    dia_semana = forms.TypedChoiceField(label='Dia da semana', coerce=int,
        choices=((-1, 'Todos os dias'), *tuple(enumerate(('Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo')))))
    equivalente = forms.BooleanField(label='Comparar mês selecionado com os mesmos dias do mês anterior', required=False)
    alcance = forms.ChoiceField(label='Alcance histórico', choices=(
        ('principal', 'Período do faturamento'), ('3', 'Últimos 3 meses'),
        ('6', 'Últimos 6 meses'), ('12', 'Últimos 12 meses'),
        ('24', 'Últimos 24 meses'), ('personalizado', 'Período personalizado')))
    historico_inicio = forms.DateField(label='Histórico de', required=False,
        input_formats=['%Y-%m-%d'], widget=forms.DateInput(attrs={'type': 'date'}))
    historico_fim = forms.DateField(label='Histórico até', required=False,
        input_formats=['%Y-%m-%d'], widget=forms.DateInput(attrs={'type': 'date'}))
    comparacao = forms.ChoiceField(label='Comparação mensal', choices=(
        ('anterior', 'Mês anterior'), ('ano_anterior', 'Mesmo mês do ano anterior')))

    def clean(self):
        dados = super().clean()
        if dados.get('alcance') == 'personalizado':
            a, b = dados.get('historico_inicio'), dados.get('historico_fim')
            if not a:
                self.add_error('historico_inicio', 'Informe o início do histórico.')
            if not b:
                self.add_error('historico_fim', 'Informe o fim do histórico.')
            if a and b and a > b:
                self.add_error('historico_fim', 'O fim deve ser igual ou posterior ao início.')
        return dados


def intervalo_historico(opcoes, hoje):
    """Calendar months including the current month; independent from cards."""
    alcance = opcoes.get('alcance', 'principal')
    if alcance == 'principal':
        return None
    if alcance == 'personalizado':
        return opcoes['historico_inicio'], opcoes['historico_fim']
    ano, mes = divmod(hoje.year * 12 + hoje.month - int(alcance), 12)
    return date(ano, mes+1, 1), hoje


def intervalos_predefinidos(hoje):
    anterior_fim = hoje.replace(day=1) - timedelta(days=1)
    intervalos = {'hoje': (hoje, hoje), 'mes_anterior': (anterior_fim.replace(day=1), anterior_fim)}
    for dias in (7, 30, 60, 90):
        intervalos[f'dias_{dias}'] = (hoje - timedelta(days=dias - 1), hoje)
    for periodo, meses in (('mes_atual', 1), ('meses_2', 2), ('meses_3', 3)):
        ano, mes = divmod(hoje.year * 12 + hoje.month - meses, 12)
        intervalos[periodo] = (date(ano, mes + 1, 1), hoje)
    return intervalos


class PeriodoRelatorioForm(forms.Form):
    periodo = forms.ChoiceField(label='Período', choices=PERIODOS)
    data_inicial = forms.DateField(label='Data inicial', required=False,
                                  input_formats=['%Y-%m-%d'], widget=forms.DateInput(attrs={'type': 'date'}))
    data_final = forms.DateField(label='Data final', required=False,
                                input_formats=['%Y-%m-%d'], widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, data=None, *args, **kwargs):
        intervalos = intervalos_predefinidos(timezone.localdate(timezone=BELEM))
        self.periodos_datas = {chave: {'inicio': inicio.isoformat(), 'fim': fim.isoformat()}
                              for chave, (inicio, fim) in intervalos.items()}
        if data is not None:
            data = data.copy()
            previsto = self.periodos_datas.get(data.get('periodo'))
            if previsto:
                inicio, fim = data.get('data_inicial', ''), data.get('data_final', '')
                if not inicio and not fim:
                    data['data_inicial'], data['data_final'] = previsto['inicio'], previsto['fim']
                elif (inicio, fim) != (previsto['inicio'], previsto['fim']):
                    # Datas explícitas nunca são descartadas por um seletor desatualizado.
                    data['periodo'] = 'personalizado'
        super().__init__(data, *args, **kwargs)

    def clean(self):
        dados = super().clean()
        periodo = dados.get('periodo')
        if not periodo:
            return dados
        inicio, fim = dados.get('data_inicial'), dados.get('data_final')
        if not inicio:
            self.add_error('data_inicial', 'Informe uma data inicial válida.')
        if not fim:
            self.add_error('data_final', 'Informe uma data final válida.')
        if inicio and fim and inicio > fim:
            self.add_error('data_final', 'A data final deve ser igual ou posterior à inicial.')
        dados['inicio'], dados['fim'] = inicio, fim
        return dados
