"""Real old/new processes sharing one disposable PostgreSQL test database."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless
from urllib.parse import quote
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction, IntegrityError, OperationalError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, EventoVenda
from .models import OperacaoSincronizacao
from . import tests_local_sales as local_tests


WORKER = r'''
import ast,contextlib,io,json,os,sys
from pathlib import Path
from unittest.mock import patch
import psycopg2
from psycopg2.extensions import parse_dsn
job=json.loads(sys.stdin.readline())
expected=parse_dsn(os.environ['MIXED_TEST_DSN'])
assert expected['host']=='127.0.0.1' and expected['dbname'].startswith('test_')
real_connect=psycopg2.connect
def guarded_connect(dsn=None,*args,**kwargs):
    params=parse_dsn(dsn or '');params.update(kwargs)
    if any(str(params.get(k))!=str(expected[k]) for k in ['host','port','user']):raise RuntimeError('External connection rejected')
    if params.get('dbname',params.get('database'))!=expected['dbname'] or params.get('service') or params.get('hostaddr'):raise RuntimeError('External database rejected')
    return real_connect(dsn,*args,**kwargs)
psycopg2.connect=guarded_connect
import django
django.setup()
from django.conf import settings
settings.LOCAL_SALES_LOOPBACK=True
from django.contrib.auth import get_user_model
from django.test import Client
from offline import views,services
from offline.services import command_hash
if job.get('unpatched_reader'):
    original=ast.parse(Path('.receipt-reader-original.py').read_text(encoding='utf-8'))
    function=next(n for n in original.body if isinstance(n,ast.FunctionDef) and n.name=='_operation_result')
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<unpatched-reader>','exec'),views.__dict__)
real_process=services.process_operation
def synchronized_process(*args,**kwargs):
    if job.get('barrier'):
        print('READY',file=sys.__stdout__,flush=True)
        if sys.stdin.readline().strip()!='GO':raise RuntimeError('Missing race barrier')
    return real_process(*args,**kwargs)
services.process_operation=views.process_operation=synchronized_process
browser=Client(enforce_csrf_checks=True,HTTP_HOST='127.0.0.1')
for key,value in job['cookies'].items():browser.cookies[key]=value
if job.get('authenticated',True):browser.force_login(get_user_model().objects.get(pk=job['user']))
try:
    with contextlib.redirect_stdout(io.StringIO()):
        def request():
            if job.get('action')=='lookup':
                return browser.get(job['url'],job['query'],**({'HTTP_X_LOCAL_SALES':'1'} if job.get('local') else {}))
            sale_command=job['command']
            return browser.post('/api/vendas/online/',json.dumps({**sale_command,'payload_hash':command_hash(sale_command)}),content_type='application/json',
                HTTP_X_CSRFTOKEN=browser.cookies['csrftoken'].value,**({'HTTP_X_LOCAL_SALES':'1'} if job.get('local') else {}))
        if job.get('fail_after_writes'):
            module='offline.local_sales' if job.get('local') else 'offline.sale_commands'
            imported=__import__(module,fromlist=['criar_ou_atualizar_venda'])
            real_sale=imported.criar_ou_atualizar_venda
            def fail(*args,**kwargs):
                real_sale(*args,**kwargs)
                raise RuntimeError('Disposable failure after commercial writes')
            with patch(module+'.criar_ou_atualizar_venda',side_effect=fail):response=request()
        else:response=request()
    if job.get('lose_response') and response.status_code==200:result={'lost_response':True}
    else:result={'status':response.status_code,'body':response.json()}
except Exception as error:
    result={'error':type(error).__name__}
print('RESULT '+json.dumps(result),flush=True)
'''


@skipUnless(connection.vendor=='postgresql','Requires disposable PostgreSQL')
@override_settings(LOCAL_SALES_LOOPBACK=True)
class MixedVersionPostgreSQLTests(TransactionTestCase):
    reset_sequences=True
    build=local_tests.LocalSaleTests.build
    effects=local_tests.LocalSaleTests.effects

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temporary=tempfile.TemporaryDirectory(prefix='mixed-sales-versions-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.project=Path(__file__).resolve().parent.parent
        cls.versions={'new':cls.project}
        for revision in ['c149564','4785a58']:
            target=Path(cls.temporary.name)/revision;target.mkdir()
            archive=subprocess.check_output(['git','-c','safe.directory='+cls.project.as_posix(),'archive','--format=zip',revision],cwd=cls.project)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                assert all((target/entry.filename).resolve().is_relative_to(target.resolve()) for entry in bundle.infolist())
                bundle.extractall(target)
            original=target/'offline/views.py'
            (target/'.receipt-reader-original.py').write_bytes(original.read_bytes())
            patch_file=cls.project/'compat/receipt-reader-v1.patch'
            subprocess.run(['git','apply','--check',str(patch_file)],cwd=target,check=True,capture_output=True)
            subprocess.run(['git','apply',str(patch_file)],cwd=target,check=True,capture_output=True)
            cls.versions[revision]=target

    def setUp(self):
        local_tests.LocalSaleTests.setUp(self)
        self.product.quantidade=Decimal('100');self.product.save()
        self.assertEqual(connection.settings_dict['HOST'],'127.0.0.1')
        self.assertTrue(connection.settings_dict['NAME'].startswith('test_'))

    def cash(self,command):
        command=copy.deepcopy(command)
        data=command['payload']['dados'] if command['type']=='venda_local' else command['payload']
        amount=Decimal(data['itens'][0]['quantidade'])*10
        data['tipo_pagamento']='A vista'
        data['origem_recebimento']={'caixa':str(amount*Decimal('.4')),'banco':str(amount*Decimal('.6'))}
        return command

    def fresh_authenticated(self):
        command=copy.deepcopy(self.old_command)
        command['operation_id']=command['aggregate_id']=str(uuid4())
        return command

    def start(self,version,job):
        from psycopg2.extensions import make_dsn
        db=connection.settings_dict
        env=os.environ.copy()
        url='postgresql://'+quote(db['USER'],safe='')+'@127.0.0.1:'+str(db['PORT'])+'/'+quote(db['NAME'],safe='')
        env.update(DATABASE_URL=url,OFFLINE_TEST_DATABASE_URL=url,DJANGO_SETTINGS_MODULE='offline.test_settings',SECRET_KEY=settings.SECRET_KEY,
                   MIXED_TEST_DSN=make_dsn(host='127.0.0.1',port=db['PORT'],dbname=db['NAME'],user=db['USER']),PYTHONDONTWRITEBYTECODE='1')
        job={'user':self.user.pk,'cookies':{k:c.value for k,c in self.client.cookies.items()},**job}
        process=subprocess.Popen([sys.executable,'-B','-c',WORKER],cwd=self.versions[version],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                 text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.addCleanup(self.stop,process)
        process.stdin.write(json.dumps(job)+'\n');process.stdin.flush()
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:process.kill();process.wait(timeout=10)

    def result(self,process):
        output,error=process.communicate(timeout=40)
        self.assertEqual(process.returncode,0,error[-3000:])
        lines=[line[7:] for line in output.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines),1,output+error[-3000:])
        return json.loads(lines[0])

    def send(self,version,command,**options):
        return self.result(self.start(version,{'command':command,'local':command['type']=='venda_local','authenticated':command['type']!='venda_local',**options}))

    def race(self,commands):
        processes=[self.start(version,{'command':command,'local':command['type']=='venda_local','authenticated':command['type']!='venda_local','barrier':True}) for version,command in commands]
        with ThreadPoolExecutor(max_workers=len(processes)) as pool:
            futures=[pool.submit(p.stdout.readline) for p in processes]
            for future in futures:self.assertEqual(future.result(timeout=30).strip(),'READY')
        for process in processes:process.stdin.write('GO\n');process.stdin.flush()
        return [self.result(process) for process in processes]

    def lookup(self,version,command,local=False,**options):
        from .services import command_hash
        query={'actor_id':str(self.user.pk),'environment_id':command['environment_id'],'type':command['type'],'device_id':command['device_id'],'hash':command_hash(command)}
        url=('/api/vendas/online/' if local else '/api/offline/operations/')+command['operation_id']+'/'
        return self.result(self.start(version,{'action':'lookup','url':url,'query':query,'local':local,'authenticated':True,**options}))

    def assert_cash_effects(self,count=1):
        self.assertEqual(Venda.objects.count(),count)
        self.assertEqual(ItemVenda.objects.count(),count)
        self.assertEqual(EventoVenda.objects.count(),count)
        self.assertEqual(MovimentoFinanceiro.objects.count(),count*2)
        self.assertEqual(ContaReceber.objects.count(),0)
        self.assertEqual(sum(MovimentoFinanceiro.objects.values_list('valor',flat=True)),sum(Venda.objects.values_list('total',flat=True)))

    def test_legacy_and_new_inserts_and_cross_version_replay(self):
        first=self.cash(self.old_command)
        receipt=self.send('c149564',first)['body']['receipt']
        for version in self.versions:self.assertEqual(self.send(version,first)['body']['receipt'],receipt)
        for version in ['4785a58','new']:
            self.assertEqual(self.send(version,self.cash(self.fresh_authenticated()))['status'],200)
        self.assert_cash_effects(3)
        self.assertEqual(OperacaoSincronizacao.objects.filter(origin='authenticated',local_owner='',declared_operator='',actor=self.user).count(),3)

    def test_three_versions_same_uuid_simultaneously(self):
        command=self.cash(self.old_command)
        results=self.race([(version,command) for version in self.versions])
        self.assertEqual([r['status'] for r in results],[200]*3)
        self.assertEqual([r['body']['receipt'] for r in results],[results[0]['body']['receipt']]*3)
        self.assert_cash_effects()
        self.assertEqual(OperacaoSincronizacao.objects.count(),1)

    def test_same_uuid_divergent_content_keeps_one_winner(self):
        commands=[]
        for version,quantity in zip(self.versions,['2','3','4']):
            command=copy.deepcopy(self.old_command);command['payload']['itens'][0]['quantidade']=quantity
            commands.append((version,self.cash(command)))
        results=self.race(commands)
        self.assertEqual(sorted(r['status'] for r in results),[200,409,409])
        self.assert_cash_effects();self.assertEqual(OperacaoSincronizacao.objects.count(),1)
        before=self.effects()
        for version,command in commands:self.send(version,command)
        self.assertEqual(self.effects(),before)

    def test_old_and_new_compete_for_last_stock(self):
        self.product.quantidade=2;self.product.save()
        results=self.race([('c149564',self.cash(self.old_command)),('4785a58',self.cash(self.fresh_authenticated())),('new',self.cash(self.build()))])
        self.assertEqual(sorted(r['status'] for r in results),[200,409,409])
        self.product.refresh_from_db();self.assertEqual(self.product.quantidade,0)
        self.assert_cash_effects();self.assertEqual(OperacaoSincronizacao.objects.count(),3)

    def test_loss_after_commit_replay_across_versions(self):
        command=self.cash(self.old_command)
        self.assertEqual(self.send('4785a58',command,lose_response=True),{'lost_response':True})
        before=self.effects()
        receipts=[self.send(version,command)['body']['receipt'] for version in self.versions]
        self.assertEqual(receipts,[receipts[0]]*3)
        self.assertEqual(self.effects(),before);self.assert_cash_effects()

    def test_full_rollback_then_retry_in_each_version(self):
        for version in self.versions:
            command=self.cash(self.build() if version=='new' else self.fresh_authenticated())
            before=self.effects();operations=list(OperacaoSincronizacao.objects.values())
            self.assertEqual(self.send(version,command,fail_after_writes=True),{'error':'RuntimeError'})
            self.assertEqual(self.effects(),before)
            self.assertEqual(list(OperacaoSincronizacao.objects.values()),operations)
            self.assertEqual(self.send(version,command)['status'],200)
        self.assert_cash_effects(3)

    def test_receipt_isolation_and_unpatched_reader_negative_control(self):
        self.client.force_login(self.user)
        self.context=self.client.get('/vendas/').context['local_sales_context']
        local_command=self.cash(self.build())
        self.assertEqual(self.send('new',local_command,authenticated=True)['status'],200)
        original=list(OperacaoSincronizacao.objects.values())
        for version in ['c149564','4785a58']:
            self.assertEqual(self.lookup(version,local_command,unpatched_reader=True)['status'],200)
            rejected=self.lookup(version,local_command)
            self.assertEqual(rejected['status'],409);self.assertNotIn('receipt',rejected['body'])
            self.assertEqual(self.send(version,local_command,local=False,authenticated=True)['status'],400)
        self.assertEqual(self.lookup('new',local_command)['status'],409)
        self.assertEqual(self.lookup('new',local_command,local=True)['status'],200)
        authenticated=self.cash(self.fresh_authenticated());self.send('c149564',authenticated)
        self.assertEqual(self.lookup('new',authenticated,local=True)['status'],409)
        for version in self.versions:self.assertEqual(self.lookup(version,authenticated)['status'],200)
        self.assertEqual(list(OperacaoSincronizacao.objects.filter(operation_id=local_command['operation_id']).values()),original)

    def test_bridge_before_schema_expansion_and_existing_record_preserved(self):
        old=[('offline','0003_encerramentovendaoffline')];latest=[('offline','0005_validate_local_sale_principal')]
        try:
            MigrationExecutor(connection).migrate(old)
            probe=self.build()
            for version in ['c149564','4785a58']:
                self.assertEqual(self.lookup(version,probe)['status'],409)
                self.assertEqual(self.send(version,self.cash(self.fresh_authenticated()))['status'],200)
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(OperacaoSincronizacao.objects.filter(origin='authenticated',actor=self.user,local_owner='').count(),2)
            self.assert_cash_effects(2)
        finally:MigrationExecutor(connection).migrate(latest)

    def test_expansion_defaults_and_constraint_before_validation(self):
        latest=[('offline','0005_validate_local_sale_principal')]
        try:
            MigrationExecutor(connection).migrate([('offline','0003_encerramentovendaoffline')])
            MigrationExecutor(connection).migrate([('offline','0004_local_sale_principal')])
            with connection.cursor() as cursor:
                cursor.execute("SELECT convalidated FROM pg_constraint WHERE conname='offline_operation_principal_valid'")
                self.assertIs(cursor.fetchone()[0],False)
                cursor.execute("SELECT column_default FROM information_schema.columns WHERE table_name='offline_operacaosincronizacao' AND column_name IN ('origin','local_owner','declared_operator')")
                self.assertEqual(len([r for r in cursor.fetchall() if r[0] is not None]),3)
            for version in self.versions:self.assertEqual(self.send(version,self.cash(self.fresh_authenticated()))['status'],200)
            self.assertEqual(self.send('new',self.cash(self.build()))['status'],200)
            original=OperacaoSincronizacao.objects.filter(origin='authenticated').first()
            for values in [{'actor_id':None},{'type':'venda_local'},{'origin':'local','type':'venda_local','local_owner':'bad','declared_operator':'x'},
                           {'origin':'local','type':'venda_local','local_owner':'a'*64,'declared_operator':''}]:
                with self.assertRaises(IntegrityError),transaction.atomic():
                    OperacaoSincronizacao.objects.filter(pk=original.pk).update(**values)
            MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                cursor.execute("SELECT convalidated FROM pg_constraint WHERE conname='offline_operation_principal_valid'")
                self.assertIs(cursor.fetchone()[0],True)
            for version in self.versions:self.assertEqual(self.send(version,self.cash(self.fresh_authenticated()))['status'],200)
            self.assert_cash_effects(7)
        finally:MigrationExecutor(connection).migrate(latest)

    def test_busy_expansion_aborts_without_breaking_old_writer(self):
        latest=[('offline','0005_validate_local_sale_principal')]
        locker=connection.copy(alias='mixed-lock-holder')
        try:
            MigrationExecutor(connection).migrate([('offline','0003_encerramentovendaoffline')])
            with locker.cursor() as cursor:
                cursor.execute('BEGIN')
                cursor.execute('LOCK TABLE offline_operacaosincronizacao IN ACCESS SHARE MODE')
            with self.assertRaises(OperationalError) as failure:
                MigrationExecutor(connection).migrate([('offline','0004_local_sale_principal')])
            self.assertEqual(failure.exception.__cause__.pgcode,'55P03')
            # The failed additive transaction must leave the old writer usable.
            self.assertEqual(self.send('c149564',self.cash(self.old_command))['status'],200)
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM django_migrations WHERE app='offline' AND name='0004_local_sale_principal'")
                self.assertEqual(cursor.fetchone()[0],0)
        finally:
            locker.rollback();locker.close()
            MigrationExecutor(connection).migrate(latest)

    def test_old_open_connection_crosses_additive_migration(self):
        latest=[('offline','0005_validate_local_sale_principal')]
        try:
            MigrationExecutor(connection).migrate([('offline','0003_encerramentovendaoffline')])
            old_process=self.start('4785a58',{'command':self.cash(self.old_command),'barrier':True})
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(old_process.stdout.readline).result(timeout=30).strip(),'READY')
            # The old server and its Django connection remain alive during DDL.
            MigrationExecutor(connection).migrate([('offline','0004_local_sale_principal')])
            old_process.stdin.write('GO\n');old_process.stdin.flush()
            self.assertEqual(self.result(old_process)['status'],200)
            self.assertEqual(self.send('new',self.cash(self.build()))['status'],200)
            self.assert_cash_effects(2)
        finally:MigrationExecutor(connection).migrate(latest)
