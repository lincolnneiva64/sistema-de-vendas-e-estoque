"""Formatação monetária explícita pt-BR, independente do idioma do navegador."""
from decimal import Decimal

from django import template
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import json_script

register = template.Library()


@register.filter
def quantidade_br(valor):
    numero = format(Decimal(valor or 0), ',f')
    if '.' in numero:
        numero = numero.rstrip('0').rstrip('.')
    return numero.translate(str.maketrans(',.', '.,'))


class _JSONCompacto(DjangoJSONEncoder):
    def __init__(self, *args, **kwargs):
        kwargs['separators'] = (',', ':')
        super().__init__(*args, **kwargs)


@register.filter
def json_grafico(valor, identificador):
    return json_script(valor, identificador, encoder=_JSONCompacto)


@register.filter
def valor_evolucao(valor, analise):
    if not analise or analise.get('medida', 'faturamento') == 'faturamento':
        return moeda_br(valor)
    if analise.get('medida') == 'quantidade':
        return quantidade_br(valor) + ' ' + analise.get('unidade', '')
    casas = analise.get('casas', 3)
    if analise.get('medida') == 'vendas' and Decimal(valor or 0) % 1:
        casas = 2
    numero = format(Decimal(valor or 0), f',.{casas}f').translate(str.maketrans(',.', '.,'))
    return numero + ' ' + analise.get('unidade', '')


@register.filter
def valor_sinal_evolucao(valor, analise):
    valor = Decimal(valor or 0)
    return ('+' if valor > 0 else '-' if valor < 0 else '') + valor_evolucao(abs(valor), analise)


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
