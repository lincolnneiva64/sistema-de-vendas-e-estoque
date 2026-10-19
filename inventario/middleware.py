"""A sessão de contagem nunca é uma sessão comercial ou administrativa."""
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.http import http_date

from . import sessoes
from .erros import ErroAcesso, RespostaCsrfInventario, resposta


class AcessoLimitadoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_exception(self, request, exception):
        if isinstance(exception, ErroAcesso):
            return resposta(request, exception.codigo, str(exception))
        match = request.resolver_match
        if (isinstance(exception, PermissionDenied) and match and match.namespace == "inventario"
                and match.url_name in {"convite", "mobile", "contar", "sinalizar", "finalizar", "previa_conversao"}):
            return resposta(request, "acesso_negado", str(exception))

    def __call__(self, request):
        sessoes.preparar(request)
        response = self.get_response(request)
        match = request.resolver_match
        if (response.status_code == 403 and match and match.namespace == "inventario"
                and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
                and not getattr(request, "csrf_processing_done", False)):
            csrf = RespostaCsrfInventario(lambda req: response)
            recusa = csrf.process_view(request, match.func, match.args, match.kwargs)
            if recusa is not None:
                response = recusa
        sessao = request.inventario_session
        if request.path_info.startswith(sessoes.CAMINHO):
            patch_vary_headers(response, ("Cookie",))
        if sessao is not None and sessao.modified and response.status_code < 500:
            sessao.save()
            idade = max(0, sessao.get_expiry_age())
            response.set_cookie(
                sessoes.COOKIE, sessao.session_key, max_age=idade,
                expires=http_date(time.time() + idade), path=sessoes.CAMINHO,
                domain=settings.SESSION_COOKIE_DOMAIN,
                secure=request.is_secure() or settings.SESSION_COOKIE_SECURE,
                httponly=True, samesite="Lax",
            )
            patch_cache_control(response, private=True, no_store=True)
            patch_vary_headers(response, ("Cookie",))
        return response

    # Management views require the commercial user's own inventory permissions.
    # Counting views independently validate the opaque counting session and access.
    # Neither credential blocks the other context or grants its permissions.
