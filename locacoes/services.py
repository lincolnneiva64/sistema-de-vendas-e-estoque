from urllib.parse import quote, urlencode
import math

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from estoque.models import Funcionario

from .models import EventoLocacao, Locacao, TarefaOperacionalLocacao


STATUS_LOCACAO_ENCERRADOS = {
    Locacao.STATUS_CANCELADA,
    Locacao.STATUS_DEVOLVIDA,
    Locacao.STATUS_DEVOLVIDA_COM_AVARIA,
}

RESPONSAVEIS_REGULARIZACAO = (
    "Francisco Miranda", "Lincoln Albuquerque Neiva", "Roseli da Costa Gama",
)


def funcionarios_operacionais_locacoes():
    return Funcionario.objects.filter(ativo=True).filter(
        Q(pode_receber_checklist=True) | Q(pode_operar_sistema=True),
    ).order_by("nome", "pk")


def indicador_operacional_locacao(locacao, hoje=None):
    hoje = hoje or timezone.localdate()
    if locacao.status in {Locacao.STATUS_DEVOLVIDA, Locacao.STATUS_DEVOLVIDA_COM_AVARIA}:
        ultima = next(iter(locacao.conferencias_recolhimento.all()), None)
        return {"texto": "Devolucao concluida", "data": ultima.criado_em.date() if ultima else None,
                "horario": None, "atraso": False}
    if locacao.status == Locacao.STATUS_CANCELADA:
        return {"texto": "Cancelada", "data": None, "horario": None, "atraso": False}
    entrega = locacao.status in {Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA}
    tipo = "entrega" if entrega else "recolhimento"
    tarefa = next((t for t in locacao.tarefas_operacionais.all() if t.tipo == tipo), None)
    data = tarefa.data_agendada if tarefa else (locacao.data_entrega if entrega else locacao.data_prevista_devolucao)
    horario = (tarefa.horario_agendado if tarefa else locacao.horario_entrega) if entrega else None
    atraso = data < hoje or (data == hoje and horario is not None and horario <= timezone.localtime().time())
    texto = "Entrega prevista" if entrega else "Aguardando recolhimento"
    return {"texto": ("Atraso na entrega" if entrega else "Atraso na devolucao") if atraso else texto,
            "data": data, "horario": horario, "atraso": atraso}


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


def envios_tarefa_operacional_context(request, tarefa, incluir_responsaveis=False):
    rota = (
        "locacoes:conferencia_entrega"
        if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA
        else "locacoes:conferencia_recolhimento"
    )
    checklist_path = reverse(rota, kwargs={"pk": tarefa.pk})
    checklist_url = request.build_absolute_uri(checklist_path)
    funcionarios = []
    candidatos = (Funcionario.objects.filter(ativo=True).order_by("-pode_receber_checklist", "nome")
                  if incluir_responsaveis else Funcionario.habilitados_para_checklist())
    for funcionario in candidatos:
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
    if incluir_responsaveis:
        funcionario_padrao = next((f for f in funcionarios if f["nome"].casefold() in {
            tarefa.tentativa_por.casefold(), tarefa.confirmado_por.casefold(),
        }), funcionario_padrao or next(iter(funcionarios), None))
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


