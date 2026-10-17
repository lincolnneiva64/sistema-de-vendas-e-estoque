"""Isolated local adapter tests. Never use an operational connection."""
import copy
import json
import os
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from estoque.models import Venda, ItemVenda, Funcionario, Pedido, ItemPedido
from .models import OperacaoSincronizacao
from .services import command_hash, process_operation
from .local_sales import COOKIE, revision_for
from .tests_sale_creation import fixtures
from . import tests_sale_creation as existing_tests


@override_settings(LOCAL_SALES_LOOPBACK=True)
class LocalSaleTests(TestCase):
    def setUp(self):
        self.user,self.customer,self.product,self.old_command=fixtures()
        self.operator=Funcionario.objects.create(nome="Operador local",ativo=True,pode_operar_sistema=True)
        self.client=Client(HTTP_HOST="127.0.0.1")
        self.context=self.client.get('/vendas/').context['local_sales_context']
        self.command=self.build()

    def build(self, data=None, revision=""):
        identifier=str(uuid4())
        data=copy.deepcopy(data or self.old_command['payload'])
        data.pop('schema_version',None)
        data['operador']=self.operator.nome
        return {**self.old_command,'operation_id':identifier,'aggregate_id':identifier,
                'actor_id':self.context['actor_id'],'type':'venda_local',
                'payload':{'schema_version':1,'dados':data,'expected_revision':revision}}

    def send(self, command=None, client=None, **extra):
        command=command or self.command
        return (client or self.client).post('/api/vendas/online/',json.dumps({**command,'payload_hash':command_hash(command)}),
            content_type='application/json',HTTP_X_LOCAL_SALES='1',**extra)

    def lookup(self, command=None, client=None):
        command=command or self.command
        return (client or self.client).get('/api/vendas/online/'+command['operation_id']+'/',{'hash':command_hash(command)},HTTP_X_LOCAL_SALES='1')

    def effects(self):
        return existing_tests.SaleCreationTests.effects(self)

    def test_anonymous_page_and_receipt_replay_lost_response(self):
        response=self.send(); self.assertEqual(response.status_code,200,response.content)
        result=response.json(); self.assertTrue(result['sucesso'],result)
        before=self.effects()
        for _ in range(3):self.assertEqual(self.send().json()['receipt'],result['receipt'])
        self.assertEqual(self.lookup().json()['receipt'],result['receipt'])
        self.assertEqual(before,self.effects())
        operation=OperacaoSincronizacao.objects.get()
        self.assertEqual(operation.origin,'local'); self.assertIsNone(operation.actor_id)
        self.assertEqual(operation.declared_operator,self.operator.nome)
        self.assertEqual(Venda.objects.get().operador,self.operator.nome)

    def test_all_payments_no_duplicate_stock_or_finance(self):
        for payment in ['A prazo','A vista','consumo_proprio']:
            command=self.build();command['payload']['dados']['tipo_pagamento']=payment
            if payment=='A vista':command['payload']['dados']['origem_recebimento']={'caixa':'8','banco':'12'}
            response=self.send(command);self.assertEqual(response.status_code,200,response.content)
            self.assertTrue(response.json()['sucesso'],response.content)
            before=self.effects();self.send(command);self.assertEqual(before,self.effects())
        self.assertEqual(Venda.objects.count(),3)
        self.product.refresh_from_db();self.assertEqual(self.product.quantidade,Decimal('4'))

    def test_divergent_uuid_and_other_browser(self):
        self.send();before=self.effects()
        changed=copy.deepcopy(self.command);changed['payload']['dados']['itens'][0]['quantidade']='3'
        self.assertEqual(self.send(changed).status_code,409)
        other=Client(HTTP_HOST="127.0.0.1");other.get('/vendas/')
        self.assertEqual(self.send(client=other).status_code,400)
        self.assertEqual(self.lookup(client=other).status_code,409)
        self.assertEqual(before,self.effects())

    def test_expired_session_keeps_principal_and_authenticated_audit(self):
        self.client.force_login(self.user)
        self.context=self.client.get('/vendas/').context['local_sales_context'];self.command=self.build()
        self.assertTrue(self.send().json()['sucesso'])
        self.assertEqual(OperacaoSincronizacao.objects.get().actor_id,self.user.pk)
        owner=self.context['owner'];self.client.cookies.pop('sessionid',None)
        self.assertEqual(self.client.get('/vendas/').context['local_sales_context']['owner'],owner)
        self.assertEqual(self.send().status_code,200)

    def test_real_user_change_blocks_old_principal(self):
        self.send();before=self.effects()
        self.client.force_login(self.user)
        self.assertEqual(self.send().status_code,403)
        fresh=self.client.get('/vendas/').context['local_sales_context']
        self.assertNotEqual(fresh['owner'],self.context['owner'])
        self.assertEqual(self.lookup().status_code,409)
        self.assertEqual(before,self.effects())

    def test_authorization_disabled_even_debug_true(self):
        with override_settings(LOCAL_SALES_LOOPBACK=False,DEBUG=True):
            self.assertEqual(self.send().status_code,403)

    def test_never_render_proxy_or_non_loopback(self):
        self.assertEqual(self.send(REMOTE_ADDR='192.168.1.15').status_code,403)
        self.assertEqual(self.send(HTTP_X_FORWARDED_FOR='127.0.0.1').status_code,403)
        with patch.dict(os.environ,{'RENDER':'true'}):self.assertEqual(self.send().status_code,403)
        with override_settings(ALLOWED_HOSTS=['testserver','example.com']):
            self.assertEqual(self.send(HTTP_HOST='example.com').status_code,403)

    def test_cookie_missing_or_tampered_rejected(self):
        other=Client(HTTP_HOST="127.0.0.1");self.assertEqual(self.send(client=other).status_code,403)
        other.cookies[COOKIE]='tampered';self.assertEqual(self.send(client=other).status_code,403)

    def test_csrf_and_origin_required(self):
        browser=Client(enforce_csrf_checks=True,HTTP_HOST="127.0.0.1");page=browser.get('/vendas/');ctx=page.context['local_sales_context']
        command=copy.deepcopy(self.command);command['actor_id']=ctx['actor_id']
        self.assertEqual(self.send(command,client=browser).status_code,403)
        self.assertEqual(self.send(command,client=browser,HTTP_X_CSRFTOKEN=ctx['csrf'],HTTP_ORIGIN='https://evil.example').status_code,403)
        self.assertTrue(self.send(command,client=browser,HTTP_X_CSRFTOKEN=ctx['csrf']).json()['sucesso'])

    def test_legacy_local_transport_blocked(self):
        self.assertEqual(self.client.post('/vendas/gravar/', '{}',content_type='application/json').status_code,409)
        self.assertFalse(Venda.objects.exists())

    def test_offline_operations_stay_authenticated_and_unchanged(self):
        result=process_operation(self.old_command,self.user)
        before=list(OperacaoSincronizacao.objects.values())
        self.assertEqual(self.client.post('/api/offline/observations/',json.dumps({**self.old_command,'payload_hash':command_hash(self.old_command)}),content_type='application/json').status_code,401)
        self.assertEqual(self.client.get('/api/offline/session/').json()['authenticated'],False)
        self.assertEqual(before,list(OperacaoSincronizacao.objects.values()))
        # A logged-in local operation is not exposed by the authenticated lookup.
        self.client.force_login(self.user);self.context=self.client.get('/vendas/').context['local_sales_context'];command=self.build()
        self.send(command)
        self.assertEqual(self.client.get('/api/vendas/online/'+command['operation_id']+'/',{'type':'criar_venda','actor_id':str(self.user.pk),'environment_id':command['environment_id']}).status_code,409)

    def test_edit_version_replay_and_item_ids(self):
        sale_id=self.send().json()['venda_id'];sale=Venda.objects.get(pk=sale_id)
        data=copy.deepcopy(self.command['payload']['dados']);data['venda_id']=str(sale_id)
        data['itens'][0]['item_id']=str(sale.itens.get().pk);data['itens'][0]['quantidade']='3'
        revision=revision_for(data);edit=self.build(data,revision)
        response=self.send(edit);self.assertTrue(response.json()['sucesso'],response.content)
        before=self.effects();self.send(edit);self.assertEqual(before,self.effects())
        self.assertEqual(Venda.objects.count(),1);self.assertEqual(ItemVenda.objects.count(),1)
        stale=self.build(data,revision);self.assertEqual(self.send(stale).status_code,409)
        self.assertEqual(before,self.effects())

    def test_order_version_and_replay(self):
        order=Pedido.objects.create(cliente=self.customer,data_pedido='2026-10-07',operador=self.operator.nome)
        ItemPedido.objects.create(pedido=order,produto=self.product,quantidade=2,unidade='UN',preco_unitario=10,valor_total=20)
        data=copy.deepcopy(self.command['payload']['dados']);data['pedido_id']=str(order.pk)
        command=self.build(data,revision_for(data));response=self.send(command)
        self.assertTrue(response.json()['sucesso'],response.content)
        before=self.effects();self.send(command);self.assertEqual(before,self.effects())
        self.assertEqual(self.send(self.build(data,command['payload']['expected_revision'])).status_code,409)

    def test_invalid_employee_and_failure_roll_back(self):
        self.operator.ativo=False;self.operator.save()
        self.assertEqual(self.send().status_code,409);self.assertFalse(Venda.objects.exists())
        self.operator.ativo=True;self.operator.pode_operar_sistema=True;self.operator.save()
        command=self.build()
        with patch('offline.local_sales.criar_ou_atualizar_venda',side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):self.send(command)
        self.assertFalse(OperacaoSincronizacao.objects.filter(operation_id=command['operation_id']).exists())
        self.assertFalse(Venda.objects.exists())

    def test_database_rejects_invalid_principal(self):
        defaults=dict(operation_id=uuid4(),device_id=uuid4(),environment_id='test',type='criar_venda',schema_version=1,aggregate_id='test',payload={},payload_hash='a'*64,comando={},status='enviando')
        with self.assertRaises(IntegrityError),transaction.atomic():OperacaoSincronizacao.objects.create(**defaults)
        with self.assertRaises(IntegrityError),transaction.atomic():OperacaoSincronizacao.objects.create(**defaults,origin='local',local_owner='a'*64,declared_operator='test')


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase


