"""Text formatting only: never calculate quantities or authorize adjustments."""
from django import template
from django.utils.html import format_html_join

register = template.Library()


@register.filter
def composicao_curta(texto):
    return str(texto or '').partition(' = ')[0]


@register.filter
def total_curto(texto):
    return str(texto or '').partition(' = ')[2]


@register.filter
def parcelas_quantidade(texto):
    return format_html_join(' + ', '<span class="parcela-quantidade">{}</span>',
                            ((parte,) for parte in str(texto or '').split(' + ')))


@register.filter
def rotulo_simples(texto):
    return {
        'Equivalência física': 'Sem diferença',
        'Equivalência aproximada — resíduo decimal; sem falta física comprovada': 'Diferença de arredondamento',
        'Diferença física não comprovada — referência aproximada': 'Conferir diferença',
        'Resíduo decimal': 'Conferir',
        'Referência aproximada': 'Conferir',
    }.get(str(texto), texto)
