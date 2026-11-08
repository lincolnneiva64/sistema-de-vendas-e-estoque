"""Read-only operational presentation. No reconciliation or stock validation."""
import re

from django.db.models import BooleanField, Case, When, CharField, Exists, F, OuterRef, Subquery, Value, Q
from django.db.models.functions import Cast, Coalesce, Concat

from estoque.models import EventoVenda, ItemCompra, ItemVenda
from .models import MovimentoObservado, DetalheMovimentoEstoque
from .saldo_documental import unidade as unidade_documental, quantidade_historica, saida_realizada



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
    """Associate evidence with an exact sale/product document, not one stock write.

    Several observations can refer to the same sale at different times. Their
    multiplicity does not make the document identity ambiguous or certify their
    quantities. Repeated product items and non-exact references stay separate.
    The same association rule applies to purchases, sales and own consumption.
    Explicitly different operation identifiers preserve separate stock events.
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
    documento = (Q(origem='compra', item_compra_operacional__isnull=False) |
        Q(origem__in=('venda', 'consumo_proprio', 'devolucao_venda'), item_venda_operacional__isnull=False))
    return movimentos.annotate(operacao_ambigua=Case(
        When(origem='devolucao_venda', detalhe__acao__in=('devolucao', 'cancelamento'),
             depois__gt=F('antes'), then=Value(True)),
        When(documento & ~Exists(distintas), then=Value(False)),
        default=Exists(outros), output_field=BooleanField()))


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
    for m in consulta.order_by('sequencia', 'pk'):
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
        if (fonte == 'digital' and not r.get('independente') and r['origem'] in ('compra', 'venda', 'consumo_proprio', 'devolucao_venda')
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


def preparar_fluxos_historicos(linhas, produto_id):
    """Same historical conversion for visible rows and the SQL prefix, in bulk."""
    docs = {(r['fonte'], str(r['documento']), str(r['pk'])): r for r in linhas
            if r['fonte'] in ('compra', 'venda') and not r.get('cancelada')}
    if not docs:
        return
    encontrados = {}
    for d in DetalheMovimentoEstoque.objects.filter(
            movimento__produto_id=produto_id,
            documento_tipo__in=('compra', 'venda'),
            documento_id__in={k[1] for k in docs},
            item_referencia__in={k[2] for k in docs}).select_related('movimento'):
        encontrados.setdefault((d.documento_tipo, d.documento_id, d.item_referencia), []).append(d)
    for chave, r in docs.items():
        candidatos = encontrados.get(chave, [])
        if len(candidatos) != 1:
            continue
        d = candidatos[0]
        direcao = 1 if r['fonte'] == 'compra' else -1
        if d.acao != ('entrada' if direcao == 1 else 'saida'):
            continue
        valor = quantidade_historica(d, r['quantidade'], r['unidade'])
        m = d.movimento
        if (valor is not None and valor >= 0 and d.quantidade_entrada == r['quantidade']
                and m.antes is not None and m.depois is not None
                and m.depois - m.antes == direcao * valor):
            r['fluxo_historico'] = (direcao * valor, unidade_documental(d.unidade_base))
