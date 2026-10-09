import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from uuid import uuid4
from django.core.exceptions import ValidationError
from django.db import connection,close_old_connections
from django.test import TestCase,TransactionTestCase
from django.test import Client
from django.contrib.auth import get_user_model
from . import tests_sale_creation as creation
from . import tests_sale_revisions as revisions
from .closures import close_conflict
from .models import EncerramentoVendaOffline,OperacaoSincronizacao,RevisaoVendaOffline
from .services import process_operation,command_hash
from estoque.models import Venda


def closure_body(command):
    return {'closure_id':str(uuid4()),'hash':command_hash(command),'actor_id':command['actor_id'],
            'environment_id':command['environment_id'],'original_device_id':command['device_id'],
            'device_id':str(uuid4()),'motivo':'Teste abandonado: nenhuma venda realizada.','confirm':True}


class SaleClosureTests(TestCase):
    effects=creation.SaleCreationTests.effects
    def setUp(self):
        self.user,self.customer,self.product,self.command=creation.fixtures()
        self.command['payload']['itens'][0]['quantidade']='100'
        receipt,status=process_operation(self.command,self.user)
        self.assertEqual(status,409)
        self.body=closure_body(self.command)
        self.before=OperacaoSincronizacao.objects.values().get()
        self.client.force_login(self.user)

    def close(self,body=None):
        return self.client.post('/api/offline/operations/'+self.command['operation_id']+'/close/',
            json.dumps(body or self.body),content_type='application/json')

    def test_close_idempotent_immutable_original_and_all_commercial_effects(self):
        # Two other official sales must survive administrative closure intact.
        for mode in ['consumo_proprio','A prazo']:
            command=copy.deepcopy(self.command);identifier=str(uuid4())
            command.update(operation_id=identifier,aggregate_id=identifier,sequence=command['sequence']+1)
            command['payload']['itens'][0]['quantidade']='1';command['payload']['tipo_pagamento']=mode
            self.assertEqual(process_operation(command,self.user)[1],200)
        before=self.effects()
        operations=list(OperacaoSincronizacao.objects.order_by('pk').values())
        result=self.close();self.assertEqual(result.status_code,200,result.content)
        receipt=result.json()['closure'];self.assertEqual(receipt['status'],'encerrada_sem_venda')
        self.assertIsNone(receipt['record_id'])
        self.assertEqual(receipt['original_receipt'],self.before['resultado'])
        row=EncerramentoVendaOffline.objects.values().get()
        self.assertEqual(self.close().json(),result.json())
        self.assertEqual(EncerramentoVendaOffline.objects.values().get(),row)
        self.assertEqual(self.effects(),before)
        self.assertEqual(list(OperacaoSincronizacao.objects.order_by('pk').values()),operations)
        params={k:self.body[k] for k in ['hash','actor_id','environment_id','original_device_id']}
        queried=self.client.get('/api/offline/operations/'+self.command['operation_id']+'/closure/',params)
        self.assertEqual(queried.json()['closure'],receipt)
        self.assertEqual(Venda.objects.count(),2)

    def test_refuse_hash_actor_environment_device_missing_reason_and_confirmation(self):
        before=self.effects()
        for field,value in [('hash','0'*64),('actor_id','9999'),('environment_id','other'),
                            ('original_device_id',str(uuid4())),('motivo',' '),('confirm',False)]:
            data={**self.body,field:value};result=self.close(data)
            self.assertEqual(result.status_code,409,(field,result.content))
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)
        self.assertEqual(self.effects(),before)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(),self.before)

    def test_confirmed_unknown_processing_and_incompatible_receipt_refused(self):
        for status in ['confirmada','resultado_desconhecido','enviando']:
            OperacaoSincronizacao.objects.filter(operation_id=self.command['operation_id']).update(status=status)
            self.assertEqual(self.close().status_code,409)
        OperacaoSincronizacao.objects.filter(operation_id=self.command['operation_id']).update(status='conflito',resultado={**self.before['resultado'],'record_id':123})
        self.assertEqual(self.close().status_code,409)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)

    def test_all_existing_replacements_including_orphan_unknown_prevent_closure(self):
        child=revisions.replacement(self.command)
        # An orphan command is enough to refuse, independently of lineage/status.
        row=OperacaoSincronizacao.objects.create(operation_id=child['operation_id'],device_id=child['device_id'],
            actor=self.user,environment_id=child['environment_id'],type=child['type'],schema_version=1,
            aggregate_id=child['aggregate_id'],payload=child['payload'],payload_hash=command_hash(child),comando=child,status='enviando')
        for status in ['enviando','pendente','resultado_desconhecido','confirmada','conflito']:
            row.status=status;row.save(update_fields=['status']);self.assertEqual(self.close().status_code,409)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)

    def test_global_revision_and_original_replay_cannot_create_sale_after_closure(self):
        self.assertEqual(self.close().status_code,200)
        before=self.effects()
        child=revisions.replacement(self.command)
        receipt,status=process_operation(child,self.user)
        self.assertEqual(status,409);self.assertEqual(receipt['code'],'revisao_origem_bloqueada')
        self.assertEqual(RevisaoVendaOffline.objects.count(),0)
        self.assertEqual(process_operation(self.command,self.user)[0],self.before['resultado'])
        self.assertEqual(self.effects(),before)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.command['operation_id']),self.before)

    def test_changed_request_not_overwritten_and_lookup_scope_preserved(self):
        result=self.close().json()
        for data in [{**self.body,'motivo':'Outro motivo'},{**self.body,'closure_id':str(uuid4())}]:
            self.assertEqual(self.close(data).status_code,409)
        params={k:self.body[k] for k in ['hash','actor_id','environment_id','original_device_id']}
        self.assertEqual(self.client.get('/api/offline/operations/'+self.command['operation_id']+'/closure/',{**params,'hash':'0'*64}).status_code,409)
        self.assertEqual(self.close().json(),result)

    def test_auth_permission_csrf_and_private_response(self):
        url='/api/offline/operations/'+self.command['operation_id']+'/close/'
        self.assertEqual(Client().post(url,json.dumps(self.body),content_type='application/json').status_code,401)
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(url,json.dumps(self.body),content_type='application/json').status_code,403)
        outsider=get_user_model().objects.create_user(username='unauthorized')
        client=Client();client.force_login(outsider)
        self.assertEqual(client.post(url,json.dumps(self.body),content_type='application/json').status_code,403)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)
        response=self.close();self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response['Cache-Control'])

    def test_revision_conflict_cannot_be_closed_as_an_unlinked_root(self):
        child=revisions.replacement(self.command);child['payload']['itens'][0]['quantidade']='100'
        self.assertEqual(process_operation(child,self.user)[1],409)
        result=self.client.post('/api/offline/operations/'+child['operation_id']+'/close/',
            json.dumps(closure_body(child)),content_type='application/json')
        self.assertEqual(result.status_code,409)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)

    def test_original_command_hash_and_payload_evidence_must_agree(self):
        OperacaoSincronizacao.objects.filter(operation_id=self.command['operation_id']).update(payload={})
        self.assertEqual(self.close().status_code,409)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),0)


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL')
class SaleClosureConcurrencyTests(TransactionTestCase):
    reset_sequences=True
    def setUp(self):
        self.user,self.customer,self.product,self.command=creation.fixtures()
        self.command['payload']['itens'][0]['quantidade']='100'
        self.assertEqual(process_operation(self.command,self.user)[1],409)
        self.before=OperacaoSincronizacao.objects.values().get()

    def race(self,actions):
        gate=Barrier(2)
        def run(action):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                try:return action()
                except ValidationError:return 'refused'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,actions))

    def test_revision_and_closure_serialize_on_same_parent(self):
        body=closure_body(self.command);child=revisions.replacement(self.command)
        self.race([lambda:close_conflict(self.command['operation_id'],body,self.user,self.command['environment_id']),
                   lambda:process_operation(child,self.user)])
        closed=EncerramentoVendaOffline.objects.count();revised=RevisaoVendaOffline.objects.count()
        self.assertEqual(closed+revised,1)
        self.assertEqual(Venda.objects.count(),revised)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.command['operation_id']),self.before)
        self.product.refresh_from_db();self.assertEqual(self.product.quantidade,10-2*revised)

    def test_two_distinct_closures_one_decision_no_effects(self):
        first=closure_body(self.command);second=closure_body(self.command)
        result=self.race([lambda:close_conflict(self.command['operation_id'],first,self.user,self.command['environment_id']),
                          lambda:close_conflict(self.command['operation_id'],second,self.user,self.command['environment_id'])])
        self.assertEqual(sum(value=='refused' for value in result),1)
        self.assertEqual(EncerramentoVendaOffline.objects.count(),1);self.assertEqual(Venda.objects.count(),0)

    def test_two_replays_same_closure_return_identical_receipt(self):
        body=closure_body(self.command)
        result=self.race([lambda:close_conflict(self.command['operation_id'],body,self.user,self.command['environment_id'])]*2)
        self.assertEqual(result[0],result[1]);self.assertEqual(EncerramentoVendaOffline.objects.count(),1)
        self.assertEqual(Venda.objects.count(),0)
