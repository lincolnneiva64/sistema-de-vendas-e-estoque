from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from estoque.models import EnvioListaCompraFornecedor, Fornecedor, ListaCompraFornecedor, ResolucaoVisitaFornecedor
from estoque.services.fornecedor_visitas import calcular_proxima_visita


DIAS_ANTECEDENCIA_AVISO_VISITA = 1
HORA_LIMITE_AVISO_VISITA = time(18, 0)

ESTADO_PREPARAR_LISTA = "preparar_lista"
ESTADO_LISTA_PREPARADA_FALTA_ENVIAR = "lista_preparada_falta_enviar"
ESTADO_LISTA_ALTERADA_FALTA_REENVIAR = "lista_alterada_falta_reenviar"
ESTADO_LISTA_ALTERADA_FALTA_REENVIAR = "lista_alterada_falta_reenviar"


def _configuracao_visita_valida(fornecedor):
    intervalo = fornecedor.frequencia_visita_intervalo_dias
    dia_semana = fornecedor.frequencia_visita_dia_semana
    data_referencia = fornecedor.frequencia_visita_data_referencia
    return (
        fornecedor.frequencia_visita_ativa
        and intervalo
        and intervalo > 0
        and intervalo % 7 == 0
        and dia_semana is not None
        and 0 <= dia_semana <= 6
        and data_referencia is not None
        and data_referencia.weekday() == dia_semana
    )


def _ciclo_atual_ou_anterior(fornecedor, data_referencia):
    data_base = fornecedor.frequencia_visita_data_referencia
    intervalo = fornecedor.frequencia_visita_intervalo_dias
    if data_base > data_referencia:
        return None
    dias_passados = (data_referencia - data_base).days
    ciclos = dias_passados // intervalo
    return data_base + timedelta(days=ciclos * intervalo)


def _candidatos_de_visita(fornecedor, data_referencia):
    proxima = calcular_proxima_visita(fornecedor, data_base=data_referencia)
    ciclo_atual = _ciclo_atual_ou_anterior(fornecedor, data_referencia)
    candidatos = []
    if ciclo_atual:
        candidatos.append(ciclo_atual)
        proxima_do_ciclo = ciclo_atual + timedelta(days=fornecedor.frequencia_visita_intervalo_dias)
        candidatos.append(proxima_do_ciclo)
    elif proxima:
        candidatos.append(proxima)
    if proxima:
        candidatos.append(proxima)
    return list(dict.fromkeys(candidatos))


def _data_hora_local_referencia(data_referencia=None):
    if data_referencia is None:
        return timezone.localtime()

    if isinstance(data_referencia, datetime):
        if timezone.is_naive(data_referencia):
            return timezone.make_aware(data_referencia, timezone.get_current_timezone())
        return timezone.localtime(data_referencia)

    return timezone.make_aware(
        datetime.combine(data_referencia, time.min),
        timezone.get_current_timezone(),
    )


def _inicio_janela_aviso(data_visita):
    return timezone.make_aware(
        datetime.combine(data_visita - timedelta(days=1), time.min),
        timezone.get_current_timezone(),
    )


def _fim_janela_aviso(data_visita):
    return timezone.make_aware(
        datetime.combine(data_visita, HORA_LIMITE_AVISO_VISITA),
        timezone.get_current_timezone(),
    )


def _visita_dentro_da_janela_operacional(data_visita, data_hora_referencia):
    return _inicio_janela_aviso(data_visita) <= data_hora_referencia < _fim_janela_aviso(data_visita)


def _resolucoes_visitas_por_ciclo(fornecedor_ids):
    if not fornecedor_ids:
        return {}

    resolucoes = (
        ResolucaoVisitaFornecedor.objects
        .filter(fornecedor_id__in=fornecedor_ids)
        .only(
            "fornecedor_id",
            "data_visita_original",
            "tipo_resolucao",
            "nova_data_visita",
        )
        .order_by("fornecedor_id", "data_visita_original", "-id")
    )

    por_ciclo = {}
    for resolucao in resolucoes:
        chave = (resolucao.fornecedor_id, resolucao.data_visita_original)
        if chave not in por_ciclo:
            por_ciclo[chave] = resolucao
    return por_ciclo


