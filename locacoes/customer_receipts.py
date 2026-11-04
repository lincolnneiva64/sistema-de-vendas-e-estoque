"""Read-only customer capabilities, independent of employee credentials."""
import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST

from .models import ConferenciaRecolhimentoLocacao, EventoLocacao
from .pickup_security import PICKUP_PERMISSIONS


def protect(response):
    response['Cache-Control'] = 'no-store, private, max-age=0'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    response['X-Frame-Options'] = 'DENY'
    return response


def can_share(request, conference):
    if request.user.is_authenticated and request.user.has_perms(PICKUP_PERMISSIONS):
        return True
    # The existing employee capability may prepare a customer link only for its
    # own saved conference; it is never copied into that link or its message.
    from .models import AutorizacaoRecolhimento
    proof = request.session.get('comprovante_recolhimento', {})
    return bool(proof and AutorizacaoRecolhimento.objects.filter(
        pk=proof.get('id'), token_hash=proof.get('hash'), conferencia_id=getattr(conference, 'pk', conference),
        revogada_em__isnull=True, expira_em__gt=timezone.now(),
        funcionario__ativo=True, funcionario__pode_receber_checklist=True).exists())


@require_POST
@sensitive_variables('token', 'link', 'data')
@transaction.atomic
def share(request, pk):
    from .views import _telefone_whatsapp_locacao, _whatsapp_comprovante_recolhimento_url
    if not can_share(request, pk):
        return protect(HttpResponse('Acesso não autorizado.', status=403))
    conference = get_object_or_404(ConferenciaRecolhimentoLocacao.objects.select_for_update(of=('self',)).select_related(
        'locacao', 'locacao__cliente'), pk=pk)
    action = request.POST.get('acao', 'emitir')
    if action not in {'emitir', 'revogar'}:
        return protect(HttpResponse('Ação inválida.', status=400))
    now = timezone.now()
    if action == 'emitir':
        if not _telefone_whatsapp_locacao(conference.locacao):
            return protect(JsonResponse({'erro': 'Cliente sem WhatsApp cadastrado.'}, status=400))
        base = (getattr(settings, 'CHECKLIST_BASE_URL', '') or getattr(settings, 'SISTEMA_ONLINE_URL', '')).rstrip('/')
        path = reverse('locacoes:comprovante_cliente')
        base = base + path if base else request.build_absolute_uri(path)
        parsed = urlsplit(base)
        if parsed.scheme != 'https' and not (settings.DEBUG and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', 'testserver'}):
            return protect(JsonResponse({'erro': 'Configure HTTPS para compartilhar o comprovante.'}, status=400))
        token = secrets.token_urlsafe(32)
        conference.comprovante_token_hash = hashlib.sha256(token.encode()).hexdigest()
        conference.comprovante_expira_em = now + timedelta(days=30)
        conference.comprovante_revogado_em = None
        link = base + '#' + token
        data = {'whatsapp_url': _whatsapp_comprovante_recolhimento_url(conference, link)}
    else:
        conference.comprovante_revogado_em = now
        data = {'ok': True}
    # Conferences are immutable. Update only the three access metadata columns;
    # never relax save() or write any physical conference field.
    ConferenciaRecolhimentoLocacao.objects.filter(pk=conference.pk).update(
        comprovante_token_hash=conference.comprovante_token_hash,
        comprovante_expira_em=conference.comprovante_expira_em,
        comprovante_revogado_em=conference.comprovante_revogado_em)
    EventoLocacao.objects.create(locacao=conference.locacao, tipo='comprovante_cliente_' + action,
        responsavel=(request.user.get_username() if request.user.is_authenticated else conference.responsavel)[:120],
        descricao=f'Comprovante de recolhimento #{conference.pk}: ' + ('link emitido por 30 dias; link anterior invalidado.' if action == 'emitir' else 'link revogado.'))
    return protect(JsonResponse(data))


def receipt_context(conference):
    from .views import _avarias_recolhimento
    materials = []
    for key, label in [('mesas', 'Mesas'), ('cadeiras', 'Cadeiras')]:
        good = getattr(conference, 'boa_' + key)
        broken = getattr(conference, 'quebrada_' + key)
        if good:
            materials.append({'nome': label, 'quantidade': good, 'bom': True, 'condicao': 'Em bom estado'})
        if broken:
            materials.append({'nome': label, 'quantidade': broken, 'bom': False, 'condicao': 'Quebradas'})
    return {'conferencia': conference, 'locacao': conference.locacao,
        'cliente_nome_checklist': conference.locacao.nome_contratante,
        'materiais_recolhidos': materials, 'avarias': _avarias_recolhimento(conference),
        'recolhido_mesas': conference.boa_mesas + conference.quebrada_mesas,
        'recolhido_cadeiras': conference.boa_cadeiras + conference.quebrada_cadeiras}


@sensitive_post_parameters('token')
@sensitive_variables('token')
@ensure_csrf_cookie
@require_http_methods(['GET', 'HEAD', 'POST'])
def public(request):
    if request.method != 'POST':
        return protect(render(request, 'locacoes/comprovante_cliente_acesso.html'))
    token = request.POST.get('token', '')
    conference = None
    if re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        conference = ConferenciaRecolhimentoLocacao.objects.select_related('locacao', 'locacao__cliente').filter(
            comprovante_token_hash=hashlib.sha256(token.encode()).hexdigest(),
            comprovante_expira_em__gt=timezone.now(), comprovante_revogado_em__isnull=True).first()
    if not conference:
        return protect(JsonResponse({'erro': 'Link indisponível ou vencido. Peça um novo comprovante ao depósito.'}, status=404))
    context = receipt_context(conference)
    context['publico_cliente'] = True
    context['messages'] = []
    return protect(JsonResponse({'html': render_to_string('locacoes/checklist_recolhimento_cliente.html', context, request)}))
