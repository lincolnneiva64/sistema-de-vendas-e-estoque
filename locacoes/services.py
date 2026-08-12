from urllib.parse import quote, urlencode

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario

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


def whatsapp_web_url(telefone, texto=""):
    if not telefone and not texto:
        return ""
    url = "https://web.whatsapp.com/send"
    if telefone:
        url = f"{url}?phone={telefone}"
    elif texto:
        return f"{url}?text={quote(texto)}"
    if texto:
        url = f"{url}&text={quote(texto)}"
    return url


def telefone_funcionario_checklist(funcionario):
    telefone = Funcionario.normalizar_whatsapp(
        funcionario.telefone_whatsapp_normalizado
        or funcionario.telefone_whatsapp
        or ""
    )
    if len(telefone) in {10, 11} and not telefone.startswith("55"):
        telefone = f"55{telefone}"
    return telefone


def url_publica_checklist(request, path):
    base_url = (
        getattr(settings, "CHECKLIST_BASE_URL", "")
        or getattr(settings, "SISTEMA_ONLINE_URL", "")
    ).rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return request.build_absolute_uri(path)


def mensagem_checklist_operacional(tarefa, checklist_url):
    tipo = "Entrega" if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA else "Recolhimento"
    return "\n".join([
        f"{tipo} - {tarefa.locacao.nome_contratante}",
        "Checklist de conferencia:",
        checklist_url,
    ])


