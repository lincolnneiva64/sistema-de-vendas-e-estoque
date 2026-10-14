"""Backend protections for legacy task writes, using disposable test data."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import MovimentoFinanceiro
from . import tests as fixtures
from .models import (
    ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao,
    ConfiguracaoLocacao, EventoLocacao, FaixaPrecoLocacao, ItemLocacao,
    Locacao, MovimentoEstoqueLocacao, PagamentoLocacao, TarefaOperacionalLocacao,
)
from .services import obter_ou_criar_tarefa_operacional


class LegacyTaskSecurityTests(TestCase):
    dados_base = fixtures.LocacoesChecklistOperacionalTests.dados_base
    criar_locacao = fixtures.LocacoesChecklistOperacionalTests.criar_locacao

    def setUp(self):
        FaixaPrecoLocacao.objects.get_or_create(
            codigo=FaixaPrecoLocacao.CENTRO_PERTO,
            defaults={"nome": "Teste", "preco_jogo_diaria": 8},
        )
        fixtures.LocacoesChecklistOperacionalTests.setUp(self)
        self.user = get_user_model().objects.get(username="Camila")
        self.rental = self.criar_locacao()
        self.task = obter_ou_criar_tarefa_operacional(self.rental, "entrega")
        self.url = reverse("locacoes:tarefa_operacional_nao_possivel", args=[self.task.pk])
        self.confirm_url = reverse("locacoes:confirmar_tarefa_operacional", args=[self.task.pk])
        self.payload = {"observacao": "Cliente ausente no teste", "responsavel": "Usuario forjado"}
        self.permission = Permission.objects.get(
            content_type__app_label="locacoes", codename="change_tarefaoperacionallocacao",
        )

    def snapshot(self):
        models = (
            Locacao, ItemLocacao, TarefaOperacionalLocacao, EventoLocacao,
            ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao,
            MovimentoEstoqueLocacao, MovimentoFinanceiro, PagamentoLocacao,
            ConfiguracaoLocacao,
        )
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in models}

    def terminal(self, status="confirmada_sem_checklist", tipo="entrega"):
        TarefaOperacionalLocacao.objects.filter(pk=self.task.pk).update(
            status=status, tipo=tipo, confirmado_em=timezone.now(), confirmado_por="Registro anterior",
        )
        self.task.refresh_from_db()

    def test_anonymous_with_valid_csrf_cannot_change_any_legacy_endpoint(self):
        self.terminal()
        client = Client(enforce_csrf_checks=True)
        client.get("/vendas/")
        csrf = client.cookies["csrftoken"].value
        before = self.snapshot()
        for url in (self.url, self.confirm_url):
            with self.subTest(url=url):
                response = client.post(url, self.payload, HTTP_X_CSRFTOKEN=csrf)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Dados locais preservados", response.json()["erro"])
                self.assertEqual(self.snapshot(), before)

    def test_permission_is_required_even_with_other_rental_permissions(self):
        self.user.user_permissions.remove(self.permission)
        before = self.snapshot()
        for url in (self.url, self.confirm_url):
            with self.subTest(url=url):
                response = self.client.post(url, self.payload)
                self.assertEqual(response.status_code, 403)
                self.assertIn("Dados locais preservados", response.json()["erro"])
                self.assertEqual(self.snapshot(), before)

    def test_csrf_is_required_for_an_authorized_user(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        before = self.snapshot()
        for url in (self.url, self.confirm_url):
            self.assertEqual(client.post(url, self.payload).status_code, 403)
            self.assertEqual(self.snapshot(), before)

    def test_expired_session_cannot_write(self):
        from datetime import timedelta
        from django.contrib.sessions.models import Session
        Session.objects.filter(session_key=self.client.cookies["sessionid"].value).update(
            expire_date=timezone.now() - timedelta(seconds=1),
        )
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 401)
        self.assertEqual(self.snapshot(), before)

    def test_all_terminal_states_preserve_records_through_the_endpoint(self):
        for tipo in ("entrega", "recolhimento"):
            for status in ("confirmada", "resolvida_admin", "confirmada_sem_checklist"):
                with self.subTest(tipo=tipo, status=status):
                    self.terminal(status, tipo)
                    before = self.snapshot()
                    response = self.client.post(self.url, self.payload)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("encerrada", str(list(response.wsgi_request._messages)))
                    self.assertEqual(self.snapshot(), before)

    def test_backend_methods_reject_terminal_states_without_any_interface(self):
        for status in ("confirmada", "resolvida_admin", "confirmada_sem_checklist", "estado_desconhecido"):
            with self.subTest(status=status):
                self.terminal(status)
                before = self.snapshot()
                for action in (
                    lambda: self.task.registrar_nao_possivel("Cliente ausente", "Camila"),
                    lambda: self.task.confirmar("Camila"),
                ):
                    with self.assertRaises(ValidationError):
                        action()
                    self.assertEqual(self.snapshot(), before)

    def test_stale_pending_instance_cannot_overwrite_a_completed_task(self):
        self.assertEqual(self.task.status, "pendente")
        TarefaOperacionalLocacao.objects.filter(pk=self.task.pk).update(status="confirmada_sem_checklist")
        before = self.snapshot()
        with self.assertRaises(ValidationError):
            self.task.registrar_nao_possivel("Cliente ausente", "Camila")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.task.status, "pendente")

    def test_task_and_event_are_rolled_back_together(self):
        before = self.snapshot()
        with patch("locacoes.models.EventoLocacao.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.task.registrar_nao_possivel("Cliente ausente", "Camila")
        self.assertEqual(self.snapshot(), before)

    def test_pending_partial_and_not_possible_tasks_remain_usable(self):
        for status in ("pendente", "parcial", "nao_possivel"):
            with self.subTest(status=status):
                TarefaOperacionalLocacao.objects.filter(pk=self.task.pk).update(status=status)
                before = self.snapshot()
                event_count = EventoLocacao.objects.count()
                response = self.client.post(self.url, self.payload)
                self.assertEqual(response.status_code, 302)
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, "nao_possivel")
                self.assertEqual(self.task.tentativa_por, self.user.username)
                self.assertIsNotNone(self.task.tentativa_em)
                self.assertEqual(self.task.motivo_nao_realizado, self.payload["observacao"])
                event = EventoLocacao.objects.latest("pk")
                self.assertEqual(event.responsavel, self.user.username)
                self.assertEqual(event.tipo, "checklist_entrega_nao_possivel")
                self.assertEqual(EventoLocacao.objects.count(), event_count + 1)
                after = self.snapshot()
                for name in before.keys() - {"TarefaOperacionalLocacao", "EventoLocacao"}:
                    self.assertEqual(after[name], before[name], name)

    def test_task_change_permission_is_sufficient_without_unrelated_permissions(self):
        user = get_user_model().objects.create_user("task-only", password="test-only")
        user.user_permissions.add(self.permission)
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "nao_possivel")
        self.assertEqual(self.task.tentativa_por, "task-only")

    def test_closed_rentals_do_not_accept_a_leftover_pending_task(self):
        for status in ("cancelada", "devolvida", "devolvida_com_avaria"):
            with self.subTest(status=status):
                Locacao.objects.filter(pk=self.rental.pk).update(status=status)
                before = self.snapshot()
                with self.assertRaises(ValidationError):
                    self.task.registrar_nao_possivel("Cliente ausente", "Camila")
                self.assertEqual(self.snapshot(), before)

    def test_invalid_reason_preserves_all_records(self):
        before = self.snapshot()
        response = self.client.post(self.url, {"observacao": " ", "responsavel": "Camila"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_related_legacy_confirmation_cannot_reopen_terminal_tasks(self):
        self.terminal()
        before = self.snapshot()
        self.assertEqual(self.client.post(self.confirm_url, self.payload).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_pending_legacy_confirmation_still_routes_to_physical_checklist(self):
        before = self.snapshot()
        response = self.client.post(self.confirm_url, self.payload)
        self.assertRedirects(response, reverse("locacoes:conferencia_entrega", args=[self.task.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(self.snapshot(), before)

    def test_detailed_conferences_also_reject_the_new_terminal_status(self):
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                self.terminal(tipo=tipo)
                before = self.snapshot()
                if tipo == "entrega":
                    with self.assertRaisesMessage(ValidationError, "Esta entrega ja foi confirmada"):
                        ConferenciaEntregaLocacao.registrar(self.task, {}, {})
                else:
                    with self.assertRaisesMessage(ValidationError, "Este recolhimento ja foi concluido"):
                        ConferenciaRecolhimentoLocacao.registrar(self.task, {})
                self.assertEqual(self.snapshot(), before)
