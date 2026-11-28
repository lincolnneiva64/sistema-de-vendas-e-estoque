"""Consulta das contas de vendas já carregadas; não modifica saldos."""
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

STATUS_DIVIDAS_ABERTAS = ("aberta", "parcial")


def _resumo_financeiro(original, saldo, vencidas, vencido, primeiro_vencimento, parciais, hoje):
    """Mesmos indicadores para a consulta individual e a agregação em lote."""
    return {
        "original": original, "pago": original - saldo, "saldo": saldo,
        "vencidas": vencidas, "vencido": vencido,
        "maior_atraso": max((hoje - primeiro_vencimento).days, 0) if primeiro_vencimento else 0,
        "parciais": parciais, "data_referencia": hoje,
    }


def resumir_dividas_cliente(contas):
    hoje = timezone.localdate(timezone=ZoneInfo("America/Belem"))
    itens = []
    for conta in contas:
        if conta.status not in STATUS_DIVIDAS_ABERTAS or conta.valor_em_aberto <= 0:
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
    vencidas = [item for item in itens if item["dias_atraso"]]
    resumo = _resumo_financeiro(
        sum((item["original"] for item in itens), Decimal("0.00")),
        sum((item["saldo"] for item in itens), Decimal("0.00")),
        len(vencidas), sum((item["saldo"] for item in vencidas), Decimal("0.00")),
        min((item["vencimento"] for item in vencidas), default=None),
        sum(item["pago"] > 0 for item in itens), hoje,
    )
    return {"contas": itens, **resumo}


def resumir_dividas_clientes(clientes_ids):
    """Uma consulta agregada, sem carregar contas/recebimentos por cliente.

    Situação atual das contas de vendas, independente do período comercial.
    Balcão #1126 nunca participa da análise financeira individual.
    """
    from django.db.models import Count, F, Min, Q, Sum
    from estoque.models import ContaReceber

    ids = {pk for pk in clientes_ids if pk is not None and pk != 1126}
    if not ids:
        return {}
    hoje = timezone.localdate(timezone=ZoneInfo("America/Belem"))
    zero = Decimal("0.00")
    vencidas = Q(data_vencimento__lt=hoje)
    linhas = (ContaReceber.objects.filter(
        cliente_id__in=ids, status__in=STATUS_DIVIDAS_ABERTAS, valor_em_aberto__gt=0,
    ).values("cliente_id").annotate(
        original=Sum("valor_original"), saldo=Sum("valor_em_aberto"),
        vencidas=Count("pk", filter=vencidas),
        vencido=Sum("valor_em_aberto", filter=vencidas),
        primeiro_vencimento=Min("data_vencimento", filter=vencidas),
        parciais=Count("pk", filter=Q(valor_original__gt=F("valor_em_aberto"))),
    ).order_by())
    resumos = {pk: _resumo_financeiro(zero, zero, 0, zero, None, 0, hoje) for pk in ids}
    for linha in linhas:
        resumos[linha["cliente_id"]] = _resumo_financeiro(
            linha["original"], linha["saldo"], linha["vencidas"], linha["vencido"] or zero,
            linha["primeiro_vencimento"], linha["parciais"], hoje,
        )
    return resumos