def ordenar_itens_operacionais_rapidos(itens, data_referencia):
    def chave_ordenacao(item):
        data_agendada = item["tarefa"].data_agendada
        if item["atrasada"]:
            prioridade = 0
        elif data_agendada == data_referencia:
            prioridade = 1
        elif data_agendada > data_referencia:
            prioridade = 3
        else:
            prioridade = 2
        return (
            prioridade,
            data_agendada if prioridade == 3 else data_referencia,
            item["horario"] is None,
            item["horario"] or timezone.datetime.max.time(),
            data_agendada,
            item["locacao"].id,
        )

    return sorted(
        itens,
        key=chave_ordenacao,
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


def historico_operacional_do_dia(data_referencia):
    """Consulta encerramentos registrados, sem criar ou modificar tarefas."""
    fisicas = []
    administrativas = []
    confirmadas_sem_checklist = []

    def item(locacao, tipo, instante, responsavel="", motivo=""):
        return {
            "locacao": locacao,
            "cliente": locacao.nome_contratante,
            "tipo": tipo,
            "instante": instante,
            "materiais": resumo_materiais_compacto(materiais_locacao(locacao)),
            "status": "Entregue" if tipo == "entrega" else "Recolhido",
            "responsavel": responsavel,
            "motivo": motivo,
        }

    tarefas = (
        TarefaOperacionalLocacao.objects.select_related("locacao__cliente")
        .prefetch_related("locacao__itens")
    )
    for tarefa in tarefas.filter(
        status=TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        confirmado_em__date=data_referencia,
    ).order_by("confirmado_em", "id"):
        fisicas.append(item(tarefa.locacao, tarefa.tipo, tarefa.confirmado_em, tarefa.confirmado_por))

    for tarefa in tarefas.filter(
        status=TarefaOperacionalLocacao.STATUS_RESOLVIDA_ADMIN,
        resolvida_em__date=data_referencia,
    ).order_by("resolvida_em", "id"):
        administrativas.append(item(
            tarefa.locacao, tarefa.tipo, tarefa.resolvida_em,
            tarefa.resolvida_por, tarefa.motivo_resolucao,
        ))

    for tarefa in tarefas.filter(
        status=TarefaOperacionalLocacao.STATUS_CONFIRMADA_SEM_CHECKLIST,
        confirmado_em__date=data_referencia,
    ).order_by("confirmado_em", "id"):
        registro = item(tarefa.locacao, tarefa.tipo, tarefa.confirmado_em, tarefa.confirmado_por)
        registro["status"] = "Entrega confirmada sem checklist fisico"
        confirmadas_sem_checklist.append(registro)

    # Operacoes antigas feitas diretamente na locacao tambem registram eventos.
    # A tarefa confirmada e a fonte principal; nao duplicar seu evento.
    eventos = (
        EventoLocacao.objects.filter(criado_em__date=data_referencia, tipo__in=["entregue", "devolucao"])
        .select_related("locacao__cliente")
        .prefetch_related("locacao__itens", "locacao__tarefas_operacionais", "locacao__eventos")
        .order_by("criado_em", "id")
    )
    vistos = {(registro["locacao"].pk, registro["tipo"]) for registro in fisicas}
    for evento in eventos:
        tipo = "entrega" if evento.tipo == "entregue" else "recolhimento"
        chave = (evento.locacao_id, tipo)
        if chave in vistos or any(
            tarefa.tipo == tipo and tarefa.status in {
                TarefaOperacionalLocacao.STATUS_CONFIRMADA,
                TarefaOperacionalLocacao.STATUS_RESOLVIDA_ADMIN,
                TarefaOperacionalLocacao.STATUS_CONFIRMADA_SEM_CHECKLIST,
            }
            for tarefa in evento.locacao.tarefas_operacionais.all()
        ):
            continue
        if tipo == "recolhimento":
            # Uma devolucao parcial nao e um recolhimento concluido.
            if evento.locacao.status not in {Locacao.STATUS_DEVOLVIDA, Locacao.STATUS_DEVOLVIDA_COM_AVARIA}:
                continue
            ultimo = next((e for e in evento.locacao.eventos.all() if e.tipo == "devolucao"), None)
            if ultimo is None or ultimo.pk != evento.pk:
                continue
        fisicas.append(item(evento.locacao, tipo, evento.criado_em, evento.responsavel))
        vistos.add(chave)
    fisicas.sort(key=lambda registro: registro["instante"])
    return {
        "fisicas": fisicas,
        "administrativas": administrativas,
        "confirmadas_sem_checklist": confirmadas_sem_checklist,
    }


def prazo_operacional_label(horario_ms, agora_ms, *, tarefa_data, hoje):
    if horario_ms is None:
        return "Atrasada - sem horario definido" if tarefa_data < hoje else "Sem horario definido"
    diferenca = (horario_ms - agora_ms) / 1000
    if diferenca == 0:
        return "Agora"
    minutos = math.ceil(diferenca / 60) if diferenca > 0 else math.floor(-diferenca / 60)
    dias, resto = divmod(minutos, 1440)
    horas, minutos = divmod(resto, 60)
    partes = ([f"{dias}d"] if dias else []) + ([f"{horas}h"] if horas else []) + ([f"{minutos}min"] if minutos else [])
    texto = "".join(partes) or "menos de 1min"
    return ("Atrasada " if diferenca < 0 else "") + texto


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
                tarefa, incluir_responsaveis=True,
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
            tarefa, incluir_responsaveis=True,
        )
        itens.append(item)
        ids_existentes.add(tarefa.pk)

    # Recolhimentos parciais continuam visiveis mesmo quando reagendados.
    # O saldo pendente e a nova data permanecem definidos pela conferencia.
    for tarefa in (
        TarefaOperacionalLocacao.objects
        .select_related("locacao", "locacao__cliente", "locacao__faixa_preco")
        .prefetch_related("locacao__itens")
        .filter(tipo=TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
                status=TarefaOperacionalLocacao.STATUS_PARCIAL,
                data_agendada__gt=data_referencia,
                locacao__status__in=[Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO])
    ):
        if tarefa.pk in ids_existentes:
            continue
        item = tarefa_para_item_checklist(tarefa, data_referencia)
        item["tipo"] = tarefa.tipo
        item["tipo_label"] = "Recolhimento"
        item["materiais_resumo"] = resumo_materiais_compacto(item["materiais"])
        item["envio_operacional"] = envios_tarefa_operacional_context(request, tarefa, incluir_responsaveis=True)
        item["data_label"] = "Amanh\u00e3" if tarefa.data_agendada == data_referencia + timedelta(days=1) else tarefa.data_agendada.strftime("%d/%m")
        itens.append(item)
        ids_existentes.add(tarefa.pk)

    # Todas as datas futuras; tarefas encerradas nunca voltam ao painel.
    for tipo, campo_data, status_locacao in (
        (TarefaOperacionalLocacao.TIPO_ENTREGA, "data_entrega",
         [Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA]),
        (TarefaOperacionalLocacao.TIPO_RECOLHIMENTO, "data_prevista_devolucao",
         [Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO]),
    ):
        encerradas = TarefaOperacionalLocacao.objects.filter(tipo=tipo).exclude(
            status__in=[TarefaOperacionalLocacao.STATUS_PENDENTE,
                        TarefaOperacionalLocacao.STATUS_PARCIAL,
                        TarefaOperacionalLocacao.STATUS_NAO_POSSIVEL])
        locacoes_futuras = (
            Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens")
            .filter(**{campo_data + "__gt": data_referencia}, status__in=status_locacao)
            .exclude(pk__in=encerradas.values("locacao_id"))
        )
        for locacao in locacoes_futuras:
            tarefa = obter_ou_criar_tarefa_operacional(locacao, tipo)
            if tarefa.pk in ids_existentes or not tarefa.pendente_operacional:
                continue
            item = tarefa_para_item_checklist(tarefa, data_referencia)
            item["tipo"] = tipo
            item["tipo_label"] = "Entrega" if tipo == TarefaOperacionalLocacao.TIPO_ENTREGA else "Recolhimento"
            item["materiais_resumo"] = resumo_materiais_compacto(item["materiais"])
            item["envio_operacional"] = envios_tarefa_operacional_context(request, tarefa, incluir_responsaveis=True)
            item["data_label"] = "Amanh\u00e3" if tarefa.data_agendada == data_referencia + timedelta(days=1) else tarefa.data_agendada.strftime("%d/%m")
            itens.append(item)
            ids_existentes.add(tarefa.pk)

    funcionarios_operacionais = list(funcionarios_operacionais_locacoes())
    for item in itens:
        item["funcionarios_operacionais"] = funcionarios_operacionais
        tarefa = item["tarefa"]
        item["responsaveis_regularizacao"] = RESPONSAVEIS_REGULARIZACAO
        item["responsavel_regularizacao"] = next((nome for nome in (
            tarefa.tentativa_por, tarefa.confirmado_por, tarefa.resolvida_por,
        ) if nome in RESPONSAVEIS_REGULARIZACAO), "")
        item["data_efetiva_padrao"] = min(tarefa.data_agendada, timezone.localdate())
        item["entrega_antiga"] = (
            item["tipo"] == TarefaOperacionalLocacao.TIPO_ENTREGA
            and item["tarefa"].data_agendada < data_referencia - timedelta(days=2)
        )
    itens = ordenar_itens_operacionais_rapidos(itens, data_referencia)
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
    agora = timezone.localtime(agora or timezone.now())
    hora_atual = (
        agora.time()
        if checklist["data"] == agora.date()
        else None
    )
    for item in itens:
        vencida_no_horario = (
            not item["atrasada"]
            and item["tarefa"].data_agendada == checklist["data"]
            and item["horario"] is not None
            and hora_atual is not None
            and item["horario"] <= hora_atual
        )

        item["vencida"] = item["atrasada"] or vencida_no_horario

        item["proxima"] = (
            not item["vencida"]
            and item["tarefa"].data_agendada == checklist["data"]
            and item["horario"] is not None
            and hora_atual is not None
            and item["horario"] >= hora_atual
            and (
                timezone.datetime.combine(checklist["data"], item["horario"])
                - timezone.datetime.combine(checklist["data"], hora_atual)
            ).total_seconds() <= 60 * 60
        )

        # Janela exclusiva da aba lateral; preserva a antecedência dos cards.
        item["horario_lateral"] = (
            timezone.make_aware(
                timezone.datetime.combine(item["tarefa"].data_agendada, item["horario"])
            ).timestamp() * 1000
            if item["horario"] is not None else None
        )
        item["prazo_label"] = prazo_operacional_label(
            item["horario_lateral"], agora.timestamp() * 1000,
            tarefa_data=item["tarefa"].data_agendada, hoje=data_referencia)
        item["advertencia_lateral"] = (
            item["proxima"]
            and (timezone.datetime.combine(checklist["data"], item["horario"])
                 - timezone.datetime.combine(checklist["data"], hora_atual)
                 ).total_seconds() <= 30 * 60
        )

    itens.sort(key=lambda item: (
        0 if item["vencida"] else (1 if item["tarefa"].data_agendada == data_referencia else 2),
        item["tarefa"].data_agendada,
        item["horario"] is None, item["horario"] or timezone.datetime.max.time(),
        item["locacao"].pk,
    ))
    for indice, item in enumerate(itens, start=1):
        item["ordem_operacional"] = indice

    return {
        "data": checklist["data"],
        "itens": itens,
        "total": sum(not item["entrega_antiga"] for item in itens),
        "total_entregas": len(entregas),
        "total_pendencias": len(itens),
        "total_recolhimentos": len(recolhimentos),
        "total_entregas_antigas": sum(item["entrega_antiga"] for item in itens),
        "alerta": any(item["vencida"] and not item["entrega_antiga"] for item in itens),
        "advertencia_lateral": any(item["advertencia_lateral"] and not item["entrega_antiga"] for item in itens),
    }
