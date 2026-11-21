"""Consultas gerenciais somente leitura, agregadas no PostgreSQL.

Não chama serviços de gravação, financeiro ou sincronização.
"""
from decimal import Decimal
from datetime import date, timedelta

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, F, Func, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Lower

from estoque.models import Venda
from estoque.graficos_relatorios import evolucao_diaria, barras_evolucao
from estoque.analise_evolucao import (analisar, analisar_historico, modo_padrao,
    periodos_historicos, referencia_historica, referencia_equivalente)
from estoque.forms_relatorios import BELEM
from django.utils import timezone


ZERO = Decimal('0.00')
CENTAVO = Decimal('0.01')
# Mesmos aliases reconhecidos pelos classificadores comerciais existentes.
PRAZO = ('aprazo', 'carteira', 'fiado')
VISTA = ('avista', 'dinheiro', 'especie', 'pix', 'banco', 'transferencia',
         'cartao', 'debito', 'credito', 'boleto', 'deposito')


def _grupo_pagamento(campo='tipo_pagamento'):
    # Normalização no banco: não transfere vendas individuais ao Python.
    # Traduz também maiúsculas antes de Lower: funciona inclusive em collation C.
    sem_acentos = Func(Coalesce(F(campo), Value('')),
                       Value('áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ'),
                       Value('aaaaaeeeeiiiiooooouuuucaaaaaeeeeiiiiooooouuuuc'), function='TRANSLATE', output_field=CharField())
    return Func(Lower(sem_acentos), Value(r'[\s_]'), Value(''), Value('g'),
                function='REGEXP_REPLACE', output_field=CharField())


