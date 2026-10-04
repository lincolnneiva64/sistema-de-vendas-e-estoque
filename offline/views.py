import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from locacoes.models import TarefaOperacionalLocacao
from .services import ACTIVE_STATUSES, PROTOCOL_VERSION, process_operation, validate_command


def environment_id(request):
    return settings.OFFLINE_ENVIRONMENT_ID or request.get_host().lower()


def authorized(view):
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"erro": "Autentique-se novamente; dados locais preservados."}, status=401)
        if not request.user.has_perm("offline.registrar_observacao"):
            return JsonResponse({"erro": "Sem permissao para o piloto offline."}, status=403)
        return view(request, *args, **kwargs)
    return wrapped


@require_GET
@never_cache
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            ok = cursor.fetchone()[0] == 1
    except DatabaseError:
        ok = False
    return JsonResponse({"ok": ok, "environment": environment_id(request), "protocol_version": PROTOCOL_VERSION}, status=200 if ok else 503)


@require_GET
@authorized
def snapshot(request):
    tasks = TarefaOperacionalLocacao.objects.filter(status__in=ACTIVE_STATUSES).exclude(
        locacao__status__in=["cancelada", "devolvida", "devolvida_com_avaria"]
    ).select_related("locacao").order_by("data_agendada", "pk")[:200]
    return JsonResponse({
        "environment_id": environment_id(request), "protocol_version": 1,
        "actor": {"id": str(request.user.pk), "name": request.user.get_username()},
        "csrf_token": get_token(request),
        "tasks": [{"id": str(t.pk), "locacao_id": str(t.locacao_id), "status": t.status,
                   "label": f"{t.get_tipo_display()} · Locacao #{t.locacao_id} · {t.data_agendada:%d/%m/%Y}"} for t in tasks],
    })


@require_POST
@authorized
def synchronize(request):
    if len(request.body) > 20000:
        return JsonResponse({"erro": "Operacao muito grande."}, status=413)
    try:
        data = json.loads(request.body)
        command = validate_command(data, request.user, environment_id(request))
    except (ValueError, TypeError, ValidationError, OverflowError, UnicodeError):
        return JsonResponse({"erro": "Operacao invalida: confira identidade, formato e hash."}, status=400)
    result, status = process_operation(command, request.user)
    return JsonResponse(result, status=status)


@require_GET
@never_cache
def shell(request):
    return HttpResponse((settings.BASE_DIR / "static/offline/pilot.html").read_bytes(), content_type="text/html; charset=utf-8")


@require_GET
@never_cache
def service_worker(request):
    response = HttpResponse((settings.BASE_DIR / "static/offline/service-worker.js").read_bytes(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response