@skipUnless(connection.vendor == "postgresql", "PostgreSQL local descartavel indisponivel")
@override_settings(LOCAL_SALES_LOOPBACK=True)
class LocalSaleConcurrencyTests(TransactionTestCase):
    setUp=LocalSaleTests.setUp
    build=LocalSaleTests.build

    def concurrent(self, commands):
        owner=self.context['owner']
        barrier=Barrier(2)
        def send(command):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return process_operation(command,AnonymousUser(),local_owner=owner)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(send,command) for command in commands]
            return [future.result(timeout=30) for future in futures]

    def test_same_uuid_has_one_sale_stock_and_finance(self):
        results=self.concurrent([self.command,copy.deepcopy(self.command)])
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[0][1],200)
        self.assertEqual(Venda.objects.count(),1)
        self.assertEqual(OperacaoSincronizacao.objects.count(),1)
        self.product.refresh_from_db();self.assertEqual(self.product.quantidade,Decimal('8'))

    def test_same_uuid_different_content_keeps_winner(self):
        changed=copy.deepcopy(self.command);changed['payload']['dados']['itens'][0]['quantidade']='3'
        results=self.concurrent([self.command,changed])
        self.assertEqual(sorted(result[1] for result in results),[200,409])
        self.assertEqual(Venda.objects.count(),1)
        self.assertEqual(OperacaoSincronizacao.objects.count(),1)

    def test_concurrent_edits_reject_stale_version(self):
        result=process_operation(self.command,AnonymousUser(),local_owner=self.context['owner'])
        data=copy.deepcopy(self.command['payload']['dados']);data['venda_id']=str(result[0]['record_id'])
        data['itens'][0]['item_id']=str(ItemVenda.objects.get().pk)
        version=revision_for(data)
        first=self.build(data,version);first['payload']['dados']['itens'][0]['quantidade']='3'
        second=self.build(data,version);second['payload']['dados']['itens'][0]['quantidade']='4'
        results=self.concurrent([first,second])
        self.assertEqual(sorted(result[1] for result in results),[200,409])
        self.assertEqual(Venda.objects.count(),1)
        self.assertEqual(ItemVenda.objects.count(),1)


