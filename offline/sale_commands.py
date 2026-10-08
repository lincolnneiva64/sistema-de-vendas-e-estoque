"""Protocolo de criação; regras comerciais pertencem ao serviço oficial."""
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from estoque.models import Produto
from estoque.services.vendas import ErroGravarVenda, criar_ou_atualizar_venda

SALE_OPERATION_TYPE = "criar_venda"
CALCULATED_FIELDS = {"total", "subtotal", "estoque", "custo", "saldo", "valor_total"}
SALE_REQUIRED = {"schema_version", "data_venda", "tipo_pagamento", "operador", "itens"}
SALE_OPTIONAL = {"cliente_id", "data_vencimento", "origem_recebimento", "revisao"}
ITEM_REQUIRED = {"produto_id", "quantidade", "unidade", "preco_unitario"}


def _identifier(value):
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < int(value) < 2**63


def _text(value, limit):
    return isinstance(value, str) and len(value) <= limit


def _number_text(value):
    if not _text(value, 80):
        return False
    try:
        return Decimal(value.replace(".", "").replace(",", ".") if "," in value else value).is_finite()
    except InvalidOperation:
        return False


def validate_sale_payload(payload):
    if not isinstance(payload, dict) or not SALE_REQUIRED <= payload.keys() or payload.keys() - (SALE_REQUIRED | SALE_OPTIONAL | CALCULATED_FIELDS):
        raise ValidationError("Conteudo de criacao de venda invalido; edicao e Pedido nao sao permitidos.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValidationError("Versao de venda incompativel.")
    if payload.get("cliente_id") not in (None, "") and not _identifier(payload["cliente_id"]):
        raise ValidationError("Cliente invalido.")
    for field, limit in (("data_venda", 10), ("tipo_pagamento", 40), ("operador", 120)):
        if not _text(payload[field], limit):
            raise ValidationError("Campo de venda invalido.")
    if "data_vencimento" in payload and not _text(payload["data_vencimento"], 10):
        raise ValidationError("Vencimento invalido.")
    items = payload["itens"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValidationError("Informe de 1 a 200 itens.")
    for item in items:
        if not isinstance(item, dict) or not ITEM_REQUIRED <= item.keys() or item.keys() - (ITEM_REQUIRED | CALCULATED_FIELDS):
            raise ValidationError("Item de criacao invalido; identificadores de edicao nao sao permitidos.")
        if not _identifier(item["produto_id"]) or not _text(item["unidade"], 40):
            raise ValidationError("Produto/unidade invalido.")
        if not all(_number_text(item[field]) for field in ("quantidade", "preco_unitario")):
            raise ValidationError("Quantidade/preco invalido.")
    if "origem_recebimento" in payload:
        origin = payload["origem_recebimento"]
        if not isinstance(origin, dict) or origin.keys() - {"caixa", "banco"} or not all(_number_text(value) for value in origin.values()):
            raise ValidationError("Origem de recebimento invalida.")
    if "revisao" in payload:
        revision = payload["revisao"]
        expected = {"original_operation_id", "original_hash", "relacao", "revisada_em"}
        if not isinstance(revision, dict) or set(revision) != expected:
            raise ValidationError("Vinculo de revisao invalido.")
        try:
            identifier = revision["original_operation_id"]
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise ValueError
            if not isinstance(revision["original_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", revision["original_hash"]):
                raise ValueError
            created = parse_datetime(revision["revisada_em"]) if isinstance(revision["revisada_em"], str) else None
            if revision["relacao"] != "revisao_de_conflito" or created is None or timezone.is_naive(created):
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError("Vinculo de revisao invalido.")


def execute_sale(payload, user):
    """Resolve IDs sob lock, elimina campos calculados e chama a regra única."""
    validate_sale_payload(payload)
    ids = {int(item["produto_id"]) for item in payload["itens"]}
    products = {
        product.pk: product for product in Produto.objects.select_for_update()
        .filter(pk__in=ids, ativo=True, excluido=False).order_by("pk")
    }
    if ids != products.keys():
        raise ErroGravarVenda("Produto informado nao foi encontrado no estoque ativo.")
    data = {field: payload[field] for field in (SALE_REQUIRED | SALE_OPTIONAL) - {"schema_version", "itens", "revisao"} if field in payload}
    data["itens"] = [
        {**{field: item[field] for field in ITEM_REQUIRED},
         "produto_nome": products[int(item["produto_id"])].nome}
        for item in payload["itens"]
    ]
    return criar_ou_atualizar_venda(usuario=user, dados=data)
