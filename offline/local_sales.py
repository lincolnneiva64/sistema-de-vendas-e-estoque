"""Local authorization adapter for the existing operation/receipt protocol.

This authorizes a trusted workstation, never authenticates the declared employee.
No offline operation or authenticated draft is adopted by this adapter.
"""
import copy
import hashlib
import ipaddress
import json
import os
import secrets
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
from django.urls import reverse
from django.middleware.csrf import get_token
from django.utils.cache import patch_cache_control

from estoque.models import Funcionario, Venda, Pedido, Produto
from estoque.services.vendas import criar_ou_atualizar_venda, ErroGravarVenda

COOKIE = "local_sales_owner"
SALT = "normal-sales-owner-v1"
AGE = 365 * 24 * 3600


def permitted(request):
    if not getattr(settings, "LOCAL_SALES_LOOPBACK", False):
        return False
    if any(os.environ.get(key) for key in ("RENDER", "RENDER_SERVICE_ID")):
        return False
    if any(key in request.META for key in ("HTTP_FORWARDED", "HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PROTO")):
        return False
    try:
        peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
        host = urlsplit("//" + request.get_host()).hostname
        return peer.is_loopback and host in {"127.0.0.1", "localhost", "::1"}
    except (ValueError, ValidationError):
        return False


class LocalSalesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.local_sales_owner = None
        new_cookie = None
        relevant = request.path in {"/vendas/", "/api/vendas/online/"} or request.path.startswith("/api/vendas/online/")
        if relevant and permitted(request):
            try:
                principal = signing.loads(request.COOKIES.get(COOKIE, ""), salt=SALT, max_age=AGE)
                if not isinstance(principal, dict) or len(principal.get("nonce", "")) != 64:
                    raise signing.BadSignature()
            except (signing.BadSignature, TypeError, ValueError):
                principal = None
            user_id = str(request.user.pk) if request.user.is_authenticated else ""
            # An absent/expired Django session does not rotate this principal.
            # A positively authenticated different user creates a separate scope.
            changed = principal and user_id and user_id != principal.get("user", "")
            if principal is None or changed:
                if request.path == "/vendas/":
                    principal = {"nonce": principal["nonce"] if principal else secrets.token_hex(32), "user": user_id}
                    new_cookie = signing.dumps(principal, salt=SALT)
                else:
                    principal = None
            if principal:
                environment = settings.OFFLINE_ENVIRONMENT_ID or request.get_host().lower()
                request.local_sales_owner = hashlib.sha256((environment + ":" + principal["nonce"] + ":" + principal.get("user", "")).encode()).hexdigest()
        response = self.get_response(request)
        if request.local_sales_owner:
            patch_cache_control(response, no_store=True, private=True)
        if new_cookie:
            response.set_cookie(COOKIE, new_cookie, max_age=AGE, httponly=True, samesite="Strict", secure=request.is_secure())
        return response


