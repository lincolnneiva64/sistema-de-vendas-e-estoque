from django import template
from estoque.horarios_recebimentos import campos_recebimento, campos_fechamento

register = template.Library()


@register.inclusion_tag("estoque/includes/horarios_recebimento.html")
def horarios_recebimento(operacao, dinamico=False):
    campos = campos_recebimento(operacao)
    if dinamico:
        campos = [(rotulo, valor) for rotulo, valor in campos if rotulo != "Confirmação de envio do recibo"]
    return {"campos": campos, "dinamico": dinamico,
            "confirmacao_em": getattr(operacao, "recibo_confirmado_em", None)}


@register.inclusion_tag("estoque/includes/horarios_recebimento.html")
def horarios_fechamento(fechamento):
    return {"campos": campos_fechamento(fechamento), "fechamento": True}
