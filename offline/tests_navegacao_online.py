"""Links operacionais válidos com sessão e consultas em memória, sem banco."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone
from locacoes import pickup_links


@override_settings(DEBUG=True, ALLOWED_HOSTS=['testserver'])
class LinksOperacionaisOnlineTests(SimpleTestCase):
    def conferir(self, conferencia=None):
        token = 'A' * 43
        request = RequestFactory().get('/locacoes/recolhimento-individual/' + token + '/?retomar=1')
        request.session = {}  # Sem SessionStore/Neon.
        task = SimpleNamespace(pk=123, tipo='recolhimento', pendente_operacional=True)
        grant = SimpleNamespace(pk=1, token_hash=pickup_links.token_hash(token), revogada_em=None,
            expira_em=timezone.now()+timedelta(hours=1), conferencia_id=conferencia,
            funcionario_id=5, funcionario=SimpleNamespace(ativo=True, pode_receber_checklist=True))
        # Exercita a validação real; apenas o wrapper atomic e a persistência
        # são omitidos para não criar sessão ou operação de funcionário real.
        view = pickup_links.individual
        while hasattr(view, '__wrapped__'):
            view = view.__wrapped__
        with patch.object(pickup_links.AutorizacaoRecolhimento.objects, 'filter') as found, \
             patch.object(pickup_links.TarefaOperacionalLocacao.objects, 'select_for_update') as tasks, \
             patch.object(pickup_links.AutorizacaoRecolhimento.objects, 'select_for_update') as grants, \
             patch('locacoes.views.conferencia_recolhimento', return_value=HttpResponse()) as normal:
            found.return_value.values.return_value.first.return_value = {'pk':1, 'tarefa_id':123}
            tasks.return_value.get.return_value = task
            grants.return_value.select_related.return_value.get.return_value = grant
            response = view(request, token)
            if conferencia is None:
                normal.assert_called_once_with(request, 123)
                self.assertIs(request.pickup_grant, grant)
            else:
                normal.assert_not_called()
            self.assertEqual(request.GET['retomar'], '1')
            self.assertEqual(response['Cache-Control'], 'no-store, private')
            self.assertEqual(response['Referrer-Policy'], 'same-origin')
            return response

    def test_link_valido_de_whatsapp_chega_a_conferencia_normal(self):
        self.assertEqual(self.conferir().status_code, 200)

    def test_link_concluido_mantem_redirecionamento_ao_comprovante(self):
        response = self.conferir(conferencia=99)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/99/checklist-recolhimento/', response['Location'])
