"""One database-paginated chronology of documentary and observer evidence.

Balances require independent evidence. Sources are never summed twice or matched
by approximate time, and current product conversions are never used.
"""
import re

from django.core.paginator import Paginator
from django.db.models import (BooleanField, CharField,
                              DecimalField, Exists, F, IntegerField, OuterRef, Subquery, Value)
from django.db.models.functions import Cast, Concat, TruncDate
from django.urls import reverse
from estoque.documentos_horarios import FUSO_DOCUMENTOS
from estoque.models import ItemVenda, MovimentacaoEstoqueManual

from .rastreabilidade import ORIGENS
from .saldos_historicos import analisar
from .models import AplicacaoAjuste
from .extrato_apresentacao import identificar_vendas, tecnicos_das_vendas


COLUNAS = ('pk', 'dia', 'instante', 'fonte', 'documento', 'quantidade', 'unidade',
           'pessoa', 'cancelada', 'origem', 'antes', 'depois', 'referencia', 'devolvido', 'ordem')


def evidencia_inicial(pagina, inicio):
    """Present an already validated before-balance at the exact opening instant.

    Never subtract a receipt from a later balance or carry an anchor across a
    gap. A later document, ambiguous instant or another page cannot open a period.
    """
    if pagina.number != 1 or not inicio:
        return None
    linhas = list(pagina)
    if not linhas:
        return None
    r = linhas[0]
    tipos = {'compra': 'Reconstruído e corroborado', 'venda': 'Snapshot registrado'}
    if (r['instante'] != inicio or sum(l['instante'] == inicio for l in linhas) != 1
            or r.get('saldo_tipo') != tipos.get(r['fonte']) or r.get('saldo_tipo') is None
            or r.get('saldo') is None or r.get('saldo_antes') is None
            or r['saldo_antes'] < 0 or not r.get('saldo_unidade')):
        return None
    return dict(valor=r['saldo_antes'], unidade=r['saldo_unidade'], tipo=r['saldo_tipo'],
        origem=r.get('saldo_evidencia', ''), aviso=r.get('saldo_aviso', ''),
        documento=r['titulo'], url=r['url'])


