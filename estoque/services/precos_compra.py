"""Snapshots exatos de precos alterados por compras; sem inferencia de legado."""
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import models, transaction
from django.utils import timezone

from estoque.models import ItemCompra, Produto, PRECOS_PRODUTO_CONTROLADOS


def _valor_json(valor):
    return None if valor is None else str(valor)


def aplicar_precos_compra(item, atualizacoes):
    using = item._state.db or "default"
    with transaction.atomic(using=using):
        item = ItemCompra.objects.using(using).select_for_update().get(pk=item.pk)
        produto = Produto.objects.using(using).select_for_update().get(pk=item.produto_id)
        snapshot = dict(item.alteracoes_precos or {})
        autoria = dict(produto.autoria_precos or {})
        alterados = {}
        for campo, novo in atualizacoes.items():
            if campo not in PRECOS_PRODUTO_CONTROLADOS:
                raise ValueError("Campo de preco nao permitido.")
            anterior = getattr(produto, campo)
            if anterior == novo:
                continue
            token = uuid4().hex
            registro = snapshot.get(campo)
            # Uma revisao da mesma compra pode ampliar seu efeito anterior.
            if registro and autoria.get(campo) == registro.get("alteracao_id") and _valor_json(anterior) == registro.get("novo"):
                valor_anterior = registro["anterior"]
            else:
                valor_anterior = _valor_json(anterior)
            snapshot[campo] = {"anterior": valor_anterior, "novo": _valor_json(novo), "alteracao_id": token}
            autoria[campo] = token
            alterados[campo] = novo
        if alterados:
            # Bypass restrito: valores e autoria sao gravados juntos, sob bloqueio.
            models.QuerySet.update(
                Produto.objects.using(using).filter(pk=produto.pk),
                **alterados, autoria_precos=autoria, atualizado_em=timezone.now(),
            )
            item.alteracoes_precos = snapshot
            item.save(update_fields=["alteracoes_precos"])


def restaurar_precos_compra(item, produto):
    """Usado na exclusao, com transacao e bloqueio do Produto ja adquiridos."""
    # save(update_fields=estoque) pode normalizar precos apenas em memoria.
    # A comparacao deve usar os valores efetivamente persistidos.
    produto.refresh_from_db(fields=[*PRECOS_PRODUTO_CONTROLADOS, "autoria_precos"])
    autoria = dict(produto.autoria_precos or {})
    restaurados = {}
    for campo, registro in (item.alteracoes_precos or {}).items():
        if campo not in PRECOS_PRODUTO_CONTROLADOS or not isinstance(registro, dict):
            continue
        token = registro.get("alteracao_id")
        if not token or autoria.get(campo) != token or "anterior" not in registro or "novo" not in registro:
            continue
        try:
            novo = None if registro["novo"] is None else Decimal(registro["novo"])
            anterior = None if registro["anterior"] is None else Decimal(registro["anterior"])
            if (novo is not None and not novo.is_finite()) or (anterior is not None and not anterior.is_finite()):
                continue
        except (InvalidOperation, TypeError, ValueError):
            continue
        if getattr(produto, campo) != novo:
            continue
        restaurados[campo] = anterior
        autoria.pop(campo, None)
    if restaurados:
        models.QuerySet.update(
            Produto.objects.using(produto._state.db).filter(pk=produto.pk),
            **restaurados, autoria_precos=autoria, atualizado_em=timezone.now(),
        )
        for campo, valor in restaurados.items():
            setattr(produto, campo, valor)
        produto.autoria_precos = autoria
