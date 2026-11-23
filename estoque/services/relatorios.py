"""Consultas gerenciais somente leitura, agregadas no PostgreSQL.

Não chama serviços de gravação, financeiro ou sincronização.
"""
from decimal import Decimal
from datetime import date, timedelta

from django.core.paginator import Paginator
from django.db.models import Case, CharField, Count, F, Func, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Lower

from estoque.models import Venda, ItemVenda
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

    # CMV gerencial: custos congelados na venda, sem alterar estoque.
    itens = ItemVenda.objects.filter(
        venda__in=base.exclude(grupo='consumo').values('pk')
    )
    custo_ok = (
        Q(custo_total_snapshot__isnull=False)
        & Q(custo_total_snapshot__gte=ZERO)
        & Q(custo_unitario_snapshot__gt=ZERO)
        & Q(custo_origem_snapshot='preco_compra_atual')
        & Q(quantidade__gt=ZERO)
        & ~Q(custo_unidade_snapshot='')
    )
    custos = itens.aggregate(
        total_itens=Count('pk'),
        com_custo=Count('pk', filter=custo_ok),
        faturamento_itens=Sum('valor_total'),
        itens_sem_valor=Count('pk') - Count('valor_total'),
        custo_registrado=Sum('custo_total_snapshot', filter=custo_ok),
    )
    receita_itens = custos['faturamento_itens'] or ZERO
    cmv = custos['custo_registrado'] or ZERO
    divergencia = receita_itens - total
    completo = (
        custos['total_itens'] > 0
        and custos['total_itens'] == custos['com_custo']
        and custos['itens_sem_valor'] == 0
        and sem_valor == 0
        and divergencia == ZERO
    )

    # ESTIMATIVA EXCLUSIVA SETEMBRO 2026
    estimativa_setembro = None
    from datetime import date as _date
    from django.db.models import OuterRef as _OuterRef, Subquery as _Subquery
    from estoque.models import Compra as _Compra, ItemCompra as _ItemCompra

    if inicio <= _date(2026, 9, 30) and fim >= _date(2026, 9, 1):
        compras_ref = _ItemCompra.objects.filter(
            produto_id=_OuterRef("produto_id"),
            compra__status=_Compra.STATUS_FINALIZADA,
            compra__data_compra__gt=_OuterRef("venda__data_venda"),
            quantidade__gt=0,
            preco_unitario__gt=0,
        ).order_by("compra__data_compra", "pk")

        itens_ref = itens.filter(
            venda__data_venda__range=(
                _date(2026, 9, 1), _date(2026, 9, 30)
            )
        ).annotate(
            data_ref=_Subquery(compras_ref.values("compra__data_compra")[:1]),
            unidade_ref=_Subquery(compras_ref.values("unidade")[:1]),
            preco_ref=_Subquery(compras_ref.values("preco_unitario")[:1]),
            quantidade_ref=_Subquery(compras_ref.values("quantidade")[:1]),
            total_ref=_Subquery(compras_ref.values("valor_total")[:1]),
        ).select_related("produto", "venda")

        estimados = pendentes = registrados = 0
        custo_estimado = ZERO
        custo_historico = ZERO
        receita_coberta = ZERO
        receita_pendente = ZERO

        for item in itens_ref.iterator(chunk_size=500):
            receita = item.valor_total or ZERO
            custo = item.custo_total_snapshot

            historico_valido = (
                custo is not None
                and custo >= ZERO
                and item.custo_unitario_snapshot is not None
                and item.custo_unitario_snapshot > ZERO
                and item.custo_origem_snapshot == "preco_compra_atual"
                and item.quantidade > ZERO
                and bool(item.custo_unidade_snapshot)
            )

            if historico_valido:
                registrados += 1
                custo_historico += custo
                receita_coberta += receita
                continue

            valor = None
            data = item.data_ref
            preco = item.preco_ref
            quantidade_item = item.quantidade

            if (
                data is not None
                and preco is not None
                and preco > ZERO
                and quantidade_item is not None
                and quantidade_item > ZERO
                and 0 < (data - item.venda.data_venda).days <= 30
                and item.quantidade_ref is not None
                and item.quantidade_ref > ZERO
                and item.total_ref is not None
                and abs(item.quantidade_ref * preco - item.total_ref) <= CENTAVO
            ):
                unidade_venda = (item.unidade or "").strip().upper()
                unidade_compra = (item.unidade_ref or "").strip().upper()

                if unidade_venda and unidade_venda == unidade_compra:
                    valor = quantidade_item * preco
                elif item.produto:
                    produto = item.produto
                    base = (produto.unidade_venda_1 or "").strip().upper()
                    fracionada = (produto.unidade_venda_2 or "").strip().upper()
                    fator = produto.fator_conversao or ZERO

                    if (
                        produto.vende_fracionado
                        and unidade_compra == base
                        and unidade_venda == fracionada
                        and fator > ZERO
                    ):
                        valor = quantidade_item * preco / fator

            if valor is None:
                pendentes += 1
                receita_pendente += receita
            else:
                estimados += 1
                receita_coberta += receita
                custo_estimado += valor.quantize(CENTAVO)

        cmv_parcial = custo_historico + custo_estimado
        lucro_parcial = receita_coberta - cmv_parcial

        estimativa_setembro = {
            "registrados": registrados,
            "estimados": estimados,
            "pendentes": pendentes,
            "custo_historico": custo_historico,
            "custo_estimado": custo_estimado,
            "cmv_parcial": cmv_parcial,
            "receita_coberta": receita_coberta,
            "receita_pendente": receita_pendente,
            "lucro_parcial": lucro_parcial,
            "margem_parcial": (
                (lucro_parcial / receita_coberta * 100).quantize(CENTAVO)
                if receita_coberta > ZERO else None
            ),
        }

        # Combina os custos de outros meses, somente quando completos.
        outros = itens.exclude(
            venda__data_venda__range=(
                _date(2026, 9, 1), _date(2026, 9, 30)
            )
        )
        outros_resumo = outros.aggregate(
            total_itens=Count("pk"),
            com_custo=Count("pk", filter=custo_ok),
            custo=Sum("custo_total_snapshot", filter=custo_ok),
            receita=Sum("valor_total"),
            sem_valor=Count("pk") - Count("valor_total"),
        )

        outros_completos = (
            outros_resumo["total_itens"] == outros_resumo["com_custo"]
            and outros_resumo["sem_valor"] == 0
            and custos["itens_sem_valor"] == 0
            and sem_valor == 0
            and divergencia == ZERO
        )

        if outros_completos:
            custo_outros = outros_resumo["custo"] or ZERO
            receita_outros = outros_resumo["receita"] or ZERO

            estimativa_setembro["registrados"] += outros_resumo["com_custo"]
            estimativa_setembro["custo_historico"] += custo_outros
            estimativa_setembro["receita_coberta"] += receita_outros
            estimativa_setembro["cmv_parcial"] += custo_outros

            lucro_combinado = (
                estimativa_setembro["receita_coberta"]
                - estimativa_setembro["cmv_parcial"]
            )
            estimativa_setembro["lucro_parcial"] = lucro_combinado
            estimativa_setembro["margem_parcial"] = (
                (
                    lucro_combinado
                    / estimativa_setembro["receita_coberta"]
                    * 100
                ).quantize(CENTAVO)
                if estimativa_setembro["receita_coberta"] > ZERO
                else None
            )
            estimativa_setembro["periodo_misto"] = (
                outros_resumo["total_itens"] > 0
            )
        else:
            # Nao ocultar custos desconhecidos de outros meses.
            estimativa_setembro = None

    # EVOLUCAO_SEMANAL_MULTIPLAS_SEMANAS_20261123
    # Até 16 semanas para escolher; mais 1 para a referência anterior.
    # Nenhuma venda individual é carregada, apenas totais agregados por dia.
    semana_diaria = None
    if modo == 'semanal':
        intervalo_inicio, intervalo_fim = historico or (inicio, fim)
        ultima_data = min(intervalo_fim, hoje)
        if intervalo_inicio <= ultima_data:
            ultima_segunda = ultima_data - timedelta(days=ultima_data.weekday())
            semanas = [ultima_segunda - timedelta(weeks=k) for k in range(16)]
            semanas = sorted(s for s in semanas if s >= intervalo_inicio)
            if semanas:
                consulta_inicio = semanas[0] - timedelta(days=7)
                consulta_fim = min(hoje, semanas[-1] + timedelta(days=6))
                linhas = list(
                    todas.exclude(_forma='consumoproprio')
                    .filter(data_venda__range=(consulta_inicio, consulta_fim))
                    .values('data_venda')
                    .annotate(valor=Sum('total'), sem_total=Count('pk') - Count('total'))
                    .order_by('data_venda')
                )
                por_dia = {r['data_venda']: r for r in linhas}
                series = []
                for segunda in [semanas[0] - timedelta(days=7), *semanas]:
                    acumulado = ZERO
                    documentacao_valida = True
                    pontos = []
                    for indice in range(7):
                        dia = segunda + timedelta(days=indice)
                        linha = por_dia.get(dia)
                        if dia > hoje:
                            valor = None
                        elif linha and not linha['sem_total'] and documentacao_valida:
                            acumulado += linha['valor'] or ZERO
                            valor = acumulado
                        elif dia == hoje and not linha and documentacao_valida:
                            # Hoje está aberto: zero apenas dos registros até agora.
                            valor = acumulado
                        else:
                            # Não presumir zero em dia encerrado sem documentação.
                            documentacao_valida = False
                            valor = None
                        pontos.append({
                            'dia': dia.isoformat(),
                            'valor': str(valor) if valor is not None else None,
                            'em_andamento': dia == hoje,
                        })
                    series.append({
                        'inicio': segunda.isoformat(),
                        'fim': (segunda + timedelta(days=6)).isoformat(),
                        'parcial': segunda <= hoje <= segunda + timedelta(days=6),
                        'pontos': pontos,
                    })
                # Preferir duas semanas encerradas ao abrir o painel.
                encerradas = [s for s in semanas if s + timedelta(days=6) < hoje]
                selecionada = (encerradas[-1] if encerradas else semanas[-1])
                semana_diaria = {
                    'semanas': series,
                    'selecionada': selecionada.isoformat(),
                    'quantidade_inicial': 2,
                    'hoje': hoje.isoformat(),
                }
                analise['semana_atual'] = {
                    'inicio': (hoje - timedelta(days=hoje.weekday())).isoformat(),
                    'fim': (hoje - timedelta(days=hoje.weekday()) + timedelta(days=6)).isoformat(),
                    'dias_transcorridos': hoje.weekday() + 1,
                }

    rentabilidade = {
        'disponivel': completo,
        'cmv': cmv,
        'lucro': total - cmv if completo else None,
        'margem': ((total - cmv) / total * 100).quantize(CENTAVO)
                  if completo and total > ZERO else None,
        'quantidade': custos['total_itens'],
        'com_custo': custos['com_custo'],
        'divergencia': divergencia,
        'estimativa_setembro': estimativa_setembro,
    }

    return {
        'faturamento': total, 'quantidade': quantidade,
        'rentabilidade': rentabilidade,
        'semana_diaria': semana_diaria,
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
