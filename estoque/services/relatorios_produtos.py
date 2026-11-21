"""Commercial item aggregates only. Never converts units or writes stock."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, F, Min, Q, Sum
from django.db.models.functions import Lower, Trim, Upper

from estoque.models import ItemVenda, Venda
from estoque.analise_evolucao import analisar_historico, periodos_historicos, referencia_historica
from estoque.graficos_relatorios import barras_evolucao
from .relatorios import _grupo_pagamento, ZERO, CENTAVO
from .relatorios_produtos_indicadores import indicadores_produto


def vendas_comerciais():
    return (Venda.objects.filter(cancelada=False).order_by()
            .annotate(_forma=_grupo_pagamento()).exclude(_forma='consumoproprio'))


def itens_comerciais():
    return (ItemVenda.objects.filter(venda__in=vendas_comerciais()).order_by()
            .annotate(unidade_comercial=Upper(Trim('unidade'))))


def ranking_produtos(inicio, fim, *, pesquisa='', produto_id=None, ordem='faturamento', pagina=1):
    vendas = vendas_comerciais().filter(data_venda__range=(inicio, fim))
    itens = itens_comerciais().filter(venda__data_venda__range=(inicio, fim))
    notas = vendas.aggregate(faturamento_notas=Sum('total'), sem_total=Count('pk') - Count('total'))
    totais = itens.aggregate(total=Sum('valor_total'),
        identificados=Sum('valor_total', filter=Q(produto__isnull=False)),
        nao_identificados=Sum('valor_total', filter=Q(produto__isnull=True)))
    comercial, total_itens = notas['faturamento_notas'] or ZERO, totais['total'] or ZERO
    if produto_id is not None:
        itens = itens.filter(produto_id=produto_id)
    elif pesquisa:
        itens = itens.filter(produto__nome__icontains=pesquisa)
    linhas = itens.values('produto_id', 'produto__nome', 'unidade_comercial').annotate(
        quantidade=Sum('quantidade'), faturamento=Sum('valor_total'), vendas=Count('venda_id', distinct=True))
    ordenar = {'faturamento': ('-faturamento',), 'vendas': ('-vendas',),
               'quantidade': ('unidade_comercial', '-quantidade'),
               'nome_az': (Lower('produto__nome').asc(),),
               'nome_za': (Lower('produto__nome').desc(),)}
    linhas = linhas.order_by(*ordenar.get(ordem, ordenar['faturamento']), 'produto__nome', 'produto_id', 'unidade_comercial')
    resultado = Paginator(linhas, 25).get_page(pagina)
    resultado.object_list = list(resultado.object_list)
    for linha in resultado:
        linha['participacao'] = (linha['faturamento'] / comercial * 100).quantize(CENTAVO) if comercial > 0 else None
        linha['preco_medio'] = ((linha['faturamento'] / linha['quantidade']).quantize(CENTAVO)
            if linha['unidade_comercial'] and linha['quantidade'] > 0 else None)
        # Missing historical units cannot certify a comparable quantity.
        if not linha['unidade_comercial']:
            linha['quantidade'] = None
    return dict(linhas=resultado, comercial=comercial, total_itens=total_itens,
        identificados=totais['identificados'] or ZERO,
        nao_identificados=totais['nao_identificados'] or ZERO,
        diferenca=comercial-total_itens, sem_total=notas['sem_total'])


def unidades_produto(produto_id):
    return list(itens_comerciais().filter(produto_id=produto_id).exclude(unidade_comercial='')
                .values_list('unidade_comercial', flat=True).order_by('unidade_comercial').distinct())


def documentos_produto_dia(produto_id, dia, *, unidade='', pagina=1):
    """Read on expansion only; one row per sale/unit, with distinct-sale totals.

    Quantity and revenue remain the recorded operands. The effective price is
    weighted when several current product lines share a sale and unit.
    Footer totals cover the entire day, independently of pagination.
    """
    base = itens_comerciais().filter(produto_id=produto_id, venda__data_venda=dia)
    if unidade:
        base = base.filter(unidade_comercial=unidade)
    linhas = base.values('venda_id', 'venda__cliente_id', 'venda__cliente__nome', 'unidade_comercial').annotate(
        quantidade=Sum('quantidade'), valor_total=Sum('valor_total'), itens=Count('pk'))
    documentos = Paginator(linhas.order_by('-venda_id', 'unidade_comercial'), 25).get_page(pagina)
    documentos.object_list = list(documentos.object_list)
    for linha in documentos:
        linha['preco_efetivo'] = ((linha['valor_total']/linha['quantidade']).quantize(CENTAVO)
            if linha['unidade_comercial'] and linha['quantidade'] > 0 else None)
    totais = base.aggregate(faturamento=Sum('valor_total'), vendas=Count('venda_id', distinct=True))
    quantidades = list(base.values('unidade_comercial').annotate(quantidade=Sum('quantidade'))
                      .order_by('unidade_comercial'))
    return dict(documentos=documentos, quantidades=quantidades,
                faturamento=totais['faturamento'] or ZERO, vendas=totais['vendas'])


def historico_produto(produto_id, inicio, fim, *, hoje, modo='diario', dia_semana=-1,
                      historico=None, comparacao='anterior', medida='faturamento', unidade='', pagina=1):
    base = itens_comerciais().filter(produto_id=produto_id)
    # Same commercial item quantities and normalized units as the ranking.
    # The initial card follows the selected period, independently of the chart.
    indicadores = indicadores_produto(base.filter(venda__data_venda__range=(inicio, fim)),
        unidade_recorte=unidade if medida == 'quantidade' else None)
    if medida == 'quantidade':
        if not unidade:
            raise ValueError('Escolha a unidade comercial para comparar quantidades.')
        base = base.filter(unidade_comercial=unidade)
    a, b = historico or (inicio, fim)
    periodos = list(periodos_historicos(a, b, hoje, modo, dia_semana))
    referencias = [referencia_historica(x, y, parcial, modo, dia_semana, comparacao)
                  for x, y, parcial in periodos]
    consulta_inicio = min([a, *(r[0] for r in referencias if r)])
    # The shared forecast also compares against the entire previous month.
    if modo == 'mensal' and a <= hoje.replace(day=1) <= min(b, hoje) and hoje.replace(day=1) > date.min:
        consulta_inicio = min(consulta_inicio, (hoje.replace(day=1)-timedelta(days=1)).replace(day=1))
    valor = (Sum('valor_total') if medida == 'faturamento' else Sum('quantidade')
             if medida == 'quantidade' else Count('venda_id', distinct=True))
    diario = (base.annotate(data_venda=F('venda__data_venda')).values('data_venda')
              .annotate(faturamento=valor, quantidade=Count('venda_id', distinct=True),
                        sem_valor=Count('pk')-Count('valor_total')).order_by('data_venda'))
    dias = list(diario.filter(venda__data_venda__range=(consulta_inicio, min(b, hoje))))
    # Commercial activity certifies a day independently of this product/unit.
    # Absence of a product line on such a day is zero, not a missing archive.
    cobertura = list(vendas_comerciais().filter(data_venda__range=(consulta_inicio, min(b, hoje)))
        .values('data_venda').annotate(sem_total=Count('pk')-Count('total')).order_by('data_venda'))
    dias_cobertos = [d['data_venda'] for d in cobertura if not d['sem_total']]
    primeiro = vendas_comerciais().aggregate(inicio=Min('data_venda'))['inicio']
    precisao = Decimal('.001') if medida == 'quantidade' else CENTAVO
    analise = analisar_historico(dias, a, b, hoje, modo, dia_semana, comparacao,
                                inicio_registros=primeiro, precisao=precisao, dias_cobertos=dias_cobertos,
                                rotulo_valor={'faturamento': 'faturamento', 'quantidade': 'total em ' + unidade,
                                              'vendas': 'número de vendas'}[medida])
    analise.update(cobertura_comercial=True, medida=medida, unidade=unidade if medida == 'quantidade' else 'vendas' if medida == 'vendas' else '',
                   casas=3 if medida == 'quantidade' else 0 if medida == 'vendas' else 2)
    evolucao = Paginator(diario.filter(venda__data_venda__range=(inicio, fim)).order_by('-data_venda'), 31).get_page(pagina)
    evolucao.object_list = list(evolucao.object_list)
    totais = base.filter(venda__data_venda__range=(inicio, fim)).aggregate(
        faturamento=Sum('valor_total'), vendas=Count('venda_id', distinct=True))
    return dict(analise_evolucao=analise, grafico_evolucao=barras_evolucao(analise),
                quantidades_vendidas=indicadores['quantidades_vendidas'], indicadores_produto=indicadores,
                evolucao=evolucao, documentos=[],  # Compatibility; notes now load only on date expansion.
                faturamento=totais['faturamento'] or ZERO, quantidade=totais['vendas'])
