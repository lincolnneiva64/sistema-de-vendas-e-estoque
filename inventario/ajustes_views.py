from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import ajustes, acesso_ajustes, confirmacao_ajustes
from .models import Inventario, RodadaContagem, AplicacaoAjuste
from .views import gestor, privado


@privado
@require_http_methods(['GET', 'POST'])
def painel(request, inventario_id):
    try:
        gestor(request)
    except PermissionDenied as exc:
        if confirmacao_ajustes.solicitada(request):
            return confirmacao_ajustes.erro(request, exc, 403)
        raise
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    mensagens = confirmacao_ajustes.mensagens_validas(request, inventario_id)
    try:
        if request.method == 'GET' and confirmacao_ajustes.solicitada(request) and request.GET.get('resultado_atualizacao'):
            return confirmacao_ajustes.consultar(request, inventario_id)
        revisao_historica = acesso_ajustes.exigir(request, inventario)
    except PermissionDenied as exc:
        if confirmacao_ajustes.solicitada(request):
            return confirmacao_ajustes.erro(request, exc, 403)
        raise
    except ValueError as exc:
        if confirmacao_ajustes.solicitada(request):
            return confirmacao_ajustes.erro(request, exc)
        raise
    erro = ''
    if request.method == 'POST':
        try:
            with transaction.atomic():
                inventario = Inventario.objects.select_for_update().get(pk=inventario.pk)
                revisao_historica = acesso_ajustes.exigir(request, inventario)
                acao = request.POST.get('acao')
                if acao == 'atualizar':
                    ajustes.atualizar(request.user, inventario.pk, request.POST.get('previa'),
                        confirmacao=request.POST.get('confirmacao') == 'sim',
                        observacao=request.POST.get('observacao', ''))
                elif acao in {'aprovar', 'rejeitar'}:
                    rodada = get_object_or_404(RodadaContagem, pk=request.POST.get('rodada'), item__inventario=inventario)
                    ajustes.decidir(request.user, inventario.pk, rodada.pk, request.POST.get('previa'),
                        request.POST.get('justificativa'), aprovar=acao == 'aprovar',
                        confirmacao=request.POST.get('revisao_fisica') == 'sim')
                elif acao == 'aplicar':
                    ajustes.aplicar(request.user, inventario.pk, request.POST.get('previa'),
                        confirmacao=request.POST.get('confirmacao') == 'sim')
                else:
                    raise ValueError('Ação inválida.')
                comprovacao = (confirmacao_ajustes.conferir_gravacao(inventario.pk, request.POST.get('previa'))
                    if acao in {'atualizar', 'aplicar'} else None)
            if comprovacao:
                if not inventario.finalizada_em:
                    from django.urls import reverse
                    comprovacao['destino'] = reverse('inventario:ajustes', args=[inventario.pk])
                return confirmacao_ajustes.concluir(request, inventario.pk, comprovacao)
            messages.info(request, 'Revisão registrada. O estoque não foi atualizado por esta ação.')
            if inventario.finalizada_em:
                from django.urls import reverse
                return redirect(reverse('inventario:detalhe', args=[inventario.pk]) + '#titulo-resumo')
            return redirect('inventario:ajustes', inventario.pk)
        except PermissionDenied as exc:
            if confirmacao_ajustes.solicitada(request):
                return confirmacao_ajustes.erro(request, exc, 403)
            raise
        except (ValueError, ObjectDoesNotExist) as exc:
            if confirmacao_ajustes.solicitada(request):
                return confirmacao_ajustes.erro(request, exc)
            erro = str(exc)
    resultado = ajustes.preparar_resultado(inventario)
    linhas, aptas, resumo = resultado['linhas'], resultado['aptas'], resultado['resumo']
    erro = erro or resultado['erro']
    alvo = request.GET.get('confirmar')
    selecionadas = []
    if alvo == 'todos':
        selecionadas = aptas
    elif alvo:
        selecionadas = [l for l in aptas if str(l['rodada'].pk) == alvo]
    confirmacoes = selecionadas
    pagina = Paginator(linhas, 30).get_page(request.GET.get('pagina'))
    return render(request, 'inventario/ajustes.html', dict(inventario=inventario, linhas=pagina,
        revisao_historica=revisao_historica,
        resumo=resumo, erro=erro, confirmacoes=confirmacoes,
        messages=mensagens,
        ajustes_pendentes=sum(l['diferenca'] is not None and l['diferenca'] != 0 and l['estado'] != 'Aplicado' for l in linhas),
        token_aplicacao=ajustes.previa_atualizacao(inventario.pk, selecionadas) if selecionadas else '',
        habilitado=getattr(settings, 'INVENTARIO_AJUSTES_HABILITADOS', False),
        pode_aprovar=request.user.has_perm('inventario.aprovar_ajuste'),
        aplicacoes=Paginator(AplicacaoAjuste.objects.filter(proposta__rodada__item__inventario=inventario)
            .select_related('proposta__rodada__item', 'usuario', 'decisao').order_by('-aplicado_em', 'pk'), 30)
            .get_page(request.GET.get('auditoria_pagina'))))
