import json
from uuid import UUID
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from locacoes.models import TarefaOperacionalLocacao
from estoque.models import EntregaRotaItem
from .services import ACTIVE_STATUSES, PROTOCOL_VERSION, process_operation, validate_command
from .commercial import build_commercial_snapshot


@require_GET
def sales_shell(request):
    # Deliberately render without request/context processors: no session, CSRF,
    # customer, financial or user data may enter the shared worker cache.
    from django.template.loader import get_template
    html = get_template('estoque/vendas_layout_teste.html').render({
        'offline_sales_shell': True, 'offline_environment_id': environment_id(request), 'cliente_inicial': None,
        'pedido_importado': None, 'venda_edicao': None,
    })
    return HttpResponse(html)


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
def session(request):
    authenticated = request.user.is_authenticated
    return JsonResponse({
        "authenticated": authenticated,
        "can_prepare": authenticated and request.user.has_perm("offline.registrar_observacao"),
        "username": request.user.get_username() if authenticated else "",
    })


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
    rota_id = request.GET.get("rota")
    if rota_id is not None:
        if not rota_id.isdigit() or len(rota_id) > 18:
            return JsonResponse({"erro": "Rota invalida."}, status=400)
        items = EntregaRotaItem.objects.filter(rota_id=rota_id).exclude(status="cancelada").select_related("venda")
        return JsonResponse({
            "environment_id": environment_id(request), "protocol_version": 1,
            "actor": {"id": str(request.user.pk), "name": request.user.get_username()},
            "csrf_token": get_token(request),
            "tasks": [{"id": str(i.pk), "kind": "entrega_venda", "rota_id": str(i.rota_id),
                       "venda_id": str(i.venda_id), "status": i.status,
                       "label": f"Entrega · Venda #{i.venda_id}"} for i in items if not i.venda.cancelada],
        })
    tasks = TarefaOperacionalLocacao.objects.filter(status__in=ACTIVE_STATUSES).exclude(
        locacao__status__in=["cancelada", "devolvida", "devolvida_com_avaria"]
    ).select_related("locacao").order_by("data_agendada", "pk")
    task_id = request.GET.get("task")
    if task_id is not None:
        if not task_id.isdigit() or len(task_id) > 18:
            return JsonResponse({"erro": "Tarefa invalida."}, status=400)
        tasks = tasks.filter(pk=task_id)
    tasks = tasks[:200]
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
@authorized
def commercial_snapshot(request):
    if not request.user.has_perms(["estoque.view_cliente", "estoque.view_produto"]):
        return JsonResponse({"erro": "Sem permissão para consultar o catálogo comercial."}, status=403)
    device_id = request.GET.get("device_id")
    if device_id is not None:
        try:
            if str(UUID(device_id)) != device_id:
                raise ValueError
        except ValueError:
            return JsonResponse({"erro": "Device inválido."}, status=400)
    response = JsonResponse(build_commercial_snapshot(request.user, environment_id(request), device_id))
    response["Cache-Control"] = "private, no-store"
    return response


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