def _data_visita_apos_resolucoes(fornecedor_id, data_visita, resolucoes_por_ciclo):
    data_atual = data_visita
    ciclos_visitados = set()

    while data_atual:
        chave = (fornecedor_id, data_atual)
        if chave in ciclos_visitados:
            return None
        ciclos_visitados.add(chave)

        resolucao = resolucoes_por_ciclo.get(chave)
        if not resolucao:
            return data_atual

        if (
            resolucao.tipo_resolucao
            == ResolucaoVisitaFornecedor.TIPO_ADIADA
            and resolucao.nova_data_visita
        ):
            data_atual = resolucao.nova_data_visita
            continue

        return None

    return None


def datas_validas_ciclo_visita_fornecedor(
    fornecedor,
    data_referencia=None,
    resolucoes_por_ciclo=None,
):
    data_hora_referencia = _data_hora_local_referencia(data_referencia)
    data_referencia = data_hora_referencia.date()
    if not _configuracao_visita_valida(fornecedor):
        return []

    candidatos = _candidatos_de_visita(fornecedor, data_referencia)

    if resolucoes_por_ciclo is None:
        resolucoes_por_ciclo = _resolucoes_visitas_por_ciclo([fornecedor.id])

    datas_efetivas = []
    for data_visita in candidatos:
        data_efetiva = _data_visita_apos_resolucoes(
            fornecedor.id,
            data_visita,
            resolucoes_por_ciclo,
        )
        if not data_efetiva:
            continue

        dentro_da_janela = _visita_dentro_da_janela_operacional(data_efetiva, data_hora_referencia)
        if dentro_da_janela and data_efetiva not in datas_efetivas:
            datas_efetivas.append(data_efetiva)

    return datas_efetivas


def data_ciclo_visita_valida(fornecedor, data_visita, data_referencia=None):
    if not data_visita:
        return False
    return data_visita in datas_validas_ciclo_visita_fornecedor(fornecedor, data_referencia=data_referencia)


def _prioridade(dias_para_visita):
    if dias_para_visita < 0:
        return 0
    if dias_para_visita == 0:
        return 1
    if dias_para_visita == 1:
        return 2
    if dias_para_visita <= 3:
        return 3
    return 4


def _titulo_mensagem(estado, dias_para_visita):
    if estado == ESTADO_LISTA_ALTERADA_FALTA_REENVIAR:
        return "Lista alterada", "Lista alterada depois do envio, falta reenviar ao vendedor."
    if estado == ESTADO_LISTA_PREPARADA_FALTA_ENVIAR:
        return "Lista preparada", "Lista preparada, falta enviar ao vendedor."
    if dias_para_visita < 0:
        return "Visita atrasada", "Visita atrasada: lista ainda nao preparada."
    if dias_para_visita == 0:
        return "Visita hoje", "Visita prevista para hoje: prepare a lista."
    if dias_para_visita == 1:
        return "Visita amanha", "Prepare a lista para a visita de amanha."
    return f"Visita em {dias_para_visita} dias", f"Prepare a lista para a visita em {dias_para_visita} dias."


def _url_preparar_lista(fornecedor_id, data_visita):
    parametros = urlencode({
        "fornecedor": fornecedor_id,
        "fornecedor_ciclo": fornecedor_id,
        "data_visita": data_visita.isoformat(),
    })
    return f"{reverse('estoque:sugestao_compra_fornecedor')}?{parametros}"



def _estado_envio_lista(lista, ultimo_envio):
    if not lista:
        return ESTADO_PREPARAR_LISTA, False

    if not ultimo_envio:
        return ESTADO_LISTA_PREPARADA_FALTA_ENVIAR, False

    if lista.atualizado_em and ultimo_envio.confirmado_em < lista.atualizado_em:
        return ESTADO_LISTA_ALTERADA_FALTA_REENVIAR, False

    return "", True



def _estado_envio_lista(lista, ultimo_envio):
    if not lista:
        return ESTADO_PREPARAR_LISTA, False

    if not ultimo_envio:
        return ESTADO_LISTA_PREPARADA_FALTA_ENVIAR, False

    if lista.atualizado_em and ultimo_envio.confirmado_em < lista.atualizado_em:
        return ESTADO_LISTA_ALTERADA_FALTA_REENVIAR, False

    return "", True


