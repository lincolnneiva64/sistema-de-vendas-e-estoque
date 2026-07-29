from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .forms import (
    CancelarLocacaoForm,
    ConfiguracaoLocacaoForm,
    FaixaPrecoLocacaoForm,
    ItensLocacaoReservaForm,
    LocacaoForm,
    MovimentoEstoqueLocacaoForm,
)
from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao, MovimentoEstoqueLocacao


def _faixa_padrao():
    return FaixaPrecoLocacao.objects.filter(ativa=True).order_by("ordem", "id").first()


def lista(request):
    status = request.GET.get("status", "").strip()
    data_inicio = parse_date(request.GET.get("data_inicio", "").strip() or "")
    data_fim = parse_date(request.GET.get("data_fim", "").strip() or "")
    locacoes = Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens")
    if status in {Locacao.STATUS_RESERVADA, Locacao.STATUS_CANCELADA}:
        locacoes = locacoes.filter(status=status)
    if data_inicio:
        locacoes = locacoes.filter(data_entrega__gte=data_inicio)
    if data_fim:
        locacoes = locacoes.filter(data_entrega__lte=data_fim)
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
        Locacao.objects.select_related("cliente", "faixa_preco").prefetch_related("itens", "eventos"),
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
        },
    )


@require_POST
def cancelar(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = CancelarLocacaoForm(request.POST)
    if form.is_valid():
        locacao.cancelar(
            motivo=form.cleaned_data.get("motivo", ""),
            responsavel=form.cleaned_data.get("responsavel", ""),
        )
        messages.success(request, f"Reserva de locacao #{locacao.id} cancelada.")
    else:
        messages.warning(request, "Nao foi possivel cancelar a reserva.")
    return redirect("locacoes:detalhe", pk=locacao.pk)


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
