from django.urls import reverse
from django.utils import timezone

from .models import Locacao, TarefaOperacionalLocacao


STATUS_LOCACAO_ENCERRADOS = {
    Locacao.STATUS_CANCELADA,
    Locacao.STATUS_DEVOLVIDA,
    Locacao.STATUS_DEVOLVIDA_COM_AVARIA,
}


def dados_agendamento_tarefa(locacao, tipo):
    if tipo == TarefaOperacionalLocacao.TIPO_ENTREGA:
        return locacao.data_entrega, locacao.horario_entrega
    return locacao.data_prevista_devolucao, None


def obter_ou_criar_tarefa_operacional(locacao, tipo):
    data_agendada, horario_agendado = dados_agendamento_tarefa(locacao, tipo)
    tarefa, criada = TarefaOperacionalLocacao.objects.get_or_create(
        locacao=locacao,
        tipo=tipo,
        defaults={
            "data_agendada": data_agendada,
            "horario_agendado": horario_agendado,
        },
    )
    if (
        not criada
        and tarefa.status in {
            TarefaOperacionalLocacao.STATUS_PENDENTE,
            TarefaOperacionalLocacao.STATUS_NAO_POSSIVEL,
        }
    ):
        campos = []
        if tarefa.data_agendada != data_agendada:
            tarefa.data_agendada = data_agendada
            campos.append("data_agendada")
        if tarefa.horario_agendado != horario_agendado:
            tarefa.horario_agendado = horario_agendado
            campos.append("horario_agendado")
        if campos:
            campos.append("atualizado_em")
            tarefa.save(update_fields=campos)
    return tarefa


def tarefas_ativas_da_locacao(locacao):
    if locacao.status in STATUS_LOCACAO_ENCERRADOS:
        return []
    tarefas = []
    if locacao.status in {Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA}:
        tarefas.append(obter_ou_criar_tarefa_operacional(locacao, TarefaOperacionalLocacao.TIPO_ENTREGA))
    if locacao.status in {Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO}:
        tarefas.append(obter_ou_criar_tarefa_operacional(locacao, TarefaOperacionalLocacao.TIPO_RECOLHIMENTO))
    return [tarefa for tarefa in tarefas if tarefa.pendente_operacional]


def materiais_locacao(locacao):
    return [
        {
            "nome": item.get_tipo_display(),
            "quantidade": item.quantidade,
            "pendente": item.quantidade_pendente(),
        }
        for item in locacao.itens.all()
    ]


def ponto_referencia_locacao(locacao):
    if locacao.cliente_id and locacao.cliente:
        return locacao.cliente.referencia or ""
    return ""


def tarefa_para_item_checklist(tarefa, data_referencia):
    locacao = tarefa.locacao
    atrasada = (
        tarefa.tipo == TarefaOperacionalLocacao.TIPO_RECOLHIMENTO
        and tarefa.data_agendada < data_referencia
    )
    return {
        "tarefa": tarefa,
        "locacao": locacao,
        "cliente": locacao.nome_contratante,
        "endereco": locacao.endereco_entrega,
        "ponto_referencia": ponto_referencia_locacao(locacao),
        "telefone": locacao.telefone_contratante,
        "horario": tarefa.horario_agendado,
        "materiais": materiais_locacao(locacao),
        "observacoes": locacao.observacao,
        "atrasada": atrasada,
        "detalhe_url": reverse(
            "locacoes:detalhe",
            kwargs={"pk": locacao.pk},
        ),
        "conferencia_entrega_url": reverse(
            "locacoes:conferencia_entrega",
            kwargs={"pk": tarefa.pk},
        ),
        "confirmar_url": reverse(
            "locacoes:confirmar_tarefa_operacional",
            kwargs={"pk": tarefa.pk},
        ),
        "nao_possivel_url": reverse(
            "locacoes:tarefa_operacional_nao_possivel",
            kwargs={"pk": tarefa.pk},
        ),
    }


def ordenar_itens_operacionais(itens):
    return sorted(
        itens,
        key=lambda item: (
            item["horario"] is None,
            item["horario"] or timezone.datetime.max.time(),
            item["locacao"].id,
        ),
    )


def checklist_operacional_locacoes(
    data_referencia=None,
    agora=None,
):
    data_referencia = (
        data_referencia or timezone.localdate()
    )
    agora = agora or timezone.localtime()

    # Garante que locacoes originalmente marcadas para este dia
    # possuam uma tarefa operacional de entrega.
    locacoes_com_entrega_original_no_dia = (
        Locacao.objects
        .select_related("cliente", "faixa_preco")
        .prefetch_related("itens")
        .filter(
            data_entrega=data_referencia,
            status__in=[
                Locacao.STATUS_RESERVADA,
                Locacao.STATUS_SAIU_PARA_ENTREGA,
            ],
        )
    )

    for locacao in locacoes_com_entrega_original_no_dia:
        obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

    tarefas_entrega = (
        TarefaOperacionalLocacao.objects
        .select_related(
            "locacao",
            "locacao__cliente",
            "locacao__faixa_preco",
        )
        .prefetch_related("locacao__itens")
        .filter(
            tipo=TarefaOperacionalLocacao.TIPO_ENTREGA,
            status__in=[
                TarefaOperacionalLocacao.STATUS_PENDENTE,
                TarefaOperacionalLocacao.STATUS_PARCIAL,
                TarefaOperacionalLocacao.STATUS_NAO_POSSIVEL,
            ],
            data_agendada=data_referencia,
            locacao__status__in=[
                Locacao.STATUS_RESERVADA,
                Locacao.STATUS_SAIU_PARA_ENTREGA,
            ],
        )
    )

    locacoes_recolhimento = (
        Locacao.objects
        .select_related("cliente", "faixa_preco")
        .prefetch_related("itens")
        .exclude(status__in=STATUS_LOCACAO_ENCERRADOS)
        .filter(
            status__in=[
                Locacao.STATUS_ENTREGUE,
                Locacao.STATUS_PENDENTE_DEVOLUCAO,
            ],
            data_prevista_devolucao__lte=data_referencia,
        )
    )

    grupos = {
        "entregas": [],
        "recolhimentos": [],
        "devolucoes_atrasadas": [],
    }

    for tarefa in tarefas_entrega:
        if tarefa.pendente_operacional:
            grupos["entregas"].append(
                tarefa_para_item_checklist(
                    tarefa,
                    data_referencia,
                )
            )

    for locacao in locacoes_recolhimento:
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )
        if not tarefa.pendente_operacional:
            continue

        chave = (
            "devolucoes_atrasadas"
            if tarefa.data_agendada < data_referencia
            else "recolhimentos"
        )
        grupos[chave].append(
            tarefa_para_item_checklist(
                tarefa,
                data_referencia,
            )
        )

    for chave, itens in grupos.items():
        grupos[chave] = ordenar_itens_operacionais(itens)

    total = sum(
        len(itens)
        for itens in grupos.values()
    )
    hora_atual = (
        agora.time()
        if data_referencia == agora.date()
        else None
    )
    tem_horario_vencido = any(
        item["horario"] is not None
        and hora_atual is not None
        and item["horario"] <= hora_atual
        for item in (
            grupos["entregas"]
            + grupos["recolhimentos"]
        )
    )
    tem_atrasada = bool(
        grupos["devolucoes_atrasadas"]
    )

    return {
        "data": data_referencia,
        "grupos": grupos,
        "total": total,
        "alerta": (
            tem_horario_vencido
            or tem_atrasada
        ),
    }
