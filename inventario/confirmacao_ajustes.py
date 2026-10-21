"""HTTP outcome of the existing adjustment service; never writes stock."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.urls import reverse

from . import ajustes, services
from .models import AplicacaoAjuste, Inventario


def solicitada(request):
    return request.headers.get('X-Inventario-Ajuste') == '1'


def erro(request, mensagem, status=400):
    return JsonResponse(dict(ok=False, estado='rejeitado', erro=str(mensagem)), status=status)


def resultado(inventario_id, previa):
    """Read a receipt for this exact signed selection, not a balance coincidence."""
    dados = ajustes.ler_token(previa)
    if dados.get('inventario') != inventario_id:
        raise ValueError('Prévia não pertence a esta conferência.')
    if dados.get('tipo') == 'atualizacao':
        entradas = dados.get('rodadas')
        if (not isinstance(entradas, list) or not 1 <= len(entradas) <= 500
                or any(not isinstance(e, dict) or not isinstance(e.get('rodada'), int)
                       or not isinstance(e.get('assinatura'), str) for e in entradas)
                or len({e['rodada'] for e in entradas}) != len(entradas)):
            raise ValueError('Prévia de atualização inválida.')
        registros = list(AplicacaoAjuste.objects.filter(
            proposta__rodada_id__in=[e['rodada'] for e in entradas],
            proposta__rodada__item__inventario_id=inventario_id).select_related('proposta', 'decisao'))
        assinaturas = {e['rodada']: e['assinatura'] for e in entradas}
        correspondem = all(a.proposta.assinatura == assinaturas.get(a.proposta.rodada_id) for a in registros)
        esperados = len(entradas)
    elif dados.get('tipo') == 'aplicacao':
        propostas = dados.get('propostas')
        if not isinstance(propostas, list) or not 1 <= len(propostas) <= 500:
            raise ValueError('Prévia de aplicação inválida.')
        registros = list(AplicacaoAjuste.objects.filter(proposta_id__in=propostas,
            proposta__rodada__item__inventario_id=inventario_id).select_related('proposta', 'decisao'))
        correspondem = len(set(propostas)) == len(propostas)
        esperados = len(propostas)
    else:
        raise ValueError('Prévia de aplicação inválida.')
    if not registros:
        return dict(ok=False, estado='nao_registrado', erro='Nenhuma aplicação deste lote foi registrada.')
    if (len(registros) != esperados or not correspondem or any(
            a.proposta.estado != 'aplicado' or a.decisao.rodada_id != a.proposta.rodada_id
            or a.antes + a.diferenca != a.depois or not a.decisao.movimentacao_id for a in registros)):
        return dict(ok=False, estado='inconclusivo', erro='Auditoria não corresponde integralmente à prévia. Consulte o histórico; não reaplique.')
    return dict(ok=True, estado='aplicado', mensagem='Estoque atualizado com sucesso',
        aplicacoes=[str(a.pk) for a in registros],
        destino=reverse('inventario:detalhe', args=[inventario_id]) + '#titulo-resumo')


def conferir_gravacao(inventario_id, previa):
    comprovacao = resultado(inventario_id, previa)
    if not comprovacao['ok']:
        raise ValueError(comprovacao['erro'])
    return comprovacao


def concluir(request, inventario_id, comprovacao):
    # Called only after the outer atomic block exits successfully.
    messages.success(request, comprovacao['mensagem'],
        extra_tags=f"estoque-aplicado:{inventario_id}:{comprovacao['aplicacoes'][0]}")
    if solicitada(request):
        return JsonResponse(comprovacao)
    from django.shortcuts import redirect
    return redirect(comprovacao['destino'])


def consultar(request, inventario_id):
    services.permitir(request.user, 'aprovar_ajuste')
    # Read-only receipt. It cannot grant administrative write access or apply a lot.
    comprovacao = resultado(inventario_id, request.GET.get('resultado_atualizacao'))
    if (comprovacao['ok'] and request.resolver_match.url_name == 'ajustes'
            and not Inventario.objects.filter(pk=inventario_id, finalizada_em__isnull=False).exists()):
        comprovacao['destino'] = reverse('inventario:ajustes', args=[inventario_id])
    return JsonResponse(comprovacao)


def mensagens_validas(request, inventario_id):
    """Consume legacy flash successes; retain only a verified stock receipt on GET."""
    validas = []
    for mensagem in messages.get_messages(request):
        if mensagem.level != messages.SUCCESS:
            validas.append(mensagem)
            continue
        prefixo = f'estoque-aplicado:{inventario_id}:'
        tags = (mensagem.extra_tags or '').split()
        recibo = next((t[len(prefixo):] for t in tags if t.startswith(prefixo)), '')
        antigas = ('Operação concluída', 'Operação de ajuste concluída', 'Atualização concluída',
                   'Estoque atualizado com sucesso')
        if (request.method == 'GET' and request.resolver_match.url_name != 'ajustes'
                and not recibo and not str(mensagem).startswith(antigas)):
            validas.append(mensagem)
            continue
        if request.method == 'GET' and recibo:
            from uuid import UUID
            try:
                identificador = UUID(recibo)
            except ValueError:
                continue
            if AplicacaoAjuste.objects.filter(pk=identificador, usuario=request.user,
                    proposta__estado='aplicado', proposta__rodada__item__inventario_id=inventario_id).exists():
                validas.append(mensagem)
    previa = request.GET.get('resultado_atualizacao') if request.method == 'GET' else None
    if previa:
        from django.contrib.messages.storage.base import Message
        try:
            services.permitir(request.user, 'aprovar_ajuste')
            comprovacao = resultado(inventario_id, previa)
        except (ValueError, PermissionDenied):
            pass  # An unverified query parameter never supplies a success message.
        else:
            if comprovacao['ok'] and not any(m.level == messages.SUCCESS for m in validas):
                validas.append(Message(messages.SUCCESS, comprovacao['mensagem']))
    return validas