def resumo_vendas(inicio, fim, pagina=1, *, modo=None, dia_semana=None, equivalente=False, hoje=None,
                 historico=None, comparacao='anterior'):
    hoje = hoje or timezone.localdate(timezone=BELEM)
    modo = modo or modo_padrao(inicio, fim, hoje)
    dia_semana = hoje.weekday() if dia_semana is None else dia_semana
    todas = (Venda.objects.filter(cancelada=False)
            .order_by().annotate(_forma=_grupo_pagamento()).annotate(
                grupo=Case(
                    When(_forma='consumoproprio', then=Value('consumo')),
                    When(_forma__in=PRAZO, then=Value('prazo')),
                    When(_forma__in=VISTA, then=Value('vista')),
                    default=Value('nao_classificado'), output_field=CharField())))
    base = todas.filter(data_venda__range=(inicio, fim))
    grupos = {nome: {'valor': ZERO, 'quantidade': 0, 'sem_valor': 0}
              for nome in ('consumo', 'prazo', 'vista', 'nao_classificado')}
    # No máximo quatro linhas, independentemente da quantidade de vendas.
    for linha in base.values('grupo').annotate(valor=Sum('total'), quantidade=Count('pk'),
                                              com_valor=Count('total')):
        grupos[linha['grupo']] = {
            'valor': linha['valor'] or ZERO, 'quantidade': linha['quantidade'],
            'sem_valor': linha['quantidade'] - linha['com_valor'],
        }
    comerciais = [grupos[nome] for nome in ('vista', 'prazo', 'nao_classificado')]
    total = sum((grupo['valor'] for grupo in comerciais), ZERO)
    quantidade = sum(grupo['quantidade'] for grupo in comerciais)
    sem_valor = sum(grupo['sem_valor'] for grupo in comerciais)
    diario = (base.exclude(_forma='consumoproprio').values('data_venda')
              .annotate(faturamento=Coalesce(Sum('total'), Value(ZERO)), quantidade=Count('pk'),
                        vista=Coalesce(Sum('total', filter=Q(_forma__in=VISTA)), Value(ZERO)),
                        prazo=Coalesce(Sum('total', filter=Q(_forma__in=PRAZO)), Value(ZERO)))
              .order_by('-data_venda'))
    # COUNT de dias e LIMIT/OFFSET; nunca pagina vendas individuais em Python.
    evolucao = Paginator(diario, 31).get_page(pagina)
    evolucao.object_list = list(evolucao.object_list)
    for dia in evolucao.object_list:
        dia['nao_classificado'] = dia['faturamento'] - dia['vista'] - dia['prazo']
    # No mês padrão (até 31 dias), a página já contém toda a série. Períodos
    # maiores usam apenas uma agregação adicional estreita, nunca vendas brutas.
    serie = evolucao.object_list
    if evolucao.paginator.num_pages > 1:
        serie = list(base.exclude(_forma='consumoproprio').values('data_venda')
                     .annotate(faturamento=Coalesce(Sum('total'), Value(ZERO))).order_by('data_venda'))
    referencia_dias = None
    if not historico and modo == 'semanal' and inicio <= hoje and inicio > date.min:
        anterior_inicio = date.fromordinal(max(1, inicio.toordinal()-7))
        referencia_dias = list(todas.exclude(_forma='consumoproprio').filter(
            data_venda__range=(anterior_inicio, inicio-timedelta(days=1))).values('data_venda')
            .annotate(faturamento=Coalesce(Sum('total'), Value(ZERO))).order_by('data_venda'))
    if historico or modo == 'mensal':
        a, b = historico or (inicio, fim)
        periodos = list(periodos_historicos(a, b, hoje, modo, dia_semana))
        referencias = [referencia_historica(x, y, parcial, modo, dia_semana, comparacao)
                      for x, y, parcial in periodos]
        consulta_inicio = min([a, *(r[0] for r in referencias if r)])
        inicio_mes = hoje.replace(day=1)
        if modo == 'mensal' and a <= inicio_mes <= min(b, hoje) and inicio_mes > date.min:
            consulta_inicio = min(consulta_inicio, (inicio_mes-timedelta(days=1)).replace(day=1))
        # One narrow daily aggregate for every bar and all references together.
        dias = list(todas.exclude(_forma='consumoproprio').filter(
            data_venda__range=(consulta_inicio, min(b, hoje))).values('data_venda').annotate(
                faturamento=Sum('total'), quantidade=Count('pk'),
                sem_valor=Count('pk')-Count('total')).order_by('data_venda'))
        # Earliest surviving document is an observed boundary, not a certified
        # system go-live date. It must not be inferred from a filtered series.
        primeiro = Venda.objects.order_by().aggregate(inicio=Min('data_venda'))['inicio'] if modo == 'mensal' else None
        analise = analisar_historico(dias, a, b, hoje, modo, dia_semana, comparacao, inicio_registros=primeiro)
        if not historico:
            analise['historico'] = False
    else:
        analise = analisar(serie, inicio, fim, hoje, modo, dia_semana, referencia_dias=referencia_dias)
    if not historico and modo == 'mensal' and equivalente:
        referencia, motivo = referencia_equivalente(inicio, fim, hoje)
        comparacao = {'motivo': motivo}
        if referencia:
            atual_a, atual_b, anterior_a, anterior_b = referencia
            item = next((i for i in analise['itens'] if i['inicio'] == atual_a.isoformat()), None)
            comp = item.get('comparacao') if item else None
            if comp:
                comparacao.update(atual=comp['atual'], anterior=comp['anterior'],
                    atual_inicio=atual_a, atual_fim=atual_b, anterior_inicio=anterior_a, anterior_fim=anterior_b,
                    diferenca=item['diferenca'], variacao=item['variacao'], inclui_hoje=comp['inclui_hoje'],
                    indicadores_atual=item.get('indicadores'), indicadores_anterior=item.get('indicadores_referencia'))
            else:
                comparacao['motivo'] = item['motivo'] if item else 'Períodos não equivalentes.'
        analise['equivalente'] = comparacao
    participacoes = []
    for chave, rotulo in (('vista', 'À vista'), ('prazo', 'A prazo'), ('nao_classificado', 'Não classificado')):
        grupo = grupos[chave]
        grupo['percentual'] = (grupo['valor']/total*100).quantize(CENTAVO) if total > ZERO else ZERO
        if chave != 'nao_classificado' or grupo['quantidade']:
            participacoes.append({'chave': chave, 'rotulo': rotulo, **grupo})
    return {
        'faturamento': total, 'quantidade': quantidade,
        'ticket_medio': (total / quantidade).quantize(CENTAVO) if quantidade else ZERO,
        'vista': grupos['vista'], 'prazo': grupos['prazo'], 'consumo': grupos['consumo'],
        'total_registros_consulta': total + grupos['consumo']['valor'],
        'nao_classificado': grupos['nao_classificado'], 'sem_valor': sem_valor,
        'evolucao': evolucao,
        'grafico_diario': evolucao_diaria(serie, inicio, fim, hoje), 'participacoes': participacoes,
        'analise_evolucao': analise,
        'grafico_evolucao': barras_evolucao(analise),
        'participacao_disponivel': total > ZERO and all(g['valor'] >= ZERO for g in comerciais),
    }
