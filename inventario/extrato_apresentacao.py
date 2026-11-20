"""Read-only operational presentation. No reconciliation or stock validation."""
import re

from django.db.models import BooleanField, Case, When, CharField, Exists, F, OuterRef, Subquery, Value, Q
from django.db.models.functions import Cast, Coalesce, Concat

from estoque.models import EdicaoCompra, EventoVenda, ItemCompra, ItemVenda
from .models import MovimentoObservado, DetalheMovimentoEstoque
from .saldo_documental import unidade as unidade_documental, saida_realizada, provar_atribuicao, provar_entrada_legada, decimal_fracao



def edicoes_do_produto(nome, **vinculo):
    """Shared scope for item edits, without invalidating unrelated sale items."""
    return EventoVenda.objects.filter(**vinculo).filter(
        Q(tipo_evento='quantidade_item_alterada', descricao__startswith=Concat(
            Value('Quantidade alterada na nota: '), nome, Value('. De '))) |
        Q(tipo_evento='item_adicionado_na_nota', descricao__startswith=Concat(
            Value('Item adicionado na nota: '), nome, Value(', quantidade '))) |
        Q(tipo_evento='item_removido_da_nota', descricao__startswith=Concat(
            Value('Item removido da nota: '), nome, Value(', quantidade '))))


def identificar_vendas(movimentos, vendas):
    # An explicit document reference AND identical recorded snapshot values are
    # necessary. Equal amounts or nearby timestamps alone never hide an event.
    outros_itens = ItemVenda.objects.filter(venda_id=OuterRef('venda_id'),
        produto_id=OuterRef('produto_id')).exclude(pk=OuterRef('pk'))
    documentos = vendas.annotate(outro_item=Exists(outros_itens),
        ref_documento=Concat(Value('Venda #'), Cast('venda_id', CharField()))).filter(
            outro_item=False, produto_id=OuterRef('produto_id'),
            ref_documento=OuterRef('referencia'), estoque_antes=OuterRef('antes'),
            estoque_depois=OuterRef('depois'), estoque_movimentado__isnull=False,
            estoque_unidade_snapshot__gt='', venda__cancelada=False,
            venda__estoque_devolvido_cancelamento=False).annotate(
                origem_documento=Case(When(venda__tipo_pagamento='consumo_proprio',
                    then=Value('consumo_proprio')), default=Value('venda'), output_field=CharField())
            ).filter(origem_documento=OuterRef('origem'))
    outros_movimentos = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        origem__in=('venda', 'consumo_proprio', 'devolucao_venda'), referencia=OuterRef('referencia')).exclude(pk=OuterRef('pk'))
    # Edits prevent folding a mutable document into an original stock movement.
    # Attribution/quantity checks are performed in bulk by extrato_edicoes.
    edicoes = edicoes_do_produto(OuterRef('produto__nome'),
        venda__itens__produto_id=OuterRef('produto_id')).annotate(
            ref=Concat(Value('Venda #'), Cast('venda_id', CharField()))).filter(ref=OuterRef('referencia'))
    return movimentos.annotate(outro_movimento=Exists(outros_movimentos),
        venda_com_edicao=Exists(edicoes),
        item_venda_documentado=Subquery(documentos.order_by('pk').values('pk')[:1]))


def tecnicos_das_vendas(movimentos, pagina):
    ids = [l['pk'] for l in pagina if l['fonte'] == 'venda']
    if not ids:
        return {}
    por_item = {}
    for m in movimentos.filter(origem__in=('venda', 'consumo_proprio'), outro_movimento=False, venda_com_edicao=False,
            item_venda_documentado__in=ids).order_by('registrado_em', 'sequencia', 'pk'):
        por_item.setdefault(m.item_venda_documentado, []).append(m)
    return por_item


