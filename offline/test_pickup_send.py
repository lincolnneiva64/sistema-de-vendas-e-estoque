"""Operational sidebar -> secure employee delegation -> receipt, synthetic PostgreSQL only."""
import base64
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario, MovimentoFinanceiro
from locacoes.models import AutorizacaoRecolhimento, ConferenciaRecolhimentoLocacao, EventoLocacao
from locacoes.services import obter_ou_criar_tarefa_operacional, telefone_funcionario_checklist
from .test_pickup_links import LinkFixture
from .browser_support import Chrome
from .tests_sale_sync_browser import CHROME
from . import test_sales_sidebars_browser as sidebars


class SendFixture(LinkFixture):
    def setUp(self):
        self.seed()
        self.client.force_login(self.user)
        self.send_url = reverse('locacoes:enviar_recolhimento', args=[self.task.pk])

    def send(self, client=None, employee=None):
        return (client or self.client).post(self.send_url, {'funcionario':(employee or self.employee).pk})

    def link(self, response):
        self.assertEqual(response.status_code, 200)
        texto = parse_qs(urlparse(response.json()['whatsapp_url']).query)['text'][0]
        return urlparse(texto.splitlines()[-1]).path


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class SendTests(SendFixture, TestCase):
    def test_repeat_reuses_same_link_expiry_hash_and_single_audit(self):
        first = self.send()
        path = self.link(first)
        expiry = AutorizacaoRecolhimento.objects.get().expira_em
        second_client = Client(); second_client.force_login(self.user)
        second = self.send(second_client)
        self.assertEqual(first.json()['whatsapp_url'], second.json()['whatsapp_url'])
        self.assertTrue(second.json()['reutilizado'])
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
        grant = AutorizacaoRecolhimento.objects.get()
        self.assertEqual(grant.expira_em, expiry)
        self.assertEqual(EventoLocacao.objects.filter(tipo='recolhimento_link_emitido').count(), 1)
        token = path.rstrip('/').split('/')[-1]
        self.assertEqual(len(token), 43)
        self.assertNotEqual(token, grant.token_hash)
        self.assertNotIn(token, str(list(EventoLocacao.objects.values())))
        self.assertNotIn('recolhimento-individual', str(list(EventoLocacao.objects.values())))
        self.assertIn('no-store', second['Cache-Control'])
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_get_login_permission_csrf_and_https(self):
        self.assertEqual(self.client.get(self.send_url).status_code, 405)
        self.assertEqual(self.send(Client()).status_code, 401)
        self.user.user_permissions.remove(Permission.objects.get(content_type__app_label='locacoes', codename='autorizar_recolhimento'))
        self.assertEqual(self.send().status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='locacoes', codename='autorizar_recolhimento'))
        secure = Client(enforce_csrf_checks=True); secure.force_login(self.user)
        self.assertEqual(self.send(secure).status_code, 403)
        secure.get('/vendas/')
        token = secure.cookies['csrftoken'].value
        with override_settings(DEBUG=False):
            response = secure.post(self.send_url, {'funcionario':self.employee.pk}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())
        self.assertEqual(secure.post(self.send_url, {'funcionario':self.employee.pk}, HTTP_X_CSRFTOKEN=token).status_code, 200)

    def test_invalid_employee_phone_and_task_never_issue(self):
        self.assertEqual(self.client.post(self.send_url, {'funcionario':'invalido'}).status_code, 400)
        other = Funcionario.objects.create(nome='Não habilitado fictício', ativo=True, pode_receber_checklist=False)
        self.assertEqual(self.send(employee=other).status_code, 400)
        # Simulate an inconsistent legacy row; current model validation forbids it.
        Funcionario.objects.filter(pk=other.pk).update(pode_receber_checklist=True)
        self.assertEqual(self.send(employee=other).status_code, 400)
        delivery = obter_ou_criar_tarefa_operacional(self.rental, 'entrega')
        response = self.client.post(reverse('locacoes:enviar_recolhimento', args=[delivery.pk]), {'funcionario':self.employee.pk})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AutorizacaoRecolhimento.objects.exists())

    def test_expiry_revocation_and_employee_change_replace_without_touching_counts(self):
        old = self.link(self.send())
        AutorizacaoRecolhimento.objects.update(expira_em=timezone.now()-timedelta(seconds=1))
        current = self.link(self.send())
        self.assertNotEqual(old, current)
        self.assertEqual(Client().get(old).status_code, 403)
        self.assertEqual(self.link(self.send()), current)
        self.client.post(reverse('locacoes:autorizar_recolhimento', args=[self.task.pk]), {'acao':'revogar'})
        self.assertEqual(Client().get(current).status_code, 403)
        current = self.link(self.send())
        other = Funcionario.objects.create(nome='Outra funcionária fictícia', ativo=True, pode_receber_checklist=True,
            telefone_whatsapp='(91) 98888-1234')
        changed = self.send(employee=other)
        self.assertEqual(Client().get(current).status_code, 403)
        self.assertEqual(AutorizacaoRecolhimento.objects.filter(revogada_em__isnull=True).get().funcionario_id, other.pk)
        self.assertEqual(parse_qs(urlparse(changed.json()['whatsapp_url']).query)['phone'][0], telefone_funcionario_checklist(other))
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_legacy_administrative_link_replaced_once_and_manager_page_remains(self):
        from locacoes.pickup_links import token_hash
        token = secrets.token_urlsafe(32)
        AutorizacaoRecolhimento.objects.create(tarefa=self.task, funcionario=self.employee, emitida_por=self.user,
            token_hash=token_hash(token), expira_em=timezone.now()+timedelta(hours=24))
        old = reverse('locacoes:recolhimento_individual', args=[token])
        new = self.link(self.send())
        self.assertNotEqual(old, new)
        self.assertEqual(Client().get(old).status_code, 403)
        self.assertEqual(new, self.link(self.send()))
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 2)
        self.assertEqual(self.client.get(reverse('locacoes:autorizar_recolhimento', args=[self.task.pk])).status_code, 200)

    def test_new_administrative_link_is_reused_without_rotation(self):
        old = self.issue()
        self.assertEqual(old, self.link(self.send()))
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
        self.assertIsNone(AutorizacaoRecolhimento.objects.get().revogada_em)

    def test_partial_preserved_then_completed_and_send_rejected_after_completion(self):
        link = self.link(self.send())
        employee, data = self.employee_form(link)
        data.update(boa_cadeiras='2', justificativa_parcial='Duas cadeiras ficaram no local fictício',
            previsao_conclusao=(timezone.localtime()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
        self.assertEqual(employee.post(link, data).status_code, 302)
        partial = list(ConferenciaRecolhimentoLocacao.objects.values())
        new = self.link(self.send())
        self.assertNotEqual(new, link)
        self.assertEqual(partial, list(ConferenciaRecolhimentoLocacao.objects.values()))
        employee, data = self.employee_form(new)
        data.update(boa_mesas='0', boa_cadeiras='2')
        self.assertEqual(employee.post(new, data).status_code, 302)
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 2)
        self.task.refresh_from_db(); self.assertEqual(self.task.status, 'confirmada')

    def test_failed_audit_rolls_back_grant_and_revocation(self):
        old = self.issue()
        other = Funcionario.objects.create(nome='Funcionário fictício para reatribuir', ativo=True,
            pode_receber_checklist=True, telefone_whatsapp='(91) 98888-1234')
        with patch('locacoes.pickup_links.EventoLocacao.objects.create', side_effect=RuntimeError('Falha fictícia')):
            with self.assertRaises(RuntimeError): self.send(employee=other)
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
        self.assertIsNone(AutorizacaoRecolhimento.objects.get().revogada_em)
        self.assertEqual(Client().get(old).status_code, 200)

    def test_standard_secret_key_rotation_fallback_keeps_existing_link(self):
        from django.conf import settings
        original = self.link(self.send())
        key = settings.SECRET_KEY
        with override_settings(SECRET_KEY=secrets.token_urlsafe(50), SECRET_KEY_FALLBACKS=[key]):
            self.assertEqual(original, self.link(self.send()))
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)

    def test_application_and_access_logs_redact_bearer_paths(self):
        token = secrets.token_urlsafe(32)
        path = '/locacoes/recolhimento-individual/'+token+'/'
        for name in ('django.request', 'django.server', 'django.security.csrf', 'gunicorn.access', 'gunicorn.error'):
            with self.assertLogs(name, level='WARNING') as captured:
                logger = logging.getLogger(name)
                logger.warning('403 %s', path)
                logger.warning('URL %s', path.replace('/', '%2F'))
                try: raise ValueError(path)
                except ValueError: logger.exception('Erro fictício ao consultar acesso')
            self.assertNotIn(token, '\n'.join(captured.output))
            self.assertIn('[oculto]', '\n'.join(captured.output))
        link = self.issue()
        with self.assertLogs('django.security.csrf', level='WARNING') as captured:
            response = Client(enforce_csrf_checks=True).post(link, self.payload)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(link.rstrip('/').split('/')[-1], '\n'.join(captured.output))


