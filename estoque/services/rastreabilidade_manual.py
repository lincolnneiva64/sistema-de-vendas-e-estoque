"""Optional callback used exclusively by the Sales stock-correction endpoint."""
from uuid import NAMESPACE_URL, uuid5

from django.db import connection
from django.db.models import Subquery

from inventario.models import DetalheMovimentoEstoque, MovimentoObservado, SequenciaEstoque
from inventario.rastreabilidade import identificar


def registrar(resultado, saldo_original, *, usuario, operador_declarado=''):
    if not connection.in_atomic_block:
        raise ValueError('Auditoria manual exige a transação do ajuste.')
    produto, historico = resultado['produto'], resultado['historico']
    depois = resultado['estoque_depois']
    if saldo_original == depois:
        return None
    referencia = f'Movimentação manual #{historico.pk}'
    # The legacy manual writer normalizes a NULL baseline to zero. Keep that
    # behavior untouched, but attribute the observer's genuine unknown baseline.
    if saldo_original is None:
        identificar(produto.pk, None, depois, 'ajuste_manual', referencia)
    sequencia = SequenciaEstoque.objects.filter(produto_id=produto.pk).values('sequencia')[:1]
    movimento = MovimentoObservado.objects.filter(produto_id=produto.pk,
        sequencia=Subquery(sequencia), antes=saldo_original, depois=depois,
        origem='ajuste_manual', referencia=referencia).first()
    if movimento is None:
        raise ValueError('Movimento observado do ajuste manual não confirmado. Operação revertida.')
    unidade = (produto.unidade_venda_1 or produto.unidade_compra or '').strip()
    declarado = str(operador_declarado or '').strip()
    evidencia = dict(motivo=historico.motivo, observacao=historico.observacao)
    # Missing legacy units stay explicitly unknown, never assumed to mean UN.
    unidade = unidade or 'Não informada'
    return DetalheMovimentoEstoque.objects.create(
        movimento=movimento, operacao_id=uuid5(NAMESPACE_URL, f'estoque.movimentacao_manual:{historico.pk}'),
        ordem=1, acao='ajuste', documento_tipo='movimentacao_estoque_manual', documento_id=str(historico.pk),
        executor=usuario, executor_nome=usuario.get_username(),
        # Declared names are attribution only, never authentication or inferred FKs.
        operador_nome=(declarado or historico.operador_nome)[:150], contexto='autenticado',
        unidade_base=unidade, unidade_entrada=unidade, quantidade_entrada=depois,
        conversao='nenhuma', fator=None, evidencia=evidencia)
