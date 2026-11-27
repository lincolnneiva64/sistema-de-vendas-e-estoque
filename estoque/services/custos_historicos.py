"""Read-only batched evidence and commercial reconciliation of frozen costs."""
from bisect import bisect_right
from collections import defaultdict
from fractions import Fraction
from decimal import Decimal
import re

from django.db.models import Exists, OuterRef, Count, Subquery

from estoque.models import ItemVenda, ItemVendaRemovido, EventoVenda
from inventario.extrato_apresentacao import identificar_vendas
from inventario.extrato_edicoes import QUANTIDADE
from inventario.models import (ConfirmacaoContagem, DetalheMovimentoEstoque, MovimentoObservado,
    ItemInventario, EventoInventario)
from inventario.saldo_documental import numero, unidade, projecao_operacional

INCLUSAO = re.compile(r'^Item adicionado na nota: (.+), quantidade (\d+(?:\.\d+)?) (\S+), '
    r'preco unitario R\$ (\d+(?:\.\d+)?)(?:, valor acrescentado R\$ (\d+(?:\.\d+)?))?\.$')
REMOCAO = re.compile(r'^Item removido da nota: (.+), quantidade (\d+(?:\.\d+)?) (\S+)'
    r'(?:, valor abatido R\$ (\d+(?:\.\d+)?))?\.$')


def _inclusao_compativel(evento,item):
    match=INCLUSAO.fullmatch(evento['descricao']) if evento['tipo_evento']=='item_adicionado_na_nota' else None
    return bool(match and match[1]==item['produto__nome'] and numero(match[2])==item['quantidade']
        and unidade(match[3])==unidade(item['unidade']) and numero(match[4])==item['preco_unitario']
        and (numero(match[5]) if match[5] is not None else
            (numero(match[2])*numero(match[4])).quantize(Decimal('.01')))==item['valor_total'])


def _eventos_documentados(ids):
    """Corroborate documented additions and complete same-unit edit chains.

    Stock/document identity is already established independently. Audit prose
    only corroborates operands; ambiguous names/additions fail. Quantity edits
    still have to reproduce the very same stock projection with a frozen factor.
    """
    if not ids:
        return {}
    homonimos = ItemVenda.objects.filter(venda_id=OuterRef('venda_id'),
        produto__nome=OuterRef('produto__nome')).exclude(produto_id=OuterRef('produto_id'))
    duplicados = ItemVenda.objects.filter(venda_id=OuterRef('venda_id'),
        produto_id=OuterRef('produto_id')).exclude(pk=OuterRef('pk'))
    removidos = ItemVendaRemovido.objects.filter(venda_id=OuterRef('venda_id'),produto_id=OuterRef('produto_id'))
    itens = list(ItemVenda.objects.filter(pk__in=ids).annotate(nome_ambiguo=Exists(homonimos),
        item_ambiguo=Exists(duplicados),remocao=Exists(removidos)).values(
        'pk','produto_id','venda_id','produto__nome','quantidade','unidade','preco_unitario','valor_total',
        'nome_ambiguo','item_ambiguo','remocao'))
    arquivos=defaultdict(list)
    if any(i['remocao'] for i in itens):
        for ar in ItemVendaRemovido.objects.filter(venda_id__in={i['venda_id'] for i in itens},
                produto_id__in={i['produto_id'] for i in itens}).values(
                'pk','venda_id','produto_id','item_venda_original_id','quantidade_snapshot','unidade_snapshot',
                'valor_total_snapshot','estoque_antes','estoque_depois','estoque_movimentado',
                'estoque_unidade_snapshot','estoque_devolvido_em','status','estoque_devolvido'):
            arquivos[(ar['venda_id'],ar['produto_id'])].append(ar)
    eventos = defaultdict(list)
    for e in EventoVenda.objects.filter(venda_id__in={i['venda_id'] for i in itens},
            tipo_evento__in=('quantidade_item_alterada','item_adicionado_na_nota','item_removido_da_nota')).values(
            'pk','venda_id','tipo_evento','descricao','criado_em'):
        eventos[e['venda_id']].append(e)
    validas = {}
    for i in itens:
        nome = i['produto__nome']
        relacionados = [e for e in eventos[i['venda_id']] if any(e['descricao'].startswith(prefix+nome+suffix)
            for prefix,suffix in (('Quantidade alterada na nota: ','. De '),
                ('Item adicionado na nota: ',', quantidade '),('Item removido da nota: ',', quantidade ')))]
        relacionados.sort(key=lambda e:(e['criado_em'],e['pk']))
        if i['nome_ambiguo'] or i['item_ambiguo'] or not relacionados:
            continue
        if i['remocao']:
            ars=arquivos[(i['venda_id'],i['produto_id'])]
            if (len(ars)!=1 or len(relacionados)!=2 or not ars[0]['item_venda_original_id']
                    or ars[0]['item_venda_original_id']==i['pk'] or ars[0]['status']!='removido'
                    or not ars[0]['estoque_devolvido']):
                continue
            old=ars[0];rem,add=relacionados
            match=REMOCAO.fullmatch(rem['descricao']) if rem['tipo_evento']=='item_removido_da_nota' else None
            if (match and match[1]==nome and numero(match[2])==old['quantidade_snapshot']
                    and unidade(match[3])==unidade(old['unidade_snapshot'])
                    and (match[4] is None or numero(match[4])==old['valor_total_snapshot'])
                    and _inclusao_compativel(add,i)):
                validas[i['pk']]=dict(pk=add['pk'],criado_em=add['criado_em'],arquivo=old,
                    remocao_em=rem['criado_em'],evento_remocao_id=rem['pk'])
            continue
        if all(e['tipo_evento']=='quantidade_item_alterada' for e in relacionados):
            ajustes = []
            for e in relacionados:
                match = QUANTIDADE.match(e['descricao'])
                if (not match or match[1]!=nome or unidade(match[3])!=unidade(i['unidade'])
                        or unidade(match[5])!=unidade(i['unidade'])
                        or numero(match[2])<=0 or numero(match[4])<=0
                        or (ajustes and ajustes[-1]['depois']!=numero(match[2]))):
                    break
                ajustes.append(dict(evento_id=e['pk'],criado_em=e['criado_em'],
                    antes=numero(match[2]),depois=numero(match[4])))
            if len(ajustes)==len(relacionados) and ajustes[-1]['depois']==i['quantidade']:
                validas[i['pk']] = dict(pk=None,criado_em=relacionados[0]['criado_em'],ajustes=ajustes)
            continue
        if len(relacionados) != 1:
            continue
        e = relacionados[0]
        if _inclusao_compativel(e,i):
            validas[i['pk']] = e
    return validas