@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class SendConcurrencyTests(SendFixture, TransactionTestCase):
    def test_simultaneous_clicks_different_sessions_issue_only_one_capability(self):
        barrier = Barrier(2)
        def execute():
            close_old_connections()
            try:
                client = Client(); client.force_login(self.user)
                barrier.wait(10)
                response = self.send(client)
                return response.status_code, response.json()['whatsapp_url']
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _:execute(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(first[0], 200)
        self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
        self.assertEqual(EventoLocacao.objects.filter(tipo='recolhimento_link_emitido').count(), 1)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
@override_settings(DEBUG=True, CHECKLIST_BASE_URL='', SISTEMA_ONLINE_URL='')
class SendSidebarBrowserTests(SendFixture, StaticLiveServerTestCase):
    host = '127.0.0.1'
    click = sidebars.SalesSidebarBrowserTests.click

    def setUp(self):
        super().setUp()
        helper = self.helper
        self.rental = helper.colocar_na_rua(helper.criar_locacao(jogos=10,
            pessoa_avulsa_nome='Cliente fictício do envio pelo painel',
            data_entrega=helper.hoje-timedelta(days=1), data_prevista_devolucao=helper.hoje))
        self.task = obter_ou_criar_tarefa_operacional(self.rental, 'recolhimento')
        self.send_url = reverse('locacoes:enviar_recolhimento', args=[self.task.pk])
        self.employee = Funcionario.objects.create(nome='Zeladora fictícia selecionada no painel', ativo=True,
            pode_receber_checklist=True, telefone_whatsapp='(91) 98888-1234')

    def open_panel(self, tab, width):
        tab.call('Network.enable')
        tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value, url=self.live_server_url))
        tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=900,deviceScaleFactor=1,mobile=width<768))
        tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
        tab.wait('window.salesDraftUI?.ready && document.readyState === "complete"')
        self.click(tab, '#btnLocacoesOperacionaisVenda')
        selector = f'[data-tarefa-id="{self.task.pk}"]'
        tab.evaluate('document.querySelector('+json.dumps(selector+' .funcionario-select')+').value='+json.dumps(str(self.employee.pk))+';document.querySelector('+json.dumps(selector+' .funcionario-select')+').dispatchEvent(new Event("change"));true')
        return selector

    def popup_stub(self, tab):
        # Never contact WhatsApp or send a real message.
        tab.evaluate('window.popupCount=0;window.open=()=>{popupCount++;return {closed:false,opener:null,close(){this.closed=true},location:{replace:url=>{window.whatsappPrepared=url}}}};true')

    def flow(self, width):
        with TemporaryDirectory(prefix='pickup-send-panel-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); selector = self.open_panel(tab, width)
                button = selector+' [data-enviar-recolhimento]'
                self.popup_stub(tab)
                finance = list(MovimentoFinanceiro.objects.values())
                tab.evaluate('window.assemblyBefore=JSON.stringify(window.salesDraftBridge.capture());window.sendFetch=fetch;window.sendPosts=0;window.fetch=async(...args)=>{if(String(args[0]).includes("/enviar-recolhimento/")&&args[1]?.method==="POST"){sendPosts++;const r=await sendFetch(...args);return new Promise(resolve=>{window.releaseSend=()=>resolve(r)})}return sendFetch(...args)};true')
                self.click(tab, button)
                tab.wait('!!window.releaseSend')
                self.click(tab, button)
                self.assertEqual(tab.evaluate('window.sendPosts'), 1)
                self.assertEqual(tab.evaluate('window.popupCount'), 1)
                self.assertEqual(tab.evaluate('document.querySelector('+json.dumps(button)+').textContent'), 'Preparando link...')
                tab.evaluate('window.releaseSend();window.fetch=window.sendFetch;true')
                tab.wait('!!window.whatsappPrepared')
                self.assertEqual(tab.evaluate('location.pathname'), '/vendas/')
                href = tab.evaluate('window.whatsappPrepared')
                self.click(tab, button)
                tab.wait('window.popupCount === 2 && !document.querySelector('+json.dumps(button)+').hasAttribute("aria-busy")')
                self.assertEqual(href, tab.evaluate('window.whatsappPrepared'))
                self.assertEqual(AutorizacaoRecolhimento.objects.filter(tarefa=self.task).count(), 1)
                self.assertEqual(AutorizacaoRecolhimento.objects.get(tarefa=self.task).funcionario_id, self.employee.pk)
                self.assertEqual(tab.evaluate('JSON.stringify(window.salesDraftBridge.capture()) === assemblyBefore'), True)
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                self.assertEqual(finance, list(MovimentoFinanceiro.objects.values()))
                query = parse_qs(urlparse(href).query)
                self.assertEqual(query['phone'][0], telefone_funcionario_checklist(self.employee))
                link = query['text'][0].splitlines()[-1]
                self.assertIn('/recolhimento-individual/', link)
                self.assertNotIn('/autorizar-recolhimento/', link)
                destino = Path('artifacts/recolhimento-envio'); destino.mkdir(parents=True, exist_ok=True)
                (destino/f'painel-{width}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png'})['data']))
                # A new browser profile has no administrative session at all.
                with TemporaryDirectory(prefix='pickup-send-employee-') as employee_profile:
                    employee_chrome = Chrome(CHROME, employee_profile).start()
                    try:
                        employee_tab = employee_chrome.tab()
                        employee_tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=900,deviceScaleFactor=1,mobile=width<768))
                        employee_tab.call('Page.navigate', {'url':link})
                        employee_tab.wait('document.getElementById("botao-confirmar")?.dataset.confirmacaoPronta === "true"')
                        self.assertFalse(employee_tab.evaluate('!!document.querySelector("input[name=password]")'))
                        employee_tab.evaluate('document.getElementById("id_boa_mesas").value="10";document.getElementById("id_boa_cadeiras").value="40";const f=document.getElementById("botao-confirmar").form;f.requestSubmit();f.requestSubmit();true')
                        employee_tab.wait('document.body.textContent.includes("Recolhimento registrado")')
                        saved = ConferenciaRecolhimentoLocacao.objects.get(tarefa=self.task)
                        self.assertEqual((saved.boa_mesas,saved.boa_cadeiras), (10,40))
                        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
                        receipt = employee_tab.evaluate('location.pathname')
                        customer_href = employee_tab.evaluate('[...document.querySelectorAll("a")].find(a=>a.href.startsWith("https://wa.me/")).href')
                        customer_text = parse_qs(urlparse(customer_href).query)['text'][0]
                        self.assertNotIn('recolhimento-individual', customer_text)
                        self.assertNotIn(link, customer_text)
                        self.assertEqual(Client().get(receipt).status_code, 200)
                        self.assertEqual(Client().post(receipt, {'checklist_funcionario':self.employee.pk}).status_code, 403)
                        self.assertFalse(employee_tab.evaluate('(await(await fetch("/api/offline/session/")).json()).authenticated'))
                    finally: employee_chrome.stop()
                self.task.refresh_from_db(); self.rental.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada')
                self.assertEqual(self.rental.status, 'devolvida')
                tab.call('Page.reload')
                tab.wait('window.salesDraftUI?.ready')
                self.assertFalse(tab.evaluate('!!document.querySelector('+json.dumps(selector)+')'))
            finally: chrome.stop()

    def test_real_sidebar_mobile_to_employee_confirmation_and_customer_receipt(self): self.flow(390)
    def test_real_sidebar_desktop_to_employee_confirmation_and_customer_receipt(self): self.flow(1366)

    def test_protected_sale_assembly_and_daily_panel_reuse_same_authorization(self):
        with TemporaryDirectory(prefix='pickup-send-protected-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); selector = self.open_panel(tab, 390)
                self.popup_stub(tab)
                tab.evaluate('window.saleBefore=JSON.stringify(salesDraftBridge.capture());salesOffline.active=true;true')
                self.click(tab, selector+' [data-enviar-recolhimento]')
                tab.wait('!!window.whatsappPrepared')
                first = tab.evaluate('window.whatsappPrepared')
                self.assertTrue(tab.evaluate('JSON.stringify(salesDraftBridge.capture()) === saleBefore'))
                tab.call('Page.navigate', {'url':self.live_server_url+reverse('locacoes:checklist_operacional')})
                tab.wait('document.readyState === "complete" && !!document.querySelector("[data-enviar-recolhimento]")')
                self.popup_stub(tab)
                button = '[data-enviar-recolhimento="'+self.send_url+'"]'
                tab.evaluate('const b=document.querySelector('+json.dumps(button)+'),s=b.closest("[data-envio-operacional]").querySelector(".funcionario-select");s.value='+json.dumps(str(self.employee.pk))+';s.dispatchEvent(new Event("change"));true')
                tab.evaluate('document.querySelector('+json.dumps(button)+').scrollIntoView({block:"center",behavior:"instant"});true')
                self.click(tab, button)
                tab.wait('!!window.whatsappPrepared')
                self.assertEqual(first, tab.evaluate('window.whatsappPrepared'))
                self.assertEqual(AutorizacaoRecolhimento.objects.filter(tarefa=self.task).count(), 1)
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
            finally: chrome.stop()

    def test_sidebar_failure_recovers_and_expired_session_never_opens_login(self):
        with TemporaryDirectory(prefix='pickup-send-failure-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); selector = self.open_panel(tab, 390)
                button = selector+' [data-enviar-recolhimento]'
                self.popup_stub(tab)
                tab.evaluate('window.sendFetch=fetch;window.fetch=(...args)=>String(args[0]).includes("/enviar-recolhimento/")?Promise.reject(new TypeError("Sem rede")):sendFetch(...args);true')
                self.click(tab, button)
                tab.wait('document.querySelector("[data-envio-recolhimento-feedback]")?.textContent.includes("Sem resposta")')
                self.assertFalse(tab.evaluate('document.querySelector('+json.dumps(button)+').hasAttribute("aria-busy")'))
                tab.evaluate('window.fetch=window.sendFetch;true')
                # Server commits the grant, then the response is lost. Retry must reuse it.
                tab.evaluate('window.fetch=async(...args)=>{const r=await sendFetch(...args);if(String(args[0]).includes("/enviar-recolhimento/"))throw new TypeError("Resposta perdida");return r};true')
                self.click(tab, button)
                tab.wait('document.querySelector("[data-envio-recolhimento-feedback]").textContent.includes("Sem resposta") && !document.querySelector('+json.dumps(button)+').hasAttribute("aria-busy")')
                self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
                tab.evaluate('window.fetch=window.sendFetch;window.open=()=>null;true')
                self.click(tab, button)
                tab.wait('!!document.querySelector("[data-envio-recolhimento-feedback] a")')
                self.assertTrue(tab.evaluate('document.querySelector("[data-envio-recolhimento-feedback] a").href.startsWith("https://web.whatsapp.com/send?")'))
                self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
                tab.evaluate('const s=document.querySelector('+json.dumps(selector+' .funcionario-select')+');s.value='+json.dumps(str(self.helper.entregador_checklist.pk))+';s.dispatchEvent(new Event("change"));true')
                self.assertFalse(tab.evaluate('!!document.querySelector("[data-envio-recolhimento-feedback] a")'))
                tab.evaluate('document.querySelector('+json.dumps(selector+' .funcionario-select')+').value='+json.dumps(str(self.employee.pk))+';true')
                from django.contrib.sessions.models import Session
                Session.objects.filter(session_key=self.client.cookies['sessionid'].value).update(expire_date=timezone.now()-timedelta(seconds=1))
                self.click(tab, button)
                tab.wait('document.querySelector("[data-envio-recolhimento-feedback]").textContent.includes("sessão expirou")')
                self.assertEqual(tab.evaluate('location.pathname'), '/vendas/')
                self.assertFalse(tab.evaluate('!!document.querySelector("input[name=password]")'))
                self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                # Reauthentication in another tab rotates cookies; the old DOM must not
                # force a stale CSRF token or lose the employee selection.
                self.client.force_login(self.user)
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                tab.call('Network.setCookie', dict(name='csrftoken', value=secrets.token_hex(16), url=self.live_server_url))
                self.click(tab, button)
                tab.wait('!!document.querySelector("[data-envio-recolhimento-feedback] a")')
                self.assertEqual(AutorizacaoRecolhimento.objects.count(), 1)
                self.assertEqual(tab.evaluate('document.querySelector('+json.dumps(selector+' .funcionario-select')+').value'), str(self.employee.pk))
            finally: chrome.stop()
