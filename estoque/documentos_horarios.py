"""Read-only presentation of persisted timestamps; never infer physical times."""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone


FUSO_DOCUMENTOS = ZoneInfo("America/Belem")


def horario_documento(valor):
    if not isinstance(valor, datetime) or timezone.is_naive(valor):
        return "Horário não registrado"
    return timezone.localtime(valor, FUSO_DOCUMENTOS).strftime("%d/%m/%Y %H:%M:%S")


def campos_horarios(documento):
    campos = [("Registro no sistema", getattr(documento, "criado_em", None))]
    entrada = getattr(documento, "estoque_entrada_realizada_em", None)
    if entrada:
        campos.append(("Entrada registrada no sistema", entrada))
    atualizacao = getattr(documento, "atualizado_em", None)
    if atualizacao:
        campos.append(("Última atualização", atualizacao))
    return [(rotulo, horario_documento(valor).replace(" ", " às ", 1)
             if isinstance(valor, datetime) and timezone.is_aware(valor)
             else "Horário não registrado") for rotulo, valor in campos]


def linhas_horarios(documento):
    return [f"{rotulo}: {valor}" for rotulo, valor in campos_horarios(documento)] + ["Horário de Belém (PA)"]


def desenhar_horarios(draw, documento, x, y, fonte, fonte_negrito):
    """Shared raster/PDF footer: dark labels and bold persisted timestamps."""
    altura_linha = max(fonte.getbbox("Ag")[3], fonte_negrito.getbbox("Ag")[3]) + 12
    for rotulo, valor in campos_horarios(documento):
        label = f"{rotulo}: "
        draw.text((x, y), label, font=fonte, fill="#334155")
        largura_label = draw.textlength(label, font=fonte)
        draw.text((x + largura_label, y), valor, font=fonte_negrito, fill="#1e293b")
        y += altura_linha
    draw.text((x, y), "Horário de Belém (PA)", font=fonte, fill="#334155")
    return y + altura_linha


def acrescentar_rodape_imagem(imagem, documento, fonte, fonte_negrito=None):
    """Extend the canvas so the footer cannot cover existing items or totals."""
    from PIL import Image, ImageDraw

    fonte_negrito = fonte_negrito or fonte
    altura_linha = max(fonte.getbbox("Ag")[3], fonte_negrito.getbbox("Ag")[3]) + 12
    resultado = Image.new("RGB", (imagem.width, imagem.height + 32 + len(linhas_horarios(documento)) * altura_linha), "#ffffff")
    resultado.paste(imagem, (0, 0))
    draw = ImageDraw.Draw(resultado)
    desenhar_horarios(draw, documento, 44, imagem.height + 16, fonte, fonte_negrito)
    return resultado
