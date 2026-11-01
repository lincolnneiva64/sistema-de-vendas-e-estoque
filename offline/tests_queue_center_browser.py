import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from estoque.models import DespesaDiaria, ItemVenda, Produto, Venda
from .browser_support import Chrome
from .tests_sale_creation import fixtures as sale_fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
ENVIRONMENT = 'offline-isolated-tests'


@skipUnless(os.environ.get('OFFLINE_BROWSER_TESTS') != '0' and Path(CHROME).is_file(),
            'Chrome unavailable or OFFLINE_BROWSER_TESTS=0')
class OfflineQueueCenterBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.sale_command = sale_fixtures()
        self.actor_id = str(self.user.pk)
        self.device_id = str(uuid4())
        self.client = Client()
        self.client.force_login(self.user)
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='estoque',
            codename__in=['add_venda', 'view_cliente', 'view_produto']))

    def command(self, index, *, operation_type='criar_venda', status='resultado_desconhecido'):
        operation_id = str(uuid4())
        return {
            'command': {
                'operation_id': operation_id, 'device_id': self.device_id, 'actor_id': self.actor_id,
                'environment_id': ENVIRONMENT, 'type': operation_type, 'schema_version': 1,
                'aggregate_id': operation_id, 'sequence': index,
                'created_at': f'2026-10-31T18:5{index % 10}:00-03:00',
                'payload': {
                    'schema_version': 1, 'cliente_id': str(self.customer.pk),
                    'data_venda': '2026-10-31', 'tipo_pagamento': 'A prazo',
                    'itens': [{'produto_id': str(self.product.pk), 'quantidade': '2',
                               'unidade': 'UN', 'preco_unitario': '25'}],
                } if operation_type == 'criar_venda' else {'locacao_id': '1', 'tarefa_status': 'pendente',
                                                            'observacao': f'Observação {index}'},
            },
            'status': status,
            'attempts': 1,
        }

    def open_page(self, chrome):
        tab = chrome.tab()
        tab.call('Network.setCookie', {
            'name': 'sessionid', 'value': self.client.cookies['sessionid'].value,
            'url': self.live_server_url,
        })
        tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
        tab.wait("!!document.getElementById('offline-operations')")
        tab.evaluate("""window.queueCore=await import('/offline/assets/2-8f/core.js');
            window.queueRepo=new queueCore.Repository(await queueCore.openDB());true""")
        return tab

    def seed(self, tab, operations=(), completed=()):
        specs = json.dumps(list(operations) + list(completed))
        tab.evaluate(f"""(async()=>{{
            const scope={{actor_id:{json.dumps(self.actor_id)},environment_id:{json.dumps(ENVIRONMENT)}}};
            await queueRepo.put('snapshots',{{key:'pilot',actor:{{id:scope.actor_id,name:'Usuário de teste'}},
                environment_id:scope.environment_id}});
            await queueRepo.put('metadata',{{key:'device',id:{json.dumps(self.device_id)},sequence:100}});
            await queueRepo.put('metadata',{{key:'sales-identity',...scope}});
            for(const spec of {specs}){{
                const command=spec.command;
                const operation={{...command,payload_hash:await queueCore.hash(command),status:spec.status,
                    attempts:spec.attempts,last_error:'Resposta do envio não recebida',server_result:null,
                    original_labels:command.type==='criar_venda'?{{
                        cliente:{{id:command.payload.cliente_id,nome:{json.dumps(self.customer.nome)}}},
                        produtos:[{{id:command.payload.itens[0].produto_id,nome:{json.dumps(self.product.nome)}}}]
                    }}:undefined}};
                if(spec.status==='confirmada'){{
                    operation.server_result={{operation_id:operation.operation_id,hash:operation.payload_hash,
                        status:'confirmada',record_id:operation.sequence,completed_at:new Date().toISOString()}};
                    operation.last_error='';
                }}
                await queueRepo.put('operations',operation);
                if(spec.status==='confirmada')await queueRepo.put('history',{{...operation,synchronized_at:new Date().toISOString()}});
            }}
            return true;
        }})()""")
        tab.call('Page.reload')
        tab.wait("!!document.getElementById('offline-operations')")
        tab.evaluate("""window.queueCore=await import('/offline/assets/2-8f/core.js');
            window.queueRepo=new queueCore.Repository(await queueCore.openDB());true""")
        if operations:
            tab.wait(f"document.querySelectorAll('#offline-operations .offline-operation-card').length==={len(operations)}")

    def track_get_only(self, tab, mode='real'):
        tab.evaluate(f"""window.queueRealFetch=fetch;window.queueCalls=[];
            window.fetch=async(url,options={{}})=>{{
                const method=(options.method||'GET').toUpperCase();
                if(method==='POST'){{window.queueCalls.push({{method,url:String(url)}});throw Error('POST proibido no teste de consulta')}}
                if(String(url).includes('/api/offline/operations/')){{
                    window.queueCalls.push({{method,url:String(url)}});
                    if({json.dumps(mode)}!=='real'){{
                        const operation=await queueRepo.get('operations',new URL(url,location.href).pathname.split('/').filter(Boolean).pop());
                        const base={{operation_id:operation.operation_id,hash:operation.payload_hash}};
                        if({json.dumps(mode)}==='absent')return new Response(JSON.stringify({{...base,lookup:'nao_encontrada'}}),{{status:200}});
                        if({json.dumps(mode)}==='inconclusive')return new Response(JSON.stringify({{...base,lookup:'indeterminada'}}),{{status:503}});
                    }}
                }}
                return queueRealFetch(url,options);
            }};true""")

    def click_verify(self, tab, operation_id=None):
        selector = '[data-verify-operation]' if operation_id is None else '[data-verify-operation=' + json.dumps(operation_id) + ']'
        tab.evaluate(f"document.querySelector({json.dumps(selector)}).click();true")

    def test_empty_and_multiple_pending_cards_use_only_saved_sale_details(self):
        with TemporaryDirectory(prefix='offline-queue-empty-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_page(chrome)
                self.seed(tab)
                tab.wait("document.getElementById('offline-operations').textContent.includes('Nenhuma operação precisa de atenção')")
                self.assertFalse(tab.evaluate("document.getElementById('offline-completed-history').open"))
                operations = [self.command(index) for index in range(1, 4)]
                self.seed(tab, operations)
                tab.wait("document.querySelectorAll('#offline-operations .offline-operation-card').length===3")
                card = tab.evaluate("document.querySelector('#offline-operations .offline-operation-card').textContent")
                self.assertIn(self.customer.nome, card)
                self.assertIn(self.product.nome, card)
                self.assertIn('50,00', card)
                self.assertIn('2026-10-31', card)
                self.assertEqual(tab.evaluate("document.querySelectorAll('#offline-operations [data-verify-operation]').length"), 3)
            finally:
                chrome.stop()

    def test_committed_sale_is_reconciled_by_get_without_second_post(self):
        from .services import command_hash

        with TemporaryDirectory(prefix='offline-queue-reconcile-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                command = copy.deepcopy(self.sale_command)
                command['payload']['tipo_pagamento'] = 'consumo_proprio'
                response = self.client.post('/api/vendas/online/',
                    data=json.dumps({**command, 'payload_hash':command_hash(command)}),
                    content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(DespesaDiaria.objects.count(), 1)
                self.product.refresh_from_db()
                stock_after_commit = self.product.quantidade

                tab = self.open_page(chrome)
                local = {
                    'command':command, 'status':'resultado_desconhecido', 'attempts':1,
                }
                tab.evaluate(f"""(async()=>{{
                    const spec={json.dumps(local)};
                    await queueRepo.put('metadata',{{key:'device',id:spec.command.device_id,sequence:0}});
                    await queueRepo.put('snapshots',{{key:'pilot',actor:{{id:{json.dumps(self.actor_id)}}},
                        environment_id:{json.dumps(ENVIRONMENT)}}});
                    await queueRepo.put('operations',{{...spec.command,payload_hash:await queueCore.hash(spec.command),
                        status:spec.status,attempts:spec.attempts,last_error:'Resposta perdida',server_result:null,
                        original_labels:{{cliente:{{id:spec.command.payload.cliente_id,nome:{json.dumps(self.customer.nome)}}},
                            produtos:[{{id:spec.command.payload.itens[0].produto_id,nome:{json.dumps(self.product.nome)}}}]}}}});
                    return true;
                }})()""")
                tab.call('Page.reload')
                tab.wait("!!document.getElementById('offline-operations')")
                tab.evaluate("""window.queueCore=await import('/offline/assets/2-8f/core.js');
                    window.queueRepo=new queueCore.Repository(await queueCore.openDB());true""")
                tab.wait("document.querySelectorAll('#offline-operations .offline-operation-card').length===1")
                self.track_get_only(tab)
                self.click_verify(tab, command['operation_id'])
                tab.wait("(await queueRepo.get('operations'," + json.dumps(command['operation_id']) + ")).status==='confirmada'")
                self.assertEqual(tab.evaluate("queueCalls.filter(call=>call.method==='GET').length"), 1)
                self.assertEqual(tab.evaluate("queueCalls.filter(call=>call.method==='POST').length"), 0)
                query = tab.evaluate("""(()=>{const url=new URL(queueCalls[0].url,location.href);
                    return Object.fromEntries(['actor_id','environment_id','type','device_id','hash']
                        .map(key=>[key,url.searchParams.get(key)]))})()""")
                self.assertEqual(query, {
                    'actor_id':self.actor_id, 'environment_id':ENVIRONMENT, 'type':'criar_venda',
                    'device_id':command['device_id'], 'hash':command_hash(command),
                })
                tab.evaluate("document.getElementById('offline-completed-history').open=true;true")
                tab.wait("document.getElementById('offline-history-operations').textContent.includes('Confirmada após consulta no servidor')")
                self.assertEqual(Venda.objects.count(), 1)
                self.assertEqual(ItemVenda.objects.count(), 1)
                self.assertEqual(DespesaDiaria.objects.count(), 1)
                self.product.refresh_from_db()
                self.assertEqual(self.product.quantidade, stock_after_commit)
                saved = tab.evaluate("(async()=>await queueRepo.get('operations'," + json.dumps(command['operation_id']) + "))()")
                self.assertEqual(saved['resolution']['source'], 'server_check')
                self.assertEqual(saved['last_lookup']['outcome'], 'found')
            finally:
                chrome.stop()

    def test_inconclusive_and_absent_checks_preserve_the_same_operation(self):
        with TemporaryDirectory(prefix='offline-queue-unknown-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_page(chrome)
                spec = self.command(1)
                self.seed(tab, [spec])
                operation_id = spec['command']['operation_id']
                self.track_get_only(tab, 'inconclusive')
                self.click_verify(tab, operation_id)
                tab.wait("(await queueRepo.get('operations'," + json.dumps(operation_id) + ")).last_lookup?.outcome==='inconclusive'")
                self.assertEqual(tab.evaluate("(async()=> (await queueRepo.get('operations'," + json.dumps(operation_id) + ")).status)()"), 'resultado_desconhecido')
                self.assertIn('continua guardada', tab.evaluate("document.getElementById('offline-message').textContent"))
                tab.evaluate("""window.fetch=async(url,options={})=>{
                    if(String(url).includes('/api/offline/operations/')){queueCalls.push({method:'GET',url:String(url)});
                        const id=new URL(url,location.href).pathname.split('/').filter(Boolean).pop(),operation=await queueRepo.get('operations',id);
                        return new Response(JSON.stringify({operation_id:id,hash:operation.payload_hash,lookup:'nao_encontrada'}),{status:200})}
                    return queueRealFetch(url,options)};true""")
                self.click_verify(tab, operation_id)
                tab.wait("(await queueRepo.get('operations'," + json.dumps(operation_id) + ")).last_lookup?.outcome==='not_found'")
                const_operation = tab.evaluate("(async()=>await queueRepo.get('operations'," + json.dumps(operation_id) + "))()")
                self.assertEqual(const_operation['status'], 'resultado_desconhecido')
                self.assertEqual(const_operation['operation_id'], operation_id)
                self.assertEqual(tab.evaluate("queueCalls.filter(call=>call.method==='POST').length"), 0)
                self.assertIn('não encontrou', tab.evaluate("document.getElementById('offline-operations').textContent"))
            finally:
                chrome.stop()

    def test_reload_and_multiple_tabs_share_preserved_pending_operations(self):
        with TemporaryDirectory(prefix='offline-queue-tabs-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                first = self.open_page(chrome)
                specs = [self.command(index) for index in range(1, 3)]
                self.seed(first, specs)
                first.wait("document.querySelectorAll('#offline-operations .offline-operation-card').length===2")
                first.call('Page.reload')
                first.wait("document.querySelectorAll('#offline-operations .offline-operation-card').length===2")
                first.evaluate("""window.queueCore=await import('/offline/assets/2-8f/core.js');
                    window.queueRepo=new queueCore.Repository(await queueCore.openDB());true""")
                second = self.open_page(chrome)
                second.wait("document.querySelectorAll('#offline-operations .offline-operation-card').length===2")
                self.track_get_only(second, 'absent')
                self.click_verify(second, specs[0]['command']['operation_id'])
                second.wait("(await queueRepo.get('operations'," + json.dumps(specs[0]['command']['operation_id']) + ")).last_lookup?.outcome==='not_found'")
                first.wait("document.getElementById('offline-operations').textContent.includes('não encontrou a operação')")
                self.assertEqual(first.evaluate("(await queueRepo.get('operations'," + json.dumps(specs[0]['command']['operation_id']) + ")).status"), 'resultado_desconhecido')
                self.assertEqual(second.evaluate("(await queueRepo.all('operations')).filter(o=>o.status==='resultado_desconhecido').length"), 2)
            finally:
                chrome.stop()

    def test_long_history_is_collapsed_mobile_bounded_and_keeps_auto_resolution_label(self):
        with TemporaryDirectory(prefix='offline-queue-history-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_page(chrome)
                completed = [self.command(index, status='confirmada') for index in range(1, 81)]
                tab.evaluate(f"""(async()=>{{
                    const scope={{actor_id:{json.dumps(self.actor_id)},environment_id:{json.dumps(ENVIRONMENT)}}};
                    await queueRepo.put('snapshots',{{key:'pilot',actor:{{id:scope.actor_id}},environment_id:scope.environment_id}});
                    for(const spec of {json.dumps(completed)}){{
                        const command=spec.command,hash=await queueCore.hash(command);
                        const operation={{...command,payload_hash:hash,status:'confirmada',attempts:1,last_error:'',
                            server_result:{{operation_id:command.operation_id,hash,status:'confirmada',
                                record_id:command.sequence,completed_at:new Date().toISOString()}},
                            resolution:command.sequence===1?{{source:'automatic_reconciliation',resolved_at:new Date().toISOString()}}:undefined}};
                        await queueRepo.put('operations',operation);
                        await queueRepo.put('history',{{...operation,synchronized_at:new Date().toISOString()}});
                    }}
                    return true;
                }})()""")
                tab.call('Page.reload')
                tab.wait("!document.getElementById('offline-completed-history').open")
                tab.call('Emulation.setDeviceMetricsOverride', {'width':320,'height':844,'deviceScaleFactor':1,'mobile':True})
                self.assertFalse(tab.evaluate("document.getElementById('offline-completed-history').open"))
                self.assertTrue(tab.evaluate("document.getElementById('offline-history-operations').getBoundingClientRect().right<=innerWidth"))
                self.assertTrue(tab.evaluate("getComputedStyle(document.getElementById('offline-history-operations')).overflowY==='auto'"))
                tab.evaluate("document.getElementById('offline-completed-history').open=true;true")
                tab.wait("document.querySelectorAll('#offline-history-operations .offline-history-card').length===80")
                tab.wait("document.getElementById('offline-history-operations').textContent.includes('Resolvida automaticamente')")
                self.assertTrue(tab.evaluate("document.getElementById('offline-history-operations').getBoundingClientRect().height<=innerHeight"))
            finally:
                chrome.stop()
