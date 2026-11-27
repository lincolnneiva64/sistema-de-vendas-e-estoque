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
from .services.analise_desempenho import desempenho_comercial
from .services.inteligencia_clientes import participacao_clientes
from .services.investigacao_comercial import (
    investigar_classes, investigar_produtos, investigar_clientes_produto,
    outros_produtos_somente_atual, chave_classe, ordenar_coluna, COLUNAS_ORDENACAO, normalizar_chave_classe,
)
from .forms_relatorios import RankingProdutosForm, MedidaProdutoForm, PeriodoRankingProdutosForm
from django.core.paginator import Paginator
from .services.relatorios_compradores import compradores_produto
from .services.relatorios_compradores import notas_comprador_produto
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


@somente_administrador
def classes_produtos(request):
    from .forms_relatorios import AnaliseClassesProdutosForm
    from .services.analise_classes_produtos import analise_consolidada, categorias_disponiveis
    from .services.participacao_classes import participacao_classes
    comeco, sql = perf_counter(), TempoSQL()
    periodo = PeriodoRankingProdutosForm(request.GET)
    with connection.execute_wrapper(sql):
        filtros = AnaliseClassesProdutosForm(request.GET, classes=categorias_disponiveis())
        contexto = dict(form=periodo, filtros=filtros, periodos_datas=periodo.periodos_datas)
        if periodo.is_valid() and filtros.is_valid():
            inicio, fim = periodo.cleaned_data['inicio'], periodo.cleaned_data['fim']
            dados = filtros.cleaned_data
            analise = analise_consolidada(inicio, fim, hoje=timezone.localdate(timezone=BELEM),
                pesquisa=dados['q'], classe_de=dados['classe_de'], classe_ate=dados['classe_ate'],
                ordem=dados['ordem'], comparacao=dados['comparacao'])
            participacao = participacao_classes(analise)
            for classe in analise['classes']:
                classe['participacao_faturamento'] = participacao['por_classe'].get(classe['chave'])
            contexto['participacao_classes'] = participacao
            parametros = request.GET.copy()
            for nome, valor in dict(periodo=periodo['periodo'].value(),
                    data_inicial=inicio.isoformat(), data_final=fim.isoformat(), **dados).items():
                parametros[nome] = valor
            parametros.pop('pagina', None)
            retorno = parametros.urlencode()
            # Reuse the existing product/rentability/buyer reports and their filters.
            for c in analise['classes']:
                for p in (linha for grupo in c['produtos'] for linha in [grupo,*grupo['apresentacoes']]):
                    if p['produto_id'] is not None:
                        destino = parametros.copy()
                        for campo in ('classe_de','classe_ate','q','ordem'):
                            destino.pop(campo, None)
                        destino['retorno_consolidado'] = retorno
                        destino['medida'] = 'quantidade' if p['unidade'] else 'faturamento'
                        destino['unidade'] = p['unidade']
                        destino.pop('investigar', None)
                        p['url'] = reverse('estoque:relatorios_produto',args=[p['produto_id']]) + '?' + destino.urlencode()
            for p in analise['atencao']['quedas']:
                if not p.get('alvo_atencao') and p['produto_id'] is not None:
                    destino = parametros.copy()
                    for campo in ('classe_de', 'classe_ate', 'q', 'ordem', 'investigar'):
                        destino.pop(campo, None)
                    destino['retorno_consolidado'] = retorno
                    destino['medida'] = 'quantidade' if p['unidade'] else 'faturamento'
                    destino['unidade'] = p['unidade']
                    p['url'] = reverse('estoque:relatorios_produto', args=[p['produto_id']]) + '?' + destino.urlencode()
            contexto.update(analise=analise, inicio=inicio, fim=fim, filtros_query=retorno)
    return _resposta(request, 'estoque/relatorios/classes_produtos.html', contexto, comeco, sql)


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


def _investigacao_query(request, form, **extras):
    parametros = QueryDict(_parametros_produto(request, form)).copy()
    for campo in ('pagina', 'pagina_investigacao', 'pagina_clientes', 'pagina_outros', 'notas_antes', 'notas_agora'):
        parametros.pop(campo, None)
    parametros['investigar'] = 'classes'
    if 'classe' in parametros:
        parametros['classe'] = normalizar_chave_classe(parametros['classe'])
    for campo, valor in extras.items():
        if valor is None:
            parametros.pop(campo, None)
        else:
            parametros[campo] = valor
    return parametros


