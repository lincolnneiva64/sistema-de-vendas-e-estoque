"""Formatação monetária explícita pt-BR, independente do idioma do navegador."""
from decimal import Decimal

from django import template
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import json_script

register = template.Library()


class _JSONCompacto(DjangoJSONEncoder):
    def __init__(self, *args, **kwargs):
        kwargs['separators'] = (',', ':')
        super().__init__(*args, **kwargs)


@register.filter
def json_grafico(valor, identificador):
    # Preserve Django's script escaping and Decimal/date encoding, removing
    # only redundant JSON whitespace from long comparison series.
    return json_script(valor, identificador, encoder=_JSONCompacto)


@register.filter
def numero_br(valor):
    return format(Decimal(valor or 0), ',.2f').translate(str.maketrans(',.', '.,'))


@register.filter
def moeda_br(valor):
    return 'R$ ' + numero_br(valor)


@register.filter
def numero_sinal_br(valor):
    valor = Decimal(valor or 0)
    return ('+' if valor > 0 else '') + numero_br(valor)


@register.filter
def moeda_sinal_br(valor):
    valor = Decimal(valor or 0)
    return ('+' if valor > 0 else '-' if valor < 0 else '') + moeda_br(abs(valor))
