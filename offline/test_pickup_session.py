"""Synthetic pickup session, stale form, and mobile navigation regressions."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from urllib.parse import urlparse, parse_qs

from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from locacoes.models import ConferenciaRecolhimentoLocacao, EventoLocacao
from locacoes import tests as rental_tests
from locacoes.services import obter_ou_criar_tarefa_operacional
from django.urls import reverse
from .test_pickup_checklist import PickupChecklistTests, PickupChecklistBrowserTests
from .browser_support import Chrome
from .tests_sale_sync_browser import CHROME


class PickupSessionTests(PickupChecklistTests):
    def test_partial_duplicate_form_records_only_once(self):
        payload = {**self.payload, 'boa_cadeiras': '2',
                   'justificativa_parcial': 'Restam duas cadeiras',
                   'previsao_conclusao': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')}
        first = self.client.post(self.url, payload)
        second = self.client.post(self.url, payload)
        self.assertEqual(first.url, second.url)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
        self.assertEqual(EventoLocacao.objects.filter(tipo='recolhimento_usuario_confirmou').count(), 1)

    def test_missing_or_tampered_mobile_token_does_not_write(self):
        for marker in ('', '0', '1'):
            for token in ('', 'invalid'):
                self.assertEqual(self.client.post(self.url, {**self.payload,
                    'interface_simplificada': marker, 'confirmacao_token': token}).status_code, 400)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_csrf_failure_explains_no_write_and_no_redirect(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url, self.payload)
        self.assertContains(response, 'Esta tentativa não registrou', status_code=403)
        self.assertNotIn('Location', response.headers)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())


class PickupMobileSessionTests(PickupChecklistBrowserTests):
    def mobile_scenario(self, expired=False, csrf=False, ten_sets=False, concurrent=False, partial=False):
        if ten_sets:
            helper = rental_tests.LocacoesChecklistOperacionalTests()
            helper.hoje = timezone.localdate()
            from locacoes.models import FaixaPrecoLocacao
            helper.faixa = FaixaPrecoLocacao.objects.get(codigo='centro_perto')
            self.rental = helper.colocar_na_rua(helper.criar_locacao(jogos=10,
                pessoa_avulsa_nome='Cliente fictício — teste 10/40',
                data_entrega=helper.hoje-timedelta(days=1), data_prevista_devolucao=helper.hoje))
            self.task = obter_ou_criar_tarefa_operacional(self.rental, 'recolhimento')
            self.url = reverse('locacoes:conferencia_recolhimento', args=[self.task.pk])
        with TemporaryDirectory(prefix='pickup-session-mobile-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setUserAgentOverride', {'userAgent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36', 'platform': 'Android'})
                tab.call('Emulation.setTouchEmulationEnabled', {'enabled': True})
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                self.client.force_login(self.user)
                session = self.client.cookies['sessionid'].value
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': session, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('!!document.getElementById("botao-confirmar")')
                tab.wait('document.getElementById("offline-global")?.dataset.connection === "online"', timeout=20)
                tab.wait('!document.getElementById("botao-confirmar").disabled', timeout=20)
                if expired:
                    Session.objects.filter(session_key=session).update(expire_date=timezone.now()-timedelta(seconds=1))
                if csrf:
                    tab.evaluate('document.querySelector("input[name=csrfmiddlewaretoken]").value="invalid"; true')
                if partial:
                    import json
                    prediction = (timezone.localtime()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                    tab.evaluate('document.getElementById("id_boa_mesas").value="5";'
                                 'document.getElementById("id_boa_cadeiras").value="20";'
                                 'document.getElementById("id_justificativa_parcial").value="Restante no local";'
                                 'document.getElementById("id_previsao_conclusao").value='+json.dumps(prediction)+'; true')
                if concurrent:
                    # Bypass the UI guard to exercise the PostgreSQL row lock with simultaneous POSTs.
                    result = tab.evaluate('''const f=document.getElementById('botao-confirmar').form;
                        const replies=await Promise.all([1,2].map(()=>fetch(location.href, {
                            method:'POST',credentials:'same-origin',body:new FormData(f)})));
                        replies.map(r=>({status:r.status,url:r.url}))''')
                    self.assertEqual([r['status'] for r in result], [200, 200])
                    self.assertEqual(result[0]['url'], result[1]['url'])
                    tab.call('Page.navigate', {'url': result[0]['url']})
                else:
                    # Two immediate submissions exercise the browser guard and native POST.
                    tab.evaluate('const f=document.getElementById("botao-confirmar").form; f.requestSubmit(); f.requestSubmit(); true')
                if expired or csrf:
                    tab.wait('document.body.textContent.includes("Recolhimento não confirmado nesta tentativa")')
                    self.assertEqual(tab.evaluate('location.pathname'), self.url)
                    self.assertFalse(tab.evaluate('!!document.querySelector("input[name=password]")'))
                    self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                    return
                tab.wait('location.search.includes("conferencia=") && document.body.textContent.includes("Recolhimento registrado")')
                self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
                if ten_sets:
                    saved = ConferenciaRecolhimentoLocacao.objects.get()
                    self.assertEqual((saved.boa_mesas, saved.boa_cadeiras), (5, 20) if partial else (10, 40))
                    self.task.refresh_from_db()
                    self.assertEqual(self.task.status, 'parcial' if partial else 'confirmada')
                self.assertEqual(EventoLocacao.objects.filter(tipo='recolhimento_usuario_confirmou').count(), 1)
                href = tab.evaluate('document.querySelector(".sucesso a").href')
                self.assertEqual(urlparse(href).hostname, 'wa.me')
                text = parse_qs(urlparse(href).query)['text'][0]
                self.assertIn('Comprovante do recolhimento:', text)
                receipt_url = text.split('Comprovante do recolhimento:\n')[1]
                before = EventoLocacao.objects.count()
                # Follow the actual communication target locally; never send a WhatsApp message.
                tab.call('Network.deleteCookies', {'name': 'sessionid', 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': receipt_url})
                tab.wait('document.title.startsWith("Checklist de recolhimento")')
                self.assertFalse(tab.evaluate('!!document.querySelector("input[name=password]")'))
                self.assertEqual(EventoLocacao.objects.count(), before)
                self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
            finally:
                chrome.stop()

    def test_mobile_double_submit_and_customer_receipt(self):
        self.mobile_scenario()

    def test_mobile_expired_session_stays_on_operation(self):
        self.mobile_scenario(expired=True)

    def test_mobile_csrf_failure_stays_on_operation(self):
        self.mobile_scenario(csrf=True)

    def test_mobile_ten_tables_forty_chairs_concurrent_posts(self):
        self.mobile_scenario(ten_sets=True, concurrent=True)

    def test_mobile_partial_concurrent_posts_do_not_collect_remaining_items(self):
        self.mobile_scenario(ten_sets=True, concurrent=True, partial=True)