def _selecoes_documentadas(produto_ids):
    """Inventory creation freezes all selections before emitting `criado`.

    Selection changes only exclude/reinclude existing items; they never replace
    the preserved conversion. Require a unique closing creation event and the
    original item count, rather than using the parent's creation time alone.
    """
    eventos = EventoInventario.objects.filter(inventario_id=OuterRef('inventario_id'),acao='criado')
    contagem = ItemInventario.objects.filter(inventario_id=OuterRef('inventario_id')).values(
        'inventario_id').annotate(n=Count('pk')).values('n')
    qs = ItemInventario.objects.filter(produto_id__in=produto_ids).annotate(
        evento_id=Subquery(eventos.order_by('pk').values('pk')[:1]),
        evento_em=Subquery(eventos.order_by('pk').values('criado_em')[:1]),
        evento_dados=Subquery(eventos.order_by('pk').values('dados')[:1]),
        eventos_n=Subquery(eventos.values('inventario_id').annotate(n=Count('pk')).values('n')),
        total_itens=Subquery(contagem)).values('pk','produto_id','cadastro','inventario__criado_em',
            'evento_id','evento_em','evento_dados','eventos_n','total_itens')
    resultado = defaultdict(list)
    for item in qs:
        dados = item['evento_dados']
        if (item['eventos_n'] != 1 or not isinstance(dados,dict)
                or isinstance(dados.get('quantidade'),bool)
                or dados.get('quantidade') != item['total_itens']
                or item['evento_em'] is None or item['evento_em'] < item['inventario__criado_em']):
            continue
        resultado[item['produto_id']].append(dict(pk=None,ok_em=item['evento_em'],
            sequencia_ok=None,rodada__item_id=item['pk'],rodada__item__cadastro=item['cadastro'],
            evento_criacao_id=item['evento_id']))
    return resultado


