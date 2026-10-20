from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import ajustes, acesso_ajustes
from .models import Inventario, RodadaContagem, AplicacaoAjuste
from .views import gestor, privado


@privado
@require_http_methods(['GET', 'POST'])
def painel(request, inventario_id):
    gestor(request)
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    revisao_historica = acesso_ajustes.exigir(request, inventario)
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
                messages.success(request, 'Operação de ajuste concluída. Confira os resultados e a auditoria abaixo.')
                if inventario.finalizada_em:
                    from django.urls import reverse
                    return redirect(reverse('inventario:detalhe', args=[inventario.pk]) + '#titulo-resumo')
                return redirect('inventario:ajustes', inventario.pk)
        except PermissionDenied:
            raise
        except (ValueError, ObjectDoesNotExist) as exc:
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
        token_aplicacao=ajustes.previa_atualizacao(inventario.pk, selecionadas) if selecionadas else '',
        habilitado=getattr(settings, 'INVENTARIO_AJUSTES_HABILITADOS', False),
        pode_aprovar=request.user.has_perm('inventario.aprovar_ajuste'),
        aplicacoes=Paginator(AplicacaoAjuste.objects.filter(proposta__rodada__item__inventario=inventario)
            .select_related('proposta__rodada__item', 'usuario', 'decisao').order_by('-aplicado_em', 'pk'), 30)
            .get_page(request.GET.get('auditoria_pagina'))))
