"""Read-only cost preflight; the sale service remains the final authority."""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.urls import reverse

from estoque.models import Produto
from estoque.views import _custo_produto_para_unidade_venda, _decimal_do_front


def inspect_costs(items):
    if not isinstance(items, list) or not 0 < len(items) <= 200:
        raise ValidationError("Confira os produtos da venda.")
    for item in items:
        if not isinstance(item, dict) or not str(item.get("produto_id", "")).isascii() or not str(item.get("produto_id", "")).isdigit() or not 0 < int(item["produto_id"]) < 2**63:
            raise ValidationError("Confira os produtos da venda.")
    products = Produto.objects.filter(pk__in={int(i["produto_id"]) for i in items}, ativo=True, excluido=False).in_bulk()
    result = []
    for item in items:
        product = products.get(int(item["produto_id"]))
        unit = str(item.get("unidade", "")).strip().upper()
        try:
            price = _decimal_do_front(item.get("preco_unitario"))
            if not price.is_finite() or price <= 0 or price > Decimal("1e12"):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise ValidationError("Confira os preços da venda.")
        units = {str(product.unidade_venda_1 or product.unidade_compra or "").strip().upper()} if product else set()
        if product and product.vende_fracionado:
            units.add(str(product.unidade_venda_2 or "").strip().upper())
        cost = _custo_produto_para_unidade_venda(product, unit) if product and unit and unit in units else None
        available = cost is not None and cost > 0
        result.append({"produto_id": str(item["produto_id"]), "nome": product.nome if product else "Produto indisponível",
                       "unidade": unit, "preco": str(price), "custo": str(cost) if available else None,
                       "custo_base": str(product.preco_compra) if product and product.preco_compra is not None else "",
                       "custo_fracionado": str(product.preco_compra_fracionado) if product and product.preco_compra_fracionado is not None else "",
                       "estado": ("abaixo" if price < cost else "ok") if available else "indisponivel",
                       "editar_url": reverse("estoque:produto_editar", args=[product.pk]) if product else None})
    return result
