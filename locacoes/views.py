from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms import modelformset_factory
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from urllib.parse import quote

from .forms import (
    AcaoOperacionalLocacaoForm,
    CancelarLocacaoForm,
    ConferenciaEntregaLocacaoForm,
    ConferenciaRecolhimentoLocacaoForm,
    ConfiguracaoLocacaoForm,
    DevolucaoLocacaoForm,
    FaixaPrecoLocacaoForm,
    ItensLocacaoReservaForm,
    LocacaoForm,
    MovimentoEstoqueLocacaoForm,
    NaoPossivelOperacionalLocacaoForm,
    PagamentoLocacaoForm,
    ReciboStatusForm,
    TermoLocacaoForm,
    VencimentoSaldoLocacaoForm,
)
from .models import (
    ConferenciaEntregaLocacao,
    ConferenciaRecolhimentoLocacao,
    ConfiguracaoLocacao,
    Cliente,
    EventoLocacao,
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
    TarefaOperacionalLocacao,
)
from estoque.models import Funcionario
from .services import checklist_operacional_locacoes, obter_ou_criar_tarefa_operacional, tarefas_ativas_da_locacao


def _faixa_padrao():
    return FaixaPrecoLocacao.objects.filter(ativa=True).order_by("ordem", "id").first()


def _initial_locacao_edicao(locacao):
    return {
        "tipo_pessoa": locacao.tipo_pessoa,
        "cliente": locacao.cliente,
        "pessoa_avulsa_nome": locacao.pessoa_avulsa_nome,
        "pessoa_avulsa_telefone": locacao.pessoa_avulsa_telefone,
        "endereco_entrega": locacao.endereco_entrega,
        "data_entrega": locacao.data_entrega,
        "horario_entrega": locacao.horario_entrega,
        "data_evento": locacao.data_evento,
        "horario_evento": locacao.horario_evento,
        "data_prevista_devolucao": locacao.data_prevista_devolucao,
        "data_vencimento_saldo": locacao.data_vencimento_saldo,
        "faixa_preco": locacao.faixa_preco,
        "observacao": locacao.observacao,
    }


def _initial_itens_edicao(locacao):
    initial = {
        "jogos": 0,
        "mesas_avulsas": 0,
        "cadeiras_avulsas": 0,
        "preco_jogo_diaria": None,
        "preco_mesa_avulsa_diaria": None,
        "preco_cadeira_avulsa_diaria": None,
    }
    mapa = {
        ItemLocacao.TIPO_JOGO: ("jogos", "preco_jogo_diaria"),
        ItemLocacao.TIPO_MESA_AVULSA: ("mesas_avulsas", "preco_mesa_avulsa_diaria"),
        ItemLocacao.TIPO_CADEIRA_AVULSA: ("cadeiras_avulsas", "preco_cadeira_avulsa_diaria"),
    }
    for item in locacao.itens.all():
        campos = mapa.get(item.tipo)
        if not campos:
            continue
        campo_qtd, campo_preco = campos
        initial[campo_qtd] += item.quantidade
        if initial[campo_preco] is None:
            initial[campo_preco] = item.preco_diaria_snapshot
    return initial


def _acoes_locacao(locacao, request=None):
    tarefa_entrega = None
    tarefa_recolhimento = None
    if locacao.status in {
        Locacao.STATUS_RESERVADA,
        Locacao.STATUS_SAIU_PARA_ENTREGA,
    }:
        tarefa_entrega = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )
    if locacao.status in {
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        tarefa_recolhimento = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )
    whatsapp_recolhimento = (
        _whatsapp_recolhimento_context(request, tarefa_recolhimento)
        if request and tarefa_recolhimento
        else {}
    )
    return {
        "pode_editar": locacao.pode_editar_amplamente(),
        "pode_cancelar": locacao.pode_cancelar(),
        "pode_excluir": locacao.pode_excluir(),
        "impedimentos_exclusao": locacao.impedimentos_exclusao(),
        "tarefa_entrega": tarefa_entrega,
        "tarefa_recolhimento": tarefa_recolhimento,
        "whatsapp_recolhimento": whatsapp_recolhimento,
        "tem_pagamentos": locacao.pagamentos.exists(),
    }


def _mensagem_recibo_whatsapp(pagamento):
    locacao = pagamento.locacao
    quitada = locacao.saldo_devedor <= Decimal("0.00")
    itens = [
        f"- {item.quantidade} {item.get_tipo_display().lower()}"
        for item in locacao.itens.all()
    ]
    situacao = "Pagamento quitado" if quitada else "Pagamento parcial"

    linhas = [
        f"RECIBO DE LOCACAO No {locacao.id}",
        "",
        "Cliente:",
        locacao.nome_contratante,
        "",
        "Evento:",
        f"{locacao.data_evento:%d/%m/%Y} as {locacao.horario_evento:%H:%M}",
        "",
        "Entrega:",
        f"{locacao.data_entrega:%d/%m/%Y} as {locacao.horario_entrega:%H:%M}",
        "",
        "Devolucao prevista:",
        f"{locacao.data_prevista_devolucao:%d/%m/%Y}",
        "",
        "Materiais locados:",
        *(itens or ["- Nenhum material informado"]),
        "",
        "Pagamento:",
        f"Situacao: {situacao}",
        f"Valor pago: R$ {pagamento.valor:.2f}",
        f"Saldo: R$ {locacao.saldo_devedor:.2f}",
    ]

    if pagamento.observacao:
        linhas.extend(["", "Observacao:", pagamento.observacao])

    linhas.extend(["", "Obrigado pela preferencia."])

    return "\n".join(linhas)


def _whatsapp_web_url(telefone, texto=""):
    if not telefone and not texto:
        return ""
    url = "https://web.whatsapp.com/send"
    if telefone:
        url = f"{url}?phone={telefone}"
    elif texto:
        url = f"{url}?text={quote(texto)}"
        return url
    if texto:
        url = f"{url}&text={quote(texto)}"
    return url


def _whatsapp_recibo_url(pagamento):
    return _whatsapp_web_url(_telefone_whatsapp_locacao(pagamento.locacao))


def _telefone_whatsapp_locacao(locacao):
    telefone = "".join(
        caractere
        for caractere in locacao.telefone_contratante
        if caractere.isdigit()
    )
    if len(telefone) in {10, 11}:
        telefone = f"55{telefone}"
    return telefone


def _whatsapp_locacao_url(locacao):
    return _whatsapp_web_url(_telefone_whatsapp_locacao(locacao))


def _itens_checklist_entrega(conferencia):
    itens = [
        (
            "Jogos",
            conferencia.entregue_jogos,
            [
                {
                    "nome": "Mesas",
                    "quantidade": (
                        conferencia.entregue_jogos
                        * ConfiguracaoLocacao.JOGO_MESAS
                    ),
                },
                {
                    "nome": "Cadeiras",
                    "quantidade": (
                        conferencia.entregue_jogos
                        * ConfiguracaoLocacao.JOGO_CADEIRAS
                    ),
                },
            ],
        ),
        ("Mesas", conferencia.entregue_mesas_avulsas, []),
        ("Cadeiras", conferencia.entregue_cadeiras_avulsas, []),
    ]
    return [
        {
            "nome": nome,
            "quantidade": quantidade,
            "composicao": composicao,
        }
        for nome, quantidade, composicao in itens
        if quantidade
    ]


def _nome_material_checklist_entrega(nome):
    nomes = {
        "Mesas": "Mesa",
        "Cadeiras": "Cadeira",
    }
    return nomes.get(nome, nome)


def _linha_item_checklist_pendente(quantidade, material):
    return f"\u2610 {quantidade} {material}(s)"


def _materiais_checklist_entrega_pendentes(conferencia):
    materiais = []
    vistos = set()
    for item in _itens_checklist_entrega(conferencia):
        partes = item["composicao"] or [{"nome": item["nome"]}]
        for parte in partes:
            nome = _nome_material_checklist_entrega(parte["nome"])
            chave = (nome, parte.get("quantidade") or item["quantidade"])
            if chave not in vistos:
                vistos.add(chave)
                materiais.append({
                    "nome": nome,
                    "quantidade": parte.get("quantidade") or item["quantidade"],
                })
    return materiais


def _itens_checklist_entrega_formato_rota(conferencia):
    return [
        {
            "produto_nome": material["nome"],
            "quantidade": material["quantidade"],
            "unidade": "un",
        }
        for material in _materiais_checklist_entrega_pendentes(conferencia)
    ]


def _texto_checklist_entrega(conferencia):
    locacao = conferencia.locacao
    linhas = [
        "\u2610 CHECKLIST DE ENTREGA",
        "",
        "Cliente",
        locacao.nome_contratante,
        "",
        "Endereco",
        locacao.endereco_entrega,
        "",
        "Materiais conferidos",
        "",
    ]

    materiais = _materiais_checklist_entrega_pendentes(conferencia)
    if materiais:
        linhas.extend(
            _linha_item_checklist_pendente(material["quantidade"], material["nome"])
            for material in materiais
        )
    else:
        linhas.append("Nenhum material informado")

    linhas.append("\u2610 Materiais em bom estado")

    linhas.extend([
        "",
        "Recebido por",
        conferencia.recebedor_nome,
        "",
        "Data/Hora",
        timezone.localtime(conferencia.criado_em).strftime("%d/%m/%Y %H:%M"),
        "",
        "Responsavel pela entrega",
        conferencia.responsavel,
    ])

    return "\n".join(linhas)


def _funcionarios_checklist_locacoes():
    return Funcionario.habilitados_para_checklist()


def _telefone_funcionario_checklist(funcionario):
    telefone = Funcionario.normalizar_whatsapp(
        funcionario.telefone_whatsapp_normalizado
        or funcionario.telefone_whatsapp
        or ""
    )
    if len(telefone) in {10, 11} and not telefone.startswith("55"):
        telefone = f"55{telefone}"
    return telefone