def associar_documentos(movimentos, compras, vendas):
    """Identify documents separately from the decision to fold their evidence.

    Exact attribution alone never proves that two rows are the same stock event.
    Multiple observations, inconsistent values and ambiguous items stay visible.
    Only a single corroborated observation is represented by its document.
    """
    movimentos = movimentos.annotate(
        tipo_atribuido=Coalesce('detalhe__documento_tipo', Value('')),
        documento_atribuido=Coalesce('detalhe__documento_id', Value('')),
        item_atribuido=Coalesce('detalhe__item_referencia', Value('')))
    outros = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        referencia=OuterRef('referencia')).exclude(pk=OuterRef('pk'))
    for fonte, modelo, documentos in (('compra', ItemCompra, compras), ('venda', ItemVenda, vendas)):
        campo = fonte + '_id'
        repetidos = modelo.objects.filter(produto_id=OuterRef('produto_id'),
            **{campo: OuterRef(campo)}).exclude(pk=OuterRef('pk'))
        candidatos = documentos.annotate(item_repetido=Exists(repetidos),
            referencia_operacional=Concat(Value('Compra #' if fonte == 'compra' else 'Venda #'),
                Cast(campo, CharField())), item_texto=Cast('pk', CharField()),
            documento_texto=Cast(campo, CharField()), tipo_texto=Value(fonte, CharField()),
            vazio=Value('', CharField())).filter(produto_id=OuterRef('produto_id')).filter(
            # Legacy exact references remain usable only without contradictory
            # structured attribution. A structured item can disambiguate repeats.
            (Q(item_repetido=False, referencia_operacional=OuterRef('referencia'),
               vazio=OuterRef('item_atribuido')) &
             (Q(vazio=OuterRef('tipo_atribuido')) |
              Q(tipo_texto=OuterRef('tipo_atribuido'), documento_texto=OuterRef('documento_atribuido')))) |
            Q(tipo_texto=OuterRef('tipo_atribuido'), documento_texto=OuterRef('documento_atribuido'),
              item_texto=OuterRef('item_atribuido')))
        movimentos = movimentos.annotate(**{f'item_{fonte}_operacional':
            Subquery(candidatos.order_by('pk').values('pk')[:1])})
    movimentos = movimentos.annotate(identidade_operacao=Coalesce(
        Cast('detalhe__operacao_id', CharField()), Value('')))
    distintas = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        detalhe__operacao_id__isnull=False).annotate(
            vazio=Value('', CharField()), operacao_texto=Cast('detalhe__operacao_id', CharField())).filter(
            Q(vazio=OuterRef('tipo_atribuido'), referencia=OuterRef('referencia')) |
            Q(detalhe__documento_tipo=OuterRef('tipo_atribuido'),
              detalhe__documento_id=OuterRef('documento_atribuido'))).exclude(
            operacao_texto=OuterRef('identidade_operacao'))
    # Document identity is not event identity. Only a single observation with
    # matching recorded values can be folded; all other evidence stays visible.
    vendas_coerentes = vendas.filter(produto_id=OuterRef('produto_id'),
        pk=OuterRef('item_venda_operacional'), estoque_antes=OuterRef('antes'),
        estoque_depois=OuterRef('depois'), estoque_movimentado__gt=0).filter(
        estoque_depois=F('estoque_antes') - F('estoque_movimentado'),
        venda__cancelada=False, venda__estoque_devolvido_cancelamento=False).annotate(
            edicao_alvo=Exists(edicoes_do_produto(OuterRef('produto__nome'), venda_id=OuterRef('venda_id')))
        ).filter(edicao_alvo=False)
    compras_coerentes = compras.filter(produto_id=OuterRef('produto_id'),
        pk=OuterRef('item_compra_operacional'), quantidade__gt=0).filter(
        quantidade=OuterRef('detalhe__quantidade_entrada')
    )
    compras_legadas = compras.filter(produto_id=OuterRef('produto_id'),
        pk=OuterRef('item_compra_operacional'), quantidade__gt=0).annotate(
        variacao=OuterRef('depois') - OuterRef('antes')).filter(quantidade=F('variacao'))
    vendas_atuais = vendas.filter(pk=OuterRef('item_venda_operacional'),
        produto_id=OuterRef('produto_id'), venda__cancelada=False,
        venda__estoque_devolvido_cancelamento=False,
        quantidade=OuterRef('detalhe__quantidade_entrada'), unidade=OuterRef('detalhe__unidade_entrada')).annotate(
            edicao_alvo=Exists(edicoes_do_produto(OuterRef('produto__nome'), venda_id=OuterRef('venda_id')))
        ).filter(edicao_alvo=False)
    documento = (
        Q(origem='compra', detalhe__acao='entrada') & Exists(compras_coerentes)
        | Q(origem='compra', detalhe__isnull=True) & Exists(compras_legadas)
        | (Q(origem__in=('venda', 'consumo_proprio'), detalhe__acao='saida',
             detalhe__documento_tipo='venda', item_venda_operacional__isnull=False) & ~Q(item_atribuido='') & Exists(vendas_atuais))
        | Q(origem__in=('venda', 'consumo_proprio'), detalhe__isnull=True) & Exists(vendas_coerentes))
    return movimentos.annotate(operacao_ambigua=Case(
        When(origem='devolucao_venda', detalhe__acao__in=('devolucao', 'cancelamento'),
             depois__gt=F('antes'), then=Value(True)),
        When(documento & ~Exists(distintas) & ~Exists(outros), then=Value(False)),
        default=Value(True), output_field=BooleanField()))


