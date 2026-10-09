"""Read-only UUID lookup: identity fences and no official side effects."""
import copy
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.db import DatabaseError
from django.contrib.auth import get_user_model
from . import tests_sale_creation as creation
from .services import command_hash, process_operation
from .models import OperacaoSincronizacao


class OperationLookupTests(TestCase):
    effects = creation.SaleCreationTests.effects

    def setUp(self):
        self.user, self.customer, self.product, self.command = creation.fixtures()
        self.client.force_login(self.user)

    def lookup(self, **changes):
        params = {key: self.command[key] for key in ['actor_id', 'environment_id', 'type', 'device_id']}
        params['hash'] = command_hash(self.command)
        params.update(changes)
        return self.client.get('/api/offline/operations/' + self.command['operation_id'] + '/', params)

    def test_absent_then_confirmed_and_conflict_are_read_only(self):
        before = self.effects()
        self.assertEqual(self.lookup().json()['lookup'], 'nao_encontrada')
        self.assertEqual(self.effects(), before)
        self.assertFalse(OperacaoSincronizacao.objects.exists())
        receipt, _ = process_operation(self.command, self.user)
        before = self.effects()
        operations = list(OperacaoSincronizacao.objects.values())
        for _ in range(2):
            result = self.lookup()
            self.assertEqual(result.json()['receipt'], receipt)
            self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(self.effects(), before)
        self.assertEqual(list(OperacaoSincronizacao.objects.values()), operations)
        self.command = copy.deepcopy(self.command)
        self.command['operation_id'] = self.command['aggregate_id'] = str(uuid4())
        self.command['payload']['itens'][0]['quantidade'] = '999'
        receipt, status = process_operation(self.command, self.user)
        self.assertEqual(status, 409)
        before = self.effects()
        self.assertEqual(self.lookup().json()['receipt'], receipt)
        self.assertEqual(self.effects(), before)

    def test_incompatible_identity_hash_type_device_and_stored_command(self):
        process_operation(self.command, self.user)
        for change in [{'actor_id':'999'}, {'environment_id':'other'}, {'hash':'0'*64},
                       {'type':'observacao_operacional'}, {'device_id':str(uuid4())}]:
            response = self.lookup(**change)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['lookup'], 'incompativel')
            self.assertNotIn('receipt', response.json())
        operation = OperacaoSincronizacao.objects.get()
        operation.comando = {**operation.comando, 'sequence':999}
        operation.save(update_fields=['comando'])
        self.assertEqual(self.lookup().status_code, 409)

    def test_auth_and_technical_failure(self):
        with patch('offline.views.OperacaoSincronizacao.objects.filter', side_effect=DatabaseError('Database unavailable')):
            response = self.lookup()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['lookup'], 'indeterminada')
        self.client.logout()
        self.assertEqual(self.lookup().status_code, 401)
        self.user.user_permissions.clear()
        self.client.force_login(self.user)
        self.assertEqual(self.lookup().status_code, 403)

    def test_versioned_graph_is_public_and_whitelisted(self):
        self.client.logout()
        response = self.client.get('/offline/assets/2-8f/app.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/offline/assets/2-8f/core.js', response.content)
        self.assertEqual(self.client.get('/offline/assets/2-8f/models.py').status_code, 404)

    def test_divergent_post_has_explicit_code_and_preserves_receipt(self):
        receipt, _ = process_operation(self.command, self.user)
        changed = copy.deepcopy(self.command)
        changed['sequence'] += 1
        result, status = process_operation(changed, self.user)
        self.assertEqual(status, 409)
        self.assertEqual(result['code'], 'uuid_comando_divergente')
        self.assertEqual(OperacaoSincronizacao.objects.get().resultado, receipt)

    def test_other_authenticated_actor_cannot_read_original_receipt(self):
        process_operation(self.command, self.user)
        other = get_user_model().objects.create_user(username='other-lookup', password='test')
        other.user_permissions.set(self.user.user_permissions.all())
        self.client.force_login(other)
        response = self.lookup(actor_id=str(other.pk))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('receipt', response.json())
        self.client.force_login(self.user)
        OperacaoSincronizacao.objects.update(environment_id='other-environment')
        self.assertEqual(self.lookup().status_code, 409)

    def test_inconsistent_persisted_receipt_is_indeterminate(self):
        receipt, _ = process_operation(self.command, self.user)
        before = self.effects()
        OperacaoSincronizacao.objects.update(resultado={**receipt, 'status':'conflito'})
        response = self.lookup()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['lookup'], 'indeterminada')
        self.assertEqual(self.effects(), before)