def _ordem_investigacao(request):
    direcao = request.GET.get('direcao', 'impacto')
    return direcao if direcao in ('impacto', 'quedas', 'altas') else None


def _investigar_classes_pagina(request, comeco, sql):
    form = PeriodoRankingProdutosForm(request.GET)
    contexto = dict(form=form, periodos_datas=form.periodos_datas)
    coluna = request.GET.get('ordenar_por') or None
    sentido = request.GET.get('ordem_coluna', 'asc' if coluna == 'nome' else 'desc')
    if (coluna and coluna not in COLUNAS_ORDENACAO) or sentido not in ('asc', 'desc'):
        return HttpResponseBadRequest('Escolha uma coluna e uma ordem válidas.')
    direcao = _ordem_investigacao(request)
    if direcao is None:
        return HttpResponseBadRequest('Escolha uma ordem válida para a investigação.')
    with connection.execute_wrapper(sql):
        if form.is_valid():
            hoje = timezone.localdate(timezone=BELEM)
            evolucao, opcoes, _, valida = _opcoes_evolucao(request, form, hoje)
            contexto['evolucao_form'] = evolucao
            if not valida:
                return HttpResponseBadRequest('Confira as opções de comparação do período.')
            desempenho = desempenho_comercial(form.cleaned_data['inicio'], form.cleaned_data['fim'],
                                              hoje, opcoes.get('comparacao', 'anterior'))
            classes = investigar_classes(desempenho, direcao)
            contexto.update(desempenho=desempenho, investigacao=classes)
            if classes['disponivel']:
                # Apenas apresentação: totais, reconciliação e classes por chave intactos.
                mostrar_sem_vendas = request.GET.get('classes_sem_vendas') == '1'
                contexto['classes_exibidas'] = [linha for linha in classes['linhas']
                    if mostrar_sem_vendas or linha['anterior'] != 0 or linha['atual'] != 0]
                if coluna:
                    contexto['classes_exibidas'] = ordenar_coluna(contexto['classes_exibidas'], coluna, sentido)
                contexto['mostrar_classes_sem_vendas'] = mostrar_sem_vendas
                alternar = _investigacao_query(request, form, classe=None)
                if mostrar_sem_vendas:
                    alternar.pop('classes_sem_vendas', None)
                else:
                    alternar['classes_sem_vendas'] = '1'
                contexto['alternar_classes_sem_vendas_url'] = '?' + alternar.urlencode() + '#classes-investigadas'
                classe = normalizar_chave_classe(request.GET.get('classe'))
                if classe is not None:
                    if classe not in classes['por_chave']:
                        return HttpResponseBadRequest('A classe informada não pertence a esta investigação.')
                    selecionada = classes['por_chave'][classe]
                    produtos = investigar_produtos(desempenho, selecionada,
                        pagina=request.GET.get('pagina_investigacao', 1), direcao=direcao, coluna=coluna, sentido=sentido)
                    contexto.update(classe_selecionada=selecionada, produtos_investigados=produtos)
                    if produtos['disponivel']:
                        for linha in [*produtos['linhas'], produtos['queda'], produtos['alta']]:
                            if linha is not None and linha['id'] is not None:
                                parametros = _investigacao_query(request, form, cliente_investigado=None)
                                parametros['ranking'] = request.GET.urlencode()
                                linha['url'] = reverse('estoque:relatorios_produto', args=[linha['id']]) + '?' + parametros.urlencode() + '#compradores-produto'
                for linha in classes['linhas']:
                    linha['url'] = reverse('estoque:relatorios_produtos') + '?' + _investigacao_query(
                        request, form, classe=linha['chave']).urlencode()
        parametros = _investigacao_query(request, form)
        contexto['investigacao_query'] = parametros.urlencode()
        contexto['classes_url'] = reverse('estoque:relatorios_produtos') + '?' + _investigacao_query(
            request, form, classe=None).urlencode()
        retorno = parametros.copy()
        for campo in ('investigar', 'classe', 'direcao', 'ranking', 'cliente_investigado'):
            retorno.pop(campo, None)
        contexto['vendas_url'] = reverse('estoque:relatorios_vendas') + '?' + retorno.urlencode() + '#analise-desempenho'
        contexto['campos_investigacao'] = [(k, v) for k, v in parametros.items()
            if k not in ('periodo', 'data_inicial', 'data_final', 'comparacao')]
        contexto['cabecalhos_ordenacao'] = []
        for chave, rotulo in (('nome', 'Produto' if request.GET.get('classe') is not None else 'Classe'),
                              ('anterior', 'Antes'), ('atual', 'Agora'),
                              ('diferenca', 'Diferença (R$)'), ('percentual', 'Variação (%)')):
            ativo = coluna == chave
            proximo = ('desc' if sentido == 'asc' else 'asc') if ativo else ('asc' if chave == 'nome' else 'desc')
            url = '?' + _investigacao_query(request, form, ordenar_por=chave, ordem_coluna=proximo).urlencode()
            contexto['cabecalhos_ordenacao'].append(dict(chave=chave, rotulo=rotulo, ativo=ativo,
                aria='ascending' if sentido == 'asc' else 'descending', seta='↑' if sentido == 'asc' else '↓',
                url=url + ('#produtos-investigados' if request.GET.get('classe') is not None else '#classes-investigadas')))
        contexto['ordens_investigacao'] = [dict(nome=nome, selecionada=not coluna and direcao == chave,
            url='?' + _investigacao_query(request, form, direcao=chave, ordenar_por=None, ordem_coluna=None).urlencode())
            for chave, nome in (('impacto', 'Maior mudança em reais'), ('quedas', 'Maiores reduções'), ('altas', 'Maiores aumentos'))]
        return _resposta(request, 'estoque/relatorios/investigacao_classes.html', contexto, comeco, sql)


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
    if request.GET.get('investigar') == 'classes':
        return _investigar_classes_pagina(request, comeco, sql)
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
        contexto['produto'] = get_object_or_404(Produto.objects.only('pk', 'nome', 'excluido', 'categoria'), pk=produto_id)
        medida_form = MedidaProdutoForm({'medida': request.GET.get('medida', 'faturamento'),
            'unidade': request.GET.get('unidade', '')}, unidades=unidades_produto(produto_id))
        contexto['medida_form'] = medida_form
        if form.is_valid():
            hoje = timezone.localdate(timezone=BELEM)
            analise_form, opcoes, historico, valida = _opcoes_evolucao(request, form, hoje, produto=True)
            if request.GET.get('investigar') == 'classes' and not valida:
                return HttpResponseBadRequest('Confira as opções de comparação do período.')
            contexto.update(evolucao_form=analise_form, evolucao_invalida=not valida,
                            inicio=form.cleaned_data['inicio'], fim=form.cleaned_data['fim'])
            if medida_form.is_valid():
                medida, unidade = medida_form.cleaned_data['medida'], medida_form.cleaned_data['unidade']
                contexto.update(medida=medida, unidade=unidade,
                    resumo=historico_produto(produto_id, form.cleaned_data['inicio'], form.cleaned_data['fim'],
                        hoje=hoje, modo=opcoes['modo'], dia_semana=opcoes['dia_semana'],
                        historico=historico, comparacao=opcoes.get('comparacao', 'anterior'),
                        medida=medida, unidade=unidade, pagina=request.GET.get('pagina', 1)))
        if 'resumo' in contexto and request.GET.get('investigar') == 'classes':
            direcao = _ordem_investigacao(request)
            if direcao is None:
                return HttpResponseBadRequest('Escolha uma ordem válida para a investigação.')
            desempenho = desempenho_comercial(contexto['inicio'], contexto['fim'], hoje, opcoes.get('comparacao', 'anterior'))
            classes = investigar_classes(desempenho, direcao)
            contexto.update(investigando=True, desempenho=desempenho, investigacao=classes)
            if classes['disponivel']:
                classe = chave_classe(contexto['produto'].categoria)
                if normalizar_chave_classe(request.GET.get('classe', classe)) != classe:
                    return HttpResponseBadRequest('Este produto não pertence à classe selecionada.')
                unidade = contexto.get('unidade', '') if contexto.get('medida') == 'quantidade' else ''
                clientes = investigar_clientes_produto(desempenho, produto_id,
                    pagina=request.GET.get('pagina_clientes', 1), direcao=direcao, unidade=unidade)
                contexto['clientes_produto_investigados'] = clientes
                if clientes['disponivel']:
                    for linha in clientes['linhas']:
                        parametro_cliente = 'sem-cliente' if linha['id'] is None else str(linha['id'])
                        linha['url'] = '?' + _investigacao_query(request, form,
                            cliente_investigado=parametro_cliente).urlencode() + '#evidencias-cliente'
                    cliente_param = request.GET.get('cliente_investigado')
                    if cliente_param is not None:
                        if cliente_param == 'sem-cliente':
                            cliente_id = None
                        elif cliente_param.isascii() and cliente_param.isdecimal() and len(cliente_param) <= 18:
                            cliente_id = int(cliente_param)
                        else:
                            return HttpResponseBadRequest('Informe um ID de cliente válido.')
                        if cliente_id not in clientes['por_id']:
                            return HttpResponseBadRequest('O cadastro não consta nas compras deste produto nos períodos comparados.')
                        contexto['cliente_investigado'] = clientes['por_id'][cliente_id]
                        contexto['notas_antes'] = notas_comprador_produto(produto_id,
                            desempenho['referencia_inicio'], desempenho['referencia_fim'], cliente_id,
                            pagina=request.GET.get('notas_antes', 1), unidade=unidade)
                        contexto['notas_agora'] = notas_comprador_produto(produto_id,
                            desempenho['inicio'], desempenho['fim'], cliente_id,
                            pagina=request.GET.get('notas_agora', 1), unidade=unidade)
                        # Sem vínculo não equivale a uma pessoa: não inferir sua cesta.
                        if cliente_id is not None:
                            outros = outros_produtos_somente_atual(desempenho, produto_id, cliente_id,
                                pagina=request.GET.get('pagina_outros', 1))
                            for linha in outros:
                                if linha['produto_id'] is not None:
                                    linha['url'] = reverse('estoque:relatorios_produto', args=[linha['produto_id']]) + '?' + _investigacao_query(
                                        request, form, classe=None, unidade=None, medida='faturamento').urlencode() + '#compradores-produto'
                            contexto['outros_produtos_investigados'] = outros
            parametros = _investigacao_query(request, form)
            contexto['investigacao_query'] = parametros.urlencode()
            contexto['campos_investigacao_produto'] = [(k, v) for k, v in parametros.items()
                if k in ('investigar', 'classe', 'direcao')]
            contexto['ordens_investigacao'] = [dict(nome=nome, selecionada=direcao == chave,
                url='?' + _investigacao_query(request, form, direcao=chave).urlencode() + '#compradores-produto')
                for chave, nome in (('impacto', 'Maior mudança em reais'), ('quedas', 'Maiores reduções'), ('altas', 'Maiores aumentos'))]
            retorno_classe = _investigacao_query(
                request, form, cliente_investigado=None, classe=chave_classe(contexto['produto'].categoria),
                ranking=None, medida=None, unidade=None)
            origem = QueryDict(contexto['retorno_ranking'])
            if origem.get('classe') == retorno_classe.get('classe'):
                retorno_classe['pagina_investigacao'] = origem.get('pagina_investigacao', '1')
            contexto['classes_produto_url'] = reverse('estoque:relatorios_produtos') + '?' + retorno_classe.urlencode()
        elif 'resumo' in contexto:
            compradores = compradores_produto(
                produto_id, contexto['inicio'], contexto['fim']
            )
            contexto['compradores_pagina'] = Paginator(
                compradores, 25
            ).get_page(request.GET.get('pagina_clientes', 1))
            contexto['total_compradores'] = len(compradores)
            parametros = request.GET.copy()
            parametros.pop('pagina_clientes', None)
            contexto['compradores_query'] = parametros.urlencode()
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
def produto_comprador_notas(request, produto_id):
    periodo = PeriodoRelatorioForm(request.GET)
    if not periodo.is_valid():
        return HttpResponseBadRequest("Periodo invalido.")

    cliente_param = request.GET.get("cliente_id", "")
    if cliente_param == "sem-cliente":
        cliente_id = None
    elif cliente_param.isascii() and cliente_param.isdecimal() and len(cliente_param) <= 18:
        cliente_id = int(cliente_param)
    else:
        return HttpResponseBadRequest("Cliente invalido.")

    get_object_or_404(Produto.objects.only("pk"), pk=produto_id)

    notas = notas_comprador_produto(
        produto_id,
        periodo.cleaned_data["inicio"],
        periodo.cleaned_data["fim"],
        cliente_id,
        pagina=request.GET.get("pagina_notas", 1),
    )

    parametros = request.GET.copy()
    parametros.pop("pagina_notas", None)

    return render(
        request,
        "estoque/relatorios/produto_comprador_notas.html",
        {"notas": notas, "parametros_notas": parametros.urlencode()},
    )


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
            contexto['desempenho'] = desempenho_comercial(
                form.cleaned_data['inicio'], form.cleaned_data['fim'], hoje,
                opcoes.get('comparacao', 'anterior'))
            contexto['clientes_variacao'] = participacao_clientes(contexto['desempenho'])
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
