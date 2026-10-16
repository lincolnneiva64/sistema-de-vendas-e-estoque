"""Read-only grouping of rentals; never regularizes historical records."""
from datetime import date

from django.utils import timezone

from .models import Locacao, TarefaOperacionalLocacao


MARCO_OPERACIONAL = date(2026, 10, 1)
ABAS = (("andamento", "Em andamento"), ("concluidas", "Concluídas"), ("historico", "Histórico"))


def _desde_marco(instante):
    return instante is not None and timezone.localtime(instante).date() >= MARCO_OPERACIONAL


def classificar_locacao(locacao):
    """Existing tasks and physical records are prefetched by the list view.

    Mere task creation, rescheduling, financial activity and updated_at are
    deliberately not evidence of a still-active pre-cutoff rental.
    """
    if locacao.status == Locacao.STATUS_CANCELADA:
        return "historico", "Cancelada", False
    antiga = locacao.data_entrega < MARCO_OPERACIONAL
    encerrada = locacao.status in {Locacao.STATUS_DEVOLVIDA, Locacao.STATUS_DEVOLVIDA_COM_AVARIA}
    if encerrada:
        return ("historico", "Anterior ao marco operacional", False) if antiga else ("concluidas", "", False)

    tipo = (
        TarefaOperacionalLocacao.TIPO_ENTREGA
        if locacao.status in {Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA}
        else TarefaOperacionalLocacao.TIPO_RECOLHIMENTO
    )
    tarefas = list(locacao.tarefas_operacionais.all())
    tarefa = next((item for item in tarefas if item.tipo == tipo), None)
    pendente = tarefa is None or tarefa.pendente_operacional
    if antiga:
        # A documented physical operation/attempt in the current period, with
        # an outstanding operational phase, is an exception worth surfacing.
        evidencia = any(_desde_marco(item.criado_em) for item in locacao.conferencias_entrega.all())
        evidencia = evidencia or any(_desde_marco(item.criado_em) for item in locacao.conferencias_recolhimento.all())
        evidencia = evidencia or any(
            item.pendente_operacional and _desde_marco(item.tentativa_em)
            for item in tarefas
        )
        if pendente and evidencia:
            return "andamento", "Anterior ao marco: atividade operacional registrada; verificar pendência", True
        return "historico", "Anterior ao marco operacional", False
    if pendente:
        return "andamento", "", False
    # Administrative closure does not assert physical return or settle money.
    motivo = "Sem tarefa operacional aberta; encerramento físico não registrado"
    if tarefa.status == TarefaOperacionalLocacao.STATUS_RESOLVIDA_ADMIN:
        motivo = "Encerramento administrativo; status original preservado"
    return "historico", motivo, False
