"""Format only readonly fields explicitly mapped by the selected ModelAdmin."""
from django import template
from django.utils.html import format_html
from estoque.admin_horarios import horario_admin

register = template.Library()


@register.simple_tag
def instante_horario_admin(valor):
    return horario_admin(valor, '-')


@register.simple_tag
def conteudo_horario_admin(field):
    nome = field.field['name'] if field.is_readonly else None
    if nome in getattr(field.model_admin, 'horarios_admin', {}):
        return horario_admin(getattr(field.form.instance, nome), field.empty_value_display)
    return field.contents()


@register.simple_tag
def rotulo_horario_admin(field):
    if field.is_readonly:
        rotulo = getattr(field.model_admin, 'horarios_admin', {}).get(field.field['name'])
        if rotulo:
            if field.is_first:
                return format_html('<label>{}:</label>', rotulo)
            return format_html('<label class="inline">{}:</label>', rotulo)
    return field.label_tag()