def _provar_sequencia_editada(item, movimentos, historico, auditoria):
    """Reconcile each documented edit with the existing stock rounding rule.

    The item's current before-balance may have been rebased after another sale.
    It is not substituted for the original observer balance. Every observer
    delta is checked independently, and only the current frozen cost is counted.
    """
    ajustes = auditoria.get('ajustes',[]) if auditoria else []
    arquivo = auditoria.get('arquivo') if auditoria else None
    if (not ajustes and not arquivo) or len(movimentos)<2:
        return None
    ms = sorted(movimentos,key=lambda m:(m.sequencia,m.pk))
    if any(hasattr(m,'detalhe') for m in ms):
        return None
    pos = bisect_right([c['ok_em'] for c in historico],ms[0].registrado_em)
    if not pos:
        return None
    c = historico[pos-1];cadastro=c['rodada__item__cadastro']
    base,secundaria = unidade(cadastro.get('base')),unidade(cadastro.get('secundaria'))
    fator=numero(cadastro.get('fator'))
    signature=(base,secundaria,fator,cadastro.get('fracionado'))
    if (not base or fator is None or fator<=0 or cadastro.get('fracionado') is not True
            or base==secundaria or base!=unidade(item['custo_unidade_snapshot'])
            or base!=unidade(item['estoque_unidade_snapshot']) or secundaria!=item['unidade_comercial']
            or (c['sequencia_ok'] is not None and c['sequencia_ok']>=ms[0].sequencia)):
        return None
    witnesses=[n for n in historico if c['ok_em']<=n['ok_em']<=ms[-1].registrado_em]
    after=[n for n in historico if n['ok_em']>ms[-1].registrado_em]
    for n in witnesses+after[:1]:
        data=n['rodada__item__cadastro']
        if (unidade(data.get('base')),unidade(data.get('secundaria')),
                numero(data.get('fator')),data.get('fracionado'))!=signature:
            return None
    if arquivo:
        if len(ms)!=3:
            return None
        original,retorno,atual=ms
        old_unit=unidade(arquivo['unidade_snapshot'])
        old_q=arquivo['quantidade_snapshot']
        old_projection=(old_q if old_unit==base else
            projecao_operacional(old_q,'divisao',fator) if old_unit==secundaria else None)
        new_projection=projecao_operacional(item['quantidade'],'divisao',fator)
        if (old_projection is None or old_projection<=0 or new_projection is None or new_projection<=0
                or unidade(arquivo['estoque_unidade_snapshot'])!=base
                or arquivo['estoque_movimentado']!=old_projection
                or original.origem!='venda' or retorno.origem!='devolucao_venda' or atual.origem!='venda'
                or original.antes!=arquivo['estoque_antes'] or original.depois!=arquivo['estoque_depois']
                or original.antes is None or original.depois is None
                or original.antes-original.depois!=old_projection
                or retorno.antes!=original.depois or retorno.depois!=original.antes
                or atual.antes!=retorno.depois or atual.antes!=item['estoque_antes']
                or atual.depois!=item['estoque_depois'] or atual.antes-atual.depois!=new_projection
                or item['estoque_movimentado']!=new_projection
                or arquivo['estoque_devolvido_em'] is None
                or not (original.registrado_em<=retorno.registrado_em<=arquivo['estoque_devolvido_em']
                    <=auditoria['remocao_em']<=atual.registrado_em<=auditoria['criado_em'])):
            return None
        exato=Fraction(item['quantidade'])/Fraction(fator)
        return dict(legado=True,unidade_base=base,quantidade_base=exato,
            rastreabilidade=dict(criterio='remocao_compensada_e_reinclusao_historica_v1',
                item_id=item['pk'],venda_id=item['venda_id'],movimento_id=atual.pk,
                movimento_ids=[m.pk for m in ms],evento_ids=[auditoria['evento_remocao_id'],auditoria['pk']],
                arquivo_remocao_id=arquivo['pk'],item_original_id=arquivo['item_venda_original_id'],
                confirmacao_id=c['pk'],evento_criacao_inventario_id=c.get('evento_criacao_id'),
                item_inventario_id=c['rodada__item_id'],documentado_em=c['ok_em'],fator=str(fator),
                unidade_venda=secundaria,unidade_custo=base,conversao='divisao',
                quantidade_base_numerador=exato.numerator,quantidade_base_denominador=exato.denominator))
    operations=[(ajustes[0]['antes'],None)]
    operations.extend((a['depois']-a['antes'],a['criado_em']) for a in ajustes
        if projecao_operacional(a['depois']-a['antes'],'divisao',fator)!=0)
    if len(operations)!=len(ms) or ajustes[-1]['depois']!=item['quantidade']:
        return None
    for index,(m,(q,event_time)) in enumerate(zip(ms,operations)):
        projection=projecao_operacional(q,'divisao',fator)
        if (projection is None or projection==0 or m.antes is None or m.depois is None
                or m.depois-m.antes!=-projection
                or m.origem!=('venda' if q>0 else 'devolucao_venda')
                or (index and m.registrado_em<ms[index-1].registrado_em)
                or (event_time and (event_time<m.registrado_em
                    or (index+1<len(ms) and event_time>ms[index+1].registrado_em)))):
            return None
    baixa=numero(item.get('estoque_movimentado'))
    if (baixa is None or baixa<=0 or baixa!=projecao_operacional(item['quantidade'],'divisao',fator)
            or item['estoque_antes'] is None or item['estoque_depois']!=ms[-1].depois
            or item['estoque_antes']-item['estoque_depois']!=baixa):
        return None
    exato=Fraction(item['quantidade'])/Fraction(fator)
    return dict(legado=True,unidade_base=base,quantidade_base=exato,
        rastreabilidade=dict(criterio='sequencia_edicao_e_conversao_historica_v1',
            item_id=item['pk'],venda_id=item['venda_id'],movimento_id=ms[-1].pk,
            movimento_ids=[m.pk for m in ms],evento_ids=[a['evento_id'] for a in ajustes],
            confirmacao_id=c['pk'],evento_criacao_inventario_id=c.get('evento_criacao_id'),
            item_inventario_id=c['rodada__item_id'],documentado_em=c['ok_em'],fator=str(fator),
            unidade_venda=secundaria,unidade_custo=base,conversao='divisao',
            quantidade_base_numerador=exato.numerator,quantidade_base_denominador=exato.denominator))