def revision_for(data, *, lock=False):
    model, identifier = (Venda, data.get("venda_id")) if data.get("venda_id") else (Pedido, data.get("pedido_id"))
    if not identifier:
        return ""
    query = model.objects.select_for_update() if lock else model.objects
    obj = query.filter(pk=identifier).first()
    if not obj:
        raise ErroGravarVenda("Venda/Pedido nao encontrado.", status=404)
    # Lock the aggregate before resolving its items and before the sale service.
    state = {"record": model.objects.filter(pk=obj.pk).values().get(), "items": list(obj.itens.order_by("pk").values())}
    return hashlib.sha256(json.dumps(state, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def page_context(request):
    owner = getattr(request, "local_sales_owner", None)
    if not owner:
        return None
    from .views import environment_id
    try:
        revision = revision_for({"venda_id": request.GET.get("editar"), "pedido_id": request.GET.get("pedido_id")})
    except (ErroGravarVenda, ValueError):
        revision = "invalid"
    return {"owner": owner, "actor_id": "local:" + owner, "environment_id": environment_id(request),
            "revision": revision, "csrf": get_token(request)}


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "dados", "expected_revision"} or payload["schema_version"] != 1:
        raise ValidationError("Formato local invalido.")
    data = payload["dados"]
    allowed = {"venda_id", "ajuste_separacao_id", "pedido_id", "cliente_id", "data_venda", "data_vencimento", "tipo_pagamento", "operador", "total", "itens", "next", "origem_recebimento"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValidationError("Campos de venda invalidos.")
    if not isinstance(data.get("operador"), str) or not data["operador"].strip() or len(data["operador"]) > 140:
        raise ValidationError("Selecione o funcionario operador.")
    for field in ("venda_id", "pedido_id", "cliente_id", "ajuste_separacao_id"):
        value = data.get(field)
        if value not in (None, "") and (isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or not 0 < int(value) < 2**63):
            raise ValidationError("Identificador invalido.")
    if data.get("venda_id") and data.get("pedido_id"):
        raise ValidationError("Edicao e Pedido sao contextos distintos.")
    revision = payload["expected_revision"]
    if not isinstance(revision, str) or (bool(data.get("venda_id") or data.get("pedido_id")) and (len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision))):
        raise ValidationError("Versao da venda/Pedido invalida.")
    items = data.get("itens")
    if not isinstance(items, list) or not 1 <= len(items) <= 200 or any(not isinstance(item, dict) for item in items):
        raise ValidationError("Itens invalidos.")
    for item in items:
        if set(item) - {"item_id", "produto_id", "produto_nome", "quantidade", "unidade", "preco_unitario", "valor_total", "subtotal", "estoque_antes", "estoque_depois", "estoque_movimentado", "estoque_unidade_snapshot", "custo_unitario_snapshot", "custo_total_snapshot", "custo_unidade_snapshot", "custo_origem_snapshot", "estoque_conferido"}:
            raise ValidationError("Campos de item invalidos.")
        if not str(item.get("produto_id", "")).isdigit():
            raise ValidationError("Produto invalido.")


def execute_local_sale(payload, user):
    validate_payload(payload)
    data = copy.deepcopy(payload["dados"])
    if revision_for(data, lock=True) != payload["expected_revision"]:
        raise ErroGravarVenda("Venda/Pedido alterado em outra aba. Reabra antes de salvar.", status=409)
    operators = list(Funcionario.objects.filter(nome=data["operador"], ativo=True, pode_operar_sistema=True)[:2])
    if len(operators) != 1:
        raise ErroGravarVenda("Funcionario operador invalido ou ambiguo.")
    ids = {int(item["produto_id"]) for item in data["itens"]}
    products = {p.pk: p for p in Produto.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    if ids != products.keys():
        raise ErroGravarVenda("Produto nao encontrado.")
    # Never trust names, totals or stock snapshots supplied by the client.
    data["itens"] = [{**{key: item[key] for key in ("item_id", "produto_id", "quantidade", "unidade", "preco_unitario") if key in item},
                      "produto_nome": products[int(item["produto_id"])].nome} for item in data["itens"]]
    data.pop("total", None)
    return criar_ou_atualizar_venda(usuario=user, dados=data)


def submit(request):
    owner = getattr(request, "local_sales_owner", None)
    if not permitted(request) or not owner:
        return JsonResponse({"erro": "Envio local nao autorizado."}, status=403)
    from .services import validate_command, process_operation
    from .views import environment_id
    try:
        if len(request.body) > 100000:
            raise ValidationError("Comando muito grande.")
        command = validate_command(json.loads(request.body), request.user, environment_id(request), local_owner=owner)
        receipt, status = process_operation(command, request.user, local_owner=owner)
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        return JsonResponse({"erro": "Comando local invalido."}, status=400)
    return sale_response(request, receipt, status)


def sale_response(request, receipt, status=200):
    result = {"receipt": receipt, "sucesso": receipt.get("status") == "confirmada",
              "venda_id": receipt.get("record_id"), "mensagem": receipt.get("erro", "Venda gravada com sucesso.")}
    if result["sucesso"]:
        from estoque.views import _separacao_venda_payload, _produtos_estoque_atualizados_payload
        sale = Venda.objects.get(pk=receipt["record_id"])
        result.update(visualizar_url=reverse("estoque:venda_detalhe", args=[sale.pk]),
                      separacao=_separacao_venda_payload(sale, request),
                      produtos_estoque_atualizados=_produtos_estoque_atualizados_payload(list(sale.itens.values_list("produto_id", flat=True))))
    return JsonResponse(result, status=status)



def lookup(request, operation_id):
    from .models import OperacaoSincronizacao
    from .services import command_hash
    owner = getattr(request, "local_sales_owner", None)
    if not permitted(request) or not owner:
        return JsonResponse({"erro": "Consulta local nao autorizada."}, status=403)
    operation = OperacaoSincronizacao.objects.filter(operation_id=operation_id).first()
    if operation is None:
        return JsonResponse({"lookup": "nao_encontrada"})
    if operation.origin != "local" or operation.local_owner != owner or operation.payload_hash != request.GET.get("hash") or command_hash(operation.comando) != operation.payload_hash:
        return JsonResponse({"lookup": "incompativel"}, status=409)
    return JsonResponse({"lookup": "encontrada", "receipt": operation.resultado, "sucesso": operation.status == "confirmada",
                         "venda_id": operation.resultado.get("record_id"), "visualizar_url": "/vendas/" + str(operation.resultado.get("record_id")) + "/"})
