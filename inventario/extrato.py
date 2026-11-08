"""One database-paginated chronology of documentary and observer evidence.

Balances require independent evidence. Sources are never summed twice or matched
by approximate time, and current product conversions are never used.
"""
import re
from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import (BooleanField, CharField,
                              Case, DecimalField, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When)
from django.db.models.functions import Cast, Concat, TruncDate, Trim, Upper
from django.urls import reverse
from estoque.documentos_horarios import FUSO_DOCUMENTOS
from estoque.models import ItemVenda, ItemVendaRemovido, MovimentacaoEstoqueManual

from .rastreabilidade import ORIGENS
from .saldos_historicos import analisar
from .saldo_documental import aplicar_saldos_documentais, unidade as unidade_documental, quantidade_historica
from .models import AplicacaoAjuste, DetalheMovimentoEstoque, MovimentoObservado
from .extrato_apresentacao import (identificar_vendas, tecnicos_das_vendas,
    associar_documentos, evidencias_documentais, apresentar_operacoes, edicoes_do_produto, preparar_fluxos_historicos)


COLUNAS = ('pk', 'dia', 'instante', 'fonte', 'documento', 'quantidade', 'unidade',
           'pessoa', 'cancelada', 'origem', 'antes', 'depois', 'referencia', 'devolvido', 'ordem', 'cliente_nome', 'retorno_efetivo',
           'referencia_antes', 'referencia_baixa', 'referencia_depois', 'referencia_unidade', 'referencia_habilitada', 'independente')


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



def apresentar_ajustes_manuais(pagina, produto_id):
    """Display persisted manual corrections without changing balance evidence."""
    linhas = [r for r in pagina if r['fonte'] == 'manual' and r['origem'] == 'ajuste']
    if not linhas:
        return
    ids = [r['documento'] for r in linhas]
    documentos = {m.pk: m for m in MovimentacaoEstoqueManual.objects.filter(
        pk__in=ids, produto_id=produto_id, tipo='ajuste')}
    detalhes = {}
    for d in DetalheMovimentoEstoque.objects.filter(
            documento_tipo='movimentacao_estoque_manual', documento_id__in=[str(i) for i in ids],
            acao='ajuste', movimento__produto_id=produto_id,
            movimento__origem='ajuste_manual').select_related('movimento'):
        detalhes.setdefault(d.documento_id, []).append(d)
    for r in linhas:
        m = documentos.get(r['documento'])
        dados = dict(responsavel=r['pessoa'], instante=r['instante'], antes=r['antes'],
                     depois=r['depois'], motivo=m.motivo if m else '', unidade='',
                     diferenca=(r['depois'] - r['antes'])
                     if r['antes'] is not None and r['depois'] is not None else None)
        candidatos = detalhes.get(str(r['documento']), [])
        if len(candidatos) == 1 and m:
            d = candidatos[0]
            mov = d.movimento
            if (mov.referencia == f'Movimentação manual #{m.pk}'
                    and mov.depois == m.estoque_depois == r['depois']
                    and m.estoque_antes == r['antes']
                    and (mov.antes == m.estoque_antes
                         or mov.antes is None and m.estoque_antes == 0)):
                dados.update(antes=mov.antes, responsavel=d.operador_nome or m.operador_nome,
                             diferenca=d.quantidade_movimentada)
                unidade = unidade_documental(d.unidade_base)
                if (unidade and quantidade_historica(d, d.quantidade_entrada,
                        d.unidade_entrada) == mov.depois):
                    dados.update(unidade=unidade, diferenca=d.quantidade_movimentada)
        r['ajuste_manual'] = dados


