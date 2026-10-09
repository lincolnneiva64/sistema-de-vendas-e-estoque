"""2.8D: online transport shares real transactional sale receipts."""
import copy
import json
from decimal import Decimal
from django.test import TestCase, Client
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto
from .models import OperacaoSincronizacao
from .services import command_hash
from .tests_sale_creation import fixtures


class OnlineSaleTests(TestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.command = fixtures()
        self.user.user_permissions.clear()  # Online creation does not require pilot permission.
        self.client.force_login(self.user)

    def send(self, command=None):
        command = command or self.command
        return self.client.post('/api/vendas/online/', json.dumps({**command, 'payload_hash':command_hash(command)}),
                                content_type='application/json')

    def lookup(self):
        return self.client.get('/api/vendas/online/' + self.command['operation_id'] + '/', {
            'actor_id':str(self.user.pk), 'environment_id':self.command['environment_id'],
            'device_id':self.command['device_id'], 'type':'criar_venda', 'hash':command_hash(self.command)})

    def test_normal_lost_response_lookup_and_same_uuid_replay_all_payments(self):
        for payment in ['A prazo', 'A vista', 'consumo_proprio']:
            with self.subTest(payment=payment):
                from uuid import uuid4
                self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
                self.command['payload']['tipo_pagamento'] = payment
                if payment == 'A vista': self.command['payload']['origem_recebimento'] = {'caixa':'8', 'banco':'12'}
                else: self.command['payload'].pop('origem_recebimento', None)
                response = self.send()
                self.assertEqual(response.status_code, 200, response.content)
                body = response.json()
                self.assertTrue(body['sucesso'])
                self.assertEqual(body['venda_id'], body['receipt']['record_id'])
                self.assertEqual(self.lookup().json()['receipt'], body['receipt'])
                models = [Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto, OperacaoSincronizacao]
                before = {m.__name__:list(m.objects.order_by('pk').values()) for m in models}
                for _ in range(2):
                    self.assertEqual(self.send().json()['receipt'], body['receipt'])
                self.assertEqual(before, {m.__name__:list(m.objects.order_by('pk').values()) for m in models})
        self.assertEqual(Venda.objects.count(), 3)
        self.assertEqual(ItemVenda.objects.count(), 3)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, Decimal('4'))
        self.assertEqual(ContaReceber.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        self.assertEqual(DespesaDiaria.objects.count(), 1)

    def test_absence_and_divergent_command_never_change_original(self):
        self.assertEqual(self.lookup().json()['lookup'], 'nao_encontrada')
        receipt = self.send().json()['receipt']
        changed = copy.deepcopy(self.command)
        changed['payload']['itens'][0]['quantidade'] = '3'
        self.assertEqual(self.send(changed).status_code, 409)
        self.assertEqual(self.lookup().json()['receipt'], receipt)
        self.assertEqual(Venda.objects.count(), 1)

    def test_auth_scope_and_csrf(self):
        self.send()
        other = Client()
        self.assertEqual(other.post('/api/vendas/online/', '{}', content_type='application/json').status_code, 401)
        self.assertEqual(other.get('/api/vendas/online/' + self.command['operation_id'] + '/').status_code, 401)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post('/api/vendas/online/', '{}', content_type='application/json').status_code, 403)
        self.client.logout()
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='other-online')
        self.client.force_login(user)
        self.assertEqual(self.lookup().status_code, 409)
