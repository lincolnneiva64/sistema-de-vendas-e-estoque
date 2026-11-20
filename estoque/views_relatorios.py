"""Relatórios administrativos; sem escrita e sem cache financeiro compartilhado."""
from functools import wraps
from time import perf_counter

from django.contrib.auth.views import redirect_to_login
from django.db import connection
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import cache_control, never_cache
from django.views.decorators.http import require_safe
from django.views.decorators.vary import vary_on_cookie

from .forms_relatorios import PeriodoRelatorioForm, EvolucaoRelatorioForm, intervalo_historico, BELEM
from .analise_evolucao import modo_padrao
from django.utils import timezone
from .services.relatorios import resumo_vendas


def somente_administrador(view):
    @wraps(view)
    @never_cache
    @cache_control(private=True)
    @vary_on_cookie
    @require_safe
    def protegida(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse('sistema_login'))
        if not (user.is_active and user.is_staff and user.is_superuser):
            return HttpResponseForbidden('Área restrita aos administradores autorizados.')
        return view(request, *args, **kwargs)
    return protegida


class TempoSQL:
    def __init__(self):
        self.segundos = 0

    def __call__(self, execute, sql, params, many, context):
        inicio = perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.segundos += perf_counter() - inicio


def _resposta(request, template, contexto, inicio, sql):
    resposta = render(request, template, contexto)
    total = perf_counter() - inicio
    # Somente tempos, sem SQL, filtros, clientes ou valores no cabeçalho.
    resposta['Server-Timing'] = (
        f'report-db;dur={sql.segundos * 1000:.2f}, '
        f'report-app;dur={max(0, total - sql.segundos) * 1000:.2f}, '
        f'report-total;dur={total * 1000:.2f}'
    )
    return resposta


@somente_administrador
def inicio(request):
    comeco = perf_counter()
    return _resposta(request, 'estoque/relatorios/inicio.html', {}, comeco, TempoSQL())


@somente_administrador
def vendas(request):
    comeco, sql = perf_counter(), TempoSQL()
    form = PeriodoRelatorioForm(request.GET or {'periodo': 'mes_atual'})
    contexto = {'form': form, 'periodos_datas': form.periodos_datas}
    with connection.execute_wrapper(sql):
        if form.is_valid():
            hoje = timezone.localdate(timezone=BELEM)
            padrao = modo_padrao(form.cleaned_data['inicio'], form.cleaned_data['fim'], hoje)
            analise_form = EvolucaoRelatorioForm({
                'modo': request.GET.get('modo', padrao),
                'dia_semana': request.GET.get('dia_semana', str(hoje.weekday())),
                'equivalente': request.GET.get('equivalente', ''),
                'alcance': request.GET.get('alcance', 'principal'),
                'historico_inicio': request.GET.get('historico_inicio', ''),
                'historico_fim': request.GET.get('historico_fim', ''),
                'comparacao': request.GET.get('comparacao', 'anterior'),
            })
            evolucao_valida = analise_form.is_valid()
            contexto['evolucao_invalida'] = not evolucao_valida
            opcoes = analise_form.cleaned_data if evolucao_valida else {
                'modo': padrao, 'dia_semana': hoje.weekday(), 'equivalente': False}
            contexto['evolucao_form'] = analise_form
            historico = intervalo_historico(opcoes, hoje)
            if not historico and ((opcoes['modo'] == 'diario' and opcoes['dia_semana'] == -1) or
                                  (opcoes['modo'] == 'mensal' and opcoes.get('comparacao') == 'ano_anterior')):
                historico = form.cleaned_data['inicio'], form.cleaned_data['fim']
            calculo = {k: opcoes[k] for k in ('modo', 'dia_semana', 'equivalente')}
            calculo.update(historico=historico, comparacao=opcoes.get('comparacao', 'anterior'))
            contexto.update(resumo=resumo_vendas(form.cleaned_data['inicio'], form.cleaned_data['fim'],
                                                request.GET.get('pagina', 1), hoje=hoje, **calculo),
                            inicio=form.cleaned_data['inicio'], fim=form.cleaned_data['fim'])
        parametros = request.GET.copy()
        parametros.pop('pagina', None)
        for campo in ('periodo', 'data_inicial', 'data_final'):
            parametros[campo] = form[campo].value() or ''
        if 'evolucao_form' in contexto:
            parametros['modo'] = opcoes['modo']
            parametros['dia_semana'] = str(opcoes['dia_semana'])
            parametros.pop('equivalente', None)
            if opcoes['equivalente']:
                parametros['equivalente'] = 'on'
            for campo in ('alcance', 'historico_inicio', 'historico_fim', 'comparacao'):
                valor = opcoes.get(campo)
                if valor:
                    parametros[campo] = valor.isoformat() if hasattr(valor, 'isoformat') else valor
        contexto['filtros_query'] = parametros.urlencode()
        return _resposta(request, 'estoque/relatorios/vendas.html', contexto, comeco, sql)
