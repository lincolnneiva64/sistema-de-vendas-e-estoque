"""Read-only associations for option B. No reconciliation or stock validation."""
from django.db.models import CharField, Exists, OuterRef, Subquery, Value, Q
from django.db.models.functions import Cast, Concat

from estoque.models import EventoVenda, ItemVenda
from .models import MovimentoObservado


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
            venda__estoque_devolvido_cancelamento=False).exclude(venda__tipo_pagamento='consumo_proprio')
    outros_movimentos = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        origem='venda', referencia=OuterRef('referencia')).exclude(pk=OuterRef('pk'))
    # Edits prevent folding a mutable document into an original stock movement.
    # Attribution/quantity checks are performed in bulk by extrato_edicoes.
    edicoes = EventoVenda.objects.filter(venda__itens__produto_id=OuterRef('produto_id')).annotate(
        ref=Concat(Value('Venda #'), Cast('venda_id', CharField()))).filter(
        ref=OuterRef('referencia')).filter(
            Q(tipo_evento='quantidade_item_alterada', descricao__startswith=Concat(
                Value('Quantidade alterada na nota: '), OuterRef('produto__nome'), Value('. De '))) |
            Q(tipo_evento='item_adicionado_na_nota', descricao__startswith=Concat(
                Value('Item adicionado na nota: '), OuterRef('produto__nome'), Value(', quantidade '))) |
            Q(tipo_evento='item_removido_da_nota', descricao__startswith=Concat(
                Value('Item removido da nota: '), OuterRef('produto__nome'), Value(', quantidade '))))
    return movimentos.annotate(outro_movimento=Exists(outros_movimentos),
        venda_com_edicao=Exists(edicoes),
        item_venda_documentado=Subquery(documentos.order_by('pk').values('pk')[:1]))


def tecnicos_das_vendas(movimentos, pagina):
    ids = [l['pk'] for l in pagina if l['fonte'] == 'venda']
    if not ids:
        return {}
    por_item = {}
    for m in movimentos.filter(origem='venda', outro_movimento=False, venda_com_edicao=False,
            item_venda_documentado__in=ids).order_by('registrado_em', 'sequencia', 'pk'):
        por_item.setdefault(m.item_venda_documentado, []).append(m)
    return por_item