def conciliar_custos_comerciais(itens, provas, motivos):
    """Reconcile recorded money; never infer a cost from today's purchase price.

    The current commercial factor is a candidate, corroborated independently by
    the frozen sale operands, its recorded debit and an identified earlier
    finalized purchase at the frozen unit cost. This is managerial evidence,
    explicitly distinct from an immutable stock conversion attribution.
    Contradictory historical records and invalid explicit attributions retain
    their existing decision; they cannot fall through to this reconciliation.
    """
    from estoque.models import Produto, ItemCompra, Compra
    from .relatorios_produtos_indicadores import dinheiro_exato
    allowed = ('Há custo congelado, mas falta fator em documentação histórica anterior à venda.',
               'Venda com múltiplos movimentos; o custo do item exige conciliação documental.')
    candidates = [i for i in itens if i['pk'] not in provas and motivos.get(i['pk']) in allowed
                  and i.get('venda__data_venda') is not None]
    if not candidates:
        return {}
    ids = {i['produto_id'] for i in candidates}
    products = {p.pk:p for p in Produto.objects.filter(pk__in=ids).only(
        'unidade_compra','unidade_venda_2','fator_conversao','vende_fracionado')}
    purchases = defaultdict(list)
    for purchase in ItemCompra.objects.filter(produto_id__in=ids,
            compra__status=Compra.STATUS_FINALIZADA,compra__cancelada=False).order_by('compra__data_compra','pk').values(
            'pk','produto_id','compra_id','compra__data_compra','unidade','preco_unitario'):
        purchases[purchase['produto_id']].append(purchase)
    result = {}
    for item in candidates:
        p = products.get(item['produto_id'])
        base = unidade(item['custo_unidade_snapshot'])
        q, cost, total, debit = (numero(item.get(k)) for k in
            ('quantidade','custo_unitario_snapshot','custo_total_snapshot','estoque_movimentado'))
        if (p is None or not p.vende_fracionado or not p.fator_conversao or p.fator_conversao <= 0
                or base != unidade(p.unidade_compra) or base != unidade(item['estoque_unidade_snapshot'])
                or item['unidade_comercial'] != unidade(p.unidade_venda_2)
                or q is None or q <= 0 or cost is None or cost <= 0 or total is None
                or item['estoque_antes'] is None or item['estoque_depois'] is None):
            continue
        exact = Fraction(q) / Fraction(p.fator_conversao)
        if (debit != projecao_operacional(q,'divisao',p.fator_conversao)
                or item['estoque_antes']-item['estoque_depois'] != debit
                or dinheiro_exato(exact*Fraction(cost)) != total):
            continue
        matches = [c for c in purchases[item['produto_id']]
            if c['compra__data_compra'] <= item['venda__data_venda']
            and unidade(c['unidade']) == base and c['preco_unitario'] == cost]
        if not matches:
            continue
        purchase = matches[-1]
        result[item['pk']] = dict(legado=True, gerencial=True, unidade_base=base,
            quantidade_base=exact, rastreabilidade=dict(
                criterio='snapshot_compra_e_conversao_comercial_conciliados_v1',
                item_id=item['pk'],venda_id=item['venda_id'],compra_id=purchase['compra_id'],
                item_compra_id=purchase['pk'],data_compra=purchase['compra__data_compra'],
                fator=str(p.fator_conversao),unidade_venda=item['unidade_comercial'],unidade_custo=base,
                quantidade_base_numerador=exact.numerator,quantidade_base_denominador=exact.denominator,
                fator_origem='cadastro_comercial_corroborado_por_snapshot_e_compra',
                prova_fisica=False))
    return result


