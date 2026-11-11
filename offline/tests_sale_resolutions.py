"""Fictional decisions only. No operational identifiers or connections."""
import copy
import json
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4
from django.test import TestCase, TransactionTestCase, Client
from django.db import connection, close_old_connections, DatabaseError
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from . import tests_sale_creation as creation
from .tests_sale_revisions import replacement
from .services import process_operation, command_hash
from .models import OperacaoSincronizacao, AbandonoVendaOffline
from .resolutions import resolve
from estoque.models import Venda, Produto


def body(command):
    return dict(command=command,payload_hash=command_hash(command),closure_id=str(uuid4()),
        device_id=str(uuid4()),motivo='Tentativa abandonada; a venda correta já foi registrada.',confirm=True)


class ResolutionFixture:
    effects=creation.SaleCreationTests.effects
    def setUp(self):
        self.user,self.customer,self.product,self.command=creation.fixtures()
        self.client.force_login(self.user)

    def conflict(self):
        self.command['payload']['itens'][0]['preco_unitario']='1'
        self.assertEqual(process_operation(self.command,self.user)[1],409)

    def post(self,command=None,data=None):
        command=command or self.command
        return self.client.post('/api/offline/operations/'+command['operation_id']+'/resolution/',
            json.dumps(data or body(command)),content_type='application/json')


