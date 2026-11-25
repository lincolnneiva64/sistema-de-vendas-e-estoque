"""Suspensão reversível; nenhuma conexão ou gravação de banco nestes testes."""
from types import SimpleNamespace
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.template.loader import get_template

from offline import views
from offline import services


@override_settings(OFFLINE_SUSPENDED=True, DEBUG=True, ALLOWED_HOSTS=['testserver'])
class SuspensionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(is_authenticated=True, pk=1, has_perm=lambda p: True,
                                    has_perms=lambda p: True, get_username=lambda: 'teste')

    def request(self, path, **headers):
        request = self.factory.post(path, data='{}', content_type='application/json', **headers)
        request.user = self.user
        return request

    def test_sync_e_bloqueado_antes_de_processar_qualquer_operacao(self):
        with patch('offline.views.process_operation') as process:
            response = views.synchronize(self.request('/api/offline/observations/'))
            self.assertEqual(response.status_code, 503)
            process.assert_not_called()

    def test_aba_antiga_nao_envia_pendencia_pelo_endpoint_online(self):
        with patch('offline.views.process_operation') as process:
            response = views.online_sale(self.request('/api/vendas/online/'))
            self.assertEqual(response.status_code, 503)
            process.assert_not_called()

    def test_envio_online_local_continua_usando_adaptador_existente(self):
        request = self.request('/api/vendas/online/', HTTP_X_ONLINE_ONLY='1', HTTP_X_LOCAL_SALES='1')
        with patch('offline.local_sales.submit', return_value=JsonResponse({'simulado': True})) as submit:
            self.assertEqual(views.online_sale(request).status_code, 200)
            submit.assert_called_once_with(request)

    def test_online_autenticado_mantem_validacao_e_processador_idempotente(self):
        command = {'type':'criar_venda', 'payload':{}}
        request = self.request('/api/vendas/online/', HTTP_X_ONLINE_ONLY='1')
        with patch('offline.views.validate_command', return_value=command) as validate, \
             patch('offline.views.process_operation', return_value=({'status':'conflito', 'erro':'teste'}, 409)) as process:
            self.assertEqual(views.online_sale(request).status_code, 409)
            validate.assert_called_once()
            process.assert_called_once_with(command, self.user)

    def test_shells_e_modulos_offline_nao_sao_ativados(self):
        request = self.factory.get('/offline/')
        for view in (views.shell, views.sales_shell, views.revision_shell):
            self.assertEqual(view(request).status_code, 503)
        self.assertEqual(views.versioned_asset(request, 'app.js').status_code, 503)
        self.assertEqual(views.versioned_asset(request, 'online-only.js').status_code, 200)

    def test_worker_recebe_politica_reversivel_sem_apagar_filas(self):
        request = self.factory.get('/service-worker.js')
        self.assertTrue(views.service_worker(request).content.startswith(b'self.OFFLINE_SUSPENDED = true;'))
        with override_settings(OFFLINE_SUSPENDED=False):
            self.assertTrue(views.service_worker(request).content.startswith(b'self.OFFLINE_SUSPENDED = false;'))
            self.assertEqual(views.versioned_asset(request, 'app.js').status_code, 200)

    def test_template_de_vendas_separa_fluxos_sem_carregar_fila(self):
        template = get_template('estoque/vendas_layout_teste.html')
        with self.settings(OFFLINE_SUSPENDED=True):
            html = template.render({'offline_suspended': True})
        self.assertIn('/offline/assets/online-only-v1/online-only.js', html)
        self.assertNotIn('src="/offline/assets/local-controlled-v1/app.js"', html)
        self.assertNotIn('src="/offline/assets/sale-cost-check-v1/local-sales.js"', html)
        self.assertNotIn('src="/offline/assets/sale-cost-check-v1/sales-draft-ui.js"', html)
        self.assertIn("['SC', 'CT'].includes(unidadeConversao)", html)
        html = template.render({'offline_suspended': False})
        self.assertIn('src="/offline/assets/local-controlled-v1/app.js"', html)

    @override_settings(OFFLINE_SUSPENDED=False)
    def test_desligar_chave_restaura_endpoint_existente(self):
        request = self.request('/api/offline/observations/')
        with patch('offline.views.validate_command', return_value={}), \
             patch('offline.views.process_operation', return_value=({'status':'conflito'}, 409)) as process:
            self.assertEqual(views.synchronize(request).status_code, 409)
            process.assert_called_once()


