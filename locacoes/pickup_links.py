"""Task-scoped employee links, following the existing external-checklist hash pattern.

No session authentication is granted by this module. Only the dedicated pickup
view accepts a validated capability. Plaintext tokens are never persisted.
"""
import hashlib
import base64
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac, constant_time_compare
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from estoque.models import Funcionario
from .models import AutorizacaoRecolhimento, TarefaOperacionalLocacao, EventoLocacao
from .pickup_security import PICKUP_PERMISSIONS


def token_hash(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def protect(response):
    response['Cache-Control'] = 'no-store, private'
    # no-referrer makes Chromium send Origin:null on native form POSTs.
    # same-origin protects the bearer URL from external sites while preserving CSRF.
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def unavailable(request):
    return protect(render(request, 'locacoes/recolhimento_link_indisponivel.html', status=403))


def _send_token(grant, key):
    """Recoverable only by the server; the existing capability still stores just SHA256.

    Domain separation and immutable grant identity prevent use as any other token.
    Reproducing the opaque 256-bit token lets retries reuse it without plaintext storage.
    """
    identity = f'{grant.pk}:{grant.tarefa_id}:{grant.funcionario_id}:{grant.criada_em.isoformat()}:{grant.expira_em.isoformat()}'
    digest = salted_hmac('locacoes.pickup.panel-send.v1', identity, secret=key, algorithm='sha256').digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _individual_url(request, token):
    from .services import url_publica_checklist
    link = url_publica_checklist(request, reverse('locacoes:recolhimento_individual', args=[token]))
    parsed = urlsplit(link)
    if parsed.scheme != 'https' and not (settings.DEBUG and parsed.scheme == 'http'
            and parsed.hostname in ('127.0.0.1', 'localhost', 'testserver')):
        raise ValueError('Configure HTTPS para emitir links individuais com segurança.')
    return link


def _new_grant(task, employee, user, now):
    # The temporary unique digest is replaced within the caller's transaction.
    grant = AutorizacaoRecolhimento.objects.create(tarefa=task, funcionario=employee, emitida_por=user,
        token_hash=token_hash(secrets.token_urlsafe(32)), expira_em=now+timedelta(hours=24))
    token = _send_token(grant, settings.SECRET_KEY)
    grant.token_hash = token_hash(token)
    grant.save(update_fields=['token_hash'])
    return grant, token


@never_cache
@require_http_methods(['POST'])
@csrf_protect
@transaction.atomic
def send(request, pk):
    """Delegate from the operational panel; never confirm a pickup or mark a message sent."""
    def error(message, status):
        return protect(JsonResponse({'erro': message}, status=status))

    if not request.user.is_authenticated:
        return error('Sua sessão expirou. Entre novamente em outra aba e tente enviar por este painel.', 401)
    if not request.user.has_perms(PICKUP_PERMISSIONS + ['locacoes.autorizar_recolhimento']):
        return error('Sua conta não tem permissão para autorizar recolhimentos.', 403)
    task = TarefaOperacionalLocacao.objects.select_for_update().filter(pk=pk, tipo='recolhimento').first()
    if not task:
        return error('Recolhimento não encontrado.', 404)
    employee_id = request.POST.get('funcionario', '')
    employee = Funcionario.habilitados_para_checklist().filter(pk=employee_id).first() if employee_id.isdigit() else None
    if not employee:
        return error('Selecione um funcionário habilitado para receber checklist.', 400)
    from .services import telefone_funcionario_checklist, whatsapp_web_url
    phone = telefone_funcionario_checklist(employee)
    if not phone:
        return error('O funcionário selecionado está sem WhatsApp válido cadastrado.', 400)
    if not task.pendente_operacional:
        return error('Este recolhimento já foi encerrado. Consulte o comprovante; nenhum novo link foi gerado.', 409)
    try:
        # Validate configuration before any write. This placeholder is never issued.
        _individual_url(request, 'A' * 43)
    except ValueError as exc:
        return error(str(exc), 400)
    now = timezone.now()
    grant = task.autorizacoes_recolhimento.filter(funcionario=employee, revogada_em__isnull=True,
        conferencia__isnull=True, expira_em__gt=now).order_by('-pk').first()
    token = ''
    if grant:
        for key in [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]:
            candidate = _send_token(grant, key)
            if constant_time_compare(token_hash(candidate), grant.token_hash):
                token = candidate
                break
    reused = bool(token)
    if not reused:
        # Same lock order and single active assignment as administrative issuance.
        # Legacy random links cannot be recovered: replace once, without touching counts.
        task.autorizacoes_recolhimento.filter(revogada_em__isnull=True).update(revogada_em=now)
        grant, token = _new_grant(task, employee, request.user, now)
        EventoLocacao.objects.create(locacao_id=task.locacao_id, tipo='recolhimento_link_emitido',
            responsavel=request.user.get_username(),
            descricao=f'Autorizacao #{grant.pk}; tarefa #{task.pk}; funcionario #{employee.pk}: {employee.nome}; '
                      f'validade {grant.expira_em.isoformat()}; origem: envio operacional pelo painel.')
    link = _individual_url(request, token)
    message = (f'Recolhimento — {task.locacao.nome_contratante}\nResponsável: {employee.nome}\n'
               f'Link individual válido até {timezone.localtime(grant.expira_em):%d/%m/%Y %H:%M}. '
               f'Não encaminhe a terceiros.\n{link}')
    return protect(JsonResponse({'whatsapp_url': whatsapp_web_url(phone, message),
        'autorizacao_id': grant.pk, 'reutilizado': reused, 'expira_em': grant.expira_em.isoformat()}))


@never_cache
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def manage(request, pk):
    if not request.user.is_authenticated:
        return HttpResponse('Entre com sua conta administrativa para autorizar o funcionário.', status=401)
    if not request.user.has_perms(PICKUP_PERMISSIONS + ['locacoes.autorizar_recolhimento']):
        return HttpResponse('Você não tem permissão para autorizar recolhimentos.', status=403)
    task = get_object_or_404(TarefaOperacionalLocacao.objects.select_for_update(), pk=pk, tipo='recolhimento')
    link, whatsapp = '', ''
    error = ''
    if request.method == 'POST':
        action = request.POST.get('acao')
        if action not in ('emitir', 'revogar'):
            return HttpResponse('Ação inválida.', status=400)
        if action == 'emitir':
            employee_id = request.POST.get('funcionario', '')
            employee = Funcionario.habilitados_para_checklist().filter(pk=employee_id).first() if employee_id.isdigit() else None
            if not employee:
                return HttpResponse('Selecione um funcionário habilitado.', status=400)
            if not task.pendente_operacional:
                error = 'Este recolhimento está encerrado. A conferência registrada foi preservada; consulte o comprovante.'
            from .services import url_publica_checklist, whatsapp_web_url, telefone_funcionario_checklist
            token = secrets.token_urlsafe(32)
            link = url_publica_checklist(request, reverse('locacoes:recolhimento_individual', args=[token]))
            parsed = urlsplit(link)
            if parsed.scheme != 'https' and not (settings.DEBUG and parsed.scheme == 'http'
                    and parsed.hostname in ('127.0.0.1', 'localhost', 'testserver')):
                return HttpResponse('Configure HTTPS para emitir links individuais com segurança.', status=400)
            if error:
                link = ''
        if not error:
            now = timezone.now()
            task.autorizacoes_recolhimento.filter(revogada_em__isnull=True).update(revogada_em=now)
            if action == 'emitir':
                grant, token = _new_grant(task, employee, request.user, now)
                link = url_publica_checklist(request, reverse('locacoes:recolhimento_individual', args=[token]))
                whatsapp = whatsapp_web_url(telefone_funcionario_checklist(employee),
                    f'Recolhimento — {task.locacao.nome_contratante}\nResponsável: {employee.nome}\n'
                    f'Link individual válido por 24 horas. Não encaminhe a terceiros.\n{link}')
                description = f'Autorizacao #{grant.pk}; tarefa #{task.pk}; funcionario #{employee.pk}: {employee.nome}; validade {grant.expira_em.isoformat()}.'
            else:
                description = f'Autorizações revogadas para tarefa #{task.pk}.'
            EventoLocacao.objects.create(locacao_id=task.locacao_id,
                tipo='recolhimento_link_emitido' if action == 'emitir' else 'recolhimento_link_revogado',
                responsavel=request.user.get_username(), descricao=description)
    return protect(render(request, 'locacoes/recolhimento_autorizar.html', {
        'tarefa': task, 'funcionarios': Funcionario.habilitados_para_checklist(),
        'selecionado': request.GET.get('funcionario', ''), 'link': link, 'whatsapp': whatsapp,
        'erro': error, 'autorizacoes': task.autorizacoes_recolhimento.select_related('funcionario', 'emitida_por').order_by('-pk'),
        'ultima_conferencia': task.conferencias_recolhimento.order_by('-pk').first(),
    }))


@never_cache
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def individual(request, token):
    # Fixed token alphabet/length prevents unbounded hashing and ambiguous encodings.
    if len(token) != 43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
        return unavailable(request)
    found = AutorizacaoRecolhimento.objects.filter(token_hash=token_hash(token)).values('pk', 'tarefa_id').first()
    if not found:
        return unavailable(request)
    # All issue/revoke/confirm paths take the task lock before grant rows.
    task = TarefaOperacionalLocacao.objects.select_for_update().get(pk=found['tarefa_id'])
    grant = AutorizacaoRecolhimento.objects.select_for_update().select_related('funcionario').get(pk=found['pk'])
    if (grant.revogada_em or grant.expira_em <= timezone.now() or task.tipo != 'recolhimento'
            or not grant.funcionario.ativo or not grant.funcionario.pode_receber_checklist):
        return unavailable(request)
    request.session['comprovante_recolhimento'] = {'id': grant.pk, 'hash': grant.token_hash}
    if grant.conferencia_id:
        return protect(redirect('locacoes:checklist_recolhimento_cliente', pk=grant.conferencia_id))
    if not task.pendente_operacional:
        return unavailable(request)
    if request.method == 'POST' and str(request.POST.get('funcionario', '')) != str(grant.funcionario_id):
        return unavailable(request)
    request.pickup_grant = grant
    from .views import conferencia_recolhimento
    return protect(conferencia_recolhimento(request, task.pk))