def cronologia(compras, vendas, removidos, movimentos, params, selecionada, inicio, fim, produto_id=None, rodada=None):
    produto_id = produto_id or (selecionada.produto_id if selecionada else None)
    timeline = params.get('visual') != 'a'
    decimal = DecimalField(max_digits=12, decimal_places=3)

    def normalizar(qs, fonte, dia, instante, **campos):
        defaults = dict(documento=Value(None, IntegerField()), quantidade=Value(None, decimal),
            unidade=Value('', CharField()), pessoa=Value('', CharField()),
            cancelada=Value(False, BooleanField()), origem=Value('', CharField()),
            antes=Cast(Value(None), decimal), depois=Cast(Value(None), decimal),
            referencia=Value('', CharField()), devolvido=Value(False, BooleanField()), ordem=F('pk'))
        defaults.update(campos)
        return qs.order_by().annotate(dia=dia, instante=instante,
            fonte=Value(fonte, CharField()), **defaults).values(*COLUNAS)

    recebidas = compras.filter(compra__estoque_entrada_realizada_em__gte=inicio,
        compra__estoque_entrada_realizada_em__lt=fim) if inicio else compras.none()
    # Alias names must not conflict with concrete model fields (unidade etc.).
    # Project all sources through values() first, so annotate can reuse these names.
    compras_q = normalizar(recebidas.values('pk'), 'compra',
        TruncDate('compra__estoque_entrada_realizada_em', tzinfo=FUSO_DOCUMENTOS), F('compra__estoque_entrada_realizada_em'),
        documento=F('compra_id'), quantidade=F('quantidade'), unidade=F('unidade'),
        pessoa=F('compra__fornecedor__nome'), cancelada=F('compra__cancelada'))
    vendas_q = normalizar(vendas.values('pk'), 'venda', TruncDate('venda__criado_em', tzinfo=FUSO_DOCUMENTOS),
        F('venda__criado_em'), documento=F('venda_id'),
        quantidade=F('quantidade'), unidade=F('unidade'), pessoa=F('venda__operador'),
        cancelada=F('venda__cancelada'), origem=F('venda__tipo_pagamento'),
        devolvido=F('venda__estoque_devolvido_cancelamento'))
    removidos_q = normalizar(removidos.values('pk'), 'remocao', TruncDate('criado_em', tzinfo=FUSO_DOCUMENTOS), F('criado_em'),
        documento=F('venda_id'), quantidade=F('quantidade_snapshot'), unidade=F('unidade_snapshot'),
        pessoa=F('operador'), origem=F('status'), devolvido=F('estoque_devolvido'))
    manuais = MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id,
        criado_em__gte=inicio, criado_em__lt=fim) if produto_id and inicio else MovimentacaoEstoqueManual.objects.none()
    # Deduplicate only a proven document identity and identical raw before/after.
    # Never merge by approximate time, quantity, product name or current unit.
    correspondente = manuais.filter(produto_id=OuterRef('produto_id'),
        estoque_antes=OuterRef('antes'), estoque_depois=OuterRef('depois')).annotate(
        ref=Concat(Value('Movimentação manual #'), Cast('pk', CharField()))).filter(ref=OuterRef('referencia'))
    movimentos = movimentos.annotate(manual_documentado=Exists(correspondente)).exclude(
        origem='ajuste_manual', manual_documentado=True)
    tecnicos = None
    regularizacoes_q = None
    if timeline:
        tecnicos = identificar_vendas(movimentos, vendas)
        movimentos = tecnicos.exclude(origem='venda', outro_movimento=False,
            venda_com_edicao=False, item_venda_documentado__isnull=False)
        # Only a persisted application with the actual manual-movement FK proves
        # an applied regularization. Neither a title nor an observation is proof.
        aplicacao = AplicacaoAjuste.objects.filter(
            decisao__movimentacao_id=OuterRef('pk'),
            decisao__item__produto_id=OuterRef('produto_id'),
            proposta__rodada_id=F('decisao__rodada_id'),
            proposta__rodada__item_id=F('decisao__item_id'))
        todos_manuais = MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id).annotate(
            aplicado_em=Subquery(aplicacao.values('aplicado_em')[:1]))
        regulares = todos_manuais.filter(aplicado_em__gte=inicio, aplicado_em__lt=fim) if inicio else todos_manuais.none()
        regularizacoes_q = normalizar(regulares.values('pk'), 'regularizacao',
            TruncDate('aplicado_em', tzinfo=FUSO_DOCUMENTOS), F('aplicado_em'), documento=F('pk'),
            antes=F('estoque_antes'), depois=F('estoque_depois'))
        referencia_regular = regulares.filter(estoque_antes=OuterRef('antes'),
            estoque_depois=OuterRef('depois')).annotate(
                ref=Concat(Value('Movimentação manual #'), Cast('pk', CharField()))).filter(
                    ref=OuterRef('referencia'))
        movimentos = movimentos.annotate(regular_documentado=Exists(referencia_regular)).exclude(
            origem='ajuste_manual', regular_documentado=True)
        manuais = manuais.annotate(aplicado_em=Subquery(aplicacao.values('aplicado_em')[:1])).filter(aplicado_em__isnull=True)
    movimentos_q = normalizar(movimentos.values('pk'), 'digital', TruncDate('registrado_em', tzinfo=FUSO_DOCUMENTOS), F('registrado_em'),
        origem=F('origem'), antes=F('antes'), depois=F('depois'), referencia=F('referencia'), ordem=F('sequencia'))
    manuais_q = normalizar(manuais.values('pk'), 'manual', TruncDate('criado_em', tzinfo=FUSO_DOCUMENTOS), F('criado_em'),
        documento=F('pk'), antes=F('estoque_antes'), depois=F('estoque_depois'),
        origem=F('tipo'), pessoa=F('operador_nome'),
        referencia=Concat('motivo', Value(' · '), 'observacao', output_field=CharField()))
    # One return per sale/product, irrespective of its original business date.
    # Its current items do not prove the quantity/unit physically returned.
    devolucoes = ItemVenda.objects.none()
    sem_data = False
    if produto_id and inicio:
        todos = ItemVenda.objects.filter(produto_id=produto_id)
        primeiro = todos.filter(venda_id=OuterRef('venda_id')).order_by('pk').values('pk')[:1]
        devolucoes = todos.filter(pk=Subquery(primeiro),
            venda__estoque_devolvido_cancelamento_em__gte=inicio,
            venda__estoque_devolvido_cancelamento_em__lt=fim)
        sem_data = todos.filter(venda__estoque_devolvido_cancelamento=True,
            venda__estoque_devolvido_cancelamento_em__isnull=True).exists()
    devolucoes_q = normalizar(devolucoes.values('pk'), 'cancelamento',
        TruncDate('venda__estoque_devolvido_cancelamento_em', tzinfo=FUSO_DOCUMENTOS), F('venda__estoque_devolvido_cancelamento_em'),
        documento=F('venda_id'), devolvido=F('venda__estoque_devolvido_cancelamento'))
    fontes = [vendas_q, removidos_q, movimentos_q, devolucoes_q, manuais_q]
    if regularizacoes_q is not None:
        fontes.append(regularizacoes_q)
    if params.get('visual') != 'a' and rodada and rodada.ok_em and inicio and inicio < fim:
        # Include exactly this historical OK, even outside the selected interval.
        # UNION keeps chronological pagination in SQL, without loading all events.
        conferencia_q = normalizar(type(rodada).objects.filter(pk=rodada.pk).values('pk'),
            'conferencia', TruncDate('ok_em', tzinfo=FUSO_DOCUMENTOS), F('ok_em'))
        fontes.append(conferencia_q)
    unificada = compras_q.union(*fontes, all=True).order_by(
        'dia', 'instante', 'fonte', 'ordem', 'pk')
    pagina = Paginator(unificada, 50).get_page(params.get('extrato_pagina'))
    pagina.devolucao_sem_data = sem_data
    tecnicos_por_item = tecnicos_das_vendas(tecnicos, pagina) if timeline else {}
    registros_aplicados = [l['pk'] for l in pagina if l['fonte'] == 'regularizacao']
    aplicadas = {a.decisao.movimentacao_id: a for a in AplicacaoAjuste.objects.filter(
        decisao__movimentacao_id__in=registros_aplicados).select_related(
            'decisao__movimentacao', 'decisao__item')}
    for linha in pagina:
        fonte = linha['fonte']
        linha.update(entrada=None, saida=None, saldo=None, url='', classe='pendente', fonte_texto='Documento atual')
        if fonte == 'compra':
            linha.update(titulo=f"Compra #{linha['documento']}", classe='compra', entrada=linha['quantidade'],
                nota='Entrada documentada · Fornecedor: ' + (linha['pessoa'] or 'Não disponível'),
                url=reverse('estoque:compras_detalhe', args=[linha['documento']]))
        elif fonte == 'conferencia':
            linha.update(titulo='Conferência física — OK', classe='conferencia')
        elif fonte == 'venda':
            tipo = 'Consumo próprio' if linha['origem'] == 'consumo_proprio' else 'Venda'
            linha.update(titulo=f"{tipo} #{linha['documento']}", classe='venda', saida=linha['quantidade'],
                nota='Quantidade documental · Responsável: ' + (linha['pessoa'] or 'Não disponível'),
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
            linha['registros_tecnicos'] = tecnicos_por_item.get(linha['pk'], [])
        elif fonte == 'regularizacao':
            aplicada = aplicadas[linha['pk']]
            conferencia_id = aplicada.decisao.item.inventario_id
            linha.update(titulo=f'Regularização — Conferência nº {conferencia_id}', classe='manual',
                fonte_texto='Regularização de conferência efetivamente aplicada',
                nota=f'Ajuste manual #{linha["pk"]} · Aplicação {aplicada.pk} · Lote {aplicada.lote}',
                saldo=aplicada.depois, saldo_antes=aplicada.antes, saldo_unidade=aplicada.unidade,
                saldo_tipo='Saldo registrado na aplicação',
                saldo_evidencia=f'Aplicação vinculada à conferência. Registro manual original #{linha["pk"]}: {linha["antes"]} → {linha["depois"]}.',
                saldo_aviso='Saldo histórico da aplicação; não representa o saldo atual.',
                diferenca_aplicada=aplicada.diferenca, unidade=aplicada.unidade,
                url=reverse('inventario:movimento_manual',
                    args=[conferencia_id, aplicada.decisao.rodada_id, linha['pk']]),
                movimentacao_em=aplicada.decisao.movimentacao.criado_em, aplicacao_registrada=aplicada)
        elif fonte == 'manual':
            linha.update(titulo=f"Ajuste manual #{linha['documento']}" if linha['origem']=='ajuste' else f"Conferência manual #{linha['documento']}",
                classe='manual', fonte_texto='Registro manual documentado',
                nota='Unidade histórica não gravada. Evidência bruta; não convertida nem somada ao observador.',
                saldo_evidencia=f"Registro #{linha['documento']}: {linha['antes']} → {linha['depois']} (unidade histórica não comprovada). {linha['referencia']}",
                saldo_aviso='Este registro interrompe a reconstrução. Observações digitais com referência exata e mesmos saldos são representadas pelo documento, sem duplicação.',
                saldo_pontual=True, saldo_continuidade='Sequência interrompida')
            if rodada:
                linha['url'] = reverse('inventario:movimento_manual',args=[rodada.item.inventario_id,rodada.pk,linha['pk']])
        elif fonte == 'cancelamento':
            linha.update(titulo=f"Devolução por cancelamento · Venda #{linha['documento']}",
                nota='Data efetiva registrada da devolução. Quantidade e unidade devolvidas não comprovadas; não somada novamente aos registros digitais.',
                saldo_aviso='Sequência interrompida: não há snapshot imutável da quantidade e unidade devolvidas.',
                saldo_continuidade='Sequência interrompida', saldo_pontual=True,
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
        elif fonte == 'remocao':
            linha.update(titulo=f"Remoção/devolução · Venda #{linha['documento']}",
                nota='Estado atual: ' + linha['origem'] + (' · Estoque devolvido' if linha['devolvido'] else ' · Sem devolução de estoque confirmada'),
                url=reverse('estoque:venda_detalhe', args=[linha['documento']]))
        else:
            linha.update(titulo=ORIGENS.get(linha['origem'], ORIGENS['nao_identificada']),
                fonte_texto='Registro digital', nota=linha['referencia'] or 'Origem sem documento identificado',
                classe='compra' if linha['origem'] == 'compra' else 'venda' if linha['origem'] == 'venda' else 'pendente')
            # Only explicit document references can create links; no fuzzy matching.
            referencia = re.fullmatch(r'(Compra|Venda) #(\d+)', linha['referencia'])
            if referencia:
                destino = 'compras_detalhe' if referencia[1] == 'Compra' else 'venda_detalhe'
                linha['url'] = reverse('estoque:' + destino, args=[int(referencia[2])])
        if linha['cancelada']:
            linha['nota'] += ' · Cancelada atualmente'
        linha['detalhe_id'] = f"registro-{fonte}-{linha['pk']}"
    if produto_id:
        analisar(produto_id, pagina, fim)
    if timeline and rodada:
        from .saldos_apresentacao import apresentar
        apresentar(pagina, rodada)
    if produto_id:
        from .extrato_edicoes import apresentar_edicoes
        apresentar_edicoes(pagina, produto_id)
        from .extrato_unidades import apresentar_unidades
        apresentar_unidades(pagina, produto_id)
    pagina.saldo_inicial = evidencia_inicial(pagina, inicio)
    for sentido, numero in (('anterior', pagina.number - 1), ('proxima', pagina.number + 1)):
        consulta = params.copy()
        consulta['extrato_pagina'] = numero
        if selecionada:
            consulta['compra'] = selecionada.compra_id
        setattr(pagina, sentido + '_url', '?' + consulta.urlencode())
    return pagina
