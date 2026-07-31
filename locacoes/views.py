from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
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
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
    TarefaOperacionalLocacao,
)
from .services import checklist_operacional_locacoes, obter_ou_criar_tarefa_operacional, tarefas_ativas_da_locacao


def _faixa_padrao():
    return FaixaPrecoLocacao.objects.filter(ativa=True).order_by("ordem", "id").first()


def _mensagem_recibo_whatsapp(pagamento):
    locacao = pagamento.locacao
    saldo = locacao.saldo_devedor
    status = "QUITADA" if saldo <= 0 else "SALDO PENDENTE"
    itens = ", ".join(
        f"{item.quantidade} {item.get_tipo_display()}"
        for item in locacao.itens.all()
    )
    return "\n".join([
        f"Recibo de pagamento - Locacao #{locacao.id}",
        f"Cliente/Pessoa: {locacao.nome_contratante}",
        f"Valor pago agora: R$ {pagamento.valor:.2f}",
        f"Forma: {pagamento.get_forma_pagamento_display()}",
        f"Total contratado: R$ {locacao.total:.2f}",
        f"Total pago: R$ {locacao.total_pago:.2f}",
        f"Saldo restante: R$ {saldo:.2f} ({status})",
        f"Materiais: {itens}",
        f"Entrega: {locacao.data_entrega:%d/%m/%Y}",
        f"Devolucao prevista: {locacao.data_prevista_devolucao:%d/%m/%Y}",
    ])


def _whatsapp_recibo_url(pagamento):
    telefone = "".join(caractere for caractere in pagamento.locacao.telefone_contratante if caractere.isdigit())
    if len(telefone) in {10, 11}:
        telefone = f"55{telefone}"
    if not telefone:
        return ""
    return f"https://web.whatsapp.com/send?phone={telefone}&text={quote(_mensagem_recibo_whatsapp(pagamento))}"


def lista(request):
    status = request.GET.get("status", "").strip()
    hoje = timezone.localdate()
    primeira_abertura = not request.GET
    data_inicio_texto = hoje.isoformat() if primeira_abertura else request.GET.get("data_inicio", "").strip()
    data_fim_texto = hoje.isoformat() if primeira_abertura else request.GET.get("data_fim", "").strip()
    data_inicio = parse_date(data_inicio_texto or "")
    data_fim = parse_date(data_fim_texto or "")
    locacoes_qs = Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens")
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


