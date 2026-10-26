"""Read-only grouping of rentals; never regularizes historical records."""
from datetime import date

from django.utils import timezone

from .models import Locacao, TarefaOperacionalLocacao


MARCO_OPERACIONAL = date(2026, 10, 1)
ABAS = (("entregas", "Entregas"), ("recolhimentos", "Recolhimentos"))


def resumo_financeiro_locacoes(cliente="", status="", financeiro="", inicio=None, fim=None):
    """Duas agregações independentes, sem filas operacionais ou paginação."""
    from datetime import datetime, time
    from decimal import Decimal
    from django.db.models import Count, Q, Sum
    from .models import PagamentoLocacao

    contratos = Locacao.objects.all()
    if cliente:
        contratos = contratos.filter(Q(cliente__nome__icontains=cliente) | Q(pessoa_avulsa_nome__icontains=cliente))
    if status in dict(Locacao.STATUS_CHOICES):
        contratos = contratos.filter(status=status)
    if financeiro == "com_saldo":
        contratos = contratos.filter(saldo_devedor__gt=0)
    elif financeiro == "quitadas":
        contratos = contratos.filter(saldo_devedor__lte=0)

    # Pagamentos de contratos anteriores também entram; não limitar pelo cadastro.
    pagamentos = PagamentoLocacao.objects.filter(locacao_id__in=contratos.values("pk"))
    def periodo(queryset, campo):
        if inicio:
            limite = timezone.make_aware(datetime.combine(inicio, time.min), timezone.get_current_timezone())
            queryset = queryset.filter(**{campo + "__gte": limite})
        if fim:
            limite = timezone.make_aware(datetime.combine(fim, time.max), timezone.get_current_timezone())
            queryset = queryset.filter(**{campo + "__lte": limite})
        return queryset

    cancelada = Q(status=Locacao.STATUS_CANCELADA)
    valores = periodo(contratos, "criado_em").aggregate(
        quantidade=Count("pk", filter=~cancelada),
        contratado=Sum("total", filter=~cancelada, default=Decimal("0.00")),
        saldo=Sum("saldo_devedor", filter=~cancelada, default=Decimal("0.00")),
        canceladas_quantidade=Count("pk", filter=cancelada),
        canceladas_contratado=Sum("total", filter=cancelada, default=Decimal("0.00")),
    )
    cancelada = Q(locacao__status=Locacao.STATUS_CANCELADA)
    valores.update(periodo(pagamentos, "data_hora").aggregate(
        recebido=Sum("valor", filter=~cancelada, default=Decimal("0.00")),
        canceladas_recebido=Sum("valor", filter=cancelada, default=Decimal("0.00")),
    ))
    valores["somente_canceladas"] = status == Locacao.STATUS_CANCELADA
    valores["inicio"] = inicio
    valores["fim"] = fim
    restricoes = []
    if cliente:
        restricoes.append("Cliente: " + cliente)
    if status in dict(Locacao.STATUS_CHOICES):
        restricoes.append("Status atual: " + dict(Locacao.STATUS_CHOICES)[status])
    if financeiro in {"com_saldo", "quitadas"}:
        restricoes.append("Financeiro atual: " + {"com_saldo": "Com saldo a receber", "quitadas": "Quitadas"}[financeiro])
    valores["restricoes"] = restricoes
    return valores