def envios_tarefa_operacional_context(request, tarefa):
    rota = (
        "locacoes:conferencia_entrega"
        if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA
        else "locacoes:conferencia_recolhimento"
    )
    checklist_path = reverse(rota, kwargs={"pk": tarefa.pk})
    checklist_url = request.build_absolute_uri(checklist_path)
    funcionarios = []
    for funcionario in Funcionario.habilitados_para_checklist():
        checklist_funcionario_path = (
            f"{checklist_path}?{urlencode({'funcionario': funcionario.pk})}"
        )
        checklist_funcionario_url = url_publica_checklist(
            request,
            checklist_funcionario_path,
        )
        telefone = telefone_funcionario_checklist(funcionario)
        mensagem = mensagem_checklist_operacional(
            tarefa,
            checklist_funcionario_url,
        )
        funcionarios.append({
            "id": funcionario.pk,
            "nome": funcionario.nome,
            "telefone": telefone,
            "telefone_exibicao": funcionario.telefone_whatsapp or telefone,
            "whatsapp_url": whatsapp_web_url(telefone, mensagem) if telefone else "",
        })
    funcionario_padrao = next(
        (funcionario for funcionario in funcionarios if funcionario["telefone"]),
        None,
    )
    return {
        "checklist_url": checklist_url,
        "funcionarios": funcionarios,
        "funcionario_padrao": funcionario_padrao,
        "tem_funcionario_com_whatsapp": any(
            funcionario["telefone"]
            for funcionario in funcionarios
        ),
    }


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
    atrasada = tarefa.data_agendada < data_referencia
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
        "conferencia_recolhimento_url": reverse(
            "locacoes:conferencia_recolhimento",
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


def ordenar_itens_operacionais_rapidos(itens):
    return sorted(
        itens,
        key=lambda item: (
            not item["atrasada"],
            item["horario"] is None,
            item["horario"] or timezone.datetime.max.time(),
            item["tarefa"].data_agendada,
            item["locacao"].id,
        ),
    )


def resumo_materiais_compacto(materiais):
    partes = [
        f"{material['quantidade']} {material['nome'].lower()}"
        for material in materiais
        if material["quantidade"]
    ]
    return ", ".join(partes) or "Sem materiais"


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

    # Cria a tarefa das locacoes cujo recolhimento
    # original ja venceu ou vence na data consultada.
    locacoes_com_recolhimento_vencido = (
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

    for locacao in locacoes_com_recolhimento_vencido:
        obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

    tarefas_recolhimento = (
        TarefaOperacionalLocacao.objects
        .select_related(
            "locacao",
            "locacao__cliente",
            "locacao__faixa_preco",
        )
        .prefetch_related("locacao__itens")
        .filter(
            tipo=TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
            status__in=[
                TarefaOperacionalLocacao.STATUS_PENDENTE,
                TarefaOperacionalLocacao.STATUS_PARCIAL,
                TarefaOperacionalLocacao.STATUS_NAO_POSSIVEL,
            ],
            data_agendada__lte=data_referencia,
            locacao__status__in=[
                Locacao.STATUS_ENTREGUE,
                Locacao.STATUS_PENDENTE_DEVOLUCAO,
            ],
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

    for tarefa in tarefas_recolhimento:
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
    tem_atrasada = any(
        item["atrasada"]
        for item in (
            grupos["entregas"]
            + grupos["recolhimentos"]
            + grupos["devolucoes_atrasadas"]
        )
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


def painel_operacional_rapido_locacoes(request, data_referencia=None, agora=None):
    data_referencia = data_referencia or timezone.localdate()
    locacoes_com_entrega_atrasada = (
        Locacao.objects
        .select_related("cliente", "faixa_preco")
        .prefetch_related("itens")
        .filter(
            data_entrega__lt=data_referencia,
            status__in=[
                Locacao.STATUS_RESERVADA,
                Locacao.STATUS_SAIU_PARA_ENTREGA,
            ],
        )
    )
    for locacao in locacoes_com_entrega_atrasada:
        obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

    checklist = checklist_operacional_locacoes(
        data_referencia=data_referencia,
        agora=agora,
    )
    itens = []
    for chave in ("entregas", "recolhimentos", "devolucoes_atrasadas"):
        for item in checklist["grupos"][chave]:
            item = dict(item)
            tarefa = item["tarefa"]
            item["tipo"] = tarefa.tipo
            item["tipo_label"] = (
                "Entrega"
                if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA
                else "Recolhimento"
            )
            item["materiais_resumo"] = resumo_materiais_compacto(item["materiais"])
            item["envio_operacional"] = envios_tarefa_operacional_context(
                request,
                tarefa,
            )
            itens.append(item)

    ids_existentes = {item["tarefa"].id for item in itens}
    tarefas_entrega_atrasadas = (
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
            data_agendada__lt=data_referencia,
            locacao__status__in=[
                Locacao.STATUS_RESERVADA,
                Locacao.STATUS_SAIU_PARA_ENTREGA,
            ],
        )
    )
    for tarefa in tarefas_entrega_atrasadas:
        if tarefa.id in ids_existentes or not tarefa.pendente_operacional:
            continue
        item = tarefa_para_item_checklist(tarefa, data_referencia)
        item["tipo"] = tarefa.tipo
        item["tipo_label"] = "Entrega"
        item["materiais_resumo"] = resumo_materiais_compacto(item["materiais"])
        item["envio_operacional"] = envios_tarefa_operacional_context(
            request,
            tarefa,
        )
        itens.append(item)

    itens = ordenar_itens_operacionais_rapidos(itens)
    for indice, item in enumerate(itens, start=1):
        item["ordem_operacional"] = indice

    entregas = [
        item
        for item in itens
        if item["tipo"] == TarefaOperacionalLocacao.TIPO_ENTREGA
    ]
    recolhimentos = [
        item
        for item in itens
        if item["tipo"] == TarefaOperacionalLocacao.TIPO_RECOLHIMENTO
    ]
    agora = agora or timezone.localtime()
    hora_atual = (
        agora.time()
        if checklist["data"] == agora.date()
        else None
    )
    for item in itens:
        item["proxima"] = (
            not item["atrasada"]
            and item["horario"] is not None
            and hora_atual is not None
            and item["horario"] >= hora_atual
            and (
                timezone.datetime.combine(checklist["data"], item["horario"])
                - timezone.datetime.combine(checklist["data"], hora_atual)
            ).total_seconds() <= 60 * 60
        )

    return {
        "data": checklist["data"],
        "itens": itens,
        "total": len(itens),
        "total_entregas": len(entregas),
        "total_recolhimentos": len(recolhimentos),
        "alerta": any(item["atrasada"] or item["proxima"] for item in itens),
    }
