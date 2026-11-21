"""Relatórios administrativos; sem escrita e sem cache financeiro compartilhado."""
from functools import wraps
from time import perf_counter

from django.contrib.auth.views import redirect_to_login
from django.db import connection
from django.db.models import Case, When, Value
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse, QueryDict
from django import forms
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.views.decorators.cache import cache_control, never_cache
from django.views.decorators.http import require_safe
from django.views.decorators.vary import vary_on_cookie

from .forms_relatorios import PeriodoRelatorioForm, EvolucaoRelatorioForm, intervalo_historico, BELEM
from .analise_evolucao import modo_padrao
from django.utils import timezone
from .services.relatorios import resumo_vendas
from .forms_relatorios import RankingProdutosForm, MedidaProdutoForm, PeriodoRankingProdutosForm
from .models import Produto
from .services.relatorios_produtos import ranking_produtos, unidades_produto, historico_produto, documentos_produto_dia


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


def _opcoes_evolucao(request, form, hoje, *, produto=False):
    padrao = modo_padrao(form.cleaned_data['inicio'], form.cleaned_data['fim'], hoje)
    analise_form = EvolucaoRelatorioForm({
        'modo': request.GET.get('modo', padrao),
        'dia_semana': request.GET.get('dia_semana', '-1' if produto else str(hoje.weekday())),
        'equivalente': request.GET.get('equivalente', ''),
        'alcance': request.GET.get('alcance', 'principal'),
        'historico_inicio': request.GET.get('historico_inicio', ''),
        'historico_fim': request.GET.get('historico_fim', ''),
        'comparacao': request.GET.get('comparacao', 'anterior'),
    })
    valida = analise_form.is_valid()
    opcoes = analise_form.cleaned_data if valida else {
        'modo': padrao, 'dia_semana': -1 if produto else hoje.weekday(), 'equivalente': False}
    historico = intervalo_historico(opcoes, hoje)
    if not historico and ((opcoes['modo'] == 'diario' and opcoes['dia_semana'] == -1) or
                          (opcoes['modo'] == 'mensal' and opcoes.get('comparacao') == 'ano_anterior')):
        historico = form.cleaned_data['inicio'], form.cleaned_data['fim']
    return analise_form, opcoes, historico, valida


def _parametros_produto(request, form, **extras):
    parametros = request.GET.copy()
    parametros.pop('pagina', None)
    for campo in ('periodo', 'data_inicial', 'data_final'):
        parametros[campo] = form[campo].value() or ''
    for campo, valor in extras.items():
        parametros[campo] = valor
    return parametros.urlencode()


@somente_administrador
def produtos_sugestoes(request):
    termo = request.GET.get('q', '').strip()[:120]
    if len(termo) < 2:
        return JsonResponse({'produtos': [], 'mais': False})
    # Catalog identity only: no prices, stock, sale history or per-product queries.
    produtos = list(Produto.objects.filter(excluido=False, nome__icontains=termo).annotate(
        _prioridade=Case(When(nome__istartswith=termo, then=Value(0)), default=Value(1)))
        .order_by('_prioridade', 'nome', 'pk').values('id', 'nome')[:9])
    return JsonResponse({'produtos': produtos[:8], 'mais': len(produtos) > 8})


@somente_administrador
def produtos(request):
    comeco, sql = perf_counter(), TempoSQL()
    form = PeriodoRankingProdutosForm(request.GET or {'periodo': 'mes_atual'})
    busca = RankingProdutosForm({'q': request.GET.get('q', ''), 'produto_id': request.GET.get('produto_id', ''),
                                'ordem': request.GET.get('ordem', 'faturamento')})
    contexto = dict(form=form, busca=busca, periodos_datas=form.periodos_datas)
    with connection.execute_wrapper(sql):
        if busca.is_valid() and busca.cleaned_data['produto_id']:
            selecionado = Produto.objects.only('pk', 'nome').filter(pk=busca.cleaned_data['produto_id']).first()
            if selecionado is None:
                busca.add_error('produto_id', 'Produto não encontrado. Pesquise novamente ou limpe a seleção.')
            else:
                contexto['produto_selecionado'] = selecionado
                busca.data = busca.data.copy()
                busca.data['q'] = selecionado.nome
        if form.is_valid() and busca.is_valid():
            contexto.update(ranking=ranking_produtos(form.cleaned_data['inicio'], form.cleaned_data['fim'],
                pesquisa=busca.cleaned_data['q'], produto_id=busca.cleaned_data['produto_id'],
                ordem=busca.cleaned_data['ordem'], pagina=request.GET.get('pagina', 1)),
                inicio=form.cleaned_data['inicio'], fim=form.cleaned_data['fim'])
        contexto['filtros_query'] = _parametros_produto(request, form,
            q=busca['q'].value() or '', produto_id=busca['produto_id'].value() or '')
        retorno = QueryDict(contexto['filtros_query']).copy()
        if contexto.get('ranking'):
            retorno['pagina'] = str(contexto['ranking']['linhas'].number)
        contexto['ranking_query'] = retorno.urlencode()
        rapido = QueryDict(contexto['filtros_query']).copy()
        for campo in ('q', 'produto_id', 'pagina', 'ranking'):
            rapido.pop(campo, None)
        rapido['ordem'] = busca['ordem'].value() or 'faturamento'
        contexto['filtro_rapido_campos'] = list(rapido.items())
        if contexto.get('produto_selecionado'):
            parametros = QueryDict(contexto['filtros_query']).copy()
            for campo in ('produto_id', 'unidade', 'pagina'):
                parametros.pop(campo, None)
            parametros['ranking'] = contexto['ranking_query']
            contexto['analise_url'] = reverse('estoque:relatorios_produto', args=[selecionado.pk]) + '?' + parametros.urlencode()
        contexto['opcoes_grafico'] = {k: request.GET[k] for k in (
            'modo', 'dia_semana', 'alcance', 'historico_inicio', 'historico_fim',
            'comparacao', 'equivalente', 'medida', 'unidade') if k in request.GET}
        return _resposta(request, 'estoque/relatorios/produtos.html', contexto, comeco, sql)