class SaleResolutionTests(ResolutionFixture,TestCase):
    def test_additive_migration_matches_models_without_executing_it(self):
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.state import ModelState
        from .models import ControleResolucaoVendaOffline
        state=MigrationLoader(None).project_state([('offline','0006_resolucao_venda_offline')])
        for model in (AbandonoVendaOffline,ControleResolucaoVendaOffline):
            declared=state.models[('offline',model._meta.model_name)]
            current=ModelState.from_model(model)
            self.assertEqual(set(declared.fields),set(current.fields))
            for field in declared.fields:
                def signature(value):
                    _,path,args,kwargs=value.deconstruct()
                    if 'to' in kwargs:kwargs['to']=str(kwargs['to']).lower()
                    return path,args,kwargs
                self.assertEqual(signature(declared.fields[field]),signature(current.fields[field]))

    def test_conflict_history_and_existing_official_sale_preserved(self):
        self.conflict()
        good=replacement(self.command);good['payload'].pop('revisao');good['payload']['itens'][0]['preco_unitario']='10'
        good['payload']['data_venda']='2026-11-10';good['payload']['data_vencimento']='2026-12-10'
        for index in range(5):
            product=Produto.objects.create(nome=f'Produto oficial fictício {index}',preco_compra=5,
                preco_vista=Decimal('56.24'),preco_prazo=Decimal('56.24'),unidade_venda_1='UN',quantidade=10)
            good['payload']['itens'].append(dict(produto_id=str(product.pk),quantidade='1',unidade='UN',preco_unitario='56.24'))
        self.assertEqual(process_operation(good,self.user)[1],200)
        self.assertEqual(str(Venda.objects.get().total),'301.20')
        self.assertEqual(Venda.objects.get().itens.count(),6)
        before=self.effects();history=list(OperacaoSincronizacao.objects.values())
        result=self.post()
        self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(result.json()['lookup'],'encontrada')
        self.assertEqual(self.effects(),before)
        self.assertEqual(list(OperacaoSincronizacao.objects.values()),history)
        self.assertEqual(process_operation(self.command,self.user)[0],history[0]['resultado'])

    def test_absent_replacement_reservation_blocks_send_then_parent_can_close(self):
        self.conflict();child=replacement(self.command)
        child['payload']['itens'][0]['preco_unitario']='10'
        before=self.effects();history=list(OperacaoSincronizacao.objects.values())
        closed=self.post(child).json()
        self.assertEqual(closed['lookup'],'encontrada')
        self.assertFalse(OperacaoSincronizacao.objects.filter(operation_id=child['operation_id']).exists())
        for _ in range(2):
            receipt,status=process_operation(child,self.user)
            self.assertEqual(status,409);self.assertEqual(receipt['code'],'encerrada_administrativamente')
        self.assertEqual(self.post().json()['lookup'],'encontrada')
        self.assertEqual(AbandonoVendaOffline.objects.count(),2)
        self.assertEqual(self.effects(),before)
        self.assertEqual(list(OperacaoSincronizacao.objects.values()),history)

    def test_unknown_server_operation_preserved_until_definitive(self):
        self.conflict()
        OperacaoSincronizacao.objects.filter(operation_id=self.command['operation_id']).update(status='enviando')
        self.assertEqual(self.post().json()['lookup'],'aguardando')
        self.assertFalse(AbandonoVendaOffline.objects.exists())

    def test_confirmed_operation_returns_official_record_without_closing(self):
        receipt,status=process_operation(self.command,self.user);self.assertEqual(status,200)
        before=self.effects()
        state=self.post().json()
        self.assertEqual(state['lookup'],'oficial');self.assertEqual(state['record_id'],receipt['record_id'])
        self.assertFalse(AbandonoVendaOffline.objects.exists());self.assertEqual(self.effects(),before)

    def test_chain_must_close_from_leaf_and_preserve_lineage(self):
        self.conflict();child=replacement(self.command,quantity='100')
        self.assertEqual(process_operation(child,self.user)[1],409)
        grandchild=replacement(child,quantity='100')
        self.assertEqual(process_operation(grandchild,self.user)[1],409)
        history=list(OperacaoSincronizacao.objects.values())
        self.assertEqual(self.post().json()['lookup'],'dependencias')
        self.assertEqual(self.post(child).json()['lookup'],'dependencias')
        for cmd in (grandchild,child,self.command):
            self.assertEqual(self.post(cmd).json()['lookup'],'encontrada')
        self.assertEqual(list(OperacaoSincronizacao.objects.values()),history)
        self.assertEqual(AbandonoVendaOffline.objects.count(),3)

    def test_replay_and_lost_response_lookup_return_same_decision(self):
        self.conflict();data=body(self.command)
        first=self.post(data=data).json()
        self.assertEqual(self.post(data=data).json(),first)
        query=dict(actor_id=self.command['actor_id'],environment_id=self.command['environment_id'],
            original_device_id=self.command['device_id'],hash=command_hash(self.command))
        self.assertEqual(self.client.get('/api/offline/operations/'+self.command['operation_id']+'/resolution/',query).json(),first)
        self.assertEqual(AbandonoVendaOffline.objects.count(),1)

    def test_independent_unknown_does_not_block_or_change(self):
        self.conflict();other=replacement(self.command);other['payload'].pop('revisao')
        process_operation(other,self.user)
        OperacaoSincronizacao.objects.filter(operation_id=other['operation_id']).update(status='enviando')
        before=OperacaoSincronizacao.objects.values().get(operation_id=other['operation_id'])
        self.assertEqual(self.post().json()['lookup'],'encontrada')
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=other['operation_id']),before)

    def test_scope_hash_csrf_permissions_and_inconclusive_lookup(self):
        self.conflict();url='/api/offline/operations/'+self.command['operation_id']+'/resolution/'
        self.assertEqual(Client().post(url,'{}',content_type='application/json').status_code,401)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(url,'{}',content_type='application/json').status_code,403)
        for changes in ({'confirm':False},{'payload_hash':'0'*64},{'motivo':''}):
            self.assertEqual(self.post(data={**body(self.command),**changes}).status_code,409)
        with patch('offline.resolutions.inspect',side_effect=DatabaseError('synthetic outage')):
            self.assertEqual(self.post().status_code,503)
        self.assertFalse(AbandonoVendaOffline.objects.exists())

    def test_closed_parent_blocks_new_replacement(self):
        self.conflict();self.post()
        child=replacement(self.command);child['payload']['itens'][0]['preco_unitario']='10'
        result,status=process_operation(child,self.user)
        self.assertEqual(status,409);self.assertEqual(result['code'],'revisao_origem_bloqueada')
        self.assertFalse(Venda.objects.exists())


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL')
class SaleResolutionConcurrencyTests(ResolutionFixture,TransactionTestCase):
    reset_sequences=True
    def race(self,actions):
        gate=Barrier(len(actions))
        def run(action):
            close_old_connections()
            try:
                gate.wait(timeout=15)
                return action()
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:return list(pool.map(run,actions))

    def test_send_versus_abandon_absent_command_has_one_outcome(self):
        command=self.command;data=body(command)
        decision,sent=self.race([lambda:resolve(command['operation_id'],data,self.user,command['environment_id']),
            lambda:process_operation(command,self.user)])
        if decision['lookup']=='encontrada':
            self.assertEqual(sent[1],409);self.assertEqual(Venda.objects.count(),0)
        else:
            self.assertEqual(decision['lookup'],'oficial');self.assertEqual(Venda.objects.count(),1)
            self.assertFalse(AbandonoVendaOffline.objects.exists())

    def test_concurrent_same_and_different_requests_share_one_receipt(self):
        data=body(self.command)
        results=self.race([lambda:resolve(self.command['operation_id'],data,self.user,self.command['environment_id']),
            lambda:resolve(self.command['operation_id'],body(self.command),self.user,self.command['environment_id'])])
        self.assertEqual(results[0],results[1]);self.assertEqual(AbandonoVendaOffline.objects.count(),1)
        self.assertEqual(Venda.objects.count(),0)

    def test_global_uuid_is_reserved_even_when_another_actor_sends(self):
        other=get_user_model().objects.create_user(username='another-resolution-actor')
        command=copy.deepcopy(self.command);command['actor_id']=str(other.pk)
        def close():
            try:return resolve(self.command['operation_id'],body(self.command),self.user,self.command['environment_id'])
            except ValidationError:return {'lookup':'recusada'}
        decision,sent=self.race([close,lambda:process_operation(command,other)])
        if decision['lookup']=='encontrada':
            self.assertEqual(sent[1],409);self.assertFalse(Venda.objects.exists())
        else:
            self.assertEqual(decision['lookup'],'recusada');self.assertEqual(Venda.objects.count(),1)
            self.assertFalse(AbandonoVendaOffline.objects.exists())

    def test_parent_closure_versus_child_send_never_creates_sale_after_closure(self):
        self.conflict();child=replacement(self.command);child['payload']['itens'][0]['preco_unitario']='10'
        decision,sent=self.race([lambda:resolve(self.command['operation_id'],body(self.command),self.user,self.command['environment_id']),
            lambda:process_operation(child,self.user)])
        if decision['lookup']=='encontrada':
            self.assertEqual(sent[1],409);self.assertFalse(Venda.objects.exists())
        else:self.assertEqual(decision['lookup'],'dependencias');self.assertEqual(Venda.objects.count(),1)
