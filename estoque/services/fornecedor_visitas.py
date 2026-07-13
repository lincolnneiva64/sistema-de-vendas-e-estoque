from datetime import timedelta

from django.utils import timezone


def calcular_proxima_visita(fornecedor, data_base=None):
    if data_base is None:
        data_base = timezone.localdate()

    if not getattr(fornecedor, "frequencia_visita_ativa", False):
        return None

    intervalo = getattr(fornecedor, "frequencia_visita_intervalo_dias", None)
    data_referencia = getattr(fornecedor, "frequencia_visita_data_referencia", None)
    dia_semana = getattr(fornecedor, "frequencia_visita_dia_semana", None)
    if not intervalo or not data_referencia or dia_semana is None:
        return None
    if intervalo <= 0 or intervalo % 7 != 0:
        return None
    if dia_semana < 0 or dia_semana > 6:
        return None
    if data_referencia.weekday() != dia_semana:
        return None

    if data_referencia >= data_base:
        return data_referencia

    dias_passados = (data_base - data_referencia).days
    ciclos = (dias_passados + intervalo - 1) // intervalo
    return data_referencia + timedelta(days=ciclos * intervalo)
