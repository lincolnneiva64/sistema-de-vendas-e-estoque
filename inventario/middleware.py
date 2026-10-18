"""A sessão de contagem nunca é uma sessão comercial ou administrativa."""
import time

from django.conf import settings
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.http import http_date

from . import sessoes


class AcessoLimitadoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sessoes.preparar(request)
        response = self.get_response(request)
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
