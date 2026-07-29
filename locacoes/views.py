from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from urllib.parse import quote

from .forms import (
    AcaoOperacionalLocacaoForm,
    CancelarLocacaoForm,
    ConfiguracaoLocacaoForm,
    DevolucaoLocacaoForm,
    FaixaPrecoLocacaoForm,
    ItensLocacaoReservaForm,
    LocacaoForm,
    MovimentoEstoqueLocacaoForm,
    PagamentoLocacaoForm,
    ReciboStatusForm,
    TermoLocacaoForm,
)
from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao, MovimentoEstoqueLocacao, PagamentoLocacao


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
    data_inicio = parse_date(request.GET.get("data_inicio", "").strip() or "")
    data_fim = parse_date(request.GET.get("data_fim", "").strip() or "")
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
            "data_inicio": request.GET.get("data_inicio", ""),
            "data_fim": request.GET.get("data_fim", ""),
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
            try:
                locacao = Locacao.criar_reserva(
                    locacao_form.cleaned_data,
                    itens,
                    responsavel=locacao_form.cleaned_data.get("responsavel", ""),
                )
                sinal_valor = locacao_form.cleaned_data.get("sinal_valor")
                if sinal_valor and sinal_valor > 0:
                    pagamento = locacao.registrar_pagamento(
                        sinal_valor,
                        locacao_form.cleaned_data.get("sinal_forma_pagamento"),
                        observacao=locacao_form.cleaned_data.get("sinal_observacao", "") or "Sinal da locacao.",
                        responsavel=locacao_form.cleaned_data.get("responsavel", ""),
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
        locacao_form = LocacaoForm(initial={"faixa_preco": faixa_inicial})
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
        Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens", "eventos", "pagamentos"),
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
    return render(
        request,
        "locacoes/detalhe.html",
        {
            "locacao": locacao,
            "necessidade": necessidade,
            "disponibilidade": disponibilidade,
            "cancelar_form": CancelarLocacaoForm(),
            "acao_form": AcaoOperacionalLocacaoForm(),
            "pagamento_form": PagamentoLocacaoForm(),
        },
    )


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
    form = AcaoOperacionalLocacaoForm(request.POST)
    if form.is_valid():
        try:
            locacao.confirmar_entrega(
                responsavel=form.cleaned_data.get("responsavel", ""),
                observacao=form.cleaned_data.get("observacao", ""),
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))
        else:
            messages.success(request, "Entrega confirmada. Material esta na rua.")
    return redirect("locacoes:detalhe", pk=locacao.pk)


@ensure_csrf_cookie
def registrar_devolucao(request, pk):
    locacao = get_object_or_404(
        Locacao.objects.prefetch_related("itens").select_related("cliente"),
        pk=pk,
    )
    if locacao.status not in {
        Locacao.STATUS_SAIU_PARA_ENTREGA,
        Locacao.STATUS_ENTREGUE,
        Locacao.STATUS_PENDENTE_DEVOLUCAO,
    }:
        messages.warning(request, "Esta locacao nao pode registrar devolucao neste status.")
        return redirect("locacoes:detalhe", pk=locacao.pk)

    if request.method == "POST":
        form = DevolucaoLocacaoForm(request.POST, locacao=locacao)
        if form.is_valid():
            try:
                locacao.registrar_devolucao(
                    form.retornos_por_item(),
                    responsavel=form.cleaned_data.get("responsavel", ""),
                    observacao=form.cleaned_data.get("observacao", ""),
                )
            except ValidationError as exc:
                messages.warning(request, "; ".join(exc.messages))
            else:
                messages.success(request, "Devolucao registrada.")
                return redirect("locacoes:detalhe", pk=locacao.pk)
    else:
        form = DevolucaoLocacaoForm(locacao=locacao)

    linhas_devolucao = []
    for item in locacao.itens.all():
        linhas_devolucao.append({
            "item": item,
            "boa": form[f"item_{item.id}_boa"],
            "quebrada": form[f"item_{item.id}_quebrada"],
            "perdida": form[f"item_{item.id}_perdida"],
            "descartada": form[f"item_{item.id}_descartada"],
        })

    return render(
        request,
        "locacoes/devolucao.html",
        {
            "locacao": locacao,
            "form": form,
            "linhas_devolucao": linhas_devolucao,
        },
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