@somente_administrador
def produto(request, produto_id):
    comeco, sql = perf_counter(), TempoSQL()
    form = PeriodoRelatorioForm(request.GET or {'periodo': 'mes_atual'})
    contexto = dict(form=form, periodos_datas=form.periodos_datas, produto_relatorio=True,
                    relatorio_url=reverse('estoque:relatorios_produto', args=[produto_id]),
                    retorno_ranking=request.GET.get('ranking', ''))
    with connection.execute_wrapper(sql):
        contexto['produto'] = get_object_or_404(Produto.objects.only('pk', 'nome', 'excluido'), pk=produto_id)
        medida_form = MedidaProdutoForm({'medida': request.GET.get('medida', 'faturamento'),
            'unidade': request.GET.get('unidade', '')}, unidades=unidades_produto(produto_id))
        contexto['medida_form'] = medida_form
        if form.is_valid():
            hoje = timezone.localdate(timezone=BELEM)
            analise_form, opcoes, historico, valida = _opcoes_evolucao(request, form, hoje, produto=True)
            contexto.update(evolucao_form=analise_form, evolucao_invalida=not valida,
                            inicio=form.cleaned_data['inicio'], fim=form.cleaned_data['fim'])
            if medida_form.is_valid():
                medida, unidade = medida_form.cleaned_data['medida'], medida_form.cleaned_data['unidade']
                contexto.update(medida=medida, unidade=unidade,
                    resumo=historico_produto(produto_id, form.cleaned_data['inicio'], form.cleaned_data['fim'],
                        hoje=hoje, modo=opcoes['modo'], dia_semana=opcoes['dia_semana'],
                        historico=historico, comparacao=opcoes.get('comparacao', 'anterior'),
                        medida=medida, unidade=unidade, pagina=request.GET.get('pagina', 1)))
        contexto['filtros_query'] = _parametros_produto(request, form)
        contexto['retorno_ranking'] = contexto['retorno_ranking'] or contexto['filtros_query']
        documentos_query = QueryDict(mutable=True)
        for campo in ('periodo', 'data_inicial', 'data_final'):
            documentos_query[campo] = form[campo].value() or ''
        for campo in ('medida', 'unidade'):
            documentos_query[campo] = contexto.get(campo, '')
        contexto['documentos_query'] = documentos_query.urlencode()
        return _resposta(request, 'estoque/relatorios/produto.html', contexto, comeco, sql)


@somente_administrador
def produto_dia(request, produto_id):
    periodo = PeriodoRelatorioForm(request.GET)
    dia_campo = forms.DateField(input_formats=['%Y-%m-%d'])
    try:
        dia = dia_campo.clean(request.GET.get('dia'))
    except forms.ValidationError:
        return HttpResponseBadRequest('Informe uma data válida para conferir as notas.')
    if not periodo.is_valid() or not periodo.cleaned_data['inicio'] <= dia <= periodo.cleaned_data['fim']:
        return HttpResponseBadRequest('A data deve pertencer ao período consultado.')
    produto = get_object_or_404(Produto.objects.only('pk', 'nome'), pk=produto_id)
    medida = MedidaProdutoForm({'medida':request.GET.get('medida', 'faturamento'),
        'unidade':request.GET.get('unidade', '')}, unidades=unidades_produto(produto_id))
    if not medida.is_valid():
        return HttpResponseBadRequest('Escolha uma medida e uma unidade válidas.')
    unidade = medida.cleaned_data['unidade'] if medida.cleaned_data['medida'] == 'quantidade' else ''
    documentos = documentos_produto_dia(produto_id, dia, unidade=unidade, pagina=request.GET.get('pagina', 1))
    parametros = request.GET.copy()
    parametros.pop('pagina', None)
    return render(request, 'estoque/relatorios/produto_dia.html', dict(
        produto=produto, dia=dia, dia_notas=documentos, unidade=unidade, filtros_query=parametros.urlencode()))


@somente_administrador
def vendas(request):
    comeco, sql = perf_counter(), TempoSQL()
    form = PeriodoRelatorioForm(request.GET or {'periodo': 'mes_atual'})
    contexto = {'form': form, 'periodos_datas': form.periodos_datas}
    with connection.execute_wrapper(sql):
        if form.is_valid():
            hoje = timezone.localdate(timezone=BELEM)
            analise_form, opcoes, historico, evolucao_valida = _opcoes_evolucao(request, form, hoje)
            contexto['evolucao_invalida'] = not evolucao_valida
            contexto['evolucao_form'] = analise_form
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
