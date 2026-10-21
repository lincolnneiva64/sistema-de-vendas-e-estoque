"""Expiring task capabilities; never login or commercial permissions."""
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import resolve, Resolver404
from django.utils import timezone
from django.utils.crypto import constant_time_compare

SALT = 'acesso-operacional-v1'
KEY = 'acessos_operacionais'


def escopo(module, name, kw):
    if module == 'estoque.views':
        if name == 'entrega_rota_checklist':
            return ['entrega', kw['pk']]
        if name == 'entrega_rota_checklist_cliente':
            return ['entrega_cliente', kw['rota_id'], 'item' if kw.get('rota_item_id') else 'venda', kw.get('rota_item_id') or kw['venda_id']]
        if name in {'separacao_venda_detalhe', 'separacao_venda_item_salvar'}:
            return ['separacao', kw['pk']]
    if module == 'locacoes.views' and name == 'conferencia_entrega':
        return ['locacao_entrega', kw['pk']]
    if module == 'locacoes.views' and name == 'checklist_entrega_cliente':
        return ['comprovante_entrega', kw['pk']]
    if module == 'locacoes.views' and name == 'checklist_recolhimento_cliente':
        return ['comprovante_recolhimento', kw['pk']]
    return None


def validar(token):
    if not isinstance(token, str) or len(token) > 4096:
        return None
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.OPERACIONAL_LINK_MAX_AGE)
        scope = data.get('scope')
        if (not isinstance(scope, list) or len(scope) not in {2, 4}
                or not isinstance(scope[1], int) or isinstance(scope[1], bool) or scope[1] <= 0
                or (len(scope) == 4 and (scope[0] != 'entrega_cliente' or scope[2] not in {'item', 'venda'}
                    or not isinstance(scope[3], int) or isinstance(scope[3], bool) or scope[3] <= 0))):
            return None
        user = get_user_model().objects.get(pk=data['issuer'], is_active=True)
        if not constant_time_compare(data['proof'], user.get_session_auth_hash()):
            return None
        from .acesso import ESCRITA
        from locacoes.pickup_security import PICKUP_PERMISSIONS
        permissions = {'entrega': ESCRITA['entrega_rota_checklist'], 'entrega_cliente': ESCRITA['entrega_rota_checklist'],
            'separacao': ESCRITA['separacao_venda_detalhe'],
            'comprovante_recolhimento': tuple(PICKUP_PERMISSIONS),
            'comprovante_entrega': ('locacoes.add_conferenciaentregalocacao', 'locacoes.change_tarefaoperacionallocacao'),
            'locacao_entrega': ('locacoes.add_conferenciaentregalocacao', 'locacoes.change_tarefaoperacionallocacao')}
        if not user.has_perms(permissions[data['scope'][0]]):
            return None
        if data.get('employee'):
            from estoque.models import Funcionario
            if not Funcionario.objects.filter(pk=data['employee'], ativo=True, pode_receber_checklist=True).exists():
                return None
        return data
    except (signing.BadSignature, KeyError, IndexError, AttributeError, TypeError, ValueError, get_user_model().DoesNotExist):
        return None


def proteger_path(request, path):
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return path
    try:
        match = resolve(parts.path)
    except Resolver404:
        return path
    scope = escopo(match.func.__module__, match.func.__name__, match.kwargs)
    if not scope:
        return path
    tokens = request.session.get(KEY, [])
    token = ''
    if request.user.is_authenticated:
        # Delegation always requires write permission, including from a GET page.
        from .acesso import ESCRITA
        from locacoes.pickup_security import PICKUP_PERMISSIONS
        permissions = {'entrega': ESCRITA['entrega_rota_checklist'], 'entrega_cliente': ESCRITA['entrega_rota_checklist'],
            'separacao': ESCRITA['separacao_venda_detalhe'],
            'comprovante_recolhimento': tuple(PICKUP_PERMISSIONS),
            'comprovante_entrega': ('locacoes.add_conferenciaentregalocacao', 'locacoes.change_tarefaoperacionallocacao'),
            'locacao_entrega': ('locacoes.add_conferenciaentregalocacao', 'locacoes.change_tarefaoperacionallocacao')}[scope[0]]
        if request.user.has_perms(permissions):
            query = parse_qs(parts.query)
            token = signing.dumps(dict(scope=scope, issuer=request.user.pk,
                proof=request.user.get_session_auth_hash(), employee=query.get('funcionario', [''])[0]), salt=SALT, compress=True)
    if not token:
        for candidate in tokens:
            data = validar(candidate)
            if data and corresponde(data['scope'], scope):
                token = candidate
                break
    if not token:
        return path  # Bare legacy links now require an authorized account.
    query = parse_qs(parts.query)
    query['acesso_operacional'] = [token]
    return urlunsplit(('', '', parts.path, urlencode(query, doseq=True), parts.fragment))


def autorizar(request, module, name, kw):
    if module == 'locacoes.views' and name == 'checklist_recolhimento_cliente' and request.method in {'GET', 'HEAD'}:
        from locacoes.models import AutorizacaoRecolhimento
        proof = request.session.get('comprovante_recolhimento', {})
        if proof and AutorizacaoRecolhimento.objects.filter(pk=proof.get('id'), token_hash=proof.get('hash'),
                conferencia_id=kw['pk'], revogada_em__isnull=True, expira_em__gt=timezone.now(),
                funcionario__ativo=True, funcionario__pode_receber_checklist=True).exists():
            return True
    scope = escopo(module, name, kw)
    if not scope or request.method not in {'GET', 'HEAD', 'POST'}:
        return False
    if (scope[0] in {'entrega_cliente', 'comprovante_recolhimento'} or name == 'checklist_entrega_cliente') and request.method not in {'GET', 'HEAD'}:
        return False
    supplied = request.GET.get('acesso_operacional')
    tokens = [supplied] if supplied else request.session.get(KEY, [])
    for token in tokens:
        data = validar(token)
        if data and corresponde(data['scope'], scope):
            if supplied:
                request.session[KEY] = ([token] + [t for t in request.session.get(KEY, []) if t != token])[:8]
            return True
    return False


def corresponde(grant, scope):
    if grant[0] == 'locacao_entrega' and scope[0] == 'comprovante_entrega':
        from locacoes.models import ConferenciaEntregaLocacao
        return ConferenciaEntregaLocacao.objects.filter(pk=scope[1], tarefa_id=grant[1]).exists()
    return grant == scope or (grant[0] == 'entrega' and scope[0] == 'entrega_cliente' and grant[1] == scope[1])