def evidencias_documentais(movimentos, pagina):
    """Fetch linked evidence for this document page, including pagination edges."""
    compras = [r['pk'] for r in pagina if r['fonte'] == 'compra']
    vendas = [r['pk'] for r in pagina if r['fonte'] == 'venda']
    if not compras and not vendas:
        return {}
    consulta = movimentos.filter(operacao_ambigua=False).filter(
        Q(origem='compra', item_compra_operacional__in=compras) |
        Q(origem__in=('venda', 'consumo_proprio', 'devolucao_venda'), item_venda_operacional__in=vendas))
    resultado = {}
    for m in consulta.select_related('detalhe').order_by('sequencia', 'pk'):
        fonte = 'venda' if m.origem in ('venda', 'consumo_proprio', 'devolucao_venda') else 'compra'
        item = getattr(m, f'item_{fonte}_operacional')
        resultado.setdefault((fonte, item), []).append(m)
    return resultado


def apresentar_operacoes(pagina, evidencias):
    """Commercial display only. Leave physical flow and balance evidence intact."""
    ids = [r['pk'] for r in pagina if r['fonte'] == 'digital' and r.get('independente')]
    detalhes = {d.movimento_id: d for d in DetalheMovimentoEstoque.objects.filter(movimento_id__in=ids)}
    for r in pagina:
        fonte = r['fonte']
        if fonte == 'digital' and r.get('independente'):
            d = detalhes[r['pk']]
            variacao = r['depois'] - r['antes']
            r.update(entrada=variacao if variacao > 0 else None,
                saida=-variacao if variacao < 0 else None, unidade=d.unidade_base,
                unidades_evidencia=None, saldo=r['depois'], saldo_antes=r['antes'],
                saldo_unidade=d.unidade_base, saldo_tipo='Saldo registrado',
                titulo=('Devolução' if r['origem']=='devolucao_venda' else 'Movimentação') +
                    f" identificada · {r['referencia']}")
        r['evidencias_operacionais'] = evidencias.get((fonte, r['pk']), [])
        if fonte == 'compra' and len(r['evidencias_operacionais']) == 1:
            m = r['evidencias_operacionais'][0]
            detalhe = getattr(m, 'detalhe', None)
            if detalhe and m.depois is not None and unidade_documental(detalhe.unidade_base):
                r['saldo_registrado_documento'] = dict(valor=m.depois,
                    unidade=unidade_documental(detalhe.unidade_base), origem=f'Registro #{m.pk}')
        if (fonte == 'digital' and not r.get('independente') and not r.get('unidade_comprovada') and r['origem'] in ('compra', 'venda', 'consumo_proprio', 'devolucao_venda')
                and re.fullmatch(r'(Compra|Venda) #\d+', r['referencia'])):
            # An unpaired observer is evidence, not another documentary sale/receipt.
            r['evidencia_comercial_nao_consolidada'] = True
        if fonte == 'venda':
            r['operacao_documental'] = dict(direcao='saida', quantidade=r['quantidade'],
                unidade=r['unidade'], efetivada=saida_realizada(r))
            if r.get('documento_atual') or r.get('unidades_evidencia'):
                r['aviso_operacional'] = 'Quantidade da nota; baixa no estoque não comprovada.'
                r['saldo_operacional_indisponivel'] = bool(r.get('documento_atual')
                    or not r.get('unidades_evidencia', {}).get('baixa'))
        elif fonte == 'remocao' and r.get('retorno_efetivo'):
            if r['quantidade'] is not None and r['quantidade'] > 0 and unidade_documental(r['unidade']):
                r['operacao_documental'] = dict(direcao='entrada', quantidade=r['quantidade'],
                    unidade=r['unidade'], efetivada=True)


