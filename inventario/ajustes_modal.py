"""Modal HTTP adapter for the existing preview, application and verified receipt."""
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from . import acesso_ajustes, ajustes, confirmacao_ajustes, services
from .models import Inventario
from .views import gestor, privado


def estado(inventario):
    resultado = ajustes.preparar_resultado(inventario)
    bloqueados = [dict(nome=l['rodada'].item.cadastro['nome'], motivo=l['bloqueio'])
                  for l in resultado['linhas'] if l['estado'] == 'Bloqueado']
    pendentes = sum(l['estado'] != 'Aplicado' and l['diferenca'] is not None and l['diferenca'] != 0
                    for l in resultado['linhas'])
    return resultado, dict(aptos=len(resultado['aptas']), bloqueados=bloqueados,
        pendentes=pendentes, aplicados=resultado['resumo'].get('aplicados', 0), erro=resultado['erro'])


def recibo(inventario, previa):
    comprovacao = confirmacao_ajustes.resultado(inventario.pk, previa)
    if comprovacao['ok']:
        # Audit receipt, not a success flag from the client or a coincident balance.
        _, resumo = estado(inventario)
        comprovacao.update(quantidade=len(comprovacao['aplicacoes']), conferencia_id=inventario.pk,
            conferencia_nome=inventario.titulo, resumo=resumo)
    return comprovacao


@privado
@csrf_protect
@require_http_methods(['GET', 'POST'])
def modal(request, inventario_id):
    try:
        gestor(request)
        services.permitir(request.user, 'aprovar_ajuste')
        inventario = get_object_or_404(Inventario, pk=inventario_id)
        services.exigir_nao_cancelada(inventario)
        if request.method == 'GET':
            if request.GET.get('resultado_atualizacao'):
                return JsonResponse(recibo(inventario, request.GET['resultado_atualizacao']))
            _, resumo = estado(inventario)
            return JsonResponse(dict(ok=True, resumo=resumo))
        acao = request.POST.get('acao')
        if acao == 'prever':
            # This explicit, CSRF-protected click replaces the historical access page.
            historica = acesso_ajustes.iniciar(request.user, inventario) if inventario.finalizada_em else ''
            resultado, resumo = estado(inventario)
            precisao = [l for l in resultado['linhas'] if l.get('bloqueio_codigo') == 'precisao']
            outros_bloqueados = [l for l in resultado['linhas']
                                if l['estado'] == 'Bloqueado' and l.get('bloqueio_codigo') != 'precisao']
            token = ajustes.previa_atualizacao(inventario.pk, resultado['aptas']) if resultado['aptas'] else ''
            html = render_to_string('inventario/previa_ajustes_modal.html', dict(
                inventario=inventario, atualizacao=resultado, previa_lote=token,
                pendentes_precisao=precisao, outros_bloqueados=outros_bloqueados,
                revisao_historica=historica, ajustes_habilitados=getattr(settings,'INVENTARIO_AJUSTES_HABILITADOS',False)), request=request)
            return JsonResponse(dict(ok=True, html=html, resumo=resumo))
        if acao != 'atualizar':
            raise ValueError('Acao invalida.')
        with transaction.atomic():
            inventario = Inventario.objects.select_for_update().get(pk=inventario_id)
            acesso_ajustes.exigir(request, inventario)
            ajustes.atualizar(request.user, inventario.pk, request.POST.get('previa'),
                confirmacao=request.POST.get('confirmacao') == 'sim', observacao=request.POST.get('observacao',''))
            comprovacao = confirmacao_ajustes.conferir_gravacao(inventario.pk, request.POST.get('previa'))
        # No flash or persistent success parameter: only the committed receipt certifies success.
        _, resumo = estado(inventario)
        comprovacao.update(quantidade=len(comprovacao['aplicacoes']), conferencia_id=inventario.pk,
            conferencia_nome=inventario.titulo, resumo=resumo)
        return JsonResponse(comprovacao)
    except PermissionDenied as exc:
        return confirmacao_ajustes.erro(request, exc, 403)
    except ValueError as exc:
        return confirmacao_ajustes.erro(request, exc)
