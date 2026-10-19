"""Complementary recovery tests: real existing login, retained fields, explicit POST."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from django.contrib.sessions.models import Session
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.utils import timezone
from locacoes.models import ConferenciaRecolhimentoLocacao, EventoLocacao
from . import test_pickup_checklist as pickup
from .test_pickup_checklist import PickupFixture
from .browser_support import Chrome
from .tests_sale_sync_browser import CHROME


class PickupRecoveryTests(PickupFixture, TestCase):
    def setUp(self):
        self.seed()
        self.client.force_login(self.user)
        self.payload['confirmacao_token'] = self.client.get(self.url).context['confirmacao_token']

    def expire_and_save(self):
        Session.objects.filter(session_key=self.client.cookies['sessionid'].value).update(
            expire_date=timezone.now()-timedelta(seconds=1))
        self.payload['observacao'] = 'Campos da tentativa original'
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 401)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_existing_login_restores_draft_without_confirming(self):
        self.expire_and_save()
        self.assertTrue(self.client.login(username='pickup-test', password='test-only'))
        response = self.client.get(self.url+'?retomar=1')
        self.assertEqual(response.context['form']['observacao'].value(), self.payload['observacao'])
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 302)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)

    def test_recovery_does_not_grant_permissions(self):
        self.expire_and_save()
        reader = get_user_model().objects.create_user('recovery-reader', password='test-only')
        self.client.force_login(reader)
        self.client.get(self.url+'?retomar=1')
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 403)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_updated_history_prevents_replaying_saved_partial(self):
        self.expire_and_save()
        self.assertTrue(self.client.login(username='pickup-test', password='test-only'))
        self.client.post(self.url, self.payload)
        response = self.client.get(self.url+'?retomar=1')
        self.assertIn('conferencia=', response.url)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)


class PickupRecoveryBrowserTests(PickupFixture, StaticLiveServerTestCase):
    host = '127.0.0.1'

    def click(self, tab, selector):
        import json
        tab.evaluate('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"center",behavior:"instant"});true')
        return pickup.PickupChecklistBrowserTests.click(self, tab, selector)

    def setUp(self):
        self.seed()

    def recover(self, csrf=False):
        with TemporaryDirectory(prefix='pickup-recovery-mobile-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Emulation.setDeviceMetricsOverride', {'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                self.client.force_login(self.user)
                session = self.client.cookies['sessionid'].value
                tab.call('Network.setCookie', {'name':'sessionid','value':session,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                tab.wait('!!document.getElementById("id_funcionario")')
                tab.wait('document.getElementById("offline-global")?.dataset.connection === "online"', timeout=20)
                tab.wait('!document.getElementById("botao-confirmar").disabled', timeout=20)
                tab.evaluate('document.getElementById("id_boa_cadeiras").value="4";document.getElementById("id_observacao").value="Dados preservados no mobile";true')
                if csrf:
                    tab.evaluate('document.querySelector("input[name=csrfmiddlewaretoken]").value="invalid";true')
                else:
                    Session.objects.filter(session_key=session).update(expire_date=timezone.now()-timedelta(seconds=1))
                self.click(tab, '#botao-confirmar')
                tab.wait('document.body.textContent.includes("Recolhimento não confirmado nesta tentativa")')
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                self.click(tab, 'a[href*="/offline/login/"]')
                if not csrf:
                    tab.wait('!!document.querySelector("input[name=password]")')
                    tab.evaluate('document.querySelector("input[name=username]").value="pickup-test";document.querySelector("input[name=password]").value="test-only";true')
                    self.click(tab, 'form button')
                tab.wait('location.search.includes("retomar=1") && !!document.getElementById("id_funcionario")')
                self.assertEqual(tab.evaluate('document.getElementById("id_observacao").value'), 'Dados preservados no mobile')
                self.assertEqual(tab.evaluate('document.getElementById("id_boa_cadeiras").value'), '4')
                self.assertEqual(tab.evaluate('document.getElementById("id_funcionario").value'), str(self.employee.pk))
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                tab.wait('document.getElementById("offline-global")?.dataset.connection === "online"', timeout=20)
                tab.wait('!document.getElementById("botao-confirmar").disabled', timeout=20)
                self.click(tab, '#botao-confirmar')
                tab.wait('location.search.includes("conferencia=") && document.body.textContent.includes("Recolhimento registrado")')
                self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
                self.assertEqual(EventoLocacao.objects.get(tipo='recolhimento_usuario_confirmou').responsavel, self.user.username)
                self.assertTrue(tab.evaluate('document.querySelector(".sucesso a").href.startsWith("https://wa.me/")'))
            finally:
                chrome.stop()

    def test_mobile_expiry_existing_login_review_confirm_and_share(self):
        self.recover()

    def test_mobile_csrf_recovery_uses_current_session_without_password(self):
        self.recover(csrf=True)
