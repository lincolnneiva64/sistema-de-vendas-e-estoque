"""Attribute a trigger record immediately after the locked stock write.

No stock writes, transaction changes or timestamp inference. Record identity is
product + sequence; before/after checks guard that identity, without guessing documents.
Uninstrumented SQL remains explicitly unidentified and blocks inventory approval.
"""
from contextvars import ContextVar
from functools import wraps
from django.db import connections

_venda = ContextVar("inventario_origem_venda", default=None)
ORIGENS = {
    "venda": "Venda", "consumo_proprio": "Consumo próprio", "devolucao_venda": "Devolução/estorno de venda",
    "compra": "Compra/entrada", "edicao_compra": "Edição/correção de compra", "estorno_compra": "Estorno de compra",
    "ajuste_manual": "Ajuste manual", "sincronizacao_firebird": "Sincronização Firebird",
    "nao_identificada": "Origem não identificada",
}


def contexto_venda(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        dados = kwargs.get("dados", {})
        tipo = "consumo_proprio" if "consumo" in str(dados.get("tipo_pagamento", "")).lower() else "venda"
        token = _venda.set({"origem": tipo, "referencia": f"Venda #{dados['venda_id']}" if dados.get("venda_id") else "Gravação de venda", "registros": []})
        try:
            return func(*args, **kwargs)
        finally:
            _venda.reset(token)
    return wrapped


def identificar(produto_id, antes, depois, origem, referencia="", using="default"):
    if antes == depois or not connections[using].in_atomic_block:
        return  # Cannot safely identify a nontransactional write; retain unknown origin.
    if origem == "venda" and _venda.get():
        origem, referencia = _venda.get()["origem"], _venda.get()["referencia"]
    if origem not in ORIGENS or origem == "nao_identificada":
        raise ValueError("Origem digital inválida.")
    from .models import MovimentoObservado, SequenciaEstoque
    atual = SequenciaEstoque.objects.using(using).filter(produto_id=produto_id).values_list("sequencia", flat=True).first()
    # Caller holds the existing product row lock until transaction completion.
    # NULL baselines are deliberately not attributed from a normalized zero.
    queryset = MovimentoObservado.objects.using(using).filter(produto_id=produto_id, sequencia=atual, antes=antes, depois=depois,
        origem="nao_identificada")
    ids = list(queryset.values_list("pk", flat=True))
    queryset.update(origem=origem, referencia=str(referencia)[:160])
    if _venda.get() is not None:
        _venda.get()["registros"].extend(ids)


def referenciar_venda(venda):
    contexto = _venda.get()
    if contexto is None or not connections["default"].in_atomic_block:
        return
    from .models import MovimentoObservado
    contexto["referencia"] = f"Venda #{venda.pk}"
    MovimentoObservado.objects.filter(pk__in=contexto["registros"]).update(referencia=contexto["referencia"])
