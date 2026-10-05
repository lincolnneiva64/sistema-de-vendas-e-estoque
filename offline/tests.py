import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from unittest import mock, skipUnless
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from estoque.models import ContaReceber, DespesaDiaria, MovimentoFinanceiro, Produto, Venda
from locacoes.models import (ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao, EventoLocacao,
                             FaixaPrecoLocacao, Locacao, MovimentoEstoqueLocacao, PagamentoLocacao, TarefaOperacionalLocacao)
from .models import OperacaoSincronizacao
from .services import command_hash, process_operation


def fixtures():
    user = get_user_model().objects.create_user(username="offline-pilot", password="test-only")
    user.user_permissions.add(Permission.objects.get(codename="registrar_observacao", content_type__app_label="offline"))
    faixa, _ = FaixaPrecoLocacao.objects.get_or_create(codigo="centro_perto", defaults={"nome": "Teste", "preco_jogo_diaria": "10"})
    today = date.today()
    rental = Locacao.objects.create(tipo_pessoa="avulsa", pessoa_avulsa_nome="Teste", endereco_entrega="Teste",
        data_entrega=today, horario_entrega=time(10), data_evento=today, horario_evento=time(12), data_prevista_devolucao=today,
        faixa_preco=faixa, faixa_preco_nome_snapshot=faixa.nome)
    task = TarefaOperacionalLocacao.objects.create(locacao=rental, tipo="entrega", data_agendada=today)
    command = {"operation_id": str(uuid4()), "device_id": str(uuid4()), "actor_id": str(user.pk),
        "environment_id": "offline-isolated-tests", "type": "observacao_operacional", "schema_version": 1,
        "aggregate_id": str(task.pk), "payload": {"locacao_id": str(rental.pk), "tarefa_status": task.status,
        "observacao": "Cliente pediu portão lateral. 😀"}, "created_at": timezone.now().isoformat(), "sequence": 1}
    return user, rental, task, command


