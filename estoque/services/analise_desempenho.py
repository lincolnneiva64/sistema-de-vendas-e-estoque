"""Narrativa comercial de leitura, independente dos controles do gráfico."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum

from estoque.analise_evolucao import analisar_historico, referencia_historica, variacao
from .relatorios_produtos import vendas_comerciais


def analisar_desempenho(dias, inicio, fim, hoje, comparacao='anterior'):
    fim_encerrado = min(fim, hoje - timedelta(days=1))
    resultado = dict(inicio=inicio, fim=fim_encerrado, inclui_hoje=inicio <= hoje <= fim,
                     motivo='', comparavel=False)
    if inicio > fim_encerrado:
        resultado['motivo'] = 'Sem dias encerrados no intervalo selecionado.'
        return resultado
    if (inicio.year, inicio.month) != (fim_encerrado.year, fim_encerrado.month):
        resultado['motivo'] = 'O intervalo abrange mais de um mês. A análise mensal desta entrega requer um recorte dentro de um único mês.'
        return resultado
    item = analisar_historico(dias, inicio, fim_encerrado, hoje, 'mensal',
                             comparacao=comparacao)['itens'][0]
    ref = referencia_historica(inicio, fim_encerrado, item['parcial'], 'mensal', -1, comparacao)
    resultado.update(atual=item['valor'], motivo=item['motivo'])
    if ref:
        resultado.update(referencia_inicio=ref[0], referencia_fim=ref[1])
        referencia = analisar_historico(dias, ref[0], ref[1], hoje, 'mensal')['itens'][0]
        resultado['anterior'] = referencia['valor']
    if not item['comparacao']:
        return resultado
    atual = item['indicadores']
    anterior = item['indicadores_referencia']
    medidas = []
    for chave, rotulo in (('quantidade', 'Quantidade de vendas'), ('ticket_medio', 'Ticket médio')):
        a, b = atual[chave], anterior[chave]
        medidas.append(dict(chave=chave, rotulo=rotulo, atual=a, anterior=b,
                            diferenca=a-b, percentual=variacao(Decimal(a), Decimal(b)),
                            direcao='aumentou' if a > b else 'caiu' if a < b else 'permaneceu igual'))
    resultado.update(comparavel=True, diferenca=item['diferenca'], percentual=item['variacao'],
                     medidas=medidas)
    return resultado


def desempenho_comercial(inicio, fim, hoje, comparacao='anterior'):
    fim_encerrado = min(fim, hoje - timedelta(days=1))
    if inicio > fim_encerrado or (inicio.year, inicio.month) != (fim_encerrado.year, fim_encerrado.month):
        return analisar_desempenho([], inicio, fim, hoje, comparacao)
    from calendar import monthrange
    parcial = inicio.day != 1 or fim_encerrado.day != monthrange(inicio.year, inicio.month)[1]
    referencia = referencia_historica(inicio, fim_encerrado, parcial, 'mensal', -1, comparacao)
    consulta_inicio = min(inicio, referencia[0]) if referencia else inicio
    dias = list(vendas_comerciais().filter(data_venda__range=(consulta_inicio, fim_encerrado))
                .values('data_venda').annotate(faturamento=Sum('total'), quantidade=Count('pk'),
                    sem_valor=Count('pk')-Count('total')).order_by('data_venda'))
    return analisar_desempenho(dias, inicio, fim, hoje, comparacao)
