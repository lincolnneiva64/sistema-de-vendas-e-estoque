"""Customer receipts on the disposable cluster; no real WhatsApp requests."""
import base64
import hashlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from locacoes.models import ConferenciaRecolhimentoLocacao, EventoLocacao, MovimentoEstoqueLocacao
from estoque.models import MovimentoFinanceiro
from .test_pickup_checklist import PickupFixture
from .browser_support import Chrome
from .tests_sale_sync_browser import CHROME


class ReceiptFixture(PickupFixture):
    def complete(self, **changes):
        self.client.force_login(self.user)
        token = self.client.get(self.url).context['confirmacao_token']
        result = self.client.post(self.url, {**self.payload, 'confirmacao_token': token, **changes})
        self.assertEqual(result.status_code, 302)
        self.conference = ConferenciaRecolhimentoLocacao.objects.latest('pk')
        self.share_url = reverse('locacoes:compartilhar_comprovante_cliente', args=[self.conference.pk])
        return self.client.get(result.url)

    def issue(self):
        response = self.client.post(self.share_url)
        self.assertEqual(response.status_code, 200)
        whatsapp = response.json()['whatsapp_url']
        message = parse_qs(urlsplit(whatsapp).query)['text'][0]
        link = message.split('\n')[-1]
        return whatsapp, message, link, urlsplit(link).fragment

    def read(self, token, client=None, **extra):
        return (client or Client()).post(reverse('locacoes:comprovante_cliente'), {'token': token, **extra})


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class CustomerReceiptTests(ReceiptFixture, TestCase):
    def setUp(self):
        self.seed()

    def test_complete_share_read_only_and_preserves_operation(self):
        receipt = self.complete()
        self.assertContains(receipt, 'Enviar comprovante de recolhimento ao cliente')
        self.assertNotContains(receipt, 'Enviar checklist para')
        self.assertNotContains(receipt, 'Copiar checklist')
        before = (list(MovimentoEstoqueLocacao.objects.values()), list(MovimentoFinanceiro.objects.values()),
                  list(type(self.rental).objects.values()), list(ConferenciaRecolhimentoLocacao.objects.values()))
        whatsapp, message, link, token = self.issue()
        self.assertEqual(urlsplit(whatsapp).path, '/5591999990000')
        self.assertIn('Olá, Cliente Checklist!', message)
        self.assertNotIn('acesso_operacional', message)
        self.assertNotIn('recolhimento-individual', message)
        self.assertNotIn('conferencia-recolhimento', message)
        self.conference.refresh_from_db()
        self.assertEqual(self.conference.comprovante_token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(len(token), 43)
        self.assertAlmostEqual((self.conference.comprovante_expira_em - timezone.now()).total_seconds(), 30*86400, delta=5)
        public = self.read(token)
        self.assertEqual(public.status_code, 200)
        html = public.json()['html']
        for forbidden in ['acesso_operacional', 'compartilhar-cliente', 'cdn.jsdelivr', '<script', 'Voltar</a>', 'Lincoln Checklist</strong>']:
            self.assertNotIn(forbidden, html)
        self.assertIn('Horário de Belém', html)
        self.assertIn('Recolhimento completo', html)
        self.assertIn('is-checked', html)
        self.assertIn('sem avarias', html)
        self.assertIn('no-store', public['Cache-Control'])
        self.assertEqual(public['Referrer-Policy'], 'no-referrer')
        self.assertIn('noindex', public['X-Robots-Tag'])
        self.assertNotIn(token, '\n'.join(EventoLocacao.objects.values_list('descricao', flat=True)))
        after = (list(MovimentoEstoqueLocacao.objects.values()), list(MovimentoFinanceiro.objects.values()), list(type(self.rental).objects.values()))
        self.assertEqual(before[:3], after)
        saved = before[3][0]
        current = ConferenciaRecolhimentoLocacao.objects.values().get(pk=saved['id'])
        for key, value in saved.items():
            if not key.startswith('comprovante_'):
                self.assertEqual(current[key], value)
        self.assertEqual(Client().post(self.share_url).status_code, 403)
        self.assertEqual(Client().post(reverse('locacoes:checklist_recolhimento_cliente', args=[self.conference.pk]), {'token': token}).status_code, 403)
        self.assertNotEqual(Client().get(self.url, {'token': token}).status_code, 200)

    def test_partial_and_damage_are_not_green(self):
        page = self.complete(boa_cadeiras='2', quebrada_cadeiras='1', observacao='Uma cadeira quebrada',
            justificativa_parcial='Uma cadeira ainda no local',
            previsao_conclusao=(timezone.localtime()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
        token = self.issue()[3]
        html = self.read(token).json()['html']
        self.assertIn('Recolhimento parcial', html)
        self.assertIn('Quebradas', html)
        self.assertIn('Uma cadeira ainda no local', html)
        self.assertNotIn('sem avarias', html)
        self.assertEqual(page.context['recolhido_cadeiras'], 3)
        self.assertEqual(page.context['materiais_recolhidos'][-1]['bom'], False)

    def test_lost_and_discarded_not_reported_as_collected(self):
        self.complete()
        ConferenciaRecolhimentoLocacao.objects.filter(pk=self.conference.pk).update(boa_cadeiras=2, perdida_cadeiras=1, descartada_cadeiras=1)
        page = self.client.get(reverse('locacoes:checklist_recolhimento_cliente', args=[self.conference.pk]))
        self.assertEqual(page.context['recolhido_cadeiras'], 2)
        self.assertNotContains(page, 'sem avarias')

    def test_no_phone_disables_send_and_does_not_issue(self):
        self.rental.pessoa_avulsa_telefone = ''
        self.rental.save(update_fields=['pessoa_avulsa_telefone'])
        page = self.complete()
        self.assertContains(page, 'Cliente sem WhatsApp cadastrado')
        self.assertEqual(self.client.post(self.share_url).status_code, 400)
        self.conference.refresh_from_db()
        self.assertIsNone(self.conference.comprovante_token_hash)

    def test_expired_revoked_tampered_and_renewed(self):
        self.complete()
        token = self.issue()[3]
        self.assertEqual(self.read(token).status_code, 200)
        self.assertEqual(self.read(('A' if token[0] != 'A' else 'B') + token[1:]).status_code, 404)
        ConferenciaRecolhimentoLocacao.objects.filter(pk=self.conference.pk).update(comprovante_expira_em=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.read(token).status_code, 404)
        new_token = self.issue()[3]
        self.assertNotEqual(token, new_token)
        self.assertEqual(self.read(token).status_code, 404)
        self.assertEqual(self.read(new_token).status_code, 200)
        self.assertEqual(self.client.post(self.share_url, {'acao': 'revogar'}).status_code, 200)
        self.assertEqual(self.read(new_token).status_code, 404)
        self.assertEqual(self.read(self.issue()[3]).status_code, 200)

    def test_cannot_select_another_receipt_or_elevate_customer_capability(self):
        self.complete()
        token = self.issue()[3]
        other = self.helper.colocar_na_rua(self.helper.criar_locacao(pessoa_avulsa_nome='Outro cliente sigiloso'))
        second = ConferenciaRecolhimentoLocacao.objects.get(pk=self.conference.pk)
        second.pk = None
        second.locacao = other
        second.tarefa = None
        second.comprovante_token_hash = None
        second.save()
        other_share = self.client.post(reverse('locacoes:compartilhar_comprovante_cliente', args=[second.pk]))
        self.assertEqual(other_share.status_code, 200)
        other_message = parse_qs(urlsplit(other_share.json()['whatsapp_url']).query)['text'][0]
        other_token = urlsplit(other_message.split('\n')[-1]).fragment
        self.assertIn('Outro cliente sigiloso', self.read(other_token).json()['html'])
        html = self.read(token, pk=other.pk, locacao=other.pk, acao='confirmar').json()['html']
        self.assertNotIn('Outro cliente sigiloso', html)
        client = Client()
        self.read(token, client=client)
        self.assertEqual(client.post(self.share_url, {'token': token}).status_code, 403)
        reader = get_user_model().objects.create_user('receipt-reader')
        client.force_login(reader)
        self.assertEqual(client.post(self.share_url).status_code, 403)
        self.assertEqual(client.post(self.share_url, {'acao': 'revogar'}).status_code, 403)

    def test_audit_failure_rolls_back_rotation_and_preserves_previous_link(self):
        self.complete()
        token = self.issue()[3]
        with patch('locacoes.customer_receipts.EventoLocacao.objects.create', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.share_url)
        self.assertEqual(self.read(token).status_code, 200)

    def test_csrf_https_get_and_invalid_methods(self):
        self.complete()
        self.assertEqual(self.client.get(self.share_url).status_code, 405)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.user)
        self.assertEqual(guarded.post(self.share_url).status_code, 403)
        public = reverse('locacoes:comprovante_cliente')
        self.assertEqual(guarded.post(public, {'token': 'A'*43}).status_code, 403)
        shell = guarded.get(public)
        self.assertNotIn('Cliente Checklist', shell.content.decode())
        self.assertEqual(guarded.put(public, HTTP_X_CSRFTOKEN=guarded.cookies['csrftoken'].value).status_code, 405)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.post(self.share_url).status_code, 400)

    def test_existing_operational_permissions_are_unchanged(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/vendas/').status_code, 403)
        self.assertEqual(self.client.get(reverse('locacoes:detalhe', args=[self.rental.pk])).status_code, 403)

    def test_public_receipt_never_renders_internal_session_messages(self):
        from django.test import RequestFactory
        from locacoes.customer_receipts import public
        self.complete()
        token = self.issue()[3]
        request = RequestFactory().post(reverse('locacoes:comprovante_cliente'), {'token': token})
        request.user = self.user
        request.session = self.client.session
        request._messages = ['Aviso interno privado']
        response = public(request)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Aviso interno privado', response.content.decode())

    def test_delivery_presentation_and_existing_share_message(self):
        from locacoes.models import ConferenciaEntregaLocacao
        from django.contrib.auth.models import Permission
        from locacoes.services import obter_ou_criar_tarefa_operacional
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='locacoes',
            codename__in=['add_conferenciaentregalocacao', 'view_tarefaoperacionallocacao']))
        rental = self.helper.criar_locacao()
        task = obter_ou_criar_tarefa_operacional(rental, 'entrega')
        self.client.force_login(self.user)
        result = self.client.post(reverse('locacoes:conferencia_entrega', args=[task.pk]), self.helper.dados_conferencia_entrega())
        self.assertEqual(result.status_code, 302)
        conference = ConferenciaEntregaLocacao.objects.get(locacao=rental)
        operational = self.client.get(result.url)
        whatsapp = operational.context['whatsapp_checklist_cliente_url']
        self.assertIn('Cliente Checklist', parse_qs(urlsplit(whatsapp).query)['text'][0])
        page = self.client.get(reverse('locacoes:checklist_entrega_cliente', args=[conference.pk]))
        self.assertIn('Olá, Cliente Checklist!', page.content.decode())
        self.assertIn('is-checked', page.content.decode())
        self.assertIn('1 un', page.content.decode())
        self.assertIn('4 un', page.content.decode())

    def test_migration_matches_models(self):
        from django.db import connection
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.state import ProjectState
        loader = MigrationLoader(None)
        state = loader.project_state()
        self.assertFalse(MigrationAutodetector(state, ProjectState.from_apps(__import__('django.apps', fromlist=['apps']).apps)).changes(graph=loader.graph))