class SaleDeliveryOfflineTests(TestCase):
    def setUp(self):
        from estoque.models import EntregaRota, EntregaRotaItem
        self.user, _, task, self.command = fixtures()
        sale = Venda.objects.create(data_venda=task.data_agendada)
        route = EntregaRota.objects.create(tipo='unitaria')
        self.item = EntregaRotaItem.objects.create(rota=route, venda=sale)
        self.command.update(type='observacao_entrega_venda', aggregate_id=str(self.item.pk),
            payload={'rota_id':str(route.pk), 'venda_id':str(sale.pk),
                     'tarefa_status':self.item.status, 'observacao':'Observação adicional'})
        self.client.force_login(self.user)

    def send(self, command):
        return self.client.post('/api/offline/observations/', data=json.dumps({**command, 'payload_hash':command_hash(command)}), content_type='application/json')

    def test_sale_note_idempotent_and_physical_data_unchanged(self):
        from estoque.models import EventoVenda, EntregaRotaItem
        before = EntregaRotaItem.objects.filter(pk=self.item.pk).values().get()
        snapshot = self.client.get(f'/api/offline/snapshot/?rota={self.item.rota_id}').json()
        self.assertEqual(snapshot['tasks'][0]['kind'], 'entrega_venda')
        first = self.send(self.command)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.send(self.command).json(), first.json())
        self.assertEqual(EventoVenda.objects.filter(tipo_evento='observacao_offline').count(), 1)
        self.assertEqual(EntregaRotaItem.objects.filter(pk=self.item.pk).values().get(), before)

    def test_reject_physical_payload_and_changed_delivery(self):
        for field in ('quantidade', 'conferido_cliente', 'entrega_concluida', 'status', 'estoque', 'avaria', 'locacao_id'):
            command = copy.deepcopy(self.command)
            command['payload'][field] = True
            self.assertEqual(self.send(command).status_code, 400)
        self.item.status = 'cancelada'; self.item.save(update_fields=['status'])
        response = self.send(self.command)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['status'], 'conflito')

    def test_normal_online_checklist_still_saves_physical_confirmation(self):
        response = self.client.post(f'/entregas/{self.item.rota_id}/checklist/', {
            'salvar_bloco':f'{self.item.pk}:entrega',
            f'conferido_{self.item.pk}':'on', f'concluida_{self.item.pk}':'on',
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertTrue(self.item.conferido_cliente)
        self.assertTrue(self.item.entrega_concluida)
        self.assertEqual(OperacaoSincronizacao.objects.count(), 0)


class OfflineAPITests(TestCase):
    def test_checklist_pages_and_targeted_snapshot(self):
        rental, task = self.rental, self.task
        for path in ('/locacoes/checklist-operacional/',
                     f'/locacoes/tarefas-operacionais/{task.pk}/conferencia-entrega/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'data-checklist-task="{task.pk}"')
            self.assertContains(response, '/static/offline/checklist.js')
        rental.status = 'entregue'
        rental._permitir_alterar_status = True
        rental.save(update_fields=['status'])
        pickup = TarefaOperacionalLocacao.objects.create(locacao=rental, tipo='recolhimento', data_agendada=task.data_agendada)
        response = self.client.get(f'/locacoes/tarefas-operacionais/{pickup.pk}/conferencia-recolhimento/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-checklist-task="{pickup.pk}"')
        snapshot = self.client.get(f'/api/offline/snapshot/?task={pickup.pk}').json()
        self.assertEqual([t['id'] for t in snapshot['tasks']], [str(pickup.pk)])

    def test_login_and_session_preparation(self):
        anonymous = Client()
        self.assertEqual(anonymous.get('/api/offline/session/').json(), {
            'authenticated': False, 'can_prepare': False, 'username': '',
        })
        self.assertEqual(anonymous.get('/offline/login/?next=/offline/').status_code, 200)
        response = anonymous.post('/offline/login/', {'username': self.user.username, 'password': 'test-only'})
        self.assertRedirects(response, '/offline/')
        self.assertTrue(anonymous.get('/api/offline/session/').json()['can_prepare'])
        self.assertRedirects(anonymous.get('/offline/login/'), '/offline/')
        self.user.user_permissions.clear()
        self.assertFalse(anonymous.get('/api/offline/session/').json()['can_prepare'])
        self.assertEqual(anonymous.get('/api/offline/snapshot/').status_code, 403)

    def setUp(self):
        self.user, self.rental, self.task, self.command = fixtures()
        self.client.force_login(self.user)

    def send(self, command=None, client=None):
        command = command or self.command
        return (client or self.client).post('/api/offline/observations/',
            data=json.dumps({**command, "payload_hash": command_hash(command)}, ensure_ascii=False), content_type='application/json')

    def test_uuid_persisted_and_replay_returns_exact_result(self):
        first = self.send()
        second = self.send()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        self.assertEqual(EventoLocacao.objects.filter(tipo="observacao_offline").count(), 1)
        self.assertEqual(str(OperacaoSincronizacao.objects.get().operation_id), self.command["operation_id"])

    def test_uuid_with_other_content_is_conflict(self):
        self.send()
        changed = copy.deepcopy(self.command)
        changed["payload"]["observacao"] = "Outra observação"
        self.assertEqual(self.send(changed).status_code, 409)
        self.assertEqual(EventoLocacao.objects.count(), 1)

    def test_response_lost_after_commit_recovered_with_same_uuid(self):
        result = self.send().json()  # Pretend the caller never received this response.
        self.assertEqual(self.send().json(), result)
        self.assertEqual(EventoLocacao.objects.count(), 1)

    def test_failure_after_event_creation_rolls_back_all(self):
        original_save = OperacaoSincronizacao.save
        def fail_completion(instance, *args, **kwargs):
            if kwargs.get('update_fields'):
                raise RuntimeError('simulated after event')
            return original_save(instance, *args, **kwargs)
        with mock.patch.object(OperacaoSincronizacao, 'save', fail_completion):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
        self.assertEqual(EventoLocacao.objects.count(), 0)

    def test_pilot_has_no_physical_financial_or_status_effects(self):
        models = [Venda, Produto, ContaReceber, MovimentoFinanceiro, DespesaDiaria, PagamentoLocacao,
                  MovimentoEstoqueLocacao, ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao]
        before = {m: m.objects.count() for m in models}
        rental_before = Locacao.objects.values().get(pk=self.rental.pk)
        task_before = TarefaOperacionalLocacao.objects.values().get(pk=self.task.pk)
        self.send()
        self.assertEqual(before, {m: m.objects.count() for m in models})
        self.assertEqual(rental_before, Locacao.objects.values().get(pk=self.rental.pk))
        self.assertEqual(task_before, TarefaOperacionalLocacao.objects.values().get(pk=self.task.pk))

    def test_missing_task_conflict_preserves_payload_and_replays(self):
        self.task.delete()
        first = self.send()
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json(), self.send().json())
        self.assertEqual(OperacaoSincronizacao.objects.get().payload, self.command['payload'])
        self.assertEqual(EventoLocacao.objects.count(), 0)

    def test_changed_task_conflicts(self):
        TarefaOperacionalLocacao.objects.filter(pk=self.task.pk).update(status='confirmada')
        self.assertEqual(self.send().status_code, 409)

    def test_cancelled_rental_conflicts(self):
        Locacao.objects.filter(pk=self.rental.pk).update(status='cancelada')
        self.assertEqual(self.send().status_code, 409)

    def test_health_no_store_and_no_writes(self):
        with self.assertNumQueries(1):
            response = Client().get('/api/offline/health/')
        self.assertTrue(response.json()['ok'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.json()['environment'], 'offline-isolated-tests')

    def test_health_database_failure(self):
        from django.db import DatabaseError
        with mock.patch('offline.views.connection.cursor', side_effect=DatabaseError):
            response = self.client.get('/api/offline/health/')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])

    def test_authentication_and_authorization_required(self):
        self.client.logout()
        self.assertEqual(self.send().status_code, 401)
        self.assertEqual(self.client.get('/api/offline/snapshot/').status_code, 401)
        self.user.user_permissions.clear()
        self.client.force_login(self.user)
        self.assertEqual(self.send().status_code, 403)

    def test_invalid_hash_actor_environment_and_type_rejected(self):
        for field, value in [('actor_id', '999'), ('environment_id', 'other'), ('type', 'venda'), ('schema_version', 2)]:
            with self.subTest(field=field):
                command = {**self.command, field: value}
                self.assertEqual(self.send(command).status_code, 400)
        data = {**self.command, 'payload_hash': '0' * 64}
        self.assertEqual(self.client.post('/api/offline/observations/', data=json.dumps(data), content_type='application/json').status_code, 400)
        self.assertFalse(OperacaoSincronizacao.objects.exists())

    def test_csrf_required_and_snapshot_does_not_write_domain(self):
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.user)
        response = secure.get('/api/offline/snapshot/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.send(client=secure).status_code, 403)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.json()['tasks'][0]['id'], str(self.task.pk))
        self.assertFalse(EventoLocacao.objects.exists())

    def test_shell_has_no_cdn_and_worker_is_restricted(self):
        shell = self.client.get('/offline/').content.decode()
        self.assertNotIn('cdn.', shell)
        worker = self.client.get('/service-worker.js')
        self.assertEqual(worker.headers['Service-Worker-Allowed'], '/')
        self.assertIn("event.request.method !== 'GET'", worker.content.decode())
        self.assertNotIn('/api/offline/health/', worker.content.decode())


@skipUnless(connection.vendor == 'postgresql', 'Requires isolated PostgreSQL: set OFFLINE_TEST_DATABASE_URL')
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user, self.rental, self.task, self.command = fixtures()

    def test_concurrent_same_uuid_creates_one_event(self):
        from threading import Barrier
        barrier = Barrier(2)
        user_id = self.user.pk
        def submit():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=user_id)
                barrier.wait(timeout=10)
                return process_operation(self.command, user)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        self.assertEqual(EventoLocacao.objects.count(), 1)