class OnlineIdempotencyWithoutDatabaseTests(SimpleTestCase):
    """Processador real com recibos em memória; não testa concorrência SQL.

    O wrapper atomic é omitido somente aqui para não abrir conexão. O código
    publicado conserva atomic, select_for_update e a restrição UNIQUE existentes.
    """

    def command(self, local=False):
        identifier = str(uuid4())
        data = {'schema_version':1, 'cliente_id':'41', 'data_venda':'2026-11-24',
                'operador':'Teste', 'tipo_pagamento':'A prazo',
                'itens':[{'produto_id':'544', 'unidade':'CT', 'quantidade':'0.5', 'preco_unitario':'72.00'}]}
        owner = 'a' * 64 if local else None
        payload = {'schema_version':1, 'dados':{k:v for k,v in data.items() if k != 'schema_version'}, 'expected_revision':''} if local else data
        return {'operation_id':identifier, 'device_id':str(uuid4()),
                'actor_id':'local:' + owner if local else '1', 'environment_id':'testserver',
                'type':'venda_local' if local else 'criar_venda', 'schema_version':1,
                'aggregate_id':identifier, 'payload':payload,
                'created_at':datetime.now(timezone.utc).isoformat(), 'sequence':1}, owner

    def test_hash_real_e_validacao_do_comando_online(self):
        user = SimpleNamespace(pk=1, is_authenticated=True)
        for local in (False, True):
            with self.subTest(local=local):
                command, owner = self.command(local)
                raw = {**deepcopy(command), 'payload_hash':services.command_hash(command)}
                self.assertEqual(services.validate_command(raw, user, 'testserver', local_owner=owner), command)
                data = raw['payload']['dados'] if local else raw['payload']
                data['itens'][0]['quantidade'] = '1'
                with self.assertRaises(ValidationError):
                    services.validate_command(raw, user, 'testserver', local_owner=owner)

    def check_repeated_command(self, status):
        user = SimpleNamespace(pk=1, is_authenticated=True)
        for local in (False, True):
            with self.subTest(local=local, status=status):
                command, owner = self.command(local)
                records = {}
                def get_or_create(*, operation_id, defaults):
                    if operation_id in records:
                        return records[operation_id], False
                    operation = SimpleNamespace(pk=1, actor_id=1, **deepcopy(defaults))
                    records[operation_id] = operation
                    return operation, True
                def execute(operation, current, actor, digest, **kwargs):
                    operation.status = status
                    operation.resultado = {'operation_id':current['operation_id'], 'hash':digest,
                                           'status':status, 'record_id':123 if status == 'confirmada' else None}
                    return operation.resultado, 200 if status == 'confirmada' else 409
                with patch.object(services.OperacaoSincronizacao.objects, 'get_or_create', side_effect=get_or_create), \
                     patch.object(services.OperacaoSincronizacao.objects, 'select_for_update') as locked, \
                     patch('offline.services._process_sale_creation', side_effect=execute) as executor:
                    locked.return_value.get.side_effect = lambda **kw: records[command['operation_id']]
                    process = services.process_operation.__wrapped__
                    first = process(command, user, local_owner=owner)
                    again = process(deepcopy(command), user, local_owner=owner)
                    self.assertEqual(first, again)
                    executor.assert_called_once()
                    self.assertEqual(len(records), 1)
                    changed = deepcopy(command)
                    data = changed['payload']['dados'] if local else changed['payload']
                    data['itens'][0]['quantidade'] = '1'
                    result, http = process(changed, user, local_owner=owner)
                    self.assertEqual((http, result['code']), (409, 'uuid_comando_divergente'))
                    executor.assert_called_once()

    def test_repeticao_de_confirmada_devolve_mesmo_recibo_sem_nova_venda(self):
        self.check_repeated_command('confirmada')

    def test_repeticao_de_conflito_nao_reexecuta_a_venda(self):
        self.check_repeated_command('conflito')
