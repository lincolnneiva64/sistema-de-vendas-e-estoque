"""Task-scoped employee links, following the existing external-checklist hash pattern.

No session authentication is granted by this module. Only the dedicated pickup
view accepts a validated capability. Plaintext tokens are never persisted.
"""
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
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
                grant = AutorizacaoRecolhimento.objects.create(tarefa=task, funcionario=employee,
                    emitida_por=request.user, token_hash=token_hash(token), expira_em=now+timedelta(hours=24))
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
    if grant.conferencia_id:
        return protect(redirect('locacoes:checklist_recolhimento_cliente', pk=grant.conferencia_id))
    if not task.pendente_operacional:
        return unavailable(request)
    if request.method == 'POST' and str(request.POST.get('funcionario', '')) != str(grant.funcionario_id):
        return unavailable(request)
    request.pickup_grant = grant
    from .views import conferencia_recolhimento
    return protect(conferencia_recolhimento(request, task.pk))
