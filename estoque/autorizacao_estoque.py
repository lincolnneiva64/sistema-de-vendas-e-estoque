"""Autorização curta para estoque sem trocar a identidade comercial da sessão."""
from django.conf import settings
from django.contrib.auth import load_backend
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_GET

CHAVE = "autorizacao_conferencia_estoque"
DURACAO_SEGUNDOS = 15 * 60


def usuario_estoque(request):
    if "inventario_acesso" in request.session:
        return None
    prova = request.session.get(CHAVE)
    if prova is None:
        return request.user if request.user.is_authenticated and request.user.is_active else None
    if (prova.get("backend") not in settings.AUTHENTICATION_BACKENDS
            or prova.get("expira", 0) <= timezone.now().timestamp()):
        return None
    usuario = load_backend(prova["backend"]).get_user(prova.get("usuario"))
    if (not usuario or not usuario.is_active
            or not constant_time_compare(prova.get("hash", ""), usuario.get_session_auth_hash())):
        return None
    return usuario


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def autorizar(request):
    form = AuthenticationForm(request, data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        usuario = form.get_user()
        # Rotate the session key against fixation, retaining the Sales session
        # and its identity. Do not call login() or grant global authentication.
        request.session.cycle_key()
        request.session[CHAVE] = {"usuario": usuario.pk, "backend": usuario.backend,
            "hash": usuario.get_session_auth_hash(), "expira": timezone.now().timestamp() + DURACAO_SEGUNDOS}
        return redirect("estoque:conferencia_estoque_autorizada")
    return render(request, "estoque/autorizar_conferencia.html", {"form": form})


@never_cache
@require_GET
def autorizada(request):
    if usuario_estoque(request) is None:
        return redirect("estoque:conferencia_estoque_autorizar")
    return render(request, "estoque/autorizar_conferencia.html")
