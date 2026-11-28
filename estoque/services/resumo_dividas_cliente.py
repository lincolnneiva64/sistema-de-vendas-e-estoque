"""Consulta das contas de vendas já carregadas; não modifica saldos."""
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone


def resumir_dividas_cliente(contas):
    hoje = timezone.localdate(timezone=ZoneInfo("America/Belem"))
    itens = []
    for conta in contas:
        if conta.status not in ("aberta", "parcial") or conta.valor_em_aberto <= 0:
            continue
        atraso = max((hoje - conta.data_vencimento).days, 0) if conta.data_vencimento else 0
        itens.append({
            "id": conta.id, "venda_id": conta.venda_id,
            "data_compra": getattr(conta, "data_compra_venda", None),
            "vencimento": conta.data_vencimento, "dias_atraso": atraso,
            "original": conta.valor_original,
            # Abatimento da própria conta, inclusive saldos importados sem histórico.
            "pago": conta.valor_original - conta.valor_em_aberto,
            "saldo": conta.valor_em_aberto,
        })
    itens.sort(key=lambda item: (not bool(item["dias_atraso"]), item["vencimento"] or date.max, item["id"]))
    return {
        "contas": itens,
        "original": sum((item["original"] for item in itens), Decimal("0.00")),
        "pago": sum((item["pago"] for item in itens), Decimal("0.00")),
        "saldo": sum((item["saldo"] for item in itens), Decimal("0.00")),
        "vencidas": sum(bool(item["dias_atraso"]) for item in itens),
        "maior_atraso": max((item["dias_atraso"] for item in itens), default=0),
    }