def conciliar_totais_documentados(itens, motivos):
    """Recover recorded money for an unchanged, uniquely documented addition.

    A previous non-fractional selection does not contradict a later commercial
    presentation. This decision certifies only the frozen total, never a factor
    or an exact physical quantity. Explicit historical conversions still block.
    """
    from .relatorios_produtos_indicadores import dinheiro_exato
    candidates = [i for i in itens if motivos.get(i['pk']) ==
        'Unidades ou fator histórico contraditórios ou incompatíveis com a venda.']
    if not candidates:
        return {}
    inclusoes = _eventos_documentados([i['pk'] for i in candidates])
    selections = defaultdict(list)
    for s in ItemInventario.objects.filter(produto_id__in={i['produto_id'] for i in candidates}).values(
            'pk','produto_id','cadastro','custo_base','inventario__criado_em'):
        selections[s['produto_id']].append(s)
    movements = defaultdict(list)
    for m in MovimentoObservado.objects.filter(produto_id__in={i['produto_id'] for i in candidates},
            referencia__in={f"Venda #{i['venda_id']}" for i in candidates}).select_related('detalhe'):
        movements[(m.produto_id,m.referencia)].append(m)
    result = {}
    for i in candidates:
        event = inclusoes.get(i['pk'])
        ms = movements[(i['produto_id'],f"Venda #{i['venda_id']}")]
        base = unidade(i['custo_unidade_snapshot'])
        debit, cost, total = (numero(i.get(k)) for k in
            ('estoque_movimentado','custo_unitario_snapshot','custo_total_snapshot'))
        if (not event or not event.get('pk') or event.get('ajustes') or event.get('arquivo')
                or len(ms) != 1 or debit is None or debit <= 0 or cost is None or cost <= 0
                or total is None or total <= 0 or not base
                or base != unidade(i['estoque_unidade_snapshot'])
                or i['custo_origem_snapshot'] != 'preco_compra_atual'):
            continue
        m = ms[0]
        if (m.origem != 'venda' or hasattr(m,'detalhe') or m.antes is None or m.depois is None
                or m.antes != i['estoque_antes'] or m.depois != i['estoque_depois']
                or m.antes-m.depois != debit or event['criado_em'] < m.registrado_em
                or dinheiro_exato(Fraction(debit)*Fraction(cost)) != total):
            continue
        history = selections[i['produto_id']]
        # The old factor 1 described a single-unit product, not a CT/MIL ratio.
        if not history or any(unidade(s['cadastro'].get('base')) != base
                or unidade(s['cadastro'].get('secundaria'))
                or s['cadastro'].get('fracionado') is not False
                or numero(s['cadastro'].get('fator')) != 1
                or s['inventario__criado_em'] >= m.registrado_em for s in history):
            continue
        witnesses = [s for s in history if s['custo_base'] == cost]
        if not witnesses:
            continue
        result[i['pk']] = dict(custo=total, motivo='', prova=None, evidencia=dict(
            criterio='total_congelado_inclusao_e_movimento_v1',item_id=i['pk'],venda_id=i['venda_id'],
            evento_inclusao_id=event['pk'],movimento_id=m.pk,
            itens_inventario_custo=[s['pk'] for s in witnesses],
            unidade_custo=base,baixa_registrada=str(debit),custo_unitario=str(cost),
            custo_total=str(total),prova_fisica=False,conversao_historica_comprovada=False))
    return result


