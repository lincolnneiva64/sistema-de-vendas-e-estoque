"""Read-only associations for option B. No reconciliation or stock validation."""
from django.db.models import CharField, Exists, OuterRef, Subquery, Value
from django.db.models.functions import Cast, Concat

from estoque.models import ItemVenda
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
            estoque_unidade_snapshot__gt='').exclude(venda__tipo_pagamento='consumo_proprio')
    outros_movimentos = MovimentoObservado.objects.filter(produto_id=OuterRef('produto_id'),
        origem='venda', referencia=OuterRef('referencia')).exclude(pk=OuterRef('pk'))
    return movimentos.annotate(outro_movimento=Exists(outros_movimentos),
        item_venda_documentado=Subquery(documentos.order_by('pk').values('pk')[:1]))


def tecnicos_das_vendas(movimentos, pagina):
    ids = [l['pk'] for l in pagina if l['fonte'] == 'venda']
    if not ids:
        return {}
    por_item = {}
    for m in movimentos.filter(origem='venda', outro_movimento=False,
            item_venda_documentado__in=ids).order_by('registrado_em', 'sequencia', 'pk'):
        por_item.setdefault(m.item_venda_documentado, []).append(m)
    return por_item