class LocalSaleMigrationCompatibilityTests(TransactionTestCase):
    def test_additive_migration_preserves_existing_command_and_receipt(self):
        from django.db.migrations.executor import MigrationExecutor
        old_target=[('offline','0003_encerramentovendaoffline')]
        new_target=[('offline','0005_validate_local_sale_principal')]
        executor=MigrationExecutor(connection)
        try:
            executor.migrate(old_target)
            apps=executor.loader.project_state(old_target).apps
            User=apps.get_model('auth','User')
            user=User.objects.create(username='migration-fixture-only')
            Operation=apps.get_model('offline','OperacaoSincronizacao')
            identifier=uuid4();receipt={'operation_id':str(identifier),'status':'confirmada','record_id':17}
            original=Operation.objects.create(operation_id=identifier,device_id=uuid4(),actor_id=user.pk,
                environment_id='old-environment',type='criar_venda',schema_version=1,aggregate_id=str(identifier),
                payload={'fixture':'preserve'},payload_hash='a'*64,comando={'fixture':'immutable'},status='confirmada',resultado=receipt)
            before=Operation.objects.filter(pk=original.pk).values().get()
            executor=MigrationExecutor(connection);executor.migrate(new_target)
            after=OperacaoSincronizacao.objects.filter(pk=original.pk).values().get()
            self.assertEqual({key:after[key] for key in before},before)
            self.assertEqual(after['origin'],'authenticated');self.assertEqual(after['local_owner'],'')
        finally:
            MigrationExecutor(connection).migrate(new_target)


from django.test import SimpleTestCase


class LocalSaleLauncherTests(SimpleTestCase):
    def test_only_loopback_runserver_launch_enables_transport(self):
        import manage
        for args, render, enabled in [(['manage.py','runserver'],False,True),
                (['manage.py','runserver','127.0.0.1:8123','--noreload'],False,True),
                (['manage.py','runserver','0.0.0.0:8000'],False,False),
                (['manage.py','runserver'],True,False)]:
            with self.subTest(args=args,render=render),patch.dict(os.environ,{'RENDER':'true' if render else '', 'RENDER_SERVICE_ID':''}),patch('sys.argv',args),patch('django.core.management.execute_from_command_line'):
                manage.main()
                self.assertEqual(os.environ.get('_LOCAL_SALES_LOOPBACK')=='1',enabled)
