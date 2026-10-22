import io
import logging
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import path, reverse
from django.utils.log import configure_logging

from inventario import services, views
from inventario.models import ConfirmacaoContagem, EventoInventario, RodadaContagem
from inventario import tests_diagnostico_abertura as abertura_tests


SECRET = "synthetic-secret-do-not-log"


def unexpected_error(request, token):
    password = request.POST.get("password")
    try:
        raise ValueError(f"token={token}, password={password}, cookie={request.COOKIES}")
    except ValueError as cause:
        raise RuntimeError(f"secret={SECRET}") from cause


def server_error_without_exception(request):
    return HttpResponse("sensitive response " + SECRET, status=500)


urlpatterns = [
    path("failure/<str:token>/", unexpected_error, name="logging-failure"),
    path("status/", server_error_without_exception, name="logging-status"),
]


class LoggingCapture:
    def capture(self, action):
        output = io.StringIO()
        logger = logging.getLogger("django.request")
        # Reconfigure while stderr is redirected to exercise the real settings,
        # not a test-only handler. Restore the active stderr on completion.
        try:
            with redirect_stderr(output):
                configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)
                action()
        finally:
            configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)
        self.assertEqual(len(logger.handlers), 1)
        self.assertFalse(logger.propagate)
        return output.getvalue()


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
class RequestLoggingTests(LoggingCapture, TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("logging-fixture", "", "test-only")
        self.client.force_login(user)

    def test_http_500_logs_traceback_once_without_request_values(self):
        self.client.raise_request_exception = False
        self.client.cookies["privatecookie"] = SECRET
        def request():
            response = self.client.post(f"/failure/{SECRET}/?token={SECRET}",
                {"password": SECRET}, HTTP_AUTHORIZATION="Bearer " + SECRET)
            self.assertEqual(response.status_code, 500)
        output = self.capture(request)
        self.assertEqual(output.count("ERROR django.request"), 1)
        self.assertIn("status=500 method=POST view=logging-failure", output)
        self.assertIn("Traceback (most recent call last):", output)
        self.assertIn("tests_request_logging.py", output)
        self.assertIn("unexpected_error", output)
        self.assertIn("builtins.ValueError", output)
        self.assertIn("builtins.RuntimeError", output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn(self.client.cookies["sessionid"].value, output)
        for forbidden in ("password=", "cookie=", "Bearer ", "raise RuntimeError", "sessionid", "/failure/"):
            self.assertNotIn(forbidden, output)

    def test_500_without_traceback_and_404_are_handled(self):
        def request():
            self.assertEqual(self.client.get("/status/?password=" + SECRET).status_code, 500)
            self.assertEqual(self.client.get("/missing/" + SECRET).status_code, 404)
        output = self.capture(request)
        self.assertEqual(output.count("ERROR django.request"), 1)
        self.assertIn("view=logging-status", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn(SECRET, output)

    def test_cached_traceback_and_log_message_are_not_disclosed(self):
        request = RequestFactory().get("/" + SECRET)
        request.resolver_match = SimpleNamespace(view_name="safe-view")
        logger = logging.getLogger("django.request")
        def emit():
            record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1,
                "password=%s", (SECRET,), None, extra={"request": request, "status_code": 500})
            record.exc_text = "cached exception " + SECRET
            record.stack_info = "stack source " + SECRET
            logger.handle(record)
        output = self.capture(emit)
        self.assertEqual(output.count("ERROR django.request"), 1)
        self.assertNotIn(SECRET, output)

    @override_settings(DEBUG=True)
    def test_logging_also_works_with_debug_true(self):
        output = self.capture(lambda: self.client.get("/status/"))
        self.assertEqual(output.count("ERROR django.request"), 1)


@override_settings(DEBUG=False)
class InventoryFailureLoggingTests(LoggingCapture, TestCase):
    setUp = abertura_tests.AberturaDiagnosticoTests.setUp

    def test_unexpected_inventory_error_logs_location_without_changing_records(self):
        inv = services.criar_inventario(self.usuario, criterio="fornecedor", selecionados=self.fornecedor.pk)
        models = (RodadaContagem, ConfirmacaoContagem, EventoInventario)
        before = {m: list(m.objects.order_by("pk").values()) for m in models}
        self.client.raise_request_exception = False
        def request():
            with patch.object(views, "linhas_revisao", side_effect=RuntimeError(SECRET)):
                response = self.client.get(reverse("inventario:detalhe", args=[inv.pk]))
                self.assertEqual(response.status_code, 500)
        output = self.capture(request)
        self.assertEqual(output.count("ERROR django.request"), 1)
        self.assertIn("view=inventario:detalhe", output)
        self.assertIn("views.py", output)
        self.assertIn("builtins.RuntimeError", output)
        self.assertNotIn(SECRET, output)
        for model in models:
            self.assertEqual(before[model], list(model.objects.order_by("pk").values()))
        self.assertEqual(self.client.get(reverse("inventario:detalhe", args=[inv.pk])).status_code, 200)