def preparar_fluxos_historicos(linhas, produto_id, evidencias=None):
    """Same historical conversion for visible rows and the SQL prefix, in bulk."""
    for r in linhas:
        if r['fonte'] == 'digital':
            antes, depois = r.get('antes'), r.get('depois')
            r['registro_original'] = dict(antes=antes, depois=depois,
                movimentado=abs(depois-antes) if antes is not None and depois is not None else None,
                unidade=r.get('saldo_unidade', ''))
    docs = {(r['fonte'], str(r['documento']), str(r['pk'])): r for r in linhas
            if r['fonte'] in ('compra', 'venda') and not r.get('cancelada')}
    if evidencias is None:
        associados = associar_documentos(MovimentoObservado.objects.filter(produto_id=produto_id),
            ItemCompra.objects.filter(produto_id=produto_id), ItemVenda.objects.filter(produto_id=produto_id))
        evidencias = evidencias_documentais(associados, linhas)
    compras_legadas = [r['documento'] for r in linhas if r['fonte'] == 'compra'
        and evidencias.get(('compra', r['pk']))]
    editadas = set(EdicaoCompra.objects.filter(compra_id__in=compras_legadas).values_list('compra_id', flat=True))
    for r in linhas:
        if r['fonte'] == 'venda':
            r['registro_original'] = dict(antes=r.get('referencia_antes'), depois=r.get('referencia_depois'),
                movimentado=r.get('referencia_baixa'), unidade=r.get('referencia_unidade', ''))
        vinculados = evidencias.get((r['fonte'], r['pk']), [])
        if len(vinculados) == 1:
            m = vinculados[0]
            d = getattr(m, 'detalhe', None)
            if r['fonte'] == 'compra' and not d and r['documento'] not in editadas and not r.get('cancelada'):
                prova = provar_entrada_legada(r, m)
                if prova:
                    r.update(operacao_historica=prova, fluxo_documental_exato=prova['exato'])
            if r['fonte'] == 'compra' or (r['fonte'] == 'venda' and
                    r.get('referencia_antes') is None and r.get('referencia_depois') is None):
                r['registro_original'] = dict(antes=m.antes, depois=m.depois,
                    movimentado=abs(m.depois-m.antes) if m.antes is not None and m.depois is not None else None,
                    unidade=unidade_documental(d.unidade_base) if d else
                        r.get('operacao_historica', {}).get('unidade', ''))
            elif r['fonte'] == 'venda':
                original = r['registro_original']
                for campo in ('antes', 'depois'):
                    if original[campo] is None:
                        original[campo] = getattr(m, campo)
                if not original['unidade'] and d:
                    original['unidade'] = unidade_documental(d.unidade_base)
                if original['movimentado'] is None and m.antes is not None and m.depois is not None:
                    original['movimentado'] = abs(m.depois-m.antes)
        if r.get('saldo_tipo') == 'Reconstruído e corroborado':
            r['referencia_historica'] = dict(antes=r['saldo_antes'], valor=r['saldo'],
                unidade=r['saldo_unidade'], origem=r['saldo_evidencia'])
    digitais = {r['pk']: r for r in linhas if r['fonte'] == 'digital'}
    retornos = [r for r in linhas if r['fonte'] == 'cancelamento']
    filtro = (Q(documento_tipo__in=('compra', 'venda'), documento_id__in={k[1] for k in docs},
                item_referencia__in={k[2] for k in docs}) | Q(movimento_id__in=digitais) |
              Q(documento_tipo='venda', documento_id__in={str(r['documento']) for r in retornos},
                movimento__registrado_em__in={r['instante'] for r in retornos},
                acao__in=('cancelamento', 'devolucao')))
    detalhes = list(DetalheMovimentoEstoque.objects.filter(filtro,
        movimento__produto_id=produto_id).select_related('movimento'))
    digitais_detalhados = [d for d in detalhes if d.movimento_id in digitais or retornos]
    vinculos = set()
    for tipo, modelo, campo in (('compra', ItemCompra, 'compra_id'), ('venda', ItemVenda, 'venda_id')):
        ids = [int(d.item_referencia) for d in digitais_detalhados
               if d.documento_tipo == tipo and d.item_referencia.isdigit()]
        if ids:
            vinculos.update((tipo, str(pk), str(doc)) for pk, doc in modelo.objects.filter(
                produto_id=produto_id, pk__in=ids).values_list('pk', campo))
    encontrados = {}
    for d in detalhes:
        encontrados.setdefault((d.documento_tipo, d.documento_id, d.item_referencia), []).append(d)
    for chave, r in docs.items():
        candidatos = encontrados.get(chave, [])
        if candidatos and not evidencias.get((r['fonte'], r['pk'])):
            # A document is not an event ID. Its attributed observers remain
            # separate when edited, repeated or outside this display interval.
            # Never apply the mutable document operand on top of those events.
            r.update(documento_atual=True, informacao_sem_efeito=True)
        if len(candidatos) != 1:
            continue
        d = candidatos[0]
        direcao = 1 if r['fonte'] == 'compra' else -1
        if d.acao != ('entrada' if direcao == 1 else 'saida'):
            continue
        m = d.movimento
        prova = provar_atribuicao(d)
        if prova is None:
            r['fluxo_invalido'] = True
        if (prova and d.quantidade_entrada == r['quantidade']
                and unidade_documental(r['unidade']) == unidade_documental(d.unidade_entrada)
                and m.origem == ('compra' if direcao == 1 else 'consumo_proprio'
                    if r.get('origem') == 'consumo_proprio' else 'venda')):
            r['fluxo_historico'] = (decimal_fracao(prova['exato']), prova['unidade'])
            r['fluxo_documental_exato'] = prova['exato']
            r['operacao_historica'] = prova
            if len(evidencias.get((r['fonte'], r['pk']), [])) == 1:
                r['referencia_historica'] = dict(antes=m.antes, valor=m.depois,
                    unidade=unidade_documental(d.unidade_base), origem=f'Registro #{m.pk} · {r["fonte"]} #{r["documento"]}')
    # Each immutable attributed observer is a separate event, including returns.
    # A current edited/cancelled note is information, never another stock debit.
    for r in linhas:
        if r.get('documento_atual') and not evidencias.get((r['fonte'], r['pk'])):
            r['informacao_sem_efeito'] = True
    for d in detalhes:
        m = d.movimento
        r = digitais.get(m.pk)
        prova = (provar_atribuicao(d) if (d.documento_tipo, d.item_referencia, d.documento_id) in vinculos else None)
        if (r and prova and d.documento_tipo in ('compra', 'venda')
                and m.origem in ('compra', 'venda', 'consumo_proprio', 'devolucao_venda')
                and ((m.origem == 'compra' and d.acao == 'entrada')
                     or (m.origem in ('venda', 'consumo_proprio') and d.acao == 'saida')
                     or (m.origem == 'devolucao_venda' and d.acao in ('devolucao', 'cancelamento')))):
            r.update(operacao_historica=prova, fluxo_documental_exato=prova['exato'],
                fluxo_historico=(decimal_fracao(prova['exato']), prova['unidade']),
                referencia_historica=dict(antes=m.antes, valor=m.depois, unidade=prova['unidade'], origem=f'Registro #{m.pk}'))
            r['registro_original']['unidade'] = prova['unidade']
            r['evidencia_comercial_nao_consolidada'] = False
        if prova and m.origem == 'devolucao_venda' and d.acao in ('cancelamento', 'devolucao'):
            for resumo in retornos:
                if str(resumo['documento']) == d.documento_id and resumo['instante'] == m.registrado_em:
                    resumo['informacao_sem_efeito'] = True
                    resumo.update(saldo_aviso='Cancelamento representado pelo movimento identificado no mesmo instante.',
                                  saldo_continuidade='')
