"""Counting failures without exposing credentials or weakening CSRF checks."""
import logging

from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.shortcuts import render
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.views.csrf import csrf_failure as django_csrf_failure

logger = logging.getLogger(__name__)


class ErroAcesso(PermissionDenied):
    def __init__(self, codigo, mensagem):
        self.codigo = codigo
        super().__init__(mensagem)


def resposta(request, codigo, mensagem):
    # Never log the URL: invitation paths contain bearer credentials.
    match = request.resolver_match
    logger.warning("inventario_recusa codigo=%s rota=%s metodo=%s", codigo,
                   match.url_name if match else "desconhecida", request.method)
    if "application/json" in request.headers.get("Accept", ""):
        response = JsonResponse({"codigo": codigo, "erro": mensagem}, status=403)
    else:
        response = render(request, "inventario/erro_contagem.html", {"erro": mensagem}, status=403)
    patch_cache_control(response, private=True, no_store=True)
    patch_vary_headers(response, ("Cookie",))
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def csrf_failure(request, reason=""):
    match = request.resolver_match
    if match and match.namespace == "inventario":
        return resposta(request, "csrf_invalido", "A verificação de segurança (CSRF) falhou. "
                        "Use Verificar acesso e último OK nesta tela antes de tentar novamente.")
    return django_csrf_failure(request, reason=reason)


class RespostaCsrfInventario(CsrfViewMiddleware):
    """Recheck a rejected response without changing the global CSRF handler.

    Only runs after a 403 in inventory. The standard middleware has already
    blocked the view; this checks the same rules to classify its response.
    It never calls the view or exempts any request from validation.
    """
    def _reject(self, request, reason):
        return csrf_failure(request, reason)
