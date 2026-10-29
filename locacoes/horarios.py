"""Apresentação dos horários existentes, sem inferir execução física."""
from datetime import date, time

from estoque.documentos_horarios import horario_documento


def instante_locacao(valor):
    texto = horario_documento(valor)
    return texto.replace(" ", " às ", 1) if texto != "Horário não registrado" else texto


def agenda_locacao(data, horario=None):
    # Datas e horas da agenda são valores civis locais, não instantes do servidor.
    if not isinstance(data, date):
        return "Data não informada"
    texto = data.strftime("%d/%m/%Y")
    if not isinstance(horario, time):
        return texto + " · horário não informado"
    return texto + " às " + horario.strftime("%H:%M:%S")
