"""Attribution shared by the existing individual and batch conference writers."""
from decimal import Decimal

from django.db import connection

from .models import DetalheMovimentoEstoque


def preparar(usuario, rodada, aplicacao, movimento, historico):
    """Caller holds the product and conference locks in the application transaction.

    The signed applied quantity belongs to the observer. Input conversion describes
    the frozen physical count that originated the adjustment, not a new stock delta.
    """
    if not connection.in_atomic_block:
        raise ValueError('Auditoria da regularização exige a transação de aplicação.')
    if aplicacao.antes == aplicacao.depois:
        return None
    evidencia = aplicacao.evidencia['preparacao']
    if (movimento.produto_id != rodada.item.produto_id
            or movimento.sequencia != evidencia['sequencia'] + 1
            or movimento.antes != aplicacao.antes or movimento.depois != aplicacao.depois
            or movimento.origem != 'ajuste_manual'
            or movimento.referencia != f'Movimentação manual #{historico.pk}'):
        raise ValueError('Movimento incompatível com a regularização.')
    cadastro = evidencia['cadastro']
    fisico = evidencia.get('representacao_fisica', {})
    if fisico:
        quantidade = Decimal(fisico['total_unidades'])
        unidade = fisico['secundaria']
        fator = Decimal(fisico['fator'])
    elif cadastro['fracionado']:
        fator = Decimal(cadastro['fator'])
        quantidade = rodada.embalagens * fator + rodada.avulsos
        unidade = cadastro['secundaria']
    else:
        quantidade, unidade, fator = rodada.contado_base, aplicacao.unidade, None
    return dict(
        movimento=movimento, operacao_id=aplicacao.pk, ordem=1,
        acao='regularizacao_conferencia', documento_tipo='aplicacao_ajuste',
        documento_id=str(aplicacao.pk),
        item_referencia=f'conferencia:{rodada.item.inventario_id};proposta:{aplicacao.proposta_id};rodada:{rodada.pk}',
        executor=usuario, executor_nome=usuario.get_username(),
        # The manual operation records a name, not a separately selected employee.
        # Never infer a Funcionario FK from a name or from the physical counter.
        operador_nome=historico.operador_nome, contexto='autenticado',
        unidade_base=aplicacao.unidade, quantidade_entrada=quantidade,
        unidade_entrada=unidade, conversao='divisao' if fator is not None else 'nenhuma',
        fator=fator)


def registrar(usuario, rodada, aplicacao, movimento, historico):
    dados = preparar(usuario, rodada, aplicacao, movimento, historico)
    return DetalheMovimentoEstoque.objects.create(**dados) if dados is not None else None


def registrar_lote(usuario, entradas):
    """Validate all exact identities, then insert once in the caller's transaction."""
    detalhes = []
    for rodada, aplicacao, movimento, historico in entradas:
        dados = preparar(usuario, rodada, aplicacao, movimento, historico)
        if dados is not None:
            detalhes.append(DetalheMovimentoEstoque(**dados))
    return DetalheMovimentoEstoque.objects.bulk_create(detalhes)
