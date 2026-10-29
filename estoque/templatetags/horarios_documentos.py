from django import template
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html
from datetime import datetime

from estoque.documentos_horarios import campos_horarios, horario_documento

register = template.Library()
register.filter("horario_documento", horario_documento)


@register.simple_tag
def instante_registro(valor):
    """Only display aware stored timestamps, including ISO values from counting."""
    if isinstance(valor, str):
        try:
            valor = parse_datetime(valor)
        except ValueError:
            valor = None
    texto = (horario_documento(valor).replace(" ", " às ", 1)
             if isinstance(valor, datetime) and timezone.is_aware(valor)
             else "Horário não registrado")
    iso = valor.isoformat() if isinstance(valor, datetime) and timezone.is_aware(valor) else ""
    return format_html(
        '<strong class="horario-registro" style="color:#1e293b;font-size:12px;font-weight:700;overflow-wrap:anywhere" '
        'title="Horário de Belém (PA) · America/Belem"><time datetime="{}">{}</time></strong>', iso, texto)


@register.filter
def quantidade_historico(valor):
    if valor is None:
        return "Não registrada"
    texto = format(valor, "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto.replace(".", ",")


@register.inclusion_tag("estoque/includes/rodape_horarios.html")
def rodape_horarios(documento):
    return {"campos_horarios": campos_horarios(documento)}
