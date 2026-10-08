"""Revision lineage, authoritative guards and atomic commercial effects."""
import copy
import json
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, Produto
from .models import OperacaoSincronizacao, RevisaoVendaOffline
from .services import command_hash, process_operation, validate_command
from . import tests_sale_creation as creation

fixtures = creation.fixtures


def replacement(original, quantity="2"):
    command = copy.deepcopy(original)
    identifier = str(uuid4())
    command.update(operation_id=identifier, aggregate_id=identifier, sequence=original['sequence'] + 1,
                   created_at=timezone.now().isoformat(), device_id=str(uuid4()))
    command['payload']['itens'][0]['quantidade'] = quantity
    command['payload']['revisao'] = {
        'original_operation_id': original['operation_id'], 'original_hash': command_hash(original),
        'relacao': 'revisao_de_conflito', 'revisada_em': timezone.now().isoformat(),
    }
    return command


class SaleRevisionTests(TestCase):
    effects = creation.SaleCreationTests.effects

    def setUp(self):
        self.user, self.customer, self.product, self.original = fixtures()
        self.original['payload']['itens'][0]['quantidade'] = '100'
        self.receipt, status = process_operation(self.original, self.user)
        self.assertEqual(status, 409)
        self.client.force_login(self.user)
        self.before_original = OperacaoSincronizacao.objects.values().get()

    def send(self, command):
        return self.client.post('/api/offline/observations/', json.dumps({**command,'payload_hash':command_hash(command)}), content_type='application/json')

    def lookup(self):
        params = {key:self.original[key] for key in ('actor_id','environment_id','device_id','type')}
        return self.client.get('/api/offline/operations/'+self.original['operation_id']+'/', {**params,'hash':command_hash(self.original)})

    def unchanged(self):
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.original['operation_id']), self.before_original)

    def test_link_confirmation_replay_and_read_only_lookup(self):
        before = self.effects()
        self.assertEqual(self.lookup().json()['revisions'], [])
        self.assertEqual(self.effects(), before)
        command = replacement(self.original)
        result = self.send(command)
        self.assertEqual(result.status_code,200,result.content)
        relation = RevisaoVendaOffline.objects.get()
        self.assertEqual(str(relation.original.operation_id), self.original['operation_id'])
        self.assertEqual(str(relation.substituta.operation_id), command['operation_id'])
        self.assertEqual(relation.original_hash,command_hash(self.original))
        self.assertEqual(relation.actor,self.user)
        self.assertEqual(relation.relacao,'revisao_de_conflito')
        self.assertEqual(relation.motivo_original,self.receipt['erro'])
        self.assertEqual(Venda.objects.count(),1)
        self.assertEqual(ItemVenda.objects.count(),1)
        self.assertEqual(ContaReceber.objects.count(),1)
        self.unchanged()
        after = self.effects()
        with patch('offline.services.execute_sale',side_effect=AssertionError('Replay executes sale')):
            self.assertEqual(self.send(command).json(),result.json())
        looked = self.lookup().json()
        self.assertEqual(looked['receipt'],self.receipt)
        self.assertEqual(looked['revisions'][0]['receipt'],result.json())
        self.assertEqual(self.effects(),after)
        self.unchanged()

    def test_competing_device_rejected_without_second_effect(self):
        first=self.send(replacement(self.original));self.assertEqual(first.status_code,200)
        after=self.effects()
        second=self.send(replacement(self.original))
        self.assertEqual(second.status_code,409)
        self.assertEqual(second.json()['conflict_kind'],'tecnico')
        self.assertEqual(RevisaoVendaOffline.objects.count(),1)
        self.assertEqual(self.effects(),after);self.unchanged()

    def test_conflicting_replacement_can_be_revised_as_chain(self):
        child=replacement(self.original,'100')
        self.assertEqual(self.send(child).status_code,409)
        grandchild=replacement(child)
        self.assertEqual(self.send(grandchild).status_code,200)
        self.assertEqual(RevisaoVendaOffline.objects.count(),2)
        self.assertEqual(Venda.objects.count(),1)
        chain=self.lookup().json()['revisions']
        self.assertEqual([link['replacement_operation_id'] for link in chain],[child['operation_id'],grandchild['operation_id']])
        self.unchanged()

    def test_hash_actor_environment_type_and_missing_origin_rejected(self):
        for mutation in ('hash','actor','environment','type','missing'):
            with self.subTest(mutation=mutation):
                command=replacement(self.original)
                if mutation=='hash':command['payload']['revisao']['original_hash']='0'*64
                elif mutation=='missing':command['payload']['revisao']['original_operation_id']=str(uuid4())
                else:
                    field={'actor':'actor_id','environment':'environment_id','type':'type'}[mutation]
                    value = get_user_model().objects.create_user(username='other').pk if mutation == 'actor' else {
                        'environment':'other', 'type':'observacao_operacional',
                    }[mutation]
                    OperacaoSincronizacao.objects.filter(operation_id=self.original['operation_id']).update(**{field:value})
                result=self.send(command);self.assertEqual(result.status_code,409,result.content)
                self.assertEqual(result.json()['code'],'revisao_origem_bloqueada')
                if mutation in ('actor','environment','type'):
                    OperacaoSincronizacao.objects.filter(operation_id=self.original['operation_id']).update(**{field:self.before_original[field]})
        self.assertFalse(Venda.objects.exists());self.assertFalse(RevisaoVendaOffline.objects.exists());self.unchanged()

    def test_late_confirmation_technical_conflict_and_unknown_blocked(self):
        row=OperacaoSincronizacao.objects.get(operation_id=self.original['operation_id'])
        for status,receipt in [('confirmada',{**self.receipt,'status':'confirmada','record_id':123}),
                               ('resultado_desconhecido',self.receipt),
                               ('conflito',{**self.receipt,'code':'uuid_comando_divergente'})]:
            row.status=status;row.resultado=receipt;row.save(update_fields=['status','resultado'])
            before=OperacaoSincronizacao.objects.values().get(pk=row.pk)
            response=self.send(replacement(self.original));self.assertEqual(response.status_code,409)
            self.assertEqual(OperacaoSincronizacao.objects.values().get(pk=row.pk),before)
        self.assertFalse(Venda.objects.exists());self.assertFalse(RevisaoVendaOffline.objects.exists())

    def test_date_operator_and_invalid_lineage_are_not_accepted(self):
        for field in ('data_venda','operador'):
            command=replacement(self.original);command['payload'][field]='2026-10-08' if field=='data_venda' else 'Changed'
            self.assertEqual(self.send(command).status_code,409)
        command=replacement(self.original);command['payload']['revisao']['extra']='unexpected'
        self.assertEqual(self.send(command).status_code,400)
        self.assertFalse(Venda.objects.exists());self.assertFalse(RevisaoVendaOffline.objects.exists());self.unchanged()

    def test_technical_failure_rolls_back_link_operation_and_effects(self):
        command=replacement(self.original);before=self.effects()
        with patch('offline.services.execute_sale',side_effect=RuntimeError('technical failure')):
            with self.assertRaises(RuntimeError):process_operation(command,self.user)
        self.assertEqual(OperacaoSincronizacao.objects.count(),1)
        self.assertFalse(RevisaoVendaOffline.objects.exists());self.assertEqual(self.effects(),before);self.unchanged()

    def test_shared_revision_shell_has_no_identity_or_business_data(self):
        before=self.effects()
        response=self.client.get('/offline/revisao/?operation_id='+self.original['operation_id'])
        self.assertEqual(response.status_code,200)
        for secret in (self.user.username,self.customer.nome,self.original['operation_id']):
            self.assertNotIn(secret.encode(),response.content)
        self.assertEqual(self.effects(),before);self.unchanged()


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL: set OFFLINE_TEST_DATABASE_URL')
class SaleRevisionPostgreSQLTests(TransactionTestCase):
    def test_two_devices_create_only_one_replacement_sale(self):
        user,_,product,original=fixtures();original['payload']['itens'][0]['quantidade']='100'
        process_operation(original,user);before=OperacaoSincronizacao.objects.values().get()
        commands=[replacement(original),replacement(original)];barrier=Barrier(2)
        def submit(command):
            close_old_connections()
            try:
                actor=get_user_model().objects.get(pk=user.pk)
                validated=validate_command({**command,'payload_hash':command_hash(command)},actor,'offline-isolated-tests')
                barrier.wait(timeout=10);return process_operation(validated,actor)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=[future.result(timeout=30) for future in [pool.submit(submit,command) for command in commands]]
        self.assertEqual(sorted(result[1] for result in results),[200,409])
        self.assertEqual(RevisaoVendaOffline.objects.count(),1)
        self.assertEqual(Venda.objects.count(),1);self.assertEqual(ItemVenda.objects.count(),1);self.assertEqual(ContaReceber.objects.count(),1)
        self.assertEqual(MovimentoFinanceiro.objects.count(),0)
        product.refresh_from_db();self.assertEqual(product.quantidade,Decimal('8'))
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=original['operation_id']),before)
