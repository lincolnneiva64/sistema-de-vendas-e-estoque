"""New pickups require confirmed delivery, including internal and delegated writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from django.core.exceptions import ValidationError
from django.contrib.messages import get_messages
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from offline.test_pickup_links import LinkFixture
from .models import (Locacao, ItemLocacao, TarefaOperacionalLocacao, EventoLocacao,
                     ConferenciaRecolhimentoLocacao, MovimentoEstoqueLocacao,
                     PagamentoLocacao, AutorizacaoRecolhimento)
from estoque.models import MovimentoFinanceiro


MESSAGE = 'Não é possível recolher esta locação porque a entrega ainda não foi confirmada.'


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class PickupDeliveryGuardTests(LinkFixture, TestCase):
    def setUp(self):
        self.seed()
        self.client.force_login(self.user)

    def snapshot(self):
        return {model.__name__: list(model.objects.order_by('pk').values()) for model in
                (Locacao, ItemLocacao, TarefaOperacionalLocacao, EventoLocacao,
                 ConferenciaRecolhimentoLocacao, MovimentoEstoqueLocacao,
                 PagamentoLocacao, AutorizacaoRecolhimento, MovimentoFinanceiro)}

    def test_services_reject_every_ineligible_state_without_writes(self):
        item = self.rental.itens.first()
        for state in ('reservada', 'saiu_para_entrega', 'cancelada', 'devolvida', 'devolvida_com_avaria'):
            Locacao.objects.filter(pk=self.rental.pk).update(status=state)
            # Objects deliberately retain their formerly eligible state.
            before = self.snapshot()
            for action in (
                lambda: self.rental.registrar_devolucao({item.pk: {'devolvida_boa': 1}}),
                lambda: item.registrar_retorno(devolvida_boa=1),
                lambda: item.registrar_retorno(quebrada=1),
                lambda: self.task.resolver_administrativamente('', resultado='recolhimento_realizado'),
                lambda: self.task.confirmar(),
                lambda: ConferenciaRecolhimentoLocacao.registrar(self.task, self.payload),
            ):
                with self.subTest(state=state, action=action):
                    with self.assertRaisesMessage(ValidationError, MESSAGE):
                        action()
                    self.assertEqual(self.snapshot(), before)

    def test_endpoints_and_both_link_issuers_reject_inconsistent_task(self):
        form = self.client.get(self.url)
        data = {**self.payload, 'confirmacao_token': form.context['confirmacao_token']}
        for state in ('reservada', 'saiu_para_entrega'):
            Locacao.objects.filter(pk=self.rental.pk).update(status=state)
            before = self.snapshot()
            response = self.client.post(self.url, data)
            self.assertEqual(response.status_code, 302)
            self.assertIn(MESSAGE, [str(m) for m in get_messages(response.wsgi_request)])
            response = self.client.post(reverse('locacoes:resolver_tarefa_operacional', args=[self.task.pk]),
                                        {'resultado': 'recolhimento_realizado'})
            self.assertEqual(response.status_code, 400)
            self.assertIn(MESSAGE, response.json()['erro'])
            for name, payload in (
                ('autorizar_recolhimento', {'acao': 'emitir', 'funcionario': self.employee.pk}),
                ('enviar_recolhimento', {'funcionario': self.employee.pk}),
            ):
                response = self.client.post(reverse('locacoes:' + name, args=[self.task.pk]), payload)
                self.assertEqual(response.status_code, 400)
                message = response.json()['erro'] if name == 'enviar_recolhimento' else response.content.decode()
                self.assertIn(MESSAGE, message)
            self.assertEqual(self.snapshot(), before)

    def test_preexisting_employee_link_cannot_bypass_changed_state(self):
        link = self.issue()
        employee, data = self.employee_form(link)
        Locacao.objects.filter(pk=self.rental.pk).update(status='saiu_para_entrega')
        before = self.snapshot()
        self.assertEqual(employee.post(link, data).status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_valid_administrative_resolution_preserves_physical_and_financial_data(self):
        before = self.snapshot()
        response = self.client.post(reverse('locacoes:resolver_tarefa_operacional', args=[self.task.pk]),
                                    {'resultado': 'recolhimento_realizado'})
        self.assertEqual(response.status_code, 200)
        after = self.snapshot()
        for name in before.keys() - {'TarefaOperacionalLocacao', 'EventoLocacao'}:
            self.assertEqual(before[name], after[name])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'resolvida_admin')

    def test_revocation_remains_available_after_rental_becomes_ineligible(self):
        self.issue()
        Locacao.objects.filter(pk=self.rental.pk).update(status='saiu_para_entrega')
        response = self.client.post(reverse('locacoes:autorizar_recolhimento', args=[self.task.pk]),
                                    {'acao': 'revogar'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(AutorizacaoRecolhimento.objects.get().revogada_em)

    def test_early_confirmed_delivery_allows_partial_and_completion(self):
        Locacao.objects.filter(pk=self.rental.pk).update(data_entrega=timezone.localdate() + timedelta(days=5))
        item = self.rental.itens.first()
        ItemLocacao.objects.filter(pk=item.pk).update(quantidade=2)
        self.rental.registrar_devolucao({item.pk: {'devolvida_boa': 1}}, responsavel='Teste')
        self.rental.refresh_from_db()
        self.assertEqual(self.rental.status, 'pendente_devolucao')
        remaining = {i.pk: {'devolvida_boa': i.quantidade_pendente()} for i in self.rental.itens.all()}
        self.rental.registrar_devolucao(remaining, responsavel='Teste')
        self.rental.refresh_from_db()
        self.assertEqual(self.rental.status, 'devolvida')
        self.assertEqual(self.rental.eventos.filter(tipo='devolucao').count(), 2)


class PickupDeliveryConcurrencyTests(LinkFixture, TransactionTestCase):
    def setUp(self):
        self.seed()

    def test_waiting_return_rechecks_locked_rental_state(self):
        self.assertEqual(connection.vendor, 'postgresql')
        item = self.rental.itens.first()
        started = Event()

        def return_item():
            close_old_connections()
            try:
                stale = ItemLocacao.objects.get(pk=item.pk)
                started.set()
                try:
                    stale.registrar_retorno(quebrada=1)
                except ValidationError as exc:
                    return exc.messages
                return []
            finally:
                close_old_connections()

        before_stock = list(MovimentoEstoqueLocacao.objects.values())
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                locked = Locacao.objects.select_for_update().get(pk=self.rental.pk)
                future = pool.submit(return_item)
                self.assertTrue(started.wait(10))
                Locacao.objects.filter(pk=locked.pk).update(status='saiu_para_entrega')
            self.assertEqual(future.result(timeout=15), [MESSAGE])
        item.refresh_from_db()
        self.assertEqual(item.quebrada, 0)
        self.assertEqual(list(MovimentoEstoqueLocacao.objects.values()), before_stock)