class CustomerReceiptMigrationTests(TransactionTestCase):
    def test_reversible_migration_in_disposable_database(self):
        from django.conf import settings
        from django.db import connection
        from django.db.migrations.loader import MigrationLoader
        self.assertEqual(settings.SETTINGS_MODULE, 'offline.test_settings')
        self.assertEqual(connection.settings_dict['NAME'], 'pickup_disposable')
        loader = MigrationLoader(None)
        before = loader.project_state([('locacoes', '0017_autorizacao_recolhimento')])
        after = loader.project_state([('locacoes', '0018_comprovante_cliente')])
        migration = loader.get_migration('locacoes', '0018_comprovante_cliente')
        with connection.schema_editor() as editor:
            for operation in reversed(migration.operations):
                operation.database_backwards('locacoes', editor, after, before)
        with connection.cursor() as cursor:
            columns = {column.name for column in connection.introspection.get_table_description(cursor, 'locacoes_conferenciarecolhimentolocacao')}
        self.assertNotIn('comprovante_token_hash', columns)
        with connection.schema_editor() as editor:
            for operation in migration.operations:
                operation.database_forwards('locacoes', editor, before, after)
        with connection.cursor() as cursor:
            columns = {column.name for column in connection.introspection.get_table_description(cursor, 'locacoes_conferenciarecolhimentolocacao')}
        self.assertTrue({'comprovante_token_hash', 'comprovante_expira_em', 'comprovante_revogado_em'} <= columns)


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class CustomerReceiptBrowserTests(ReceiptFixture, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def setUp(self):
        self.seed()

    def test_mobile_anonymous_receipt_and_send_popup_fallback(self):
        self.complete(boa_cadeiras='3', quebrada_cadeiras='1', observacao='Cadeira quebrada recolhida')
        token = self.issue()[3]
        with TemporaryDirectory(prefix='customer-receipt-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                tab.call('Page.navigate', {'url': self.live_server_url + reverse('locacoes:comprovante_cliente') + '#' + token})
                tab.wait('document.body.textContent.includes("Materiais efetivamente recolhidos")')
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertFalse(tab.evaluate('!!document.querySelector("form, a, button")'))
                self.assertFalse(tab.evaluate('document.body.textContent.includes("sem avarias")'))
                self.assertTrue(tab.evaluate('document.body.textContent.includes("Quebradas")'))
                self.assertEqual(tab.evaluate('document.referrer'), '')
                resources = tab.evaluate('performance.getEntriesByType("resource").map(entry => entry.name)')
                self.assertTrue(all(token not in resource and urlsplit(resource).hostname == '127.0.0.1' for resource in resources))
                evidence = Path('tmp_whatsapp_visual_comprovante')
                evidence.mkdir(exist_ok=True)
                for width, name in [(390, 'mobile'), (1366, 'desktop')]:
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width == 390})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    size = tab.call('Page.getLayoutMetrics')['cssContentSize']
                    shot = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True,
                        'clip': {'x': 0, 'y': 0, 'width': size['width'], 'height': size['height'], 'scale': 1}})
                    (evidence / (name + '.png')).write_bytes(base64.b64decode(shot['data']))
                from django.contrib.sessions.models import Session
                session = self.client.cookies['sessionid'].value
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': session, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + reverse('locacoes:checklist_recolhimento_cliente', args=[self.conference.pk])})
                tab.wait('!!document.getElementById("envio-comprovante-cliente")')
                tab.evaluate('window.open = () => null; document.querySelector("#envio-comprovante-cliente button").click(); true')
                tab.wait('!document.getElementById("envio-cliente-whatsapp").hidden')
                whatsapp = tab.evaluate('document.getElementById("envio-cliente-whatsapp").href')
                self.assertIn('Olá, Cliente Checklist!', parse_qs(urlsplit(whatsapp).query)['text'][0])
                self.assertNotIn('acesso_operacional', whatsapp)
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertEqual(self.read(token).status_code, 404)
                tab.evaluate('document.getElementById("revogar-comprovante-cliente").click(); true')
                tab.wait('document.getElementById("envio-cliente-status").textContent.includes("Link revogado")')
                message = parse_qs(urlsplit(whatsapp).query)['text'][0]
                self.assertEqual(self.read(urlsplit(message.split('\n')[-1]).fragment).status_code, 404)
            finally:
                chrome.stop()
