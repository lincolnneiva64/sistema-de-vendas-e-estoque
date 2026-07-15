from datetime import timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from estoque.models import EnvioListaCompraFornecedor, Fornecedor, ListaCompraFornecedor
from estoque.services.fornecedor_visitas import calcular_proxima_visita


DIAS_ANTECEDENCIA_AVISO_VISITA = 7

ESTADO_PREPARAR_LISTA = "preparar_lista"
ESTADO_LISTA_PREPARADA_FALTA_ENVIAR = "lista_preparada_falta_enviar"


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


def datas_validas_ciclo_visita_fornecedor(fornecedor, data_referencia=None):
    if data_referencia is None:
        data_referencia = timezone.localdate()
    if not _configuracao_visita_valida(fornecedor):
        return []
    return [
        data
        for data in _candidatos_de_visita(fornecedor, data_referencia)
        if data <= data_referencia or (data - data_referencia).days <= DIAS_ANTECEDENCIA_AVISO_VISITA
    ]


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


def _montar_aviso(fornecedor, data_visita, lista, tem_envio_confirmado, data_referencia):
    dias_para_visita = (data_visita - data_referencia).days
    estado = (
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
    if data_referencia is None:
        data_referencia = timezone.localdate()

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

    candidatos_por_fornecedor = {}
    datas_consulta = set()
    for fornecedor in fornecedores:
        candidatos = datas_validas_ciclo_visita_fornecedor(fornecedor, data_referencia=data_referencia)
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

    envios_confirmados = set(
        EnvioListaCompraFornecedor.objects
        .filter(lista_id__in=[lista.id for lista in listas_por_ciclo.values()])
        .values_list("lista_id", flat=True)
    )

    avisos = []
    for fornecedor in fornecedores:
        candidatos = candidatos_por_fornecedor.get(fornecedor.id, [])
        for data_visita in candidatos:
            lista = listas_por_ciclo.get((fornecedor.id, data_visita))
            tem_envio_confirmado = bool(lista and lista.id in envios_confirmados)
            if tem_envio_confirmado:
                continue
            avisos.append(_montar_aviso(fornecedor, data_visita, lista, tem_envio_confirmado, data_referencia))
            break

    avisos.sort(key=lambda aviso: (
        aviso["prioridade"],
        aviso["data_visita"],
        aviso["fornecedor_nome"],
        aviso["fornecedor_id"],
    ))
    return avisos