def carregar_provas(itens, *, incluir_fisicas=False):
    """One attribution query; only legacy candidates load movements/confirmations.

    Existing but invalid attributions never fall through to legacy evidence.
    Confirmations timestamp the preserved inventory selection independently of
    current product settings. A factor inferred from the stock delta is not proof.
    """
    from .relatorios_produtos_indicadores import prova_item, custo_item
    itens = list(itens)
    candidatos = [i for i in itens if incluir_fisicas or unidade(i['custo_unidade_snapshot']) != i['unidade_comercial']]
    if not candidatos:
        return {}, {}
    detalhes = defaultdict(list)
    for d in DetalheMovimentoEstoque.objects.filter(documento_tipo='venda',
            item_referencia__in=[str(i['pk']) for i in candidatos]).select_related('movimento'):
        detalhes[d.item_referencia].append(d)
    provas, motivos = {}, {}
    legados = []
    for i in candidatos:
        ds = detalhes[str(i['pk'])]
        if ds:
            provas[i['pk']] = prova_item(i, ds)
        elif custo_item(i, None)[1].startswith('Unidade de custo diferente'):
            legados.append(i)
    if not legados:
        return provas, motivos
    ids = [i['pk'] for i in legados]
    pids = {i['produto_id'] for i in legados if i['produto_id'] is not None}
    referencias = {f"Venda #{i['venda_id']}" for i in legados}
    # Reuse the stock extract's unique document/movement and edit checks.
    movimentos = identificar_vendas(MovimentoObservado.objects.filter(
        produto_id__in=pids, referencia__in=referencias), ItemVenda.objects.filter(pk__in=ids))
    # The correlated removal check uses the movement's explicit document reference,
    # without interpreting a stock delta as a document identifier.
    removidos = ItemVendaRemovido.objects.filter(produto_id=OuterRef('produto_id'),
        venda__itens__pk=OuterRef('item_venda_documentado'))
    movimentos = movimentos.annotate(item_removido=Exists(removidos)).select_related('detalhe')
    por_item = defaultdict(list)
    por_documento = defaultdict(list)
    editados = []
    for m in movimentos:
        por_documento[(m.produto_id,m.referencia)].append(m)
        if m.item_venda_documentado is not None:
            por_item[m.item_venda_documentado].append(m)
            if m.venda_com_edicao:
                editados.append(m.item_venda_documentado)
    editados.extend(i['pk'] for i in legados
        if len(por_documento[(i['produto_id'],f"Venda #{i['venda_id']}")])>1)
    inclusoes = _eventos_documentados(editados)
    confirmacoes = defaultdict(list)
    for c in ConfirmacaoContagem.objects.filter(rodada__item__produto_id__in=pids).order_by('ok_em','pk').values(
            'pk','ok_em','sequencia_ok','rodada__item_id','rodada__item__produto_id','rodada__item__cadastro'):
        confirmacoes[c['rodada__item__produto_id']].append(c)
    selecoes = _selecoes_documentadas(pids)
    historico = {}
    for pid in pids:
        historico[pid] = sorted(confirmacoes[pid] + selecoes[pid],key=lambda c:(c['ok_em'],c['pk'] or 0))
    for i in legados:
        reason = 'Sem movimento único e documentação histórica suficiente para validar a conversão legada.'
        ms = por_item[i['pk']]
        obs = por_documento[(i['produto_id'],f"Venda #{i['venda_id']}")]
        if len(obs)>1:
            prova=_provar_sequencia_editada(i,obs,historico[i['produto_id']],inclusoes.get(i['pk']))
            if prova is not None:
                provas[i['pk']]=prova
                continue
        if len(ms) != 1:
            motivos[i['pk']] = ('Venda com múltiplos movimentos; o custo do item exige conciliação documental.'
                if len(obs)>1 else 'Snapshot do item não coincide com o movimento vinculado à venda.'
                if obs else 'Movimento histórico da venda não localizado para este item.')
            continue
        m = ms[0]
        inclusao = inclusoes.get(i['pk'])
        if m.outro_movimento:
            motivos[i['pk']] = 'Venda com múltiplos movimentos; o custo do item exige conciliação documental.'
            continue
        if m.venda_com_edicao and not inclusao:
            motivos[i['pk']] = 'Inclusão ou edição registrada não concilia com quantidade, unidade e valores do item.'
            continue
        if m.item_removido or m.origem != 'venda' or hasattr(m, 'detalhe'):
            motivos[i['pk']] = 'Há remoção ou atribuição incompatível do produto nesta venda; conferir os documentos.'
            continue
        if inclusao and inclusao['criado_em'] < m.registrado_em:
            motivos[i['pk']] = 'Inclusão documental anterior ao movimento associado; cronologia incompatível.'
            continue
        cs = historico[i['produto_id']]
        position = bisect_right([c['ok_em'] for c in cs], m.registrado_em)
        if not position:
            motivos[i['pk']] = 'Há custo congelado, mas falta fator em documentação histórica anterior à venda.'
            continue
        c = cs[position-1]
        cadastro = c['rodada__item__cadastro']
        fator = numero(cadastro.get('fator'))
        base, secundaria = unidade(cadastro.get('base')), unidade(cadastro.get('secundaria'))
        signature = (base, secundaria, fator, cadastro.get('fracionado'))
        # A contradictory next confirmed selection leaves the legacy interval
        # uncertain; it cannot be resolved by choosing whichever factor fits.
        witnesses = cs[position:position+1] + [n for n in cs[:position-1] if n['ok_em'] == c['ok_em']]
        contradictory = any((unidade(n['rodada__item__cadastro'].get('base')),
            unidade(n['rodada__item__cadastro'].get('secundaria')),
            numero(n['rodada__item__cadastro'].get('fator')),
            n['rodada__item__cadastro'].get('fracionado')) != signature for n in witnesses)
        if (contradictory or fator is None or fator <= 0 or cadastro.get('fracionado') is not True
                or not base or not secundaria or base == secundaria
                or base != unidade(i['custo_unidade_snapshot'])
                or base != unidade(i['estoque_unidade_snapshot'])
                or secundaria != i['unidade_comercial']
                or (c['sequencia_ok'] is not None and c['sequencia_ok'] >= m.sequencia)):
            motivos[i['pk']] = 'Unidades ou fator histórico contraditórios ou incompatíveis com a venda.'
            continue
        q = numero(i['quantidade'])
        baixa = numero(i.get('estoque_movimentado'))
        operacional = projecao_operacional(q, 'divisao', fator)
        if (baixa is None or baixa <= 0 or m.antes is None or m.depois is None
                or m.antes - m.depois != baixa or baixa != operacional):
            motivos[i['pk']] = 'Quantidade vendida e baixa histórica não conciliam com o fator preservado.'
            continue
        ajustes = inclusao.get('ajustes',[]) if inclusao else []
        if any(projecao_operacional(a[campo],'divisao',fator)!=operacional
                for a in ajustes for campo in ('antes','depois')):
            motivos[i['pk']] = 'A edição de quantidade altera a baixa física; exige conciliação dos movimentos.'
            continue
        exato = Fraction(q) / Fraction(fator)
        provas[i['pk']] = dict(legado=True, unidade_base=base, quantidade_base=exato,
            rastreabilidade=dict(criterio=('movimento_e_confirmacao_historica_v1' if c['pk']
                else 'movimento_e_selecao_documentada_v1'),
                item_id=i['pk'], venda_id=i['venda_id'], movimento_id=m.pk,
                confirmacao_id=c['pk'], item_inventario_id=c['rodada__item_id'],
                evento_criacao_inventario_id=c.get('evento_criacao_id'),
                confirmado_em=c['ok_em'] if c['pk'] else None, documentado_em=c['ok_em'],
                fator=str(fator), conversao='divisao',
                unidade_venda=secundaria, unidade_custo=base,
                quantidade_base_numerador=exato.numerator,
                quantidade_base_denominador=exato.denominator,
                eventos_ajuste_sem_efeito_fisico=[a['evento_id'] for a in ajustes],
                criterio_edicao='mesma_projecao_historica_v1' if ajustes else None,
                evento_inclusao_id=inclusao['pk'] if inclusao else None))
    return provas, motivos
