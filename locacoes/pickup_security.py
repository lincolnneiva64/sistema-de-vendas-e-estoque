"""Operational failures must explain the outcome without navigating to offline."""
import time
from django.shortcuts import render
from django.urls import reverse
from django.views.csrf import csrf_failure as default_csrf_failure
from urllib.parse import urlencode

PICKUP_PERMISSIONS = [
    'locacoes.add_conferenciarecolhimentolocacao',
    'locacoes.change_locacao',
    'locacoes.change_tarefaoperacionallocacao',
]
DRAFT_FIELDS = (
    'funcionario', 'boa_mesas', 'boa_cadeiras', 'quebrada_mesas', 'quebrada_cadeiras',
    'pessoa_local_nome', 'pessoa_local_relacao', 'pessoa_local_relacao_outro',
    'observacao', 'justificativa_parcial', 'previsao_conclusao', 'confirmacao_token',
)


def recovery_url(request):
    return request.path + '?retomar=1'


def failure(request, message, status):
    if request.method == 'POST' and status in (401, 403):
        # Server-side draft only; never save credentials or treat this as authorization.
        request.session['pickup_recovery'] = {
            'path': request.path,
            'saved_at': time.time(),
            'fields': {key: request.POST[key][:2000] for key in DRAFT_FIELDS if key in request.POST},
        }
    response = render(request, 'locacoes/recolhimento_sessao.html', {
        'explanation': message,
        'draft_saved': request.method == 'POST' and status in (401, 403),
        'return_url': recovery_url(request),
        'login_url': reverse('offline_login') + '?' + urlencode({'next': recovery_url(request)}),
    }, status=status)
    response['Cache-Control'] = 'no-store, private'
    return response


def csrf_failure(request, reason=''):
    from django.urls import resolve, Resolver404
    try:
        view_name = resolve(request.path).view_name
        if view_name == 'locacoes:recolhimento_individual':
            response = render(request, 'locacoes/recolhimento_link_indisponivel.html', {'erro_csrf': True}, status=403)
            response['Cache-Control'] = 'no-store, private'
            response['Referrer-Policy'] = 'no-referrer'
            return response
        operational = view_name == 'locacoes:conferencia_recolhimento'
    except Resolver404:
        operational = False
    if operational:
        return failure(request,
            'A validação de segurança expirou ou mudou. Esta tentativa não registrou '
            'o recolhimento. Volte ao checklist para consultar o histórico antes de confirmar novamente.', 403)
    return default_csrf_failure(request, reason=reason)