def _contexto_disponibilidade(data_entrega, data_prevista_devolucao, itens):
    if not data_entrega or not data_prevista_devolucao:
        return None
    try:
        diarias = Locacao.calcular_diarias(data_entrega, data_prevista_devolucao)
    except ValidationError:
        return None
    necessidade = Locacao.necessidades_itens(itens)
    disponibilidade = Locacao.disponibilidade_periodo(data_entrega, data_prevista_devolucao)
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
                    },
                )
            try:
                locacao = Locacao.criar_reserva(
                    locacao_form.cleaned_data,
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
        locacao_form = LocacaoForm(initial={
            "faixa_preco": faixa_inicial,
            "data_entrega": hoje,
            "data_evento": hoje,
            "data_prevista_devolucao": hoje,
            "data_vencimento_saldo": hoje,
        })
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
    tarefa_entrega = None
    if locacao.status in {
        Locacao.STATUS_RESERVADA,
        Locacao.STATUS_SAIU_PARA_ENTREGA,
    }:
        tarefa_entrega = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

    tarefa_recolhimento = None
    if locacao.status in {
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        tarefa_recolhimento = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

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
            "cancelar_form": CancelarLocacaoForm(),
            "acao_form": AcaoOperacionalLocacaoForm(),
            "pagamento_form": PagamentoLocacaoForm(),
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
        form = ConferenciaEntregaLocacaoForm(
            request.POST,
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

    telefone = "".join(
        caractere
        for caractere in locacao.telefone_contratante
        if caractere.isdigit()
    )
    if len(telefone) in {10, 11}:
        telefone = f"55{telefone}"

    whatsapp_url = ""
    if conferencia_salva and telefone:
        whatsapp_url = (
            f"https://web.whatsapp.com/send?"
            f"phone={telefone}&"
            f"text={quote(conferencia_salva.mensagem_whatsapp_snapshot)}"
        )

    historico = locacao.conferencias_entrega.all()

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
            "conferencia_salva": conferencia_salva,
            "whatsapp_url": whatsapp_url,
            "historico": historico,
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
        form = ConferenciaRecolhimentoLocacaoForm(
            request.POST,
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

    telefone = "".join(
        caractere
        for caractere in locacao.telefone_contratante
        if caractere.isdigit()
    )

    if len(telefone) in {10, 11}:
        telefone = f"55{telefone}"

    whatsapp_url = ""
    if conferencia_salva and telefone:
        whatsapp_url = (
            "https://web.whatsapp.com/send?"
            f"phone={telefone}&"
            f"text={quote(
                conferencia_salva.mensagem_whatsapp_snapshot
            )}"
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
            "whatsapp_url": whatsapp_url,
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
    return render(
        request,
        "locacoes/recibo.html",
        {
            "pagamento": pagamento,
            "locacao": pagamento.locacao,
            "whatsapp_url": _whatsapp_recibo_url(pagamento),
            "confirmar_form": ReciboStatusForm(),
            "dispensar_form": ReciboStatusForm(),
        },
    )


@require_POST
def confirmar_recibo_enviado(request, pk):
    pagamento = get_object_or_404(PagamentoLocacao.objects.select_related("locacao"), pk=pk)
    form = ReciboStatusForm(request.POST)
    if form.is_valid():
        pagamento.confirmar_recibo_enviado(responsavel=form.cleaned_data.get("responsavel", ""))
        messages.success(request, "Recibo confirmado como enviado.")
    return redirect("locacoes:recibo_pagamento", pk=pagamento.pk)


@require_POST
def dispensar_recibo(request, pk):
    pagamento = get_object_or_404(PagamentoLocacao.objects.select_related("locacao"), pk=pk)
    form = ReciboStatusForm(request.POST)
    if form.is_valid():
        pagamento.dispensar_recibo(
            responsavel=form.cleaned_data.get("responsavel", ""),
            observacao=form.cleaned_data.get("observacao", ""),
        )
        messages.success(request, "Recibo dispensado.")
    return redirect("locacoes:recibo_pagamento", pk=pagamento.pk)


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
    locacao = get_object_or_404(Locacao, pk=pk)
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

    if request.method == "POST" and request.POST.get("acao") == "salvar_configuracoes":
        configuracao_form = ConfiguracaoLocacaoForm(request.POST, instance=configuracao)
        faixas_formset = FaixaFormSet(request.POST, queryset=faixas_qs, prefix="faixas")
        if configuracao_form.is_valid() and faixas_formset.is_valid():
            configuracao_form.save()
            faixas_formset.save()
            messages.success(request, "Configuracoes de locacoes atualizadas.")
            return redirect("locacoes:configuracoes")
        messages.warning(request, "Revise os campos destacados antes de salvar.")
    elif request.method == "POST" and request.POST.get("acao") == "registrar_movimentacao":
        configuracao_form = ConfiguracaoLocacaoForm(instance=configuracao)
        faixas_formset = FaixaFormSet(queryset=faixas_qs, prefix="faixas")
        movimentacao_form = MovimentoEstoqueLocacaoForm(request.POST)
        if movimentacao_form.is_valid():
            try:
                MovimentoEstoqueLocacao.registrar(**movimentacao_form.cleaned_data)
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for campo, erros in exc.message_dict.items():
                        for erro in erros:
                            movimentacao_form.add_error(campo if campo in movimentacao_form.fields else None, erro)
                else:
                    movimentacao_form.add_error(None, exc)
                messages.warning(request, "Nao foi possivel registrar a movimentacao.")
            else:
                messages.success(request, "Movimentacao de estoque de locacao registrada.")
                return redirect("locacoes:configuracoes")
        else:
            messages.warning(request, "Revise os dados da movimentacao antes de salvar.")
    else:
        configuracao_form = ConfiguracaoLocacaoForm(instance=configuracao)
        faixas_formset = FaixaFormSet(queryset=faixas_qs, prefix="faixas")

    historico_movimentacoes = MovimentoEstoqueLocacao.objects.all()[:12]

    return render(
        request,
        "locacoes/configuracoes.html",
        {
            "configuracao": configuracao,
            "configuracao_form": configuracao_form,
            "faixas_formset": faixas_formset,
            "movimentacao_form": movimentacao_form,
            "historico_movimentacoes": historico_movimentacoes,
            "composicao_jogo": ConfiguracaoLocacao.composicao_jogo(),
        },
    )