def _mensagem_checklist_link_whatsapp(nome_destinatario, checklist_url):
    nome = nome_destinatario or "funcionario"
    return "\n".join([
        f"Ola, {nome}.",
        "",
        "Segue o checklist de entrega:",
        "",
        checklist_url,
    ])


def _mensagem_checklist_entrega_cliente_whatsapp(conferencia, checklist_url):
    locacao = conferencia.locacao
    linhas = [
        f"Ola, {locacao.nome_contratante}.",
        "",
        "Segue o checklist conferido da sua entrega:",
        "",
        checklist_url,
    ]
    return "\n".join(linhas)


def _mensagem_checklist_recolhimento_link_whatsapp(nome_destinatario, checklist_url, locacao):
    linhas = []
    if nome_destinatario:
        linhas.extend([f"Ola, {nome_destinatario}.", ""])
    linhas.extend([
        f"Checklist de recolhimento da locacao #{locacao.id}",
        "",
        f"Cliente: {locacao.nome_contratante}",
        "",
        "Veja a conferencia do recolhimento:",
        checklist_url,
    ])
    return "\n".join(linhas)


def _whatsapp_checklist_funcionario_url(funcionario, texto):
    telefone = _telefone_funcionario_checklist(funcionario)
    return _whatsapp_web_url(telefone, texto)


def _url_publica_checklist_whatsapp(request, path):
    base_url = (
        getattr(settings, "CHECKLIST_BASE_URL", "")
        or getattr(settings, "SISTEMA_ONLINE_URL", "")
    ).rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return request.build_absolute_uri(path)


def _mensagem_tarefa_operacional_whatsapp(tarefa, checklist_url, ordem=None):
    locacao = tarefa.locacao
    tipo = (
        "entrega"
        if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA
        else "recolhimento"
    )
    titulo = f"Checklist de {tipo}"
    if ordem:
        titulo = f"{titulo} #{ordem}"
    linhas = [
        titulo,
        f"Locação #{locacao.id} - {locacao.nome_contratante}",
    ]
    if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA and tarefa.horario_agendado:
        linhas.append(f"Horário: {tarefa.horario_agendado:%H:%M}")
    linhas.extend([
        "",
        "Abrir checklist:",
        checklist_url,
    ])
    return "\n".join(linhas)


def _envios_tarefa_operacional_context(request, tarefa, ordem=None):
    rota = (
        "locacoes:conferencia_entrega"
        if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA
        else "locacoes:conferencia_recolhimento"
    )
    checklist_path = reverse(rota, kwargs={"pk": tarefa.pk})
    checklist_url = request.build_absolute_uri(checklist_path)
    checklist_whatsapp_url = _url_publica_checklist_whatsapp(request, checklist_path)
    mensagem = _mensagem_tarefa_operacional_whatsapp(
        tarefa,
        checklist_whatsapp_url,
        ordem=ordem,
    )
    funcionarios = []
    for funcionario in _funcionarios_checklist_locacoes():
        telefone = _telefone_funcionario_checklist(funcionario)
        funcionarios.append({
            "id": funcionario.pk,
            "nome": funcionario.nome,
            "telefone": telefone,
            "telefone_exibicao": funcionario.telefone_whatsapp or telefone,
            "whatsapp_url": (
                _whatsapp_web_url(telefone, mensagem)
                if telefone
                else ""
            ),
        })
    funcionario_padrao = next(
        (
            funcionario
            for funcionario in funcionarios
            if funcionario["telefone"]
        ),
        None,
    )
    return {
        "checklist_url": checklist_url,
        "checklist_whatsapp_url": checklist_whatsapp_url,
        "mensagem": mensagem,
        "funcionarios": funcionarios,
        "funcionario_padrao": funcionario_padrao,
        "tem_funcionario_com_whatsapp": any(
            funcionario["telefone"]
            for funcionario in funcionarios
        ),
    }


def _material_recolhimento_pendente(locacao):
    previsto = ConferenciaRecolhimentoLocacao.totais_entregues(locacao)
    recolhido = ConferenciaRecolhimentoLocacao.totais_recolhidos(locacao)
    recolhido_mesas = sum(
        valor for chave, valor in recolhido.items() if chave.endswith("_mesas")
    )
    recolhido_cadeiras = sum(
        valor for chave, valor in recolhido.items() if chave.endswith("_cadeiras")
    )
    return {
        "mesas": max(previsto["mesas"] - recolhido_mesas, 0),
        "cadeiras": max(previsto["cadeiras"] - recolhido_cadeiras, 0),
    }


def _mensagem_recolhimento_whatsapp(tarefa, checklist_url):
    locacao = tarefa.locacao
    material = _material_recolhimento_pendente(locacao)
    return "\n".join([
        f"Recolhimento da locacao #{locacao.id}",
        "",
        f"Cliente: {locacao.nome_contratante}",
        f"Data: {tarefa.data_agendada:%d/%m/%Y}",
        f"Endereco: {locacao.endereco_entrega}",
        (
            "Material: "
            f"{material['mesas']} mesas e {material['cadeiras']} cadeiras"
        ),
        "",
        "Abra o checklist:",
        checklist_url,
    ])


