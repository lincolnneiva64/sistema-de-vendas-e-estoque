"""Dados compactos de barras diárias; sem consultas ou bibliotecas gráficas."""
from datetime import date
from decimal import Decimal

from django.utils import timezone
from .forms_relatorios import BELEM

ZERO = Decimal('0.00')
MESES = ('Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
         'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro')


def barras_evolucao(analise):
    """Prepare visible bars without modifying the financial analysis or totals."""
    if analise.get('historico'):
        return analise
    if analise['modo'] == 'semanal' and len(analise['itens']) > 1:
        itens = analise['itens']
        inicio, fim = date.fromisoformat(itens[0]['inicio']), date.fromisoformat(itens[-1]['fim'])
        # A month overview starts at its first Monday when there is a complete
        # week to show. Keep short/custom partial periods and all financial data.
        if (inicio.day == 1 and inicio.weekday() != 0 and
                (inicio.year, inicio.month) == (fim.year, fim.month) and
                itens[0]['parcial'] and any(not i['parcial'] for i in itens[1:])):
            visiveis = itens[1:]
            return dict(analise, itens=visiveis, inicio_parcial_oculto=True,
                minimo=min(ZERO, *(i['valor'] for i in visiveis)),
                maximo=max(ZERO, *(i['valor'] for i in visiveis)))
    c = analise.get('equivalente')
    if analise['modo'] != 'mensal' or not c or c.get('motivo'):
        return analise

    def barra(inicio, fim, valor, referencia):
        return dict(inicio=inicio.isoformat(), fim=fim.isoformat(), valor=valor,
            rotulo=f'{MESES[inicio.month-1]} · {inicio:%d/%m/%Y} a {fim:%d/%m/%Y}',
            parcial=True, status='Parcial', referencia=referencia,
            variacao=None if referencia else c['variacao'],
            diferenca=None if referencia else c['diferenca'],
            motivo='Mês anterior como referência equivalente.' if referencia else
                'Percentual não calculável: faturamento anterior zero ou negativo.' if c['variacao'] is None else '')

    anterior = barra(c['anterior_inicio'], c['anterior_fim'], c['anterior'], True)
    atual = barra(c['atual_inicio'], c['atual_fim'], c['atual'], False)
    anterior['indicadores'] = c.get('indicadores_anterior')
    atual['indicadores'] = c.get('indicadores_atual')
    atual['comparacao'] = dict(tipo='parcial', rotulo='Parcial equivalente',
        inicio=atual['inicio'], fim=atual['fim'], atual=c['atual'], anterior=c['anterior'],
        anterior_inicio=anterior['inicio'], anterior_fim=anterior['fim'], inclui_hoje=c['inclui_hoje'])
    return dict(analise, itens=[anterior, atual], par_mensal=True, media=None,
        quantidade_completos=0, minimo=min(ZERO, c['anterior'], c['atual']),
        maximo=max(ZERO, c['anterior'], c['atual']))


def evolucao_diaria(dias, inicio, fim, hoje=None):
    valores = {dia['data_venda']: dia['faturamento'] for dia in dias}
    zero = Decimal('0')
    total = sum(valores.values(), zero)
    quantidade_dias = (fim-inicio).days + 1
    hoje = hoje or timezone.localdate(timezone=BELEM)
    return {
        'inicio': inicio.isoformat(), 'fim': fim.isoformat(), 'hoje': hoje.isoformat(),
        # Somente dias existentes no agregado. O navegador preenche zeros apenas
        # na janela visível, sem listas/DOM proporcionais a calendários enormes.
        'serie': [[dia.isoformat(), str(valor)] for dia, valor in sorted(valores.items())],
        'minimo': min([zero, *valores.values()]), 'maximo': max([zero, *valores.values()]),
        'dias_com_vendas': len(valores), 'dias_periodo': quantidade_dias, 'total': total,
        'media': (total/quantidade_dias).quantize(Decimal('0.01')),
        'inclui_hoje': inicio <= hoje <= fim,
    }
