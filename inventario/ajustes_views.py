from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import ajustes
from .models import Inventario, RodadaContagem, AplicacaoAjuste
from .views import gestor, privado


@privado
@require_http_methods(['GET', 'POST'])
def painel(request, inventario_id):
    gestor(request)
    inventario = get_object_or_404(Inventario, pk=inventario_id)
    erro = ''
    if request.method == 'POST':
        try:
            acao = request.POST.get('acao')
            if acao in {'aprovar', 'rejeitar'}:
                rodada = get_object_or_404(RodadaContagem, pk=request.POST.get('rodada'), item__inventario=inventario)
                ajustes.decidir(request.user, inventario.pk, rodada.pk, request.POST.get('previa'),
                    request.POST.get('justificativa'), aprovar=acao == 'aprovar',
                    confirmacao=request.POST.get('revisao_fisica') == 'sim')
            elif acao == 'aplicar':
                ajustes.aplicar(request.user, inventario.pk, request.POST.get('previa'),
                    confirmacao=request.POST.get('confirmacao') == 'sim')
            else:
                raise ValueError('Ação inválida.')
            return redirect('inventario:ajustes', inventario.pk)
        except PermissionDenied:
            raise
        except (ValueError, ObjectDoesNotExist) as exc:
            erro = str(exc)
    ultima = RodadaContagem.objects.filter(item_id=OuterRef('item_id')).order_by('-numero').values('pk')[:1]
    rodadas = RodadaContagem.objects.filter(item__inventario=inventario, pk=Subquery(ultima))
    # Bound the conservative preflight; never silently apply an unexamined tail.
    if rodadas.count() > 500:
        return render(request, 'inventario/ajustes.html', dict(inventario=inventario,
            erro='Mais de 500 produtos: divida a revisão em conferências menores. Aplicação bloqueada.', linhas=[]))
    linhas = [ajustes.avaliar(r) for r in rodadas.select_related('item__produto').order_by('item__cadastro__nome', 'pk')]
    aptas = [l['proposta'] for l in linhas if l.get('proposta') and l['estado'] == 'Aprovado']
    resumo = dict(aptos=len(aptas), bloqueados=sum(l['estado'] == 'Bloqueado' for l in linhas),
        aplicados=sum(l['estado'] == 'Aplicado' for l in linhas),
        pendentes=sum(l['estado'] == 'Pendente de revisão' for l in linhas), total=len(linhas))
    alvo = request.GET.get('confirmar')
    selecionadas = []
    if alvo == 'todos':
        selecionadas = aptas
    elif alvo:
        selecionadas = [p for p in aptas if str(p.pk) == alvo]
    confirmacoes = [l for l in linhas if l.get('proposta') in selecionadas]
    pagina = Paginator(linhas, 30).get_page(request.GET.get('pagina'))
    return render(request, 'inventario/ajustes.html', dict(inventario=inventario, linhas=pagina,
        resumo=resumo, erro=erro, confirmacoes=confirmacoes,
        token_aplicacao=ajustes.previa_aplicacao(inventario.pk, selecionadas) if selecionadas else '',
        habilitado=getattr(settings, 'INVENTARIO_AJUSTES_HABILITADOS', False),
        pode_aprovar=request.user.has_perm('inventario.aprovar_ajuste'),
        aplicacoes=Paginator(AplicacaoAjuste.objects.filter(proposta__rodada__item__inventario=inventario)
            .select_related('proposta__rodada__item', 'usuario', 'decisao').order_by('-aplicado_em', 'pk'), 30)
            .get_page(request.GET.get('auditoria_pagina'))))
