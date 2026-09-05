from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..models import MovimentacaoEstoqueManual, Produto


QUANTIDADE_ZERO = Decimal("0.000")


def _quantidade_estoque(valor, campo):
    try:
        quantidade = Decimal(str(valor if valor is not None else "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{campo} invalido.")
    if not quantidade.is_finite():
        raise ValueError(f"{campo} invalido.")
    return quantidade.quantize(QUANTIDADE_ZERO)


def _nome_operador(usuario):
    if not usuario or not getattr(usuario, "is_authenticated", False):
        return ""
    return str(usuario.get_username() or "").strip()


@transaction.atomic
def conferir_ou_ajustar_estoque(
    produto_id,
    tipo,
    novo_estoque=None,
    motivo="",
    observacao="",
    usuario=None,
):
    if tipo not in {
        MovimentacaoEstoqueManual.TIPO_CONFERENCIA,
        MovimentacaoEstoqueManual.TIPO_AJUSTE,
    }:
        raise ValueError("Tipo de movimentacao de estoque invalido.")

    motivo = str(motivo or "").strip()

    produto = (
        Produto.objects
        .select_for_update()
        .filter(pk=produto_id, ativo=True, excluido=False)
        .first()
    )
    if produto is None:
        raise ValueError("Produto inexistente, inativo ou excluido.")

    estoque_antes = _quantidade_estoque(produto.quantidade, "Estoque atual")
    if tipo == MovimentacaoEstoqueManual.TIPO_CONFERENCIA:
        estoque_depois = estoque_antes
    else:
        if novo_estoque is None or str(novo_estoque).strip() == "":
            raise ValueError("Informe o novo estoque.")
        estoque_depois = _quantidade_estoque(novo_estoque, "Novo estoque")
        if estoque_depois < QUANTIDADE_ZERO:
            raise ValueError("O novo estoque nao pode ser negativo.")

    diferenca = (estoque_depois - estoque_antes).quantize(QUANTIDADE_ZERO)
    agora = timezone.now()
    atualizacoes = {
        "estoque_conferido": True,
        "estoque_conferido_em": agora,
    }
    if hasattr(produto, "estoque_conferido_por_id"):
        atualizacoes["estoque_conferido_por_id"] = getattr(usuario, "pk", None)
    if tipo == MovimentacaoEstoqueManual.TIPO_AJUSTE or produto.quantidade is None:
        atualizacoes["quantidade"] = estoque_depois

    Produto.objects.filter(pk=produto.pk).update(**atualizacoes)
    produto.quantidade = estoque_depois
    produto.estoque_conferido = True
    produto.estoque_conferido_em = agora
    produto.estoque_conferido_por_id = getattr(usuario, "pk", None)
    historico = MovimentacaoEstoqueManual.objects.create(
        produto=produto,
        tipo=tipo,
        estoque_antes=estoque_antes,
        estoque_depois=estoque_depois,
        diferenca=diferenca,
        motivo=motivo,
        observacao=str(observacao or "").strip(),
        usuario=usuario if getattr(usuario, "is_authenticated", False) else None,
        operador_nome=_nome_operador(usuario),
    )
    return {
        "produto": produto,
        "historico": historico,
        "estoque_antes": estoque_antes,
        "estoque_depois": estoque_depois,
        "diferenca": diferenca,
    }