def cronologia(compras, vendas, removidos, movimentos, params, selecionada, inicio, fim, produto_id=None, rodada=None, _consulta=False):
    produto_id = produto_id or (selecionada.produto_id if selecionada else None)
    timeline = params.get('visual') != 'a'
    decimal = DecimalField(max_digits=12, decimal_places=3)

    def periodo(campo):
        limites = {campo + '__lt': fim}
        if _consulta is not True:
            limites[campo + '__gte'] = inicio
        return limites

    def normalizar(qs, fonte, dia, instante, **campos):
        defaults = dict(documento=Value(None, IntegerField()), quantidade=Value(None, decimal),
            unidade=Value('', CharField()), pessoa=Value('', CharField()),
            cliente_nome=Value('', CharField()), retorno_efetivo=Value(False, BooleanField()),
            referencia_antes=Cast(Value(None), decimal), referencia_baixa=Cast(Value(None), decimal),
            referencia_depois=Cast(Value(None), decimal), referencia_unidade=Value('', CharField()),
            referencia_habilitada=Value(False, BooleanField()), independente=Value(False, BooleanField()),
            cancelada=Value(False, BooleanField()), origem=Value('', CharField()),
            antes=Cast(Value(None), decimal), depois=Cast(Value(None), decimal),
            referencia=Value('', CharField()), devolvido=Value(False, BooleanField()), ordem=F('pk'))
        defaults.update(campos)
        return qs.order_by().annotate(dia=dia, instante=instante,
            fonte=Value(fonte, CharField()), **defaults).values(*COLUNAS)

    recebidas = compras.filter(**periodo('compra__estoque_entrada_realizada_em')) if inicio else compras.none()
    # Alias names must not conflict with concrete model fields (unidade etc.).
    # Project all sources through values() first, so annotate can reuse these names.
    compras_q = normalizar(recebidas.values('pk'), 'compra',
        TruncDate('compra__estoque_entrada_realizada_em', tzinfo=FUSO_DOCUMENTOS), F('compra__estoque_entrada_realizada_em'),
        documento=F('compra_id'), quantidade=F('quantidade'), unidade=F('unidade'),
        pessoa=F('compra__fornecedor__nome'), cancelada=F('compra__cancelada'))
    # Only the starting reference needs a recorded, coherent snapshot. Subsequent
    # documentary arithmetic does not require certification of each sale.
    edicoes_referencia = edicoes_do_produto(OuterRef('produto__nome'),
        venda_id=OuterRef('venda_id'))
    divergencias_referencia = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        referencia=Concat(Value('Venda #'), Cast(OuterRef('venda_id'), CharField())),
        origem__in=('venda', 'consumo_proprio', 'devolucao_venda')).exclude(
            antes=OuterRef('estoque_antes'), depois=OuterRef('estoque_depois'))
    vendas_q = normalizar(vendas.values('pk'), 'venda', TruncDate('venda__criado_em', tzinfo=FUSO_DOCUMENTOS),
        F('venda__criado_em'), documento=F('venda_id'),
        quantidade=F('quantidade'), unidade=F('unidade'), pessoa=F('venda__operador'),
        cliente_nome=F('venda__cliente__nome'),
        referencia_antes=F('estoque_antes'), referencia_baixa=F('estoque_movimentado'),
        referencia_depois=F('estoque_depois'), referencia_unidade=F('estoque_unidade_snapshot'),
        referencia_habilitada=~Exists(edicoes_referencia) & ~Exists(divergencias_referencia),
        cancelada=F('venda__cancelada'), origem=F('venda__tipo_pagamento'),
        devolvido=F('venda__estoque_devolvido_cancelamento'))
    devolvidos_removidos = ItemVendaRemovido.objects.filter(
        produto_id=produto_id, status='removido', estoque_devolvido=True,
        **periodo('estoque_devolvido_em')) if inicio else ItemVendaRemovido.objects.none()
    removidos_q = normalizar(removidos.exclude(pk__in=devolvidos_removidos.values('pk')).values('pk'),
        'remocao', TruncDate('criado_em', tzinfo=FUSO_DOCUMENTOS), F('criado_em'),
        documento=F('venda_id'), quantidade=F('quantidade_snapshot'), unidade=F('unidade_snapshot'),
        pessoa=F('operador'), origem=F('status'), devolvido=F('estoque_devolvido'),
        cliente_nome=F('venda__cliente__nome'))
    devolvidos_removidos_q = normalizar(devolvidos_removidos.values('pk'), 'remocao',
        TruncDate('estoque_devolvido_em', tzinfo=FUSO_DOCUMENTOS), F('estoque_devolvido_em'),
        documento=F('venda_id'), quantidade=F('quantidade_snapshot'), unidade=F('unidade_snapshot'),
        pessoa=F('operador'), origem=F('status'), devolvido=F('estoque_devolvido'),
        cliente_nome=F('venda__cliente__nome'), retorno_efetivo=Value(True, BooleanField()))
    manuais = MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id,
        **periodo('criado_em')) if produto_id and inicio else MovimentacaoEstoqueManual.objects.none()
    # Deduplicate only a proven document identity and identical raw before/after.
    # Never merge by approximate time, quantity, product name or current unit.
    correspondente = manuais.filter(produto_id=OuterRef('produto_id'),
        estoque_antes=OuterRef('antes'), estoque_depois=OuterRef('depois')).annotate(
        ref=Concat(Value('Movimentação manual #'), Cast('pk', CharField()))).filter(ref=OuterRef('referencia'))
    movimentos = movimentos.annotate(manual_documentado=Exists(correspondente)).exclude(
        origem='ajuste_manual', manual_documentado=True)
    # Evidence may be recorded on another page or after the document's interval.
    # Association uses the same product's documents up to the upper boundary;
    # the visible UNION still contains only the requested documents.
    compras_vinculaveis = compras.model.objects.filter(produto_id=produto_id,
        compra__estoque_entrada_realizada=True, compra__estoque_entrada_realizada_em__lt=fim) if inicio else compras.none()
    vendas_vinculaveis = vendas.model.objects.filter(produto_id=produto_id,
        venda__criado_em__lt=fim) if inicio else vendas.none()
    comerciais = associar_documentos(movimentos, compras_vinculaveis, vendas_vinculaveis)
    movimentos = comerciais
    tecnicos = None
    regularizacoes_q = None
    if timeline:
        tecnicos = identificar_vendas(movimentos, vendas)
        movimentos = tecnicos  # Association below is the only operational grouping rule.
        # Only a persisted application with the actual manual-movement FK proves
        # an applied regularization. Neither a title nor an observation is proof.
        aplicacao = AplicacaoAjuste.objects.filter(
            decisao__movimentacao_id=OuterRef('pk'),
            decisao__item__produto_id=OuterRef('produto_id'),
            proposta__rodada_id=F('decisao__rodada_id'),
            proposta__rodada__item_id=F('decisao__item_id'))
        todos_manuais = MovimentacaoEstoqueManual.objects.filter(produto_id=produto_id).annotate(
            aplicado_em=Subquery(aplicacao.values('aplicado_em')[:1]))
        regulares = todos_manuais.filter(**periodo('aplicado_em')) if inicio else todos_manuais.none()
        regularizacoes_q = normalizar(regulares.values('pk'), 'regularizacao',
            TruncDate('aplicado_em', tzinfo=FUSO_DOCUMENTOS), F('aplicado_em'), documento=F('pk'),
            antes=F('estoque_antes'), depois=F('estoque_depois'),
            referencia_depois=Subquery(aplicacao.values('depois')[:1]),
            referencia_unidade=Subquery(aplicacao.values('unidade')[:1]))
        referencia_regular = regulares.filter(estoque_antes=OuterRef('antes'),
            estoque_depois=OuterRef('depois')).annotate(
                ref=Concat(Value('Movimentação manual #'), Cast('pk', CharField()))).filter(
                    ref=OuterRef('referencia'))
        movimentos = movimentos.annotate(regular_documentado=Exists(referencia_regular)).exclude(
            origem='ajuste_manual', regular_documentado=True)
        manuais = manuais.annotate(aplicado_em=Subquery(aplicacao.values('aplicado_em')[:1])).filter(aplicado_em__isnull=True)
    retorno_documentado = devolvidos_removidos.annotate(
        documento_texto=Cast('pk', CharField()), tipo_texto=Value('item_venda_removido', CharField())
    ).filter(produto_id=OuterRef('produto_id'),
        documento_texto=OuterRef('detalhe__documento_id'),
        tipo_texto=OuterRef('detalhe__documento_tipo'))
    movimentos = movimentos.annotate(retorno_documentado=Exists(retorno_documentado)).exclude(
        origem='devolucao_venda', retorno_documentado=True)
    # Exclude only the technical side of an unambiguous document pair in SQL,
    # before pagination. Its original record is fetched into the document details.
    movimentos = movimentos.exclude(
        Q(operacao_ambigua=False, origem='compra', item_compra_operacional__isnull=False) |
        Q(operacao_ambigua=False, origem__in=('venda', 'consumo_proprio', 'devolucao_venda'), item_venda_operacional__isnull=False))
    movimentos = movimentos.annotate(base_operacional=Upper(Trim('detalhe__unidade_base'))).annotate(operacao_independente=Case(When(
        (Q(origem='compra', item_compra_operacional__isnull=False, detalhe__acao='entrada', depois__gt=F('antes')) |
         Q(origem__in=('venda', 'consumo_proprio'), item_venda_operacional__isnull=False,
           detalhe__acao__in=('saida', 'edicao'), depois__lt=F('antes')) |
         Q(origem='devolucao_venda', item_venda_operacional__isnull=False,
           detalhe__acao__in=('devolucao', 'cancelamento', 'edicao'), depois__gt=F('antes'))) &
        Q(detalhe__documento_tipo__in=('compra', 'venda')) &
        ~Q(base_operacional__in=('', 'NÃO INFORMADA', 'NAO INFORMADA')),
        then=Value(True)), default=Value(False), output_field=BooleanField()))
    movimentos_q = normalizar(movimentos.values('pk'), 'digital', TruncDate('registrado_em', tzinfo=FUSO_DOCUMENTOS), F('registrado_em'),
        origem=F('origem'), antes=F('antes'), depois=F('depois'), referencia=F('referencia'), ordem=F('sequencia'),
        independente=F('operacao_independente'))
    manuais_q = normalizar(manuais.values('pk'), 'manual', TruncDate('criado_em', tzinfo=FUSO_DOCUMENTOS), F('criado_em'),
        documento=F('pk'), antes=F('estoque_antes'), depois=F('estoque_depois'),
        origem=F('tipo'), pessoa=F('operador_nome'),
        referencia_depois=F('estoque_depois'),
        referencia=Concat('motivo', Value(' · '), 'observacao', output_field=CharField()))
    # One return per sale/product, irrespective of its original business date.
    # Its current items do not prove the quantity/unit physically returned.
    devolucoes = ItemVenda.objects.none()
    sem_data = False
    if produto_id and inicio:
        todos = ItemVenda.objects.filter(produto_id=produto_id)
        primeiro = todos.filter(venda_id=OuterRef('venda_id')).order_by('pk').values('pk')[:1]
        devolucoes = todos.filter(pk=Subquery(primeiro),
            **periodo('venda__estoque_devolvido_cancelamento_em'))
        sem_data = todos.filter(venda__estoque_devolvido_cancelamento=True,
            venda__estoque_devolvido_cancelamento_em__isnull=True).exists()
    devolucoes_q = normalizar(devolucoes.values('pk'), 'cancelamento',
        TruncDate('venda__estoque_devolvido_cancelamento_em', tzinfo=FUSO_DOCUMENTOS), F('venda__estoque_devolvido_cancelamento_em'),
        documento=F('venda_id'), devolvido=F('venda__estoque_devolvido_cancelamento'),
        cliente_nome=F('venda__cliente__nome'))
    fontes = [vendas_q, removidos_q, devolvidos_removidos_q, devolucoes_q, manuais_q]
    if regularizacoes_q is not None:
        fontes.append(regularizacoes_q)
    if params.get('visual') != 'a' and rodada and rodada.ok_em and inicio and inicio < fim:
        # Include exactly this historical OK, even outside the selected interval.
        # UNION keeps chronological pagination in SQL, without loading all events.
        conferencia_q = normalizar(type(rodada).objects.filter(pk=rodada.pk).values('pk'),
            'conferencia', TruncDate('ok_em', tzinfo=FUSO_DOCUMENTOS), F('ok_em'))
        fontes.append(conferencia_q)
    unificada = compras_q.union(*fontes, movimentos_q, all=True).order_by(
        'dia', 'instante', 'fonte', 'ordem', 'pk')
    if _consulta:
        return unificada
    fontes_operacionais = [
        vendas_q.filter(Q(venda__cancelada=False) | Q(estoque_movimentado__gt=0,
            estoque_depois=F('estoque_antes') - F('estoque_movimentado'))),
        devolvidos_removidos_q, devolucoes_q, manuais_q.filter(origem='ajuste')]
    if regularizacoes_q is not None:
        fontes_operacionais.append(regularizacoes_q)
    if params.get('visual') != 'a' and rodada and rodada.ok_em and inicio and inicio < fim:
        fontes_operacionais.append(conferencia_q)
    operacional = compras_q.union(*fontes_operacionais, movimentos_q.filter(operacao_independente=True), all=True).order_by(
        'dia', 'instante', 'fonte', 'ordem', 'pk')
    pagina = Paginator(operacional, 50).get_page(params.get('extrato_pagina'))
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
    apresentar_ajustes_manuais(pagina, produto_id)
    pagina.saldo_inicial = evidencia_inicial(pagina, inicio)
    apresentar_operacoes(pagina, evidencias_documentais(comerciais, pagina))
    def preparar(linhas):
        apresentar_ajustes_manuais(linhas, produto_id)
        preparar_fluxos_historicos(linhas, produto_id)

    def consulta(de, ate, prefixo=False):
        # The same raw UNION is retained for balance continuity. Hiding evidence
        # must never turn an unknown operation into an assumed zero movement.
        vendas_consulta = vendas.model.objects.filter(produto_id=produto_id, venda__criado_em__lt=ate)
        removidos_consulta = removidos.model.objects.filter(produto_id=produto_id, criado_em__lt=ate)
        movimentos_consulta = movimentos.model.objects.filter(produto_id=produto_id, registrado_em__lt=ate)
        if not prefixo:
            vendas_consulta = vendas_consulta.filter(venda__criado_em__gte=de)
            removidos_consulta = removidos_consulta.filter(criado_em__gte=de)
            movimentos_consulta = movimentos_consulta.filter(registrado_em__gte=de)
        return cronologia(
            compras.model.objects.filter(produto_id=produto_id,
                compra__estoque_entrada_realizada=True, compra__estoque_entrada_realizada_em__lt=ate),
            vendas_consulta, removidos_consulta, movimentos_consulta,
            params, None, de, ate, produto_id=produto_id, _consulta=True if prefixo else 'intervalo')

    def chave(r):
        return (r['dia'], r['instante'], r['fonte'], r['ordem'], r['pk'])

    linhas = list(pagina)
    if linhas:
        primeira, ultima = chave(linhas[0]), chave(linhas[-1])
        visiveis = {chave(r): r for r in linhas}
        def anteriores_pagina():
            anteriores = consulta(inicio, linhas[0]['instante'] + timedelta(microseconds=1), prefixo=True)
            for r in anteriores.reverse().iterator(chunk_size=100):
                if chave(r) < primeira:
                    yield r
        def registros_pagina():
            trecho = consulta(linhas[0]['instante'], linhas[-1]['instante'] + timedelta(microseconds=1))
            for r in trecho.iterator(chunk_size=100):
                k = chave(r)
                if primeira <= k <= ultima:
                    yield visiveis.get(k, r)
        preparar_fluxos_historicos(pagina, produto_id)
        aplicar_saldos_documentais(pagina, unificada, preparar_linhas=preparar,
            anteriores_pagina=anteriores_pagina, registros_pagina=registros_pagina)
    for sentido, numero in (('anterior', pagina.number - 1), ('proxima', pagina.number + 1)):
        consulta = params.copy()
        consulta['extrato_pagina'] = numero
        if selecionada:
            consulta['compra'] = selecionada.compra_id
        setattr(pagina, sentido + '_url', '?' + consulta.urlencode())
    return pagina
