"""Explicit access to the existing adjustment workflow for finalized records."""
from django.core import signing
from django.core.exceptions import PermissionDenied

from . import services

SALT = 'inventario-revisao-historica-v1'


def contexto(usuario, inventario):
    return dict(usuario=usuario.pk, inventario=inventario.pk, ciclo=inventario.ciclo,
        finalizada_em=inventario.finalizada_em.isoformat() if inventario.finalizada_em else None,
        finalizacao_versao=inventario.finalizacao_versao)


def iniciar(usuario, inventario):
    services.permitir(usuario, 'aprovar_ajuste')
    if not inventario.finalizada_em:
        raise PermissionDenied('Esta conferência não está no histórico.')
    return signing.dumps(contexto(usuario, inventario), salt=SALT, compress=True)


def exigir(request, inventario):
    if not inventario.finalizada_em:
        return ''
    services.permitir(request.user, 'aprovar_ajuste')
    acesso = request.POST.get('revisao_historica') or request.GET.get('revisao_historica', '')
    try:
        if not isinstance(acesso, str) or not acesso or len(acesso) > 2000:
            raise ValueError
        if signing.loads(acesso, salt=SALT, max_age=1200) != contexto(request.user, inventario):
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError):
        raise PermissionDenied('Acesse a revisão pelo botão em Ações administrativas da conferência. '
                               'O acesso é individual e válido por 20 minutos.')
    return acesso
