"""Consultas comerciais por comprador e produto. Somente leitura."""

from django.db.models import Count, Sum

from .relatorios_produtos import itens_comerciais


def compradores_produto(produto_id, inicio, fim):
    itens = itens_comerciais().filter(
        produto_id=produto_id,
        venda__data_venda__range=(inicio, fim),
    )

    compradores = list(
        itens.values("venda__cliente_id", "venda__cliente__nome")
        .annotate(
            vendas=Count("venda_id", distinct=True),
            faturamento=Sum("valor_total"),
        )
        .order_by("-faturamento", "venda__cliente__nome", "venda__cliente_id")
    )

    quantidades = list(
        itens.values("venda__cliente_id", "unidade_comercial")
        .annotate(quantidade=Sum("quantidade"))
        .order_by("venda__cliente_id", "unidade_comercial")
    )

    por_cliente = {}
    for linha in quantidades:
        por_cliente.setdefault(linha["venda__cliente_id"], []).append({
            "unidade": linha["unidade_comercial"],
            "quantidade": linha["quantidade"],
        })

    for comprador in compradores:
        comprador["quantidades"] = por_cliente.get(
            comprador["venda__cliente_id"], []
        )

    return compradores


def notas_comprador_produto(produto_id, inicio, fim, cliente_id, pagina=1, *, unidade=''):
    """Notas comerciais de um comprador, somente leitura."""
    from django.core.paginator import Paginator

    itens = itens_comerciais().filter(
        produto_id=produto_id,
        venda__data_venda__range=(inicio, fim),
        venda__cliente_id=cliente_id,
    )
    if unidade:
        itens = itens.filter(unidade_comercial=unidade)

    linhas = (
        itens.values(
            "venda_id", "venda__data_venda", "unidade_comercial"
        )
        .annotate(
            quantidade=Sum("quantidade"),
            valor=Sum("valor_total"),
        )
        .order_by("-venda__data_venda", "-venda_id", "unidade_comercial")
    )

    return Paginator(linhas, 25).get_page(pagina)