def consulta_operacional(queryset, aba, realizadas=False, todas=False):
    """Filtra e ordena no banco antes da paginação; não cria tarefas nem altera status."""
    from django.db.models import DateTimeField, Exists, F, OuterRef, Q, Subquery
    from django.db.models.functions import Cast, Coalesce, TruncDate
    from .models import ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao, EventoLocacao

    entrega = aba == "entregas"
    if entrega and todas:
        pendentes = consulta_operacional(queryset, aba)
        concluidas = consulta_operacional(queryset, aba, realizadas=True).annotate(
            _data_entrega_efetiva=Coalesce(TruncDate(F("_conclusao_registrada")), F("data_entrega")),
        )
        # Reutiliza a evidência física e a proteção do legado da consulta atual.
        return queryset.filter(
            Q(pk__in=pendentes.values("pk")) |
            Q(pk__in=concluidas.filter(_data_entrega_efetiva__gte=MARCO_OPERACIONAL).values("pk"))
        ).annotate(
            _conclusao_registrada=Subquery(concluidas.filter(pk=OuterRef("pk")).values("_conclusao_registrada")[:1]),
            _data_operacao=Coalesce(TruncDate(F("_conclusao_registrada")),
                Subquery(pendentes.filter(pk=OuterRef("pk")).values("_data_operacao")[:1]), F("data_entrega")),
            _horario_operacao=Coalesce(Subquery(pendentes.filter(pk=OuterRef("pk")).values("_horario_operacao")[:1]), F("horario_entrega")),
        ).order_by("-_data_operacao", F("_horario_operacao").desc(nulls_last=True), "-id")
    tipo = "entrega" if entrega else "recolhimento"
    tarefas = TarefaOperacionalLocacao.objects.filter(locacao_id=OuterRef("pk"), tipo=tipo)
    abertas = tarefas.filter(status__in=["pendente", "parcial", "nao_possivel"])
    campo_data = "data_entrega" if entrega else "data_prevista_devolucao"
    campo_hora = "horario_entrega" if entrega else "horario_previsto_devolucao"
    queryset = queryset.annotate(
        _tem_tarefa=Exists(tarefas), _tarefa_aberta=Exists(abertas),
        _data_operacao=Coalesce(Subquery(tarefas.values("data_agendada")[:1]), F(campo_data)),
        _horario_operacao=Coalesce(Subquery(tarefas.values("horario_agendado")[:1]), F(campo_hora)),
    )
    if realizadas:
        encerradas = [Locacao.STATUS_DEVOLVIDA, Locacao.STATUS_DEVOLVIDA_COM_AVARIA]
        if entrega:
            encerradas += [Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO]
        conferencia = ConferenciaEntregaLocacao if entrega else ConferenciaRecolhimentoLocacao
        eventos = EventoLocacao.objects.filter(locacao_id=OuterRef("pk"), tipo__in=(
            ["entregue", "checklist_entrega_completa", "entrega_confirmada_operacional"] if entrega
            else ["devolucao", "checklist_recolhimento_completo"]
        )).order_by("-criado_em")
        return queryset.filter(status__in=encerradas).annotate(
            _conclusao_registrada=Coalesce(
                Subquery(eventos.values("criado_em")[:1]),
                Subquery(conferencia.objects.filter(locacao_id=OuterRef("pk")).order_by("-criado_em").values("criado_em")[:1]),
                Subquery(tarefas.order_by("-confirmado_em").values("confirmado_em")[:1]),
            ),
        ).annotate(
            _concluida_em=Coalesce(F("_conclusao_registrada"), Cast(F(campo_data), DateTimeField())),
        ).order_by("-_concluida_em", "-id")

    # Mantém a proteção existente contra reservas antigas sem evidência operacional atual.
    # Reagendar ou registrar pagamento, isoladamente, não reabre o legado.
    todas_tarefas = TarefaOperacionalLocacao.objects.filter(locacao_id=OuterRef("pk"))
    queryset = queryset.annotate(
        _entrega_recente=Exists(ConferenciaEntregaLocacao.objects.filter(locacao_id=OuterRef("pk"), criado_em__date__gte=MARCO_OPERACIONAL)),
        _recolhimento_recente=Exists(ConferenciaRecolhimentoLocacao.objects.filter(locacao_id=OuterRef("pk"), criado_em__date__gte=MARCO_OPERACIONAL)),
        _tentativa_recente=Exists(todas_tarefas.filter(status__in=["pendente", "parcial", "nao_possivel"], tentativa_em__date__gte=MARCO_OPERACIONAL)),
    )
    estados = [Locacao.STATUS_RESERVADA, Locacao.STATUS_SAIU_PARA_ENTREGA] if entrega else [Locacao.STATUS_ENTREGUE, Locacao.STATUS_PENDENTE_DEVOLUCAO]
    return queryset.filter(status__in=estados).filter(
        Q(_tem_tarefa=False) | Q(_tarefa_aberta=True)
    ).filter(
        Q(data_entrega__gte=MARCO_OPERACIONAL) | Q(_entrega_recente=True) |
        Q(_recolhimento_recente=True) | Q(_tentativa_recente=True)
    ).order_by("_data_operacao", F("_horario_operacao").asc(nulls_last=True), "id")


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
