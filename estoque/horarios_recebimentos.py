"""Read-only timestamp presentation; financial amounts and snapshots stay intact."""
from datetime import datetime
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .documentos_horarios import horario_documento


def horario_legivel(valor):
    if isinstance(valor, str):
        try:
            valor = parse_datetime(valor)
        except ValueError:
            valor = None
    if not isinstance(valor, datetime) or timezone.is_naive(valor):
        return "Horário não registrado"
    return horario_documento(valor).replace(" ", " às ", 1)


def campos_recebimento(operacao):
    campos = [("Registro no sistema", getattr(operacao, "criado_em", None))]
    if getattr(operacao, "atualizado_em", None):
        campos.append(("Última atualização do registro", operacao.atualizado_em))
    if getattr(operacao, "recibo_confirmado_em", None):
        campos.append(("Confirmação de envio do recibo", operacao.recibo_confirmado_em))
    if getattr(operacao, "recibo_dispensado_em", None):
        campos.append(("Dispensa do recibo", operacao.recibo_dispensado_em))
    return campos


def campos_fechamento(fechamento):
    return [("Registro do fechamento", getattr(fechamento, "created_at", None)),
            ("Última atualização do registro", getattr(fechamento, "updated_at", None))]


def metadados_recebimento(operacao):
    return [(rotulo, valor.isoformat() if isinstance(valor, datetime) and timezone.is_aware(valor) else None)
            for rotulo, valor in campos_recebimento(operacao)]


def campos_comprovante(dados):
    return dados.get("_horarios_registro") or [("Registro no sistema", None)]


def texto_horarios(campos):
    return [f"{rotulo}: *{horario_legivel(valor)}*" for rotulo, valor in campos] + ["Horário de Belém (PA)"]


def imagem_com_horarios(imagem, campos, fonte, fonte_negrito):
    """Append a wrapping footer, preserving every original pixel and amount."""
    from PIL import Image, ImageDraw
    margem = 14 if imagem.width < 600 else 30
    largura = imagem.width - margem * 2
    altura_linha = max(fonte.getbbox("Ag")[3], fonte_negrito.getbbox("Ag")[3]) + 8
    medida = ImageDraw.Draw(imagem)
    linhas = []

    def quebrar(texto, font):
        resultado, atual = [], ""
        for palavra in texto.split():
            proposta = (atual + " " + palavra).strip()
            if atual and medida.textlength(proposta, font=font) > largura:
                resultado.append(atual)
                atual = palavra
            else:
                atual = proposta
        return resultado + [atual]

    for rotulo, valor in campos:
        for linha in quebrar(rotulo + ":", fonte):
            linhas.append((linha, fonte))
        for linha in quebrar(horario_legivel(valor), fonte_negrito):
            linhas.append((linha, fonte_negrito))
    linhas.append(("Horário de Belém (PA)", fonte))
    resultado = Image.new("RGB", (imagem.width, imagem.height + 24 + len(linhas) * altura_linha), "white")
    resultado.paste(imagem, (0, 0))
    draw = ImageDraw.Draw(resultado)
    for indice, (linha, font) in enumerate(linhas):
        draw.text((margem, imagem.height + 12 + indice * altura_linha), linha, font=font, fill="#1e293b")
    return resultado
