"""Internal batch writer for the inventory's already locked and validated lot.

Uses the same stock fields and manual journal as estoque_manual. PostgreSQL
row triggers still execute individually; no signals/save overrides exist on
the manual journal. Never accepts conflict-ignore or disables a trigger.
"""
from django.db import connection
from django.utils import timezone

from ..models import Produto, MovimentacaoEstoqueManual
from .estoque_manual import _quantidade_estoque, _nome_operador


def conferir_movimento(entrada, resultado, movimento):
    """Verify each trigger's exact identity before attributing its origin."""
    if (not movimento or movimento.antes != resultado['estoque_antes']
            or movimento.depois != resultado['estoque_depois']
            or movimento.origem != 'nao_identificada' or not resultado['historico'].pk):
        raise ValueError('Movimento do observador não confirmado. Operação integralmente revertida.')
    movimento.origem = 'ajuste_manual'
    movimento.referencia = f"Movimentação manual #{resultado['historico'].pk}"
    return movimento


def gravar(usuario, entradas):
    if connection.vendor != 'postgresql' or not connection.in_atomic_block:
        raise ValueError('Lote exige PostgreSQL e transação atômica.')
    ids = [e['produto'].pk for e in entradas]
    if not ids or len(ids) != len(set(ids)) or len(ids) > 500:
        raise ValueError('Produtos do lote inválidos.')
    # Do not trust supplied model instances, even though the caller holds locks.
    produtos = {p.pk: p for p in Produto.objects.select_for_update().filter(
        pk__in=ids, ativo=True, excluido=False).order_by('pk')}
    if len(produtos) != len(ids):
        raise ValueError('Produto inexistente, inativo ou excluido.')
    agora = timezone.now()
    resultados = []
    for entrada in entradas:
        produto = produtos[entrada['produto'].pk]
        antes = _quantidade_estoque(produto.quantidade, 'Estoque atual')
        depois = _quantidade_estoque(entrada['depois'], 'Novo estoque')
        if produto.quantidade is None or antes != entrada['antes'] or depois < 0:
            raise ValueError('Estoque alterado ou inválido no lote.')
        produto.quantidade = depois
        produto.estoque_conferido = True
        produto.estoque_conferido_em = agora
        produto.estoque_conferido_por_id = getattr(usuario, 'pk', None)
        historico = MovimentacaoEstoqueManual(produto=produto,
            tipo=MovimentacaoEstoqueManual.TIPO_AJUSTE, estoque_antes=antes,
            estoque_depois=depois, diferenca=depois-antes,
            motivo=str(entrada['motivo'] or '').strip(),
            observacao=str(entrada['observacao'] or '').strip(),
            usuario=usuario if getattr(usuario, 'is_authenticated', False) else None,
            operador_nome=_nome_operador(usuario))
        resultados.append(dict(produto=produto, historico=historico,
            estoque_antes=antes, estoque_depois=depois, diferenca=depois-antes))
    # All product locks were acquired in PK order, before any UPDATE.
    alterados = Produto.objects.bulk_update([produtos[i] for i in sorted(ids)],
        ['quantidade', 'estoque_conferido', 'estoque_conferido_em', 'estoque_conferido_por'])
    if alterados != len(ids):
        raise ValueError('Atualização incompleta do lote.')
    MovimentacaoEstoqueManual.objects.bulk_create([r['historico'] for r in resultados])
    from inventario.models import MovimentoObservado
    movimentos = list(MovimentoObservado.objects.filter(produto_id__in=ids,
        sequencia__in=[e['sequencia']+1 for e in entradas]))
    por_chave = {(m.produto_id, m.sequencia): m for m in movimentos}
    identificados = []
    for entrada, resultado in zip(entradas, resultados):
        movimento = por_chave.get((resultado['produto'].pk, entrada['sequencia']+1))
        identificados.append(conferir_movimento(entrada, resultado, movimento))
    if MovimentoObservado.objects.bulk_update(identificados, ['origem', 'referencia']) != len(ids):
        raise ValueError('Identificação incompleta dos movimentos.')
    return resultados
