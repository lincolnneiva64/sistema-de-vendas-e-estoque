"""Employee bearer links: authenticated delegation, no employee login, real CSRF."""
import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from urllib.parse import urlparse, parse_qs
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from locacoes.models import AutorizacaoRecolhimento, ConferenciaRecolhimentoLocacao, EventoLocacao
from locacoes import tests as rental_tests
from locacoes.services import obter_ou_criar_tarefa_operacional
from .test_pickup_checklist import PickupFixture
from .browser_support import Chrome
from .tests_sale_sync_browser import CHROME


class LinkFixture(PickupFixture):
    def seed(self):
        super().seed()
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='locacoes',codename='autorizar_recolhimento'))

    def issue(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('locacoes:autorizar_recolhimento', args=[self.task.pk]),
            {'acao':'emitir', 'funcionario':self.employee.pk})
        self.assertEqual(response.status_code, 200)
        return urlparse(response.context['link']).path

    def employee_form(self, link, csrf=True):
        client = Client(enforce_csrf_checks=csrf)
        response = client.get(link)
        self.assertEqual(response.status_code, 200)
        data = {**self.payload, 'confirmacao_token':response.context['confirmacao_token'],
                'csrfmiddlewaretoken':client.cookies['csrftoken'].value}
        return client, data


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class PickupLinkTests(LinkFixture, TestCase):
    def setUp(self):
        self.seed()

    def test_employee_confirms_without_login_and_client_receipt_has_no_authority(self):
        link = self.issue()
        employee, data = self.employee_form(link)
        # A scoped receipt session is not an authenticated commercial session.
        self.assertNotIn('_auth_user_id', employee.session)
        response = employee.post(link, data)
        self.assertEqual(response.status_code, 302)
        receipt = employee.get(response.url)
        self.assertContains(receipt, 'Recolhimento registrado')
        self.assertNotIn('_auth_user_id', employee.session)
        self.assertEqual(employee.get('/vendas/').status_code, 302)
        self.assertEqual(Client().get(response.url).status_code, 302)
        self.assertNotContains(receipt, 'id="form-envio-checklist"')
        grant = AutorizacaoRecolhimento.objects.get()
        self.assertIsNotNone(grant.conferencia_id)
        self.assertNotIn(link, receipt.context['whatsapp_url'])
        self.assertNotIn('recolhimento-individual', receipt.context['whatsapp_url'])
        # A receipt may be read by a customer; POST cannot confer operational access.
        self.assertEqual(Client().post(response.url, {'checklist_funcionario':self.employee.pk}).status_code, 403)
        self.assertEqual(employee.post(self.url, data).status_code, 401)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
        audit = EventoLocacao.objects.get(tipo='recolhimento_usuario_confirmou')
        self.assertIn(f'autorizacao #{grant.pk}', audit.descricao)
        self.assertIn(f'emitida por usuario #{self.user.pk}', audit.descricao)
        self.assertEqual(grant.conferencia.responsavel, self.employee.nome)

    def test_get_never_issues_and_administrative_post_needs_login_permission_and_csrf(self):
        url = reverse('locacoes:autorizar_recolhimento', args=[self.task.pk])
        self.assertEqual(Client().post(url, {'acao':'emitir','funcionario':self.employee.pk}).status_code, 401)
        self.client.force_login(self.user)
        self.client.get(url)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())
        secure = Client(enforce_csrf_checks=True); secure.force_login(self.user)
        self.assertEqual(secure.post(url, {'acao':'emitir','funcionario':self.employee.pk}).status_code, 403)
        self.user.user_permissions.clear()
        self.assertEqual(self.client.post(url, {'acao':'emitir','funcionario':self.employee.pk}).status_code, 403)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())

    def test_expiry_after_open_is_checked_again_on_post(self):
        link = self.issue(); employee, data = self.employee_form(link)
        AutorizacaoRecolhimento.objects.update(expira_em=timezone.now()-timedelta(seconds=1))
        self.assertEqual(employee.post(link, data).status_code, 403)
        self.assertEqual(employee.get(link).status_code, 403)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_normal_operator_permissions_do_not_allow_delegation(self):
        self.user.user_permissions.remove(Permission.objects.get(content_type__app_label='locacoes',codename='autorizar_recolhimento'))
        self.client.force_login(self.user)
        response = self.client.post(reverse('locacoes:autorizar_recolhimento',args=[self.task.pk]),
                                    {'acao':'emitir','funcionario':self.employee.pk})
        self.assertEqual(response.status_code,403)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())

    def test_prepared_migration_matches_grant_model_without_running_migrations(self):
        from django.db.migrations.loader import MigrationLoader
        historical = MigrationLoader(None).project_state().apps.get_model('locacoes','AutorizacaoRecolhimento')
        self.assertEqual({f.name for f in historical._meta.fields},{f.name for f in AutorizacaoRecolhimento._meta.fields})
        self.assertEqual(historical._meta.permissions,AutorizacaoRecolhimento._meta.permissions)

    def test_https_required_outside_local_development(self):
        from django.test import override_settings
        self.client.force_login(self.user)
        with override_settings(DEBUG=False):
            response=self.client.post(reverse('locacoes:autorizar_recolhimento',args=[self.task.pk]),
                                      {'acao':'emitir','funcionario':self.employee.pk})
        self.assertEqual(response.status_code,400)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())

    def test_revocation_after_open_is_checked_again_on_post(self):
        link = self.issue(); employee, data = self.employee_form(link)
        self.client.post(reverse('locacoes:autorizar_recolhimento', args=[self.task.pk]), {'acao':'revogar'})
        self.assertEqual(employee.post(link, data).status_code, 403)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_invalid_token_and_wrong_employee_cannot_confirm(self):
        link = self.issue(); employee, data = self.employee_form(link)
        bad = link[:-2] + ('a' if link[-2] != 'a' else 'b') + '/'
        self.assertEqual(employee.get(bad).status_code, 403)
        self.assertEqual(employee.post(link, {**data,'funcionario':'999999'}).status_code, 403)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_csrf_required_and_no_login_on_security_error(self):
        link = self.issue(); employee, data = self.employee_form(link)
        data.pop('csrfmiddlewaretoken')
        response = employee.post(link, data)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Location', response.headers)
        self.assertNotContains(response, 'name="password"', status_code=403)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_single_use_including_partial_and_rotation_preserves_record(self):
        link = self.issue(); employee, data = self.employee_form(link)
        data.update(boa_cadeiras='2',justificativa_parcial='Restante no local',
                    previsao_conclusao=(timezone.localtime()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
        first = employee.post(link, data)
        second = employee.post(link, data)
        self.assertEqual(first.url, second.url)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
        newer = self.issue()
        self.assertNotEqual(newer, link)
        self.assertEqual(employee.post(link, data).status_code, 403)
        employee2, data2 = self.employee_form(newer)
        data2.update(boa_mesas='0',boa_cadeiras='2')
        self.assertEqual(employee2.post(newer,data2).status_code,302)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(),2)
        self.task.refresh_from_db(); self.assertEqual(self.task.status,'confirmada')

    def test_capability_is_not_persisted_plaintext_and_does_not_become_login(self):
        link = self.issue(); employee, data = self.employee_form(link)
        raw = link.rstrip('/').split('/')[-1]
        grant = AutorizacaoRecolhimento.objects.get()
        self.assertNotEqual(grant.token_hash, raw)
        self.assertEqual(len(grant.token_hash),64)
        self.assertFalse(employee.get('/api/offline/session/').json()['authenticated'])
        self.assertEqual(employee.post(reverse('locacoes:autorizar_recolhimento',args=[self.task.pk]),
                                       {'acao':'emitir','funcionario':self.employee.pk,
                                        'csrfmiddlewaretoken':data['csrfmiddlewaretoken']}).status_code,401)
        self.assertEqual(employee.get(link).headers['Referrer-Policy'],'same-origin')


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class PickupLinkMobileTests(LinkFixture, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def setUp(self):
        self.seed()
        helper = rental_tests.LocacoesChecklistOperacionalTests()
        helper.hoje = timezone.localdate()
        from locacoes.models import FaixaPrecoLocacao
        helper.faixa = FaixaPrecoLocacao.objects.get(codigo='centro_perto')
        self.rental = helper.colocar_na_rua(helper.criar_locacao(jogos=10,
            pessoa_avulsa_nome='Cliente fictício do link individual',
            data_entrega=helper.hoje-timedelta(days=1),data_prevista_devolucao=helper.hoje))
        self.task = obter_ou_criar_tarefa_operacional(self.rental,'recolhimento')

    def mobile(self, concurrent=False):
        link = self.issue()
        with TemporaryDirectory(prefix='pickup-individual-mobile-') as profile:
            chrome = Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab(); tab.call('Network.enable')
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                tab.call('Emulation.setTouchEmulationEnabled',{'enabled':True})
                tab.call('Network.setUserAgentOverride',{'userAgent':'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36','platform':'Android'})
                # Fresh employee browser: no administrative cookie, username or password.
                tab.call('Page.navigate',{'url':self.live_server_url+link})
                tab.wait('!!document.getElementById("botao-confirmar")')
                tab.wait('document.getElementById("botao-confirmar").dataset.confirmacaoPronta === "true"')
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertFalse(tab.evaluate('!!document.querySelector("input[name=password]")'))
                self.assertFalse(tab.evaluate('!!document.querySelector("#offline-global")'))
                tab.evaluate('document.getElementById("id_boa_mesas").value="10";document.getElementById("id_boa_cadeiras").value="40";true')
                if concurrent:
                    replies=tab.evaluate('''const f=document.getElementById('botao-confirmar').form;
                        const results=await Promise.all([1,2].map(()=>fetch(location.href,{method:'POST',credentials:'same-origin',body:new FormData(f)})));
                        results.map(r=>({status:r.status,url:r.url}))''')
                    self.assertEqual([r['status'] for r in replies],[200,200])
                    self.assertEqual(replies[0]['url'],replies[1]['url'])
                    tab.call('Page.navigate',{'url':replies[0]['url']})
                else:
                    tab.evaluate('const f=document.getElementById("botao-confirmar").form;f.requestSubmit();f.requestSubmit();true')
                tab.wait('document.title.startsWith("Checklist de recolhimento") && document.body.textContent.includes("Recolhimento registrado")')
                self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(),1)
                saved=ConferenciaRecolhimentoLocacao.objects.get()
                self.assertEqual((saved.boa_mesas,saved.boa_cadeiras),(10,40))
                self.assertEqual(EventoLocacao.objects.filter(tipo='recolhimento_usuario_confirmou').count(),1)
                href=tab.evaluate('Array.from(document.querySelectorAll("a")).find(a=>a.href.startsWith("https://wa.me/")).href')
                message=parse_qs(urlparse(href).query)['text'][0]
                self.assertNotIn('recolhimento-individual',message)
                self.assertNotIn(link,message)
                self.assertIn('Comprovante do recolhimento:',message)
                self.assertFalse(tab.evaluate('(await(await fetch("/api/offline/session/")).json()).authenticated'))
                self.task.refresh_from_db();self.assertEqual(self.task.status,'confirmada')
            finally:
                chrome.stop()

    def test_fresh_mobile_employee_ten_tables_forty_chairs_double_click_and_whatsapp(self):
        self.mobile()

    def test_fresh_mobile_simultaneous_posts_one_record(self):
        self.mobile(concurrent=True)

    def test_manager_native_form_issues_and_revokes_with_existing_login(self):
        manage = reverse('locacoes:autorizar_recolhimento',args=[self.task.pk])
        with TemporaryDirectory(prefix='pickup-manager-mobile-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                tab.call('Page.navigate',{'url':self.live_server_url+'/offline/login/?next='+manage})
                tab.wait('!!document.querySelector("input[name=password]")')
                tab.evaluate('document.querySelector("input[name=username]").value="pickup-test";document.querySelector("input[name=password]").value="test-only";document.querySelector("form button").click();true')
                tab.wait('!!document.querySelector("select[name=funcionario]")')
                tab.evaluate('document.querySelector("select[name=funcionario]").value='+json.dumps(str(self.employee.pk))+';document.querySelector("button[value=emitir]").click();true')
                tab.wait('!!document.getElementById("enviar-link-individual")')
                self.assertEqual(AutorizacaoRecolhimento.objects.count(),1)
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                tab.evaluate('document.querySelector("button[value=revogar]").click();true')
                tab.wait('!document.getElementById("enviar-link-individual") && document.body.textContent.includes("revogado")')
                self.assertIsNotNone(AutorizacaoRecolhimento.objects.get().revogada_em)
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
            finally:
                chrome.stop()