def _montar_aviso(
    fornecedor,
    data_visita,
    lista,
    tem_envio_confirmado,
    data_referencia,
    estado_lista=None,
):
    dias_para_visita = (data_visita - data_referencia).days
    estado = estado_lista or (
        ESTADO_LISTA_PREPARADA_FALTA_ENVIAR
        if lista and not tem_envio_confirmado
        else ESTADO_PREPARAR_LISTA
    )
    titulo, mensagem = _titulo_mensagem(estado, dias_para_visita)
    if estado == ESTADO_PREPARAR_LISTA:
        acao = {
            "tipo": "preparar_lista",
            "url": _url_preparar_lista(fornecedor.id, data_visita),
            "fornecedor_id": fornecedor.id,
            "data_visita": data_visita.isoformat(),
        }
    else:
        acao = {
            "tipo": "enviar_ao_vendedor",
            "url": reverse("estoque:compras_lista_fornecedor_whatsapp", kwargs={"pk": lista.id}),
            "lista_id": lista.id,
        }
    return {
        "fornecedor_id": fornecedor.id,
        "fornecedor_nome": fornecedor.nome,
        "data_visita": data_visita.isoformat(),
        "data_visita_formatada": data_visita.strftime("%d/%m/%Y"),
        "dias_para_visita": dias_para_visita,
        "estado": estado,
        "prioridade": _prioridade(dias_para_visita),
        "titulo": titulo,
        "mensagem": mensagem,
        "lista_id": lista.id if lista else None,
        "lista_status": lista.status if lista else None,
        "tem_envio_confirmado": tem_envio_confirmado,
        "acao": acao,
    }

def obter_avisos_visitas_fornecedores(data_referencia=None):
    data_hora_referencia = _data_hora_local_referencia(data_referencia)
    data_referencia = data_hora_referencia.date()

    fornecedores = list(
        Fornecedor.objects
        .filter(ativo=True, frequencia_visita_ativa=True)
        .only(
            "id",
            "nome",
            "ativo",
            "frequencia_visita_ativa",
            "frequencia_visita_intervalo_dias",
            "frequencia_visita_dia_semana",
            "frequencia_visita_data_referencia",
        )
        .order_by("nome", "id")
    )
    fornecedores = [fornecedor for fornecedor in fornecedores if _configuracao_visita_valida(fornecedor)]
    if not fornecedores:
        return []

    resolucoes_por_ciclo = _resolucoes_visitas_por_ciclo(
        [fornecedor.id for fornecedor in fornecedores]
    )

    candidatos_por_fornecedor = {}
    datas_consulta = set()
    for fornecedor in fornecedores:
        candidatos = datas_validas_ciclo_visita_fornecedor(
            fornecedor,
            data_referencia=data_hora_referencia,
            resolucoes_por_ciclo=resolucoes_por_ciclo,
        )
        if candidatos:
            candidatos_por_fornecedor[fornecedor.id] = candidatos
            datas_consulta.update(candidatos)

    if not candidatos_por_fornecedor:
        return []

    listas_por_ciclo = {}
    listas = (
        ListaCompraFornecedor.objects
        .filter(
            fornecedor_id__in=candidatos_por_fornecedor.keys(),
            data_visita_fornecedor__in=datas_consulta,
        )
        .exclude(status=ListaCompraFornecedor.STATUS_CANCELADA)
        .order_by("fornecedor_id", "data_visita_fornecedor", "-id")
    )
    for lista in listas:
        chave = (lista.fornecedor_id, lista.data_visita_fornecedor)
        if chave not in listas_por_ciclo:
            listas_por_ciclo[chave] = lista

    ids_listas = [lista.id for lista in listas_por_ciclo.values()]
    ultimo_envio_por_lista = {}
    envios = (
        EnvioListaCompraFornecedor.objects
        .filter(lista_id__in=ids_listas)
        .only("lista_id", "confirmado_em")
        .order_by("lista_id", "-confirmado_em", "-id")
    )
    for envio in envios:
        if envio.lista_id not in ultimo_envio_por_lista:
            ultimo_envio_por_lista[envio.lista_id] = envio

    avisos = []
    for fornecedor in fornecedores:
        candidatos = candidatos_por_fornecedor.get(fornecedor.id, [])
        for data_visita in candidatos:
            lista = listas_por_ciclo.get((fornecedor.id, data_visita))
            ultimo_envio = ultimo_envio_por_lista.get(lista.id) if lista else None
            estado_lista, tem_envio_confirmado = _estado_envio_lista(lista, ultimo_envio)
            if tem_envio_confirmado:
                continue
            avisos.append(
                _montar_aviso(
                    fornecedor,
                    data_visita,
                    lista,
                    tem_envio_confirmado,
                    data_referencia,
                    estado_lista=estado_lista,
                )
            )
            break

    avisos.sort(key=lambda aviso: (
        aviso["prioridade"],
        aviso["data_visita"],
        aviso["fornecedor_nome"],
        aviso["fornecedor_id"],
    ))
    return avisos
