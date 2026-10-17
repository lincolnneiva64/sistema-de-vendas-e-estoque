"""A sessão de contagem nunca é uma sessão comercial ou administrativa."""
from django.http import JsonResponse
from django.utils.cache import patch_cache_control


class AcessoLimitadoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if "inventario_acesso" not in request.session:
            return None
        match = request.resolver_match
        if match and match.namespace == "inventario" and match.url_name in {
            "convite", "mobile", "contar", "sinalizar", "finalizar",
        }:
            return None
        response = JsonResponse({"erro": "Acesso limitado à contagem de inventário."}, status=403)
        patch_cache_control(response, private=True, no_store=True)
        return response
