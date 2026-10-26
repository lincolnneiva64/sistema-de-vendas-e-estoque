"""Atalhos de calendário da consulta; apenas preenchem os campos existentes."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone


def periodos_consulta():
    hoje = timezone.localdate(timezone=ZoneInfo("America/Belem"))
    inicio_mes = hoje.replace(day=1)
    fim_mes_passado = inicio_mes - timedelta(days=1)
    semana = hoje - timedelta(days=hoje.weekday())
    sabado = hoje - timedelta(days=(hoje.weekday() - 5) % 7)
    valores = {
        "hoje": ("Hoje", hoje, hoje),
        "ontem": ("Ontem", hoje - timedelta(days=1), hoje - timedelta(days=1)),
        "semana": ("Esta semana", semana, hoje),
        "fim_semana": ("Fim de semana", sabado, min(sabado + timedelta(days=1), hoje)),
        "mes": ("Este mês", inicio_mes, hoje),
        "mes_passado": ("Mês passado", fim_mes_passado.replace(day=1), fim_mes_passado),
    }
    return {chave: {"rotulo": rotulo, "inicio": inicio.isoformat(), "fim": fim.isoformat()}
            for chave, (rotulo, inicio, fim) in valores.items()}
