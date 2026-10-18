"""Separate server-side counting credentials; never rotate the commercial session."""
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied

from .models import AcessoInventario
from .services import validar_acesso


COOKIE = "inventario_sessionid"
CAMINHO = "/inventario/"
CHAVES_LEGADAS = ("inventario_acesso", "inventario_token_hash")


def estabelecer(request, acesso):
    # A new opaque session ID avoids adopting/fixing a previous counting session.
    # Only the existing access hash is stored server-side, never the invitation token.
    sessao = SessionStore()
    sessao["inventario_acesso"] = acesso.pk
    sessao["inventario_token_hash"] = acesso.token_hash
    sessao.set_expiry(acesso.expira_em)
    request.inventario_session = sessao


def preparar(request):
    request.inventario_session = (
        SessionStore(session_key=request.COOKIES.get(COOKIE))
        if request.path_info.startswith(CAMINHO) else None
    )
    if not any(chave in request.session for chave in CHAVES_LEGADAS):
        return
    # Move only the old counting markers. Keep auth, owner, expiry and all other
    # commercial session data, without logout(), flush(), cycle_key() or cookie deletion.
    legado = {chave: request.session.pop(chave, None) for chave in CHAVES_LEGADAS}
    if request.inventario_session and request.inventario_session.get("inventario_acesso"):
        return
    try:
        acesso = AcessoInventario.objects.filter(
            pk=legado["inventario_acesso"], token_hash=legado["inventario_token_hash"],
        ).first()
        validar_acesso(acesso)
    except (PermissionDenied, ValueError, TypeError, OverflowError):
        return
    estabelecer(request, acesso)