def _whatsapp_recolhimento_context(request, tarefa):
    locacao = tarefa.locacao
    if tarefa.tipo != TarefaOperacionalLocacao.TIPO_RECOLHIMENTO:
        return {}
    if tarefa.status == TarefaOperacionalLocacao.STATUS_CONFIRMADA:
        return {}
    if locacao.status not in {
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        return {}

    checklist_url = request.build_absolute_uri(
        reverse("locacoes:conferencia_recolhimento", kwargs={"pk": tarefa.pk})
    )
    mensagem = _mensagem_recolhimento_whatsapp(tarefa, checklist_url)
    funcionarios = list(_funcionarios_checklist_locacoes())
    funcionario = funcionarios[0] if funcionarios else None
    return {
        "checklist_url": checklist_url,
        "mensagem": mensagem,
        "funcionario": funcionario,
        "whatsapp_url": _whatsapp_checklist_funcionario_url(
            funcionario,
            mensagem,
        ) if funcionario else _whatsapp_web_url("", mensagem),
    }


def _funcionarios_checklist_envio(conferencia, checklist_url, funcionarios):
    if not conferencia or not checklist_url:
        return []
    separador = "&" if "?" in checklist_url else "?"
    return [
        {
            "funcionario": funcionario,
            "whatsapp_url": _whatsapp_checklist_funcionario_url(
                funcionario,
                _mensagem_checklist_link_whatsapp(
                    funcionario.nome,
                    f"{checklist_url}{separador}funcionario={funcionario.pk}",
                ),
            ),
        }
        for funcionario in funcionarios
    ]


def _responsavel_request(request, fallback=""):
    usuario = getattr(request, "user", None)
    if usuario is not None and getattr(usuario, "is_authenticated", False):
        return usuario.get_username()
    return fallback or "Sistema"


def _request_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _evento_checklist_entrega_enviado(conferencia):
    return (
        EventoLocacao.objects.filter(
            locacao=conferencia.locacao,
            tipo="checklist_entrega_funcionario_enviado",
            descricao__contains=f"Conferencia #{conferencia.pk}",
        )
        .order_by("-criado_em", "-id")
        .first()
    )


def _dados_evento_checklist_entrega(evento):
    if not evento:
        return {}
    descricao = evento.descricao or ""
    nome = ""
    telefone = ""
    marcador_nome = "Enviado para: "
    marcador_telefone = "Telefone: "
    if marcador_nome in descricao:
        nome = descricao.split(marcador_nome, 1)[1].split("\n", 1)[0].strip()
    if marcador_telefone in descricao:
        telefone = descricao.split(marcador_telefone, 1)[1].split("\n", 1)[0].strip()
    return {
        "funcionario_nome": nome,
        "telefone": telefone,
        "responsavel": evento.responsavel,
        "criado_em": evento.criado_em,
    }


def _evento_checklist_recolhimento_enviado(conferencia):
    return (
        EventoLocacao.objects.filter(
            locacao=conferencia.locacao,
            tipo="checklist_recolhimento_enviado",
            descricao__contains=f"Conferencia #{conferencia.pk}",
        )
        .order_by("-criado_em", "-id")
        .first()
    )


def _dados_evento_checklist_recolhimento(evento):
    return _dados_evento_checklist_entrega(evento)


def _avarias_recolhimento(conferencia):
    avarias = []
    for prefixo in ["quebrada", "perdida", "descartada"]:
        for material, singular, plural in [
            ("mesas", "mesa", "mesas"),
            ("cadeiras", "cadeira", "cadeiras"),
        ]:
            quantidade = getattr(conferencia, f"{prefixo}_{material}")
            if quantidade:
                nome = singular if quantidade == 1 else plural
                avarias.append({
                    "quantidade": quantidade,
                    "descricao": f"{quantidade} {nome} com problema",
                })
    return avarias


def _texto_checklist_recolhimento(conferencia, avarias):
    locacao = conferencia.locacao
    recolhido_mesas = (
        conferencia.boa_mesas
        + conferencia.quebrada_mesas
        + conferencia.perdida_mesas
        + conferencia.descartada_mesas
    )
    recolhido_cadeiras = (
        conferencia.boa_cadeiras
        + conferencia.quebrada_cadeiras
        + conferencia.perdida_cadeiras
        + conferencia.descartada_cadeiras
    )
    linhas = [
        f"Checklist de recolhimento da locacao #{locacao.id}",
        "",
        f"Cliente: {locacao.nome_contratante}",
        f"Endereco: {locacao.endereco_entrega}",
        f"Data/Hora: {timezone.localtime(conferencia.criado_em):%d/%m/%Y %H:%M}",
        "",
        (
            "Previsto: "
            f"{conferencia.previsto_mesas} mesa(s) e "
            f"{conferencia.previsto_cadeiras} cadeira(s)"
        ),
        (
            "Recolhido: "
            f"{recolhido_mesas} mesa(s) e "
            f"{recolhido_cadeiras} cadeira(s)"
        ),
        f"Situacao: {conferencia.get_situacao_display()}",
    ]
    if conferencia.pendente_mesas or conferencia.pendente_cadeiras:
        linhas.append(
            "Pendente: "
            f"{conferencia.pendente_mesas} mesa(s) e "
            f"{conferencia.pendente_cadeiras} cadeira(s)"
        )
    if avarias:
        linhas.extend(["", "Avarias:"])
        linhas.extend(f"- {avaria['descricao']}" for avaria in avarias)
    else:
        linhas.extend(["", "Materiais recolhidos sem avarias."])
    if conferencia.observacao:
        linhas.extend(["", "Observacao:", conferencia.observacao])
    linhas.extend(["", f"Responsavel: {conferencia.responsavel}"])
    return "\n".join(linhas)


def _funcionarios_checklist_recolhimento_envio(conferencia, checklist_url, funcionarios):
    if not conferencia or not checklist_url:
        return []
    return [
        {
            "funcionario": funcionario,
            "whatsapp_url": _whatsapp_checklist_funcionario_url(
                funcionario,
                _mensagem_checklist_recolhimento_link_whatsapp(
                    funcionario.nome,
                    checklist_url,
                    conferencia.locacao,
                ),
            ),
        }
        for funcionario in funcionarios
    ]


def lista(request):
    status = request.GET.get("status", "").strip()
    hoje = timezone.localdate()
    primeira_abertura = not request.GET
    data_inicio_texto = hoje.isoformat() if primeira_abertura else request.GET.get("data_inicio", "").strip()
    data_fim_texto = hoje.isoformat() if primeira_abertura else request.GET.get("data_fim", "").strip()
    data_inicio = parse_date(data_inicio_texto or "")
    data_fim = parse_date(data_fim_texto or "")
    locacoes_qs = (
        Locacao.objects
        .select_related("cliente", "faixa_preco")
        .prefetch_related(
            "itens",
            "pagamentos",
            "conferencias_entrega",
            "conferencias_recolhimento",
            "movimentos_estoque",
            "tarefas_operacionais",
        )
    )
    if status in {Locacao.STATUS_RESERVADA, Locacao.STATUS_CANCELADA}:
        locacoes_qs = locacoes_qs.filter(status=status)
    elif status in {
        Locacao.STATUS_SAIU_PARA_ENTREGA,
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_DEVOLVIDA,
        Locacao.STATUS_DEVOLVIDA_COM_AVARIA,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        locacoes_qs = locacoes_qs.filter(status=status)
    if data_inicio:
        locacoes_qs = locacoes_qs.filter(data_entrega__gte=data_inicio)
    if data_fim:
        locacoes_qs = locacoes_qs.filter(data_entrega__lte=data_fim)
    locacoes = list(locacoes_qs)
    for locacao in locacoes:
        locacao.necessidade_pendente_lista = Locacao.necessidades_itens(
            [
                {"tipo": item.tipo, "quantidade": item.quantidade_pendente()}
                for item in locacao.itens.all()
            ]
        )
        locacao.acoes_consulta = _acoes_locacao(locacao, request=request)
    return render(
        request,
        "locacoes/lista.html",
        {
            "locacoes": locacoes,
            "status_filtro": status,
            "data_inicio": data_inicio_texto,
            "data_fim": data_fim_texto,
            "status_opcoes": Locacao.STATUS_CHOICES,
        },
    )


def _contexto_disponibilidade(data_entrega, data_prevista_devolucao, itens, excluir_id=None):
    if not data_entrega or not data_prevista_devolucao:
        return None
    try:
        diarias = Locacao.calcular_diarias(data_entrega, data_prevista_devolucao)
    except ValidationError:
        return None
    necessidade = Locacao.necessidades_itens(itens)
    disponibilidade = Locacao.disponibilidade_periodo(
        data_entrega,
        data_prevista_devolucao,
        excluir_id=excluir_id,
    )
    return {
        **disponibilidade,
        "solicitado_mesas": necessidade["mesas"],
        "solicitado_cadeiras": necessidade["cadeiras"],
        "restante_mesas": disponibilidade["disponivel_mesas"] - necessidade["mesas"],
        "restante_cadeiras": disponibilidade["disponivel_cadeiras"] - necessidade["cadeiras"],
        "diarias": diarias,
    }


def _resumo_valores_locacao(data_entrega, data_prevista_devolucao, itens):
    try:
        diarias = Locacao.calcular_diarias(data_entrega, data_prevista_devolucao)
    except ValidationError:
        diarias = 0
    subtotais = {
        ItemLocacao.TIPO_JOGO: Decimal("0.00"),
        ItemLocacao.TIPO_MESA_AVULSA: Decimal("0.00"),
        ItemLocacao.TIPO_CADEIRA_AVULSA: Decimal("0.00"),
    }
    for item in itens:
        quantidade = int(item.get("quantidade") or 0)
        if quantidade <= 0:
            continue
        preco_diaria = Decimal(item.get("preco_diaria") or "0").quantize(Decimal("0.01"))
        subtotal = (Decimal(quantidade) * preco_diaria * Decimal(diarias)).quantize(Decimal("0.01"))
        subtotais[item["tipo"]] += subtotal
    total = sum(subtotais.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    return {
        "diarias": diarias,
        "subtotal_jogos": subtotais[ItemLocacao.TIPO_JOGO].quantize(Decimal("0.01")),
        "subtotal_mesas_avulsas": subtotais[ItemLocacao.TIPO_MESA_AVULSA].quantize(Decimal("0.01")),
        "subtotal_cadeiras_avulsas": subtotais[ItemLocacao.TIPO_CADEIRA_AVULSA].quantize(Decimal("0.01")),
        "total": total,
    }


def _inteiro_nao_negativo(valor):
    try:
        return max(int(valor or 0), 0)
    except (TypeError, ValueError):
        return 0


def _avaliar_disponibilidade_dinamica(data_entrega, data_prevista_devolucao, jogos=0, mesas_avulsas=0, cadeiras_avulsas=0, excluir_id=None):
    if not data_entrega or not data_prevista_devolucao:
        return {
            "status": "incompleto",
            "mensagem": "Informe datas e itens para verificar a disponibilidade.",
        }
    jogos = _inteiro_nao_negativo(jogos)
    mesas_avulsas = _inteiro_nao_negativo(mesas_avulsas)
    cadeiras_avulsas = _inteiro_nao_negativo(cadeiras_avulsas)
    if jogos <= 0 and mesas_avulsas <= 0 and cadeiras_avulsas <= 0:
        return {
            "status": "incompleto",
            "mensagem": "Informe datas e itens para verificar a disponibilidade.",
        }

    itens = []
    if jogos:
        itens.append({"tipo": ItemLocacao.TIPO_JOGO, "quantidade": jogos})
    if mesas_avulsas:
        itens.append({"tipo": ItemLocacao.TIPO_MESA_AVULSA, "quantidade": mesas_avulsas})
    if cadeiras_avulsas:
        itens.append({"tipo": ItemLocacao.TIPO_CADEIRA_AVULSA, "quantidade": cadeiras_avulsas})

    try:
        Locacao.calcular_diarias(data_entrega, data_prevista_devolucao)
    except ValidationError as exc:
        return {"status": "incompleto", "mensagem": "; ".join(exc.messages)}

    disponibilidade = Locacao.disponibilidade_periodo(data_entrega, data_prevista_devolucao, excluir_id=excluir_id)
    necessidade = Locacao.necessidades_itens(itens)
    disponivel_mesas = disponibilidade["disponivel_mesas"]
    disponivel_cadeiras = disponibilidade["disponivel_cadeiras"]
    jogos_disponiveis = min(disponivel_mesas, disponivel_cadeiras // ConfiguracaoLocacao.JOGO_CADEIRAS)

    faltas = []
    if necessidade["mesas"] > disponivel_mesas:
        faltas.append(f"mesas: solicitado {necessidade['mesas']}, disponível {disponivel_mesas}")
    if necessidade["cadeiras"] > disponivel_cadeiras:
        faltas.append(f"cadeiras: solicitado {necessidade['cadeiras']}, disponível {disponivel_cadeiras}")

    dados = {
        "jogos_disponiveis": jogos_disponiveis,
        "disponivel_mesas": disponivel_mesas,
        "disponivel_cadeiras": disponivel_cadeiras,
        "solicitado_mesas": necessidade["mesas"],
        "solicitado_cadeiras": necessidade["cadeiras"],
    }
    if faltas:
        return {
            **dados,
            "status": "indisponivel",
            "mensagem": "Falta de material: " + "; ".join(faltas) + ".",
        }
    return {
        **dados,
        "status": "disponivel",
        "mensagem": (
            f"Disponível: {jogos_disponiveis} jogo(s), "
            f"{disponivel_mesas} mesa(s) e {disponivel_cadeiras} cadeira(s) para o período."
        ),
    }


def resumo_valores_dinamico(request):
    data_entrega = parse_date(request.GET.get("data_entrega", ""))
    data_prevista_devolucao = parse_date(
        request.GET.get("data_prevista_devolucao", "")
    )

    def decimal_nao_negativo(valor):
        try:
            numero = Decimal(
                str(valor or "0").strip().replace(",", ".")
            ).quantize(Decimal("0.01"))
        except Exception:
            return Decimal("0.00")
        return max(numero, Decimal("0.00"))

    itens = [
        {
            "tipo": ItemLocacao.TIPO_JOGO,
            "quantidade": _inteiro_nao_negativo(
                request.GET.get("jogos")
            ),
            "preco_diaria": decimal_nao_negativo(
                request.GET.get("preco_jogo_diaria")
            ),
        },
        {
            "tipo": ItemLocacao.TIPO_MESA_AVULSA,
            "quantidade": _inteiro_nao_negativo(
                request.GET.get("mesas_avulsas")
            ),
            "preco_diaria": decimal_nao_negativo(
                request.GET.get("preco_mesa_avulsa_diaria")
            ),
        },
        {
            "tipo": ItemLocacao.TIPO_CADEIRA_AVULSA,
            "quantidade": _inteiro_nao_negativo(
                request.GET.get("cadeiras_avulsas")
            ),
            "preco_diaria": decimal_nao_negativo(
                request.GET.get("preco_cadeira_avulsa_diaria")
            ),
        },
    ]

    if not data_entrega or not data_prevista_devolucao:
        resumo = {
            "diarias": 0,
            "subtotal_jogos": Decimal("0.00"),
            "subtotal_mesas_avulsas": Decimal("0.00"),
            "subtotal_cadeiras_avulsas": Decimal("0.00"),
            "total": Decimal("0.00"),
        }
    else:
        resumo = _resumo_valores_locacao(
            data_entrega,
            data_prevista_devolucao,
            itens,
        )

    return JsonResponse({
        "diarias": resumo["diarias"],
        "subtotal_jogos": str(resumo["subtotal_jogos"]),
        "subtotal_mesas_avulsas": str(
            resumo["subtotal_mesas_avulsas"]
        ),
        "subtotal_cadeiras_avulsas": str(
            resumo["subtotal_cadeiras_avulsas"]
        ),
        "total": str(resumo["total"]),
    })


def disponibilidade_dinamica(request):
    data_entrega = parse_date(request.GET.get("data_entrega", ""))
    data_prevista_devolucao = parse_date(request.GET.get("data_prevista_devolucao", ""))
    excluir_id = _inteiro_nao_negativo(request.GET.get("excluir_id")) or None
    resultado = _avaliar_disponibilidade_dinamica(
        data_entrega,
        data_prevista_devolucao,
        jogos=request.GET.get("jogos"),
        mesas_avulsas=request.GET.get("mesas_avulsas"),
        cadeiras_avulsas=request.GET.get("cadeiras_avulsas"),
        excluir_id=excluir_id,
    )
    return JsonResponse(resultado)


def _dados_cliente_locacao(cliente):
    linhas = []

    primeira_linha = ", ".join(
        parte for parte in [
            cliente.logradouro,
            cliente.numero,
        ] if parte
    )
    if primeira_linha:
        linhas.append(primeira_linha)

    if cliente.complemento:
        linhas.append(cliente.complemento)

    if cliente.bairro:
        linhas.append(cliente.bairro)

    cidade_uf = " - ".join(
        parte for parte in [
            cliente.cidade,
            cliente.uf,
        ] if parte
    )
    if cidade_uf:
        linhas.append(cidade_uf)

    telefone = (
        cliente.whatsapp
        or cliente.telefone_alternativo
        or ""
    )

    return {
        "endereco": "\n".join(linhas),
        "numero": cliente.numero or "",
        "telefone": telefone,
    }


def dados_cliente(request, pk):
    cliente = get_object_or_404(Cliente, pk=pk)
    return JsonResponse(_dados_cliente_locacao(cliente))

@ensure_csrf_cookie
def nova(request):
    configuracao = ConfiguracaoLocacao.obter()
    faixa_inicial = _faixa_padrao()
    if request.method == "POST":
        locacao_form = LocacaoForm(request.POST)
        itens_form = ItensLocacaoReservaForm(request.POST, faixa_preco=faixa_inicial, configuracao=configuracao)
        if locacao_form.is_valid() and itens_form.is_valid():
            faixa = locacao_form.cleaned_data["faixa_preco"]
            itens = itens_form.itens(faixa, configuracao)
            disponibilidade = _contexto_disponibilidade(
                locacao_form.cleaned_data["data_entrega"],
                locacao_form.cleaned_data["data_prevista_devolucao"],
                itens,
            )
            resumo_valores = _resumo_valores_locacao(
                locacao_form.cleaned_data["data_entrega"],
                locacao_form.cleaned_data["data_prevista_devolucao"],
                itens,
            )
            sinal_valor = locacao_form.cleaned_data.get("sinal_valor") or Decimal("0.00")
            if sinal_valor > resumo_valores["total"]:
                locacao_form.add_error("sinal_valor", "Pagamento inicial nao pode ser maior que o total contratado.")
                messages.warning(request, "Revise o pagamento inicial antes de salvar.")
                return render(
                    request,
                    "locacoes/nova.html",
                    {
                        "locacao_form": locacao_form,
                        "itens_form": itens_form,
                        "configuracao": configuracao,
                        "disponibilidade": disponibilidade,
                        "modo_edicao": False,
                    },
                )
            dados_locacao = dict(
                locacao_form.cleaned_data
            )
            if (
                resumo_valores["total"] > Decimal("0.00")
                and sinal_valor == resumo_valores["total"]
            ):
                dados_locacao["sem_vencimento_saldo"] = True
                dados_locacao["data_vencimento_saldo"] = None

            try:
                locacao = Locacao.criar_reserva(
                    dados_locacao,
                    itens,
                    responsavel="",
                )
                if sinal_valor and sinal_valor > 0:
                    pagamento = locacao.registrar_pagamento(
                        sinal_valor,
                        locacao_form.cleaned_data.get("sinal_forma_pagamento"),
                        observacao=locacao_form.cleaned_data.get("sinal_observacao", "") or "Sinal da locacao.",
                        responsavel="",
                    )
                    messages.success(request, f"Reserva #{locacao.id} criada com sinal registrado.")
                    return redirect("locacoes:recibo_pagamento", pk=pagamento.pk)
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            locacao_form.add_error(campo if campo in locacao_form.fields else None, erro)
                else:
                    locacao_form.add_error(None, exc)
                messages.warning(request, "Nao foi possivel salvar a reserva.")
            else:
                messages.success(request, f"Reserva de locacao #{locacao.id} criada. Material ainda nao saiu para entrega.")
                return redirect("locacoes:detalhe", pk=locacao.pk)
        else:
            disponibilidade = None
            messages.warning(request, "Revise os dados da reserva antes de salvar.")
    else:
        hoje = timezone.localdate()
        amanha = hoje + hoje.resolution
        cliente_inicial_id = (
            request.GET.get("cliente")
            or request.GET.get("cliente_id")
            or ""
        )
        cliente_inicial = (
            Cliente.objects.filter(
                pk=cliente_inicial_id,
                ativo=True,
            ).first()
            if str(cliente_inicial_id).isdigit()
            else None
        )
        initial_locacao = {
            "faixa_preco": faixa_inicial,
            "data_entrega": hoje,
            "data_evento": hoje,
            "data_prevista_devolucao": amanha,
            "data_vencimento_saldo": hoje,
        }
        if cliente_inicial:
            dados_cliente_inicial = _dados_cliente_locacao(cliente_inicial)
            initial_locacao.update({
                "tipo_pessoa": Locacao.TIPO_PESSOA_CLIENTE,
                "cliente": cliente_inicial,
                "pessoa_avulsa_telefone": dados_cliente_inicial["telefone"],
                "endereco_entrega": dados_cliente_inicial["endereco"],
            })
        locacao_form = LocacaoForm(initial=initial_locacao)
        itens_form = ItensLocacaoReservaForm(faixa_preco=faixa_inicial, configuracao=configuracao)
        disponibilidade = None

    return render(
        request,
        "locacoes/nova.html",
        {
            "locacao_form": locacao_form,
            "itens_form": itens_form,
            "configuracao": configuracao,
            "disponibilidade": disponibilidade,
            "modo_edicao": False,
        },
    )


@ensure_csrf_cookie
def editar(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related(
            "itens",
            "pagamentos",
            "conferencias_entrega",
            "tarefas_operacionais",
        ),
        pk=pk,
    )
    if not locacao.pode_editar_amplamente():
        messages.warning(
            request,
            (
                "Esta locacao nao permite edicao ampla porque ja saiu do "
                "estado de reserva livre ou possui entrega confirmada."
            ),
        )
        return redirect("locacoes:detalhe", pk=locacao.pk)

    configuracao = ConfiguracaoLocacao.obter()
    faixa_inicial = locacao.faixa_preco or _faixa_padrao()
    if request.method == "POST":
        locacao_form = LocacaoForm(request.POST)
        itens_form = ItensLocacaoReservaForm(
            request.POST,
            faixa_preco=faixa_inicial,
            configuracao=configuracao,
        )
        if locacao_form.is_valid() and itens_form.is_valid():
            faixa = locacao_form.cleaned_data["faixa_preco"]
            itens = itens_form.itens(faixa, configuracao)
            disponibilidade = _contexto_disponibilidade(
                locacao_form.cleaned_data["data_entrega"],
                locacao_form.cleaned_data["data_prevista_devolucao"],
                itens,
                excluir_id=locacao.pk,
            )
            resumo_valores = _resumo_valores_locacao(
                locacao_form.cleaned_data["data_entrega"],
                locacao_form.cleaned_data["data_prevista_devolucao"],
                itens,
            )
            total_pago = locacao.total_pago
            if resumo_valores["total"] < total_pago:
                locacao_form.add_error(
                    None,
                    "O novo total nao pode ficar menor que o total ja pago.",
                )
                messages.warning(request, "Revise os valores antes de salvar.")
            else:
                dados_locacao = dict(locacao_form.cleaned_data)
                if resumo_valores["total"] == total_pago and total_pago > Decimal("0.00"):
                    dados_locacao["sem_vencimento_saldo"] = True
                    dados_locacao["data_vencimento_saldo"] = None
                try:
                    with transaction.atomic():
                        locacao.atualizar_reserva(
                            dados_locacao,
                            itens,
                            responsavel=_responsavel_request(request),
                        )
                        obter_ou_criar_tarefa_operacional(
                            locacao,
                            TarefaOperacionalLocacao.TIPO_ENTREGA,
                        )
                except ValidationError as exc:
                    if hasattr(exc, "message_dict"):
                        for campo, erros in exc.message_dict.items():
                            for erro in erros:
                                locacao_form.add_error(
                                    campo if campo in locacao_form.fields else None,
                                    erro,
                                )
                    else:
                        for erro in exc.messages:
                            locacao_form.add_error(None, erro)
                    messages.warning(request, "Nao foi possivel salvar a edicao.")
                else:
                    messages.success(request, f"Locacao #{locacao.id} atualizada.")
                    return redirect("locacoes:lista")
        else:
            disponibilidade = None
            messages.warning(request, "Revise os dados da locacao antes de salvar.")
    else:
        locacao_form = LocacaoForm(initial=_initial_locacao_edicao(locacao))
        itens_form = ItensLocacaoReservaForm(
            initial=_initial_itens_edicao(locacao),
            faixa_preco=faixa_inicial,
            configuracao=configuracao,
        )
        disponibilidade = _contexto_disponibilidade(
            locacao.data_entrega,
            locacao.data_prevista_devolucao,
            [
                {"tipo": item.tipo, "quantidade": item.quantidade}
                for item in locacao.itens.all()
            ],
            excluir_id=locacao.pk,
        )

    return render(
        request,
        "locacoes/nova.html",
        {
            "locacao": locacao,
            "locacao_form": locacao_form,
            "itens_form": itens_form,
            "configuracao": configuracao,
            "disponibilidade": disponibilidade,
            "modo_edicao": True,
        },
    )


def detalhe(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.select_related(
            "cliente",
            "faixa_preco",
        ).prefetch_related(
            "itens",
            "eventos",
            "pagamentos",
            "conferencias_entrega",
            "conferencias_recolhimento",
        ),
        pk=pk,
    )
    necessidade = Locacao.necessidades_itens(
        [{"tipo": item.tipo, "quantidade": item.quantidade} for item in locacao.itens.all()]
    )
    disponibilidade = Locacao.disponibilidade_periodo(
        locacao.data_entrega,
        locacao.data_prevista_devolucao,
        excluir_id=locacao.pk,
    )
    acoes_locacao = _acoes_locacao(locacao, request=request)
    tarefa_entrega = acoes_locacao["tarefa_entrega"]
    tarefa_recolhimento = acoes_locacao["tarefa_recolhimento"]

    ultimo_pagamento = locacao.pagamentos.all().first()

    return render(
        request,
        "locacoes/detalhe.html",
        {
            "locacao": locacao,
            "tarefas_operacionais": tarefas_ativas_da_locacao(locacao),
            "tarefa_entrega": tarefa_entrega,
            "tarefa_recolhimento": tarefa_recolhimento,
            "conferencias_entrega": (
                locacao.conferencias_entrega.all()
            ),
            "conferencias_recolhimento": (
                locacao.conferencias_recolhimento.all()
            ),
            "necessidade": necessidade,
            "disponibilidade": disponibilidade,
            "acoes_locacao": acoes_locacao,
            "cancelar_form": CancelarLocacaoForm(),
            "acao_form": AcaoOperacionalLocacaoForm(),
            "pagamento_form": PagamentoLocacaoForm(),
            "ultimo_pagamento": ultimo_pagamento,
            "whatsapp_recibo_url": (
                _whatsapp_recibo_url(ultimo_pagamento)
                if ultimo_pagamento
                else ""
            ),
            "recibo_form": ReciboStatusForm(),
            "vencimento_form": VencimentoSaldoLocacaoForm(initial={
                "data_vencimento_saldo": locacao.data_vencimento_saldo,
            }),
        },
    )


def checklist_operacional(request):
    data_texto = request.GET.get("data", "").strip()
    data_referencia = parse_date(data_texto) if data_texto else timezone.localdate()
    if not data_referencia:
        data_referencia = timezone.localdate()
    checklist = checklist_operacional_locacoes(data_referencia=data_referencia)
    for grupo in checklist["grupos"].values():
        for indice, item in enumerate(grupo, start=1):
            item["ordem_operacional"] = indice
            item["envio_operacional"] = _envios_tarefa_operacional_context(
                request,
                item["tarefa"],
                ordem=indice,
            )
            if item["tarefa"].tipo != TarefaOperacionalLocacao.TIPO_RECOLHIMENTO:
                continue
            item["whatsapp_recolhimento"] = _whatsapp_recolhimento_context(
                request,
                item["tarefa"],
            )
    return render(
        request,
        "locacoes/checklist_operacional.html",
        {
            "checklist": checklist,
            "data_referencia": data_referencia,
            "foco_tarefa_id": request.GET.get("tarefa", "").strip(),
            "acao_form": AcaoOperacionalLocacaoForm(),
            "nao_possivel_form": NaoPossivelOperacionalLocacaoForm(),
        },
    )


def abrir_tarefa_operacional_locacao(request, pk, tipo):
    locacao = get_object_or_404(
        Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens"),
        pk=pk,
    )
    if tipo not in {TarefaOperacionalLocacao.TIPO_ENTREGA, TarefaOperacionalLocacao.TIPO_RECOLHIMENTO}:
        messages.warning(request, "Tipo de tarefa operacional invalido.")
        return redirect("locacoes:detalhe", pk=locacao.pk)
    tarefa = obter_ou_criar_tarefa_operacional(locacao, tipo)
    return redirect(f"{reverse('locacoes:checklist_operacional')}?data={tarefa.data_agendada:%Y-%m-%d}&tarefa={tarefa.pk}")


@ensure_csrf_cookie
def conferencia_entrega(request, pk):
    tarefa = get_object_or_404(
        TarefaOperacionalLocacao.objects
        .select_related("locacao", "locacao__cliente")
        .prefetch_related(
            "locacao__itens",
            "locacao__conferencias_entrega",
        ),
        pk=pk,
        tipo=TarefaOperacionalLocacao.TIPO_ENTREGA,
    )
    locacao = tarefa.locacao

    conferencia_salva = None
    conferencia_id = request.GET.get(
        "conferencia",
        "",
    ).strip()

    if conferencia_id.isdigit():
        conferencia_salva = get_object_or_404(
            ConferenciaEntregaLocacao,
            pk=int(conferencia_id),
            locacao=locacao,
        )
    elif tarefa.status == TarefaOperacionalLocacao.STATUS_CONFIRMADA:
        conferencias_tarefa = list(
            tarefa.conferencias_entrega.all().order_by("-criado_em", "-id")[:2]
        )
        if len(conferencias_tarefa) == 1:
            conferencia_salva = conferencias_tarefa[0]

    if (
        tarefa.status
        == TarefaOperacionalLocacao.STATUS_CONFIRMADA
        and not conferencia_salva
    ):
        messages.warning(
            request,
            "Esta entrega ja foi confirmada completamente.",
        )
        return redirect("locacoes:detalhe", pk=locacao.pk)

    if (
        locacao.status not in {
            Locacao.STATUS_RESERVADA,
            Locacao.STATUS_SAIU_PARA_ENTREGA,
        }
        and not conferencia_salva
    ):
        messages.warning(
            request,
            "Esta locacao nao possui entrega pendente.",
        )
        return redirect("locacoes:detalhe", pk=locacao.pk)

    if request.method == "POST":
        dados_post = request.POST.copy()
        if (
            dados_post.get("recebedor_relacao")
            == ConferenciaEntregaLocacao.RELACAO_CLIENTE
            and not str(dados_post.get("recebedor_nome") or "").strip()
        ):
            dados_post["recebedor_nome"] = locacao.nome_contratante
        if (
            dados_post.get("recebedor_relacao")
            == ConferenciaEntregaLocacao.RELACAO_OUTRO
            and not str(dados_post.get("recebedor_relacao_outro") or "").strip()
        ):
            dados_post["recebedor_relacao_outro"] = "Outro"
        if not str(dados_post.get("responsavel") or "").strip():
            dados_post["responsavel"] = "Checklist operacional"
        form = ConferenciaEntregaLocacaoForm(
            dados_post,
            locacao=locacao,
        )
        if form.is_valid():
            try:
                conferencia = ConferenciaEntregaLocacao.registrar(
                    tarefa=tarefa,
                    dados=form.cleaned_data,
                    calculos=form.dados_conferencia(),
                )
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            form.add_error(
                                campo if campo in form.fields else None,
                                erro,
                            )
                else:
                    for erro in exc.messages:
                        form.add_error(None, erro)
                messages.warning(
                    request,
                    "Nao foi possivel registrar a conferencia.",
                )
            else:
                if (
                    conferencia.situacao
                    == ConferenciaEntregaLocacao.SITUACAO_PARCIAL
                ):
                    messages.warning(
                        request,
                        "Entrega parcial registrada. A pendencia foi mantida.",
                    )
                else:
                    messages.success(
                        request,
                        "Entrega completa registrada com sucesso.",
                    )
                return redirect(
                    f"{reverse('locacoes:conferencia_entrega', kwargs={'pk': tarefa.pk})}"
                    f"?conferencia={conferencia.pk}"
                )
        else:
            messages.warning(
                request,
                "Revise os campos destacados antes de confirmar.",
            )
    else:
        form = ConferenciaEntregaLocacaoForm(
            locacao=locacao,
        )

    historico = locacao.conferencias_entrega.all()
    checklist_path = (
        reverse(
            "locacoes:checklist_entrega_cliente",
            kwargs={"pk": conferencia_salva.pk},
        )
        if conferencia_salva
        else ""
    )
    checklist_url = request.build_absolute_uri(checklist_path) if checklist_path else ""
    checklist_cliente_whatsapp_url = (
        _url_publica_checklist_whatsapp(request, checklist_path)
        if checklist_path
        else ""
    )
    telefone_cliente_whatsapp = _telefone_whatsapp_locacao(locacao)
    whatsapp_checklist_cliente_url = (
        _whatsapp_web_url(
            telefone_cliente_whatsapp,
            _mensagem_checklist_entrega_cliente_whatsapp(
                conferencia_salva,
                checklist_cliente_whatsapp_url,
            ),
        )
        if conferencia_salva and telefone_cliente_whatsapp
        else ""
    )
    materiais_entrega = [
        {
            "chave": "jogos",
            "nome": "Jogos",
            "contratado": form.previsto_itens["jogos"],
            "acumulado": form.acumulado_itens["jogos"],
            "pendente": form.pendente_itens["jogos"],
            "mesas_jogos": (
                form.pendente_itens["jogos"]
                * ConfiguracaoLocacao.JOGO_MESAS
            ),
            "cadeiras_jogos": (
                form.pendente_itens["jogos"]
                * ConfiguracaoLocacao.JOGO_CADEIRAS
            ),
            "field": form["entregue_jogos"],
            "errors": form["entregue_jogos"].errors,
        },
        {
            "chave": "mesas_avulsas",
            "nome": "Mesas avulsas",
            "contratado": form.previsto_itens["mesas_avulsas"],
            "acumulado": form.acumulado_itens["mesas_avulsas"],
            "pendente": form.pendente_itens["mesas_avulsas"],
            "field": form["entregue_mesas_avulsas"],
            "errors": form["entregue_mesas_avulsas"].errors,
        },
        {
            "chave": "cadeiras_avulsas",
            "nome": "Cadeiras",
            "contratado": form.previsto_itens["cadeiras_avulsas"],
            "acumulado": form.acumulado_itens["cadeiras_avulsas"],
            "pendente": form.pendente_itens["cadeiras_avulsas"],
            "field": form["entregue_cadeiras_avulsas"],
            "errors": form["entregue_cadeiras_avulsas"].errors,
        },
    ]

    return render(
        request,
        "locacoes/conferencia_entrega.html",
        {
            "tarefa": tarefa,
            "locacao": locacao,
            "form": form,
            "previsto_mesas": form.previsto_mesas,
            "previsto_cadeiras": form.previsto_cadeiras,
            "acumulado_mesas": form.acumulado_mesas,
            "acumulado_cadeiras": form.acumulado_cadeiras,
            "pendente_mesas": form.pendente_mesas,
            "pendente_cadeiras": form.pendente_cadeiras,
            "materiais_entrega": materiais_entrega,
            "historico": historico,
            "whatsapp_checklist_cliente_url": whatsapp_checklist_cliente_url,
            "telefone_cliente_whatsapp": telefone_cliente_whatsapp,
            "telefone_cliente_exibicao": locacao.telefone_contratante,
            "conferencia_salva": conferencia_salva,
            "checklist_url": checklist_url,
            "checklist_cliente_whatsapp_url": checklist_cliente_whatsapp_url,
        },
    )


def checklist_entrega_cliente(request, pk):
    conferencia = get_object_or_404(
        ConferenciaEntregaLocacao.objects.select_related(
            "locacao",
            "locacao__cliente",
            "tarefa",
        ),
        pk=pk,
    )
    locacao = conferencia.locacao
    texto_checklist = _texto_checklist_entrega(conferencia)

    return render(
        request,
        "locacoes/checklist_entrega_cliente.html",
        {
            "conferencia": conferencia,
            "locacao": locacao,
            "itens_checklist": _itens_checklist_entrega(conferencia),
            "itens_checklist_rota": _itens_checklist_entrega_formato_rota(conferencia),
            "texto_checklist": texto_checklist,
            "whatsapp_url": _whatsapp_locacao_url(locacao),
        },
    )


def checklist_recolhimento_cliente(request, pk):
    conferencia = get_object_or_404(
        ConferenciaRecolhimentoLocacao.objects.select_related(
            "locacao",
            "locacao__cliente",
            "tarefa",
        ),
        pk=pk,
    )
    locacao = conferencia.locacao
    funcionarios_checklist = _funcionarios_checklist_locacoes()
    evento_envio = _evento_checklist_recolhimento_enviado(conferencia)
    checklist_url = request.build_absolute_uri(
        reverse(
            "locacoes:checklist_recolhimento_cliente",
            kwargs={"pk": conferencia.pk},
        )
    )

    if request.method == "POST" and not evento_envio:
        funcionario_id = str(request.POST.get("checklist_funcionario") or "").strip()
        funcionario = funcionarios_checklist.filter(pk=funcionario_id).first()
        if funcionario:
            telefone = _telefone_funcionario_checklist(funcionario)
            responsavel = _responsavel_request(request, conferencia.responsavel)
            EventoLocacao.objects.create(
                locacao=locacao,
                tipo="checklist_recolhimento_enviado",
                descricao=(
                    f"Checklist de recolhimento enviado ao funcionario.\n"
                    f"Conferencia #{conferencia.pk}\n"
                    f"Enviado para: {funcionario.nome}\n"
                    f"Telefone: {telefone}"
                ),
                responsavel=responsavel,
            )
            messages.success(request, "Checklist confirmado como enviado.")
            if _request_ajax(request):
                evento_envio = _evento_checklist_recolhimento_enviado(conferencia)
                return JsonResponse(
                    {
                        "ok": True,
                        "redirectUrl": reverse(
                            "locacoes:checklist_recolhimento_cliente",
                            kwargs={"pk": conferencia.pk},
                        ),
                        "envio": _dados_evento_checklist_recolhimento(evento_envio),
                    }
                )
            return redirect("locacoes:checklist_recolhimento_cliente", pk=conferencia.pk)
        if _request_ajax(request):
            return JsonResponse(
                {
                    "ok": False,
                    "erro": "Selecione um funcionario habilitado para receber o checklist.",
                },
                status=400,
            )
        messages.warning(
            request,
            "Selecione um funcionario habilitado para receber o checklist.",
        )

    evento_envio = _evento_checklist_recolhimento_enviado(conferencia)
    envio_checklist = _dados_evento_checklist_recolhimento(evento_envio)
    funcionarios_checklist_envio = _funcionarios_checklist_recolhimento_envio(
        conferencia,
        checklist_url,
        funcionarios_checklist,
    )
    funcionario_selecionado_id = str(request.GET.get("funcionario") or "")
    funcionario_selecionado = (
        funcionarios_checklist.filter(pk=funcionario_selecionado_id).first()
        if funcionario_selecionado_id
        else None
    )
    funcionario_selecionado_whatsapp_url = (
        _whatsapp_checklist_funcionario_url(
            funcionario_selecionado,
            _mensagem_checklist_recolhimento_link_whatsapp(
                funcionario_selecionado.nome,
                checklist_url,
                locacao,
            ),
        )
        if funcionario_selecionado
        else ""
    )
    if envio_checklist.get("telefone"):
        envio_checklist["whatsapp_url"] = _whatsapp_web_url(
            envio_checklist["telefone"],
            _mensagem_checklist_recolhimento_link_whatsapp(
                envio_checklist.get("funcionario_nome"),
                checklist_url,
                locacao,
            ),
        )

    telefone_cliente = _telefone_whatsapp_locacao(locacao)
    whatsapp_url = _whatsapp_web_url(
        telefone_cliente,
        _mensagem_checklist_recolhimento_link_whatsapp(
            "",
            checklist_url,
            locacao,
        ),
    ) if telefone_cliente else ""
    recolhido_mesas = (
        conferencia.boa_mesas
        + conferencia.quebrada_mesas
        + conferencia.perdida_mesas
        + conferencia.descartada_mesas
    )
    recolhido_cadeiras = (
        conferencia.boa_cadeiras
        + conferencia.quebrada_cadeiras
        + conferencia.perdida_cadeiras
        + conferencia.descartada_cadeiras
    )
    avarias = _avarias_recolhimento(conferencia)

    return render(
        request,
        "locacoes/checklist_recolhimento_cliente.html",
        {
            "conferencia": conferencia,
            "locacao": locacao,
            "checklist_url": checklist_url,
            "whatsapp_url": whatsapp_url,
            "funcionarios_checklist": funcionarios_checklist,
            "funcionarios_checklist_envio": funcionarios_checklist_envio,
            "funcionario_selecionado": funcionario_selecionado,
            "funcionario_selecionado_whatsapp_url": funcionario_selecionado_whatsapp_url,
            "evento_envio_checklist": evento_envio,
            "envio_checklist": envio_checklist,
            "avarias": avarias,
            "texto_checklist": _texto_checklist_recolhimento(conferencia, avarias),
            "recolhido_mesas": recolhido_mesas,
            "recolhido_cadeiras": recolhido_cadeiras,
        },
    )


def conferencia_recolhimento(request, pk):
    tarefa = get_object_or_404(
        TarefaOperacionalLocacao.objects
        .select_related(
            "locacao",
            "locacao__cliente",
        )
        .prefetch_related(
            "locacao__itens",
            "locacao__conferencias_entrega",
            "locacao__conferencias_recolhimento",
        ),
        pk=pk,
        tipo=TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
    )
    locacao = tarefa.locacao

    conferencia_salva = None
    conferencia_id = request.GET.get(
        "conferencia",
        "",
    ).strip()

    if conferencia_id.isdigit():
        conferencia_salva = get_object_or_404(
            ConferenciaRecolhimentoLocacao,
            pk=int(conferencia_id),
            locacao=locacao,
        )

    if (
        tarefa.status
        == TarefaOperacionalLocacao.STATUS_CONFIRMADA
        and not conferencia_salva
    ):
        messages.warning(
            request,
            "Este recolhimento ja foi concluido.",
        )
        return redirect(
            "locacoes:detalhe",
            pk=locacao.pk,
        )

    if (
        locacao.status not in {
            Locacao.STATUS_ENTREGUE,
            Locacao.STATUS_PENDENTE_DEVOLUCAO,
        }
        and not conferencia_salva
    ):
        messages.warning(
            request,
            "Esta locacao nao possui recolhimento pendente.",
        )
        return redirect(
            "locacoes:detalhe",
            pk=locacao.pk,
        )

    if request.method == "POST":
        dados_post = request.POST.copy()
        responsavel = _responsavel_request(
            request,
            str(request.POST.get("responsavel") or "").strip()
            or "Checklist operacional",
        )
        dados_post["pessoa_local_nome"] = responsavel
        dados_post["pessoa_local_relacao"] = (
            ConferenciaEntregaLocacao.RELACAO_FUNCIONARIO
        )
        dados_post["pessoa_local_relacao_outro"] = ""
        dados_post["responsavel"] = responsavel
        for campo in [
            "perdida_mesas",
            "perdida_cadeiras",
            "descartada_mesas",
            "descartada_cadeiras",
        ]:
            dados_post[campo] = "0"
        form = ConferenciaRecolhimentoLocacaoForm(
            dados_post,
            locacao=locacao,
        )

        if form.is_valid():
            try:
                conferencia = (
                    ConferenciaRecolhimentoLocacao.registrar(
                        tarefa=tarefa,
                        dados=form.cleaned_data,
                    )
                )
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            form.add_error(
                                (
                                    campo
                                    if campo in form.fields
                                    else None
                                ),
                                erro,
                            )
                else:
                    for erro in exc.messages:
                        form.add_error(None, erro)

                messages.warning(
                    request,
                    (
                        "Nao foi possivel registrar "
                        "o recolhimento."
                    ),
                )
            else:
                if (
                    conferencia.situacao
                    == ConferenciaRecolhimentoLocacao
                    .SITUACAO_PARCIAL
                ):
                    messages.warning(
                        request,
                        (
                            "Recolhimento parcial registrado. "
                            "A pendencia foi mantida."
                        ),
                    )
                else:
                    messages.success(
                        request,
                        (
                            "Recolhimento completo registrado "
                            "com sucesso."
                        ),
                    )

                return redirect(
                    (
                        reverse(
                            "locacoes:conferencia_recolhimento",
                            kwargs={"pk": tarefa.pk},
                        )
                        + f"?conferencia={conferencia.pk}"
                    )
                )
        else:
            messages.warning(
                request,
                (
                    "Revise os campos destacados "
                    "antes de confirmar."
                ),
            )
    else:
        form = ConferenciaRecolhimentoLocacaoForm(
            locacao=locacao,
        )

    checklist_url = (
        request.build_absolute_uri(
            reverse(
                "locacoes:checklist_recolhimento_cliente",
                kwargs={"pk": conferencia_salva.pk},
            )
        )
        if conferencia_salva
        else ""
    )
    whatsapp_url = ""
    if conferencia_salva:
        telefone = _telefone_whatsapp_locacao(locacao)
        if telefone:
            whatsapp_url = _whatsapp_web_url(
                telefone,
                _mensagem_checklist_recolhimento_link_whatsapp(
                    "",
                    checklist_url,
                    locacao,
                ),
            )
    funcionarios_checklist = _funcionarios_checklist_locacoes()
    funcionarios_checklist_envio = (
        _funcionarios_checklist_recolhimento_envio(
            conferencia_salva,
            checklist_url,
            funcionarios_checklist,
        )
        if conferencia_salva
        else []
    )
    whatsapp_checklist_funcionario_url = (
        funcionarios_checklist_envio[0]["whatsapp_url"]
        if funcionarios_checklist_envio
        else ""
    )

    historico = (
        locacao.conferencias_recolhimento.all()
    )

    return render(
        request,
        "locacoes/conferencia_recolhimento.html",
        {
            "tarefa": tarefa,
            "locacao": locacao,
            "form": form,
            "previsto_mesas": form.previsto_mesas,
            "previsto_cadeiras": form.previsto_cadeiras,
            "recolhido_mesas": form.recolhido_mesas,
            "recolhido_cadeiras": form.recolhido_cadeiras,
            "pendente_mesas": form.pendente_mesas,
            "pendente_cadeiras": form.pendente_cadeiras,
            "conferencia_salva": conferencia_salva,
            "checklist_url": checklist_url,
            "whatsapp_url": whatsapp_url,
            "funcionarios_checklist_envio": funcionarios_checklist_envio,
            "whatsapp_checklist_funcionario_url": whatsapp_checklist_funcionario_url,
            "historico": historico,
        },
    )


@require_POST
def confirmar_tarefa_operacional(request, pk):
    tarefa = get_object_or_404(
        TarefaOperacionalLocacao.objects.select_related("locacao"),
        pk=pk,
    )

    if tarefa.tipo == TarefaOperacionalLocacao.TIPO_ENTREGA:
        messages.warning(
            request,
            "A entrega deve ser registrada pelo checklist detalhado.",
        )
        return redirect(
            "locacoes:conferencia_entrega",
            pk=tarefa.pk,
        )

    if (
        tarefa.tipo
        == TarefaOperacionalLocacao.TIPO_RECOLHIMENTO
    ):
        messages.warning(
            request,
            (
                "O recolhimento deve ser registrado "
                "pelo checklist detalhado."
            ),
        )
        return redirect(
            "locacoes:conferencia_recolhimento",
            pk=tarefa.pk,
        )

    form = AcaoOperacionalLocacaoForm(request.POST)
    if form.is_valid():
        try:
            tarefa.confirmar(
                responsavel=form.cleaned_data.get("responsavel", ""),
                observacao=form.cleaned_data.get("observacao", ""),
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"{tarefa.get_tipo_display()} confirmada.")
    else:
        messages.warning(request, "Revise os dados antes de confirmar.")
    return redirect(f"{reverse('locacoes:checklist_operacional')}?data={tarefa.data_agendada:%Y-%m-%d}")


@require_POST
def tarefa_operacional_nao_possivel(request, pk):
    tarefa = get_object_or_404(TarefaOperacionalLocacao.objects.select_related("locacao"), pk=pk)
    form = NaoPossivelOperacionalLocacaoForm(request.POST)
    if form.is_valid():
        try:
            tarefa.registrar_nao_possivel(
                motivo=form.cleaned_data.get("observacao", ""),
                responsavel=form.cleaned_data.get("responsavel", ""),
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))
        else:
            messages.warning(request, "Tarefa mantida como pendencia para resolver ou reagendar.")
    else:
        messages.warning(request, "Informe o motivo para manter a pendencia.")
    return redirect(f"{reverse('locacoes:checklist_operacional')}?data={tarefa.data_agendada:%Y-%m-%d}&tarefa={tarefa.pk}")


@ensure_csrf_cookie
def registrar_pagamento(request, pk):
    locacao = get_object_or_404(Locacao.objects.prefetch_related("itens").select_related("cliente"), pk=pk)
    if request.method == "POST":
        form = PagamentoLocacaoForm(request.POST)
        if form.is_valid():
            try:
                pagamento = locacao.registrar_pagamento(
                    form.cleaned_data["valor"],
                    form.cleaned_data["forma_pagamento"],
                    data_hora=form.cleaned_data.get("data_hora"),
                    observacao=form.cleaned_data.get("observacao", ""),
                    responsavel=form.cleaned_data.get("responsavel", ""),
                )
            except ValidationError as exc:
                messages.warning(request, "; ".join(exc.messages))
            else:
                messages.success(request, "Pagamento de locacao registrado.")
                return redirect("locacoes:recibo_pagamento", pk=pagamento.pk)
        else:
            messages.warning(request, "Revise o pagamento antes de salvar.")
    else:
        form = PagamentoLocacaoForm()
    return render(request, "locacoes/pagamento.html", {"locacao": locacao, "form": form})


@require_POST
def alterar_vencimento_saldo(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = VencimentoSaldoLocacaoForm(request.POST)
    if form.is_valid():
        locacao.alterar_vencimento_saldo(
            form.cleaned_data["data_vencimento_saldo"],
            responsavel=form.cleaned_data.get("responsavel", ""),
            observacao=form.cleaned_data.get("observacao", ""),
        )
        messages.success(request, "Vencimento do saldo atualizado.")
    else:
        messages.warning(request, "Informe uma data valida para o vencimento do saldo.")
    return redirect("locacoes:detalhe", pk=locacao.pk)


def recibo_pagamento(request, pk):
    pagamento = get_object_or_404(
        PagamentoLocacao.objects.select_related("locacao", "locacao__cliente").prefetch_related("locacao__itens"),
        pk=pk,
    )
    locacao = pagamento.locacao
    tarefa_entrega_url = ""
    if locacao.status in {
        Locacao.STATUS_RESERVADA,
        Locacao.STATUS_SAIU_PARA_ENTREGA,
    }:
        tarefa_entrega = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )
        tarefa_entrega_url = (
            f"{reverse('locacoes:checklist_operacional')}"
            f"?data={tarefa_entrega.data_agendada:%Y-%m-%d}"
            f"&tarefa={tarefa_entrega.pk}"
        )

    return render(
        request,
        "locacoes/recibo.html",
        {
            "pagamento": pagamento,
            "locacao": locacao,
            "whatsapp_url": _whatsapp_recibo_url(pagamento),
            "mensagem_recibo": _mensagem_recibo_whatsapp(pagamento),
            "recibo_form": ReciboStatusForm(),
            "tarefa_entrega_url": tarefa_entrega_url,
        },
    )


@require_POST
def confirmar_recibo_enviado(request, pk):
    pagamento = get_object_or_404(PagamentoLocacao.objects.select_related("locacao"), pk=pk)
    form = ReciboStatusForm(request.POST)
    if form.is_valid():
        pagamento.confirmar_recibo_enviado(
            responsavel=form.cleaned_data.get(
                "responsavel",
                "",
            )
        )
        messages.success(
            request,
            "Recibo confirmado como enviado.",
        )
    else:
        messages.warning(
            request,
            "Selecione o responsavel pela acao.",
        )

    return redirect(
        "locacoes:recibo_pagamento",
        pk=pagamento.pk,
    )


@require_POST
def dispensar_recibo(request, pk):
    pagamento = get_object_or_404(PagamentoLocacao.objects.select_related("locacao"), pk=pk)
    form = ReciboStatusForm(request.POST)
    if form.is_valid():
        observacao = (
            form.cleaned_data.get("observacao", "")
            or ""
        ).strip()

        if not observacao:
            messages.warning(
                request,
                "Informe o motivo para dispensar o envio.",
            )
            return redirect(
                "locacoes:recibo_pagamento",
                pk=pagamento.pk,
            )

        pagamento.dispensar_recibo(
            responsavel=form.cleaned_data.get(
                "responsavel",
                "",
            ),
            observacao=observacao,
        )
        messages.success(
            request,
            "Envio do recibo dispensado.",
        )
    else:
        messages.warning(
            request,
            "Selecione o responsavel pela acao.",
        )

    return redirect(
        "locacoes:recibo_pagamento",
        pk=pagamento.pk,
    )


def recibos_pendentes(request):
    pagamentos = (
        PagamentoLocacao.objects.select_related("locacao", "locacao__cliente")
        .filter(recibo_status=PagamentoLocacao.RECIBO_PENDENTE)
        .order_by("-data_hora", "-id")
    )
    return render(
        request,
        "locacoes/recibos_pendentes.html",
        {
            "pagamentos": pagamentos,
            "confirmar_form": ReciboStatusForm(),
            "dispensar_form": ReciboStatusForm(),
        },
    )


@ensure_csrf_cookie
def termo(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.select_related("cliente").prefetch_related("itens"),
        pk=pk,
    )
    form = TermoLocacaoForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        locacao.registrar_termo_gerado(responsavel=form.cleaned_data.get("responsavel", ""))
        messages.success(request, "Termo de compromisso registrado como gerado.")
        return redirect("locacoes:termo", pk=locacao.pk)
    return render(request, "locacoes/termo.html", {"locacao": locacao, "form": form})


@require_POST
def cancelar(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.prefetch_related("pagamentos"),
        pk=pk,
    )
    form = CancelarLocacaoForm(request.POST)
    if form.is_valid():
        try:
            locacao.cancelar(
                motivo=form.cleaned_data.get("motivo", ""),
                responsavel=form.cleaned_data.get("responsavel", ""),
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"Reserva de locacao #{locacao.id} cancelada.")
    else:
        messages.warning(request, "Nao foi possivel cancelar a reserva.")
    return redirect("locacoes:detalhe", pk=locacao.pk)


@require_POST
def excluir(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.prefetch_related(
            "pagamentos",
            "conferencias_entrega",
            "conferencias_recolhimento",
            "movimentos_estoque",
            "tarefas_operacionais",
        ),
        pk=pk,
    )
    locacao_id = locacao.pk
    try:
        locacao.excluir_se_seguro()
    except ValidationError as exc:
        messages.warning(
            request,
            "Nao foi possivel excluir. " + "; ".join(exc.messages),
        )
        return redirect("locacoes:detalhe", pk=locacao.pk)
    messages.success(request, f"Locacao #{locacao_id} excluida.")
    return redirect("locacoes:lista")


@require_POST
def marcar_saiu_para_entrega(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = AcaoOperacionalLocacaoForm(request.POST)
    if form.is_valid():
        try:
            locacao.marcar_saiu_para_entrega(
                responsavel=form.cleaned_data.get("responsavel", ""),
                observacao=form.cleaned_data.get("observacao", ""),
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))
        else:
            messages.success(request, "Locacao marcada como saiu para entrega.")
    return redirect("locacoes:detalhe", pk=locacao.pk)


@require_POST
def confirmar_entrega(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    tarefa = obter_ou_criar_tarefa_operacional(
        locacao,
        TarefaOperacionalLocacao.TIPO_ENTREGA,
    )
    messages.warning(
        request,
        "A entrega deve ser registrada pelo checklist detalhado.",
    )
    return redirect(
        "locacoes:conferencia_entrega",
        pk=tarefa.pk,
    )


@ensure_csrf_cookie
def registrar_devolucao(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.select_related("cliente"),
        pk=pk,
    )

    if locacao.status not in {
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        messages.warning(
            request,
            (
                "O recolhimento somente pode ser iniciado "
                "depois da entrega ser concluida."
            ),
        )
        return redirect(
            "locacoes:detalhe",
            pk=locacao.pk,
        )

    tarefa = obter_ou_criar_tarefa_operacional(
        locacao,
        TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
    )

    messages.warning(
        request,
        (
            "A devolucao agora deve ser registrada "
            "pelo checklist detalhado de recolhimento."
        ),
    )

    return redirect(
        "locacoes:conferencia_recolhimento",
        pk=tarefa.pk,
    )

@ensure_csrf_cookie
def configuracoes(request):
    configuracao = ConfiguracaoLocacao.obter()
    FaixaFormSet = modelformset_factory(
        FaixaPrecoLocacao,
        form=FaixaPrecoLocacaoForm,
        extra=0,
        can_delete=False,
    )
    faixas_qs = FaixaPrecoLocacao.objects.all()

    movimentacao_form = MovimentoEstoqueLocacaoForm()
    correcao_form = MovimentoEstoqueLocacaoForm(
        prefix="correcao",
    )
    correcao_aberta = False

    if request.method == "POST" and request.POST.get("acao") == "salvar_configuracoes":
        configuracao_form = ConfiguracaoLocacaoForm(
            request.POST,
            instance=configuracao,
        )
        faixas_formset = FaixaFormSet(
            request.POST,
            queryset=faixas_qs,
            prefix="faixas",
        )
        if configuracao_form.is_valid() and faixas_formset.is_valid():
            configuracao_form.save()
            faixas_formset.save()
            messages.success(
                request,
                "Configuracoes de locacoes atualizadas.",
            )
            return redirect("locacoes:configuracoes")
        messages.warning(
            request,
            "Revise os campos destacados antes de salvar.",
        )

    elif request.method == "POST" and request.POST.get("acao") == "corrigir_saldo":
        configuracao_form = ConfiguracaoLocacaoForm(
            instance=configuracao,
        )
        faixas_formset = FaixaFormSet(
            queryset=faixas_qs,
            prefix="faixas",
        )

        dados_correcao = request.POST.copy()
        dados_correcao["correcao-tipo"] = (
            MovimentoEstoqueLocacao.TIPO_AJUSTE_INVENTARIO
        )
        dados_correcao["correcao-quantidade"] = ""

        correcao_form = MovimentoEstoqueLocacaoForm(
            dados_correcao,
            prefix="correcao",
        )
        correcao_aberta = True

        if correcao_form.is_valid():
            try:
                MovimentoEstoqueLocacao.registrar(
                    **correcao_form.cleaned_data
                )
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            correcao_form.add_error(
                                campo if campo in correcao_form.fields else None,
                                erro,
                            )
                else:
                    correcao_form.add_error(None, exc)

                messages.warning(
                    request,
                    "Nao foi possivel corrigir o saldo.",
                )
            else:
                messages.success(
                    request,
                    "Saldo de locacao corrigido com sucesso.",
                )
                return redirect("locacoes:configuracoes")
        else:
            messages.warning(
                request,
                "Revise os dados da correcao antes de salvar.",
            )

    elif request.method == "POST" and request.POST.get("acao") == "registrar_movimentacao":
        configuracao_form = ConfiguracaoLocacaoForm(
            instance=configuracao,
        )
        faixas_formset = FaixaFormSet(
            queryset=faixas_qs,
            prefix="faixas",
        )
        movimentacao_form = MovimentoEstoqueLocacaoForm(
            request.POST,
        )

        if movimentacao_form.is_valid():
            try:
                MovimentoEstoqueLocacao.registrar(
                    **movimentacao_form.cleaned_data
                )
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            movimentacao_form.add_error(
                                campo if campo in movimentacao_form.fields else None,
                                erro,
                            )
                else:
                    movimentacao_form.add_error(None, exc)

                messages.warning(
                    request,
                    "Nao foi possivel registrar a movimentacao.",
                )
            else:
                messages.success(
                    request,
                    "Movimentacao de estoque de locacao registrada.",
                )
                return redirect("locacoes:configuracoes")
        else:
            messages.warning(
                request,
                "Revise os dados da movimentacao antes de salvar.",
            )

    else:
        configuracao_form = ConfiguracaoLocacaoForm(
            instance=configuracao,
        )
        faixas_formset = FaixaFormSet(
            queryset=faixas_qs,
            prefix="faixas",
        )

    total_mesas = configuracao.total_mesas or 0
    total_cadeiras = configuracao.total_cadeiras or 0

    jogos_completos = min(
        total_mesas // ConfiguracaoLocacao.JOGO_MESAS,
        total_cadeiras // ConfiguracaoLocacao.JOGO_CADEIRAS,
    )
    mesas_sobrando = (
        total_mesas
        - jogos_completos * ConfiguracaoLocacao.JOGO_MESAS
    )
    cadeiras_sobrando = (
        total_cadeiras
        - jogos_completos * ConfiguracaoLocacao.JOGO_CADEIRAS
    )

    historico_movimentacoes = (
        MovimentoEstoqueLocacao.objects.all()[:12]
    )

    return render(
        request,
        "locacoes/configuracoes.html",
        {
            "configuracao": configuracao,
            "configuracao_form": configuracao_form,
            "faixas_formset": faixas_formset,
            "movimentacao_form": movimentacao_form,
            "correcao_form": correcao_form,
            "correcao_aberta": correcao_aberta,
            "historico_movimentacoes": historico_movimentacoes,
            "composicao_jogo": ConfiguracaoLocacao.composicao_jogo(),
            "jogos_completos": jogos_completos,
            "mesas_sobrando": mesas_sobrando,
            "cadeiras_sobrando": cadeiras_sobrando,
        },
    )


