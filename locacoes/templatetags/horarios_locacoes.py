from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def agenda_locacao(data, horario=None):
    from locacoes.horarios import agenda_locacao as formatar
    return format_html(
        '<strong style="color:#1e293b;font-size:12px;font-weight:700;overflow-wrap:anywhere" '
        'title="Horário de Belém (PA) · America/Belem">{}</strong>', formatar(data, horario))
