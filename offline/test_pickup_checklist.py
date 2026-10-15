"""Physical pickup regressions using an isolated database and Chrome profile."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from estoque.models import MovimentoFinanceiro
from locacoes.models import ConferenciaRecolhimentoLocacao, EventoLocacao, MovimentoEstoqueLocacao, FaixaPrecoLocacao
from locacoes.services import obter_ou_criar_tarefa_operacional, painel_operacional_rapido_locacoes
from locacoes import tests as rental_tests
from .browser_support import Chrome
from . import test_sales_sidebars_browser as sidebars
from .tests_sale_sync_browser import CHROME


class PickupFixture:
    def seed(self):
        FaixaPrecoLocacao.objects.get_or_create(codigo='centro_perto', defaults={'nome': 'Teste', 'preco_jogo_diaria': '8.00'})
        helper = rental_tests.LocacoesChecklistOperacionalTests()
        helper.setUp()
        self.helper = helper
        self.rental = helper.colocar_na_rua(helper.criar_locacao(
            data_entrega=helper.hoje - timedelta(days=1),
            data_prevista_devolucao=helper.hoje,
        ))
        self.employee = helper.entregador_checklist
        self.task = obter_ou_criar_tarefa_operacional(self.rental, 'recolhimento')
        self.user = get_user_model().objects.create_user('pickup-test', password='test-only')
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='locacoes',
            codename__in=['add_conferenciarecolhimentolocacao', 'change_locacao', 'change_tarefaoperacionallocacao'],
        ))
        self.url = reverse('locacoes:conferencia_recolhimento', args=[self.task.pk])
        self.payload = helper.dados_conferencia_recolhimento(
            interface_simplificada='1', funcionario=str(self.employee.pk),
            pessoa_local_nome='', pessoa_local_relacao='', observacao='',
        )


class PickupChecklistTests(PickupFixture, TestCase):
    def setUp(self):
        self.seed()
        self.client.force_login(self.user)

    def test_complete_audit_receipt_duplicates_and_counters(self):
        finance = list(MovimentoFinanceiro.objects.values())
        stock = list(MovimentoEstoqueLocacao.objects.values())
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, 302)
        receipt = self.client.get(response.url)
        self.assertContains(receipt, 'Enviar checklist pelo WhatsApp')
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual(self.task.status, 'confirmada')
        self.assertEqual(self.rental.status, 'devolvida')
        conference = ConferenciaRecolhimentoLocacao.objects.get()
        self.assertEqual(conference.responsavel, self.employee.nome)
        self.assertEqual(conference.pessoa_local_nome, self.rental.nome_contratante)
        self.assertIsNotNone(self.task.confirmado_em)
        self.assertEqual(conference.pendente_cadeiras, 0)
        audit = EventoLocacao.objects.get(tipo='recolhimento_usuario_confirmou')
        self.assertIn(self.user.username, audit.descricao)
        self.assertEqual(painel_operacional_rapido_locacoes(receipt.wsgi_request)['total'], 0)
        self.client.post(self.url, self.payload)
        self.assertEqual(ConferenciaRecolhimentoLocacao.objects.count(), 1)
        self.assertEqual(list(MovimentoFinanceiro.objects.values()), finance)
        self.assertEqual(list(MovimentoEstoqueLocacao.objects.values()), stock)
        before = EventoLocacao.objects.count()
        self.client.get(response.url)
        self.assertEqual(EventoLocacao.objects.count(), before)

    def test_partial_keeps_missing_material_pending_then_finishes(self):
        prediction = (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(self.url, {**self.payload, 'boa_cadeiras': '2',
            'justificativa_parcial': 'Duas cadeiras ainda no local', 'previsao_conclusao': prediction})
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual(self.task.status, 'parcial')
        self.assertEqual(self.rental.status, 'pendente_devolucao')
        conference = ConferenciaRecolhimentoLocacao.objects.get()
        self.assertEqual(conference.pendente_cadeiras, 2)
        self.assertEqual(conference.perdida_cadeiras, 0)
        receipt = self.client.get(response.url)
        panel = painel_operacional_rapido_locacoes(receipt.wsgi_request)
        self.assertEqual(panel['total_recolhimentos'], 1)
        page = self.client.get(self.url)
        self.assertEqual(page.context['pendente_cadeiras'], 2)
        self.client.post(self.url, {**self.payload, 'boa_mesas': '0', 'boa_cadeiras': '2'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'confirmada')

    def test_damage_records_condition_and_existing_stock_rule(self):
        response = self.client.post(self.url, {**self.payload, 'boa_cadeiras': '3',
            'quebrada_cadeiras': '1', 'observacao': 'Cadeira quebrada recolhida'})
        self.assertEqual(response.status_code, 302)
        conference = ConferenciaRecolhimentoLocacao.objects.get()
        self.assertEqual(conference.quebrada_cadeiras, 1)
        self.assertEqual(conference.pendente_cadeiras, 0)
        self.assertEqual(MovimentoEstoqueLocacao.objects.get(locacao=self.rental).tipo, 'baixa_quebra')
        self.rental.refresh_from_db()
        self.assertEqual(self.rental.status, 'devolvida_com_avaria')

    def test_employee_and_returner_are_distinct(self):
        response = self.client.post(self.url, {**self.payload,
            'pessoa_local_nome': 'Maria Devolvente', 'pessoa_local_relacao': 'cliente'})
        self.assertEqual(response.status_code, 302)
        conference = ConferenciaRecolhimentoLocacao.objects.get()
        self.assertEqual(conference.pessoa_local_nome, 'Maria Devolvente')
        self.assertEqual(conference.responsavel, self.employee.nome)

    def test_receipt_can_be_forwarded_without_client_phone(self):
        self.rental.pessoa_avulsa_telefone = ''
        self.rental.save(update_fields=['pessoa_avulsa_telefone'])
        response = self.client.post(self.url, self.payload)
        receipt = self.client.get(response.url)
        self.assertContains(receipt, 'Enviar checklist pelo WhatsApp')
        self.assertIn('text=', receipt.context['whatsapp_url'])
        self.assertNotIn('phone=', receipt.context['whatsapp_url'])

    def test_security_and_conditional_validation(self):
        anonymous = Client()
        self.assertIn('/offline/login/', anonymous.post(self.url, self.payload).url)
        secured = Client(enforce_csrf_checks=True); secured.force_login(self.user)
        self.assertEqual(secured.post(self.url, self.payload).status_code, 403)
        for override in ({'funcionario': '999999'}, {'boa_cadeiras': '5'},
                         {'boa_cadeiras': '2'}, {'boa_cadeiras': '3', 'quebrada_cadeiras': '1'}):
            self.assertEqual(self.client.post(self.url, {**self.payload, **override}).status_code, 200)
            self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def legacy_payload(self):
        data = {**self.payload, 'responsavel': self.employee.nome,
                'pessoa_local_nome': self.rental.nome_contratante,
                'pessoa_local_relacao': 'cliente'}
        data.pop('interface_simplificada')
        return data

    def test_anonymous_legacy_and_changed_marker_with_valid_csrf_cannot_write(self):
        anonymous = Client(enforce_csrf_checks=True)
        anonymous.get(self.url)
        data = {**self.legacy_payload(), 'csrfmiddlewaretoken': anonymous.cookies['csrftoken'].value}
        before = list(EventoLocacao.objects.values())
        for marker in (None, '', '0', '1'):
            payload = dict(data)
            if marker is not None:
                payload['interface_simplificada'] = marker
            response = anonymous.post(self.url, payload)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/offline/login/', response.url)
            self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
            self.assertEqual(list(EventoLocacao.objects.values()), before)
        self.task.refresh_from_db(); self.rental.refresh_from_db()
        self.assertEqual((self.task.status, self.rental.status), ('pendente', 'entregue'))

    def test_offline_permission_alone_does_not_authorize_physical_pickup(self):
        reader = get_user_model().objects.create_user('offline-only', password='test-only')
        reader.user_permissions.add(Permission.objects.get(
            content_type__app_label='offline', codename='registrar_observacao'))
        client = Client(); client.force_login(reader)
        self.assertTrue(client.get('/api/offline/session/').json()['can_prepare'])
        self.assertEqual(client.get(f'/api/offline/snapshot/?task={self.task.pk}').status_code, 200)
        for data in (self.payload, self.legacy_payload()):
            response = client.post(self.url, data)
            self.assertEqual(response.status_code, 403)
            self.assertContains(response, 'Dados locais preservados', status_code=403)
            self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())

    def test_each_required_permission_is_enforced_on_both_payloads(self):
        for codename in ('add_conferenciarecolhimentolocacao', 'change_locacao', 'change_tarefaoperacionallocacao'):
            permission = Permission.objects.get(content_type__app_label='locacoes', codename=codename)
            self.user.user_permissions.remove(permission)
            for data in (self.payload, self.legacy_payload()):
                self.assertEqual(self.client.post(self.url, data).status_code, 403)
            self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
            self.user.user_permissions.add(permission)

    def test_authorized_legacy_preserves_employee_and_audits_authenticated_user(self):
        self.assertEqual(self.client.post(self.url, self.legacy_payload()).status_code, 302)
        conference = ConferenciaRecolhimentoLocacao.objects.get()
        self.assertEqual(conference.responsavel, self.employee.nome)
        event = EventoLocacao.objects.get(tipo='recolhimento_usuario_confirmou')
        self.assertEqual(event.responsavel, self.user.username)
        self.assertIn(str(conference.pk), event.descricao)

    def test_expired_session_cannot_confirm_legacy_pickup(self):
        from django.contrib.sessions.models import Session
        Session.objects.filter(session_key=self.client.cookies['sessionid'].value).update(
            expire_date=timezone.now() - timedelta(seconds=1))
        response = self.client.post(self.url, self.legacy_payload())
        self.assertIn('/offline/login/', response.url)
        self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class PickupChecklistBrowserTests(PickupFixture, StaticLiveServerTestCase):
    host = '127.0.0.1'
    click = sidebars.SalesSidebarBrowserTests.click

    def setUp(self):
        self.seed()

    def run_pickup(self, mode='complete'):
        with TemporaryDirectory(prefix='pickup-checklist-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/login/?next=' + self.url})
                tab.wait('!!document.querySelector("input[name=password]")')
                tab.evaluate('document.querySelector("input[name=username]").value="pickup-test"; document.querySelector("input[name=password]").value="test-only"; true')
                self.click(tab, 'form button')
                tab.wait('!!document.getElementById("id_funcionario")')
                tab.wait('document.getElementById("offline-global")?.dataset.connection === "online"', timeout=20)
                tab.wait('!document.getElementById("botao-confirmar").disabled && !document.querySelector("input[name=interface_simplificada]").disabled', timeout=20)
                self.assertEqual(tab.evaluate('document.getElementById("id_funcionario").value'), str(self.employee.pk))
                self.assertFalse(tab.evaluate('!!document.querySelector("[data-local-note]")'))
                self.assertTrue(tab.evaluate("""!!document.querySelector('a[href^="tel:"]')"""))
                self.assertTrue(tab.evaluate("""!!document.querySelector('a[href*="maps/search"]')"""))
                tab.evaluate('const {Repository,openDB}=await import("/offline/assets/2-8f/core.js"); window.pickupRepo=new Repository(await openDB()); await pickupRepo.put("metadata",{key:"pickup-saved-draft",text:"rascunho original"}); true')
                if mode == 'partial':
                    prediction = (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
                    tab.evaluate('document.getElementById("id_boa_cadeiras").value="2"; document.getElementById("id_boa_cadeiras").dispatchEvent(new Event("input")); true')
                    tab.evaluate('document.getElementById("id_justificativa_parcial").value="Duas cadeiras ficaram no local"; document.getElementById("id_previsao_conclusao").value=' + repr(prediction) + '; true')
                elif mode == 'damage':
                    tab.evaluate('document.getElementById("id_boa_cadeiras").value="3"; document.getElementById("id_quebrada_cadeiras").value="1"; document.getElementById("id_observacao").value="Cadeira quebrada recolhida"; document.getElementById("id_boa_cadeiras").dispatchEvent(new Event("input")); true')
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                tab.evaluate('document.getElementById("botao-confirmar").scrollIntoView({block:"center",behavior:"instant"}); true')
                self.click(tab, '#botao-confirmar')
                tab.wait('location.search.includes("conferencia=") && document.body.textContent.includes("Recolhimento registrado")')
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'parcial' if mode == 'partial' else 'confirmada')
                self.assertTrue(tab.evaluate('document.body.textContent.includes("Enviar checklist pelo WhatsApp")'))
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('!!window.salesDraftUI?.ready')
                self.assertEqual(tab.evaluate('document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.trim()'), 'Recolhimentos ' + ('1' if mode == 'partial' else '0'))
                self.assertEqual(tab.evaluate('const {Repository,openDB}=await import("/offline/assets/2-8f/core.js"); await new Repository(await openDB()).get("metadata","pickup-saved-draft")'), {'key': 'pickup-saved-draft', 'text': 'rascunho original'})
            finally:
                chrome.stop()

    def test_real_login_click_receipt_and_no_local_note(self):
        self.run_pickup()

    def test_partial_click_keeps_counter_and_remaining_material(self):
        self.run_pickup('partial')

    def test_damage_click_records_condition(self):
        self.run_pickup('damage')

    def test_advanced_pickup_in_details_keeps_transport_and_refreshes_parent(self):
        with TemporaryDirectory(prefix='pickup-advanced-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width':1280, 'height':1000, 'deviceScaleFactor':1, 'mobile':False})
                detail = reverse('locacoes:detalhe', args=[self.rental.pk])
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/login/?next=' + detail})
                tab.wait('!!document.querySelector("input[name=password]")')
                tab.evaluate('document.querySelector("input[name=username]").value="pickup-test";document.querySelector("input[name=password]").value="test-only";true')
                self.click(tab, 'form button')
                selector = '[data-checklist-local*="conferencia-recolhimento"]'
                tab.wait(f'!!document.querySelector({selector!r})')
                tab.evaluate(f'document.querySelector({selector!r}).scrollIntoView({{block:"center",behavior:"instant"}});true')
                tab.wait(f'(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight;}})()')
                self.click(tab, selector)
                tab.wait('!!document.querySelector("[data-checklist-local-frame]").contentDocument?.getElementById("id_funcionario_conferente")')
                tab.evaluate('window.frame=document.querySelector("[data-checklist-local-frame]");window.formDocument=frame.contentDocument;true')
                tab.wait('formDocument.getElementById("offline-global")?.dataset.connection === "online"', timeout=20)
                tab.wait('!formDocument.getElementById("botao-confirmar").disabled', timeout=20)
                self.assertFalse(tab.evaluate('!!formDocument.querySelector("[name=interface_simplificada]")'))
                employee = str(self.employee.pk)
                tab.evaluate('formDocument.getElementById("id_modalidade").value="recolhimento_equipe";formDocument.getElementById("id_funcionario_conferente").value=' + repr(employee) + ';formDocument.getElementById("id_transportador").value=' + repr(employee) + ';formDocument.getElementById("id_data_efetiva").value=' + repr(timezone.localdate().isoformat()) + ';true')
                previous = tab.evaluate('performance.timeOrigin')
                tab.evaluate('formDocument.getElementById("botao-confirmar").scrollIntoView({block:"center",behavior:"instant"});frame.scrollIntoView({block:"center",behavior:"instant"});true')
                point = tab.evaluate('(()=>{const a=frame.getBoundingClientRect(),b=formDocument.getElementById("botao-confirmar").getBoundingClientRect();return {x:a.x+frame.clientLeft+b.x+b.width/2,y:a.y+frame.clientTop+b.y+b.height/2};})()')
                self.assertTrue(tab.evaluate(f'document.elementFromPoint({point["x"]},{point["y"]}) === frame'), str(point))
                for kind in ('mousePressed', 'mouseReleased'):
                    tab.call('Input.dispatchMouseEvent', {'type':kind, 'x':point['x'], 'y':point['y'], 'button':'left', 'clickCount':1})
                try:
                    tab.wait(f'performance.timeOrigin !== {previous} && !!document.querySelector("[data-checklist-local-frame]")')
                except AssertionError:
                    self.fail(str(tab.evaluate('({point:' + repr(point).replace("'", '"') + ',frameText:frame.contentDocument.body.innerText,disabled:frame.contentDocument.getElementById("botao-confirmar")?.disabled})')))

                self.task.refresh_from_db()
                self.assertEqual(self.task.status, 'confirmada')
                conference = ConferenciaRecolhimentoLocacao.objects.get()
                self.assertIn('Transportou: ' + self.employee.nome, conference.observacao)
                self.assertEqual(conference.responsavel, self.employee.nome)
                self.assertFalse(tab.evaluate(f'!!document.querySelector({selector!r})'))
            finally:
                chrome.stop()

    def test_administrative_pickup_already_done_without_extra_fields(self):
        before = type(self.rental).objects.filter(pk=self.rental.pk).values().get()
        with TemporaryDirectory(prefix="pickup-admin-chrome-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Page.navigate", {"url": self.live_server_url + "/offline/login/?next=/vendas/"})
                tab.wait('!!document.querySelector("input[name=password]")')
                tab.evaluate('document.querySelector("input[name=username]").value="pickup-test";document.querySelector("input[name=password]").value="test-only";true')
                self.click(tab, "form button")
                tab.wait("!!window.salesDraftUI?.ready")
                self.click(tab, "#btnLocacoesOperacionaisVenda")
                self.click(tab, ".locacoes-rapidas-resolver")
                self.assertEqual(tab.evaluate('document.querySelector("[data-resolver-form] option[value=recolhimento_realizado]").textContent'), "Recolhimento j\u00e1 realizado")
                tab.evaluate('document.querySelector("[data-resolver-form] [name=resultado]").value="recolhimento_realizado";document.querySelector("[data-resolver-form] [name=resultado]").dispatchEvent(new Event("change"));localStorage.setItem("pickup-admin-preserve","rascunho");true')
                self.assertTrue(tab.evaluate('Array.from(document.querySelectorAll("[data-resolver-form] [name=motivo], [data-resolver-form] [name=data_efetiva], [data-resolver-form] [name=responsavel]")).every(el=>el.disabled && !el.required)'))
                previous = tab.evaluate("performance.timeOrigin")
                self.click(tab, "[data-resolver-form] button[type=submit]")
                tab.wait(f"performance.timeOrigin !== {previous} && !!window.salesDraftUI?.ready")
                self.assertEqual(tab.evaluate('document.querySelector("[data-locacoes-filtro=recolhimento]").textContent.trim()'), "Recolhimentos 0")
                self.assertEqual(tab.evaluate('localStorage.getItem("pickup-admin-preserve")'), "rascunho")
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, "resolvida_admin")
                self.assertEqual(self.task.resolvida_por, "pickup-test")
                self.assertEqual(before, type(self.rental).objects.filter(pk=self.rental.pk).values().get())
                self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
            finally:
                chrome.stop()

    def test_panel_deadlines_mobile_and_desktop(self):
        self.client.force_login(self.user)
        for width in (390, 1280):
            with self.subTest(width=width), TemporaryDirectory(prefix="panel-deadline-") as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = chrome.tab()
                    tab.call("Emulation.setDeviceMetricsOverride", {"width":width,"height":844,"deviceScaleFactor":1,"mobile":width==390})
                    tab.call("Network.setCookie", {"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url})
                    tab.call("Page.navigate", {"url":self.live_server_url+"/vendas/"})
                    tab.wait("!!window.salesDraftUI?.ready")
                    self.click(tab, "#btnLocacoesOperacionaisVenda")
                    tab.wait('!!document.querySelector("[data-prazo-operacional]")')
                    self.assertTrue(tab.evaluate('(()=>{const p=document.querySelector("[data-prazo-operacional]"),m=p.closest(".locacoes-rapidas-marcador"),d=m?.querySelector(".locacoes-rapidas-hora");return !!m && !!d && p.getBoundingClientRect().top>=d.getBoundingClientRect().bottom && !m.previousElementSibling.querySelector("[data-prazo-operacional]") && m.previousElementSibling.textContent.includes("Prevista:");})()'))
                    self.assertEqual(tab.evaluate('getComputedStyle(document.querySelector("[data-prazo-operacional]")).color'),"rgb(185, 28, 28)")
                    self.assertIn('timeZone: "' + timezone.get_current_timezone_name() + '"',
                                  tab.evaluate("document.documentElement.innerHTML"))
                    tab.evaluate('window.originalPanelDate=Date;window.panelNow=Date.parse("2026-10-15T12:00:00-03:00");window.Date=class extends originalPanelDate{constructor(...a){super(...(a.length?a:[panelNow]));}static now(){return panelNow;}};window.deadlineCard=document.querySelector("[data-prazo-operacional]").closest("[data-tarefa-id]");deadlineCard.dataset.dataLateral="2026-10-15";true')
                    for minutes, label in [(3578,"2d 11h 38min"), (330,"5h 30min"),(45,"45min"),(20,"20min"),(-20,"Atrasada 20min"),(-180,"Atrasada 3h")]:
                        tab.evaluate(f'deadlineCard.dataset.horarioLateral=String(panelNow+{minutes}*60000);true')
                        tab.wait(f'deadlineCard.querySelector("[data-prazo-operacional]").textContent==={label!r}')
                        self.assertEqual(tab.evaluate('deadlineCard.classList.contains("atrasada")'), minutes < 0)
                        self.assertEqual(tab.evaluate('deadlineCard.classList.contains("proxima")'), 0 < minutes <= 30)
                    # Observe only text nodes: unchanged minute labels must not be rewritten.
                    tab.evaluate('window.deadlineWrites=0;window.deadlineObserver=new MutationObserver(entries=>deadlineWrites+=entries.length);deadlineObserver.observe(deadlineCard.querySelector("[data-prazo-operacional]"),{childList:true,characterData:true,subtree:true});true')
                    tab.evaluate('await new Promise(resolve=>setTimeout(resolve,2200));true')
                    self.assertEqual(tab.evaluate("deadlineWrites"),0)
                    # Cross the scheduled time, then midnight in the operational timezone.
                    tab.evaluate('deadlineCard.dataset.horarioLateral=String(panelNow+60000);true')
                    tab.wait('deadlineCard.querySelector("[data-prazo-operacional]").textContent==="1min"')
                    tab.evaluate('window.panelNow+=120000;true')
                    tab.wait('deadlineCard.querySelector("[data-prazo-operacional]").textContent==="Atrasada 1min"')
                    tab.evaluate('window.panelNow=originalPanelDate.parse("2026-10-15T23:59:00-03:00");deadlineCard.dataset.horarioLateral="";true')
                    tab.wait('!deadlineCard.classList.contains("atrasada")')
                    tab.evaluate('window.panelNow+=120000;true')
                    tab.wait('deadlineCard.querySelector("[data-prazo-operacional]").textContent==="Atrasada - sem horario definido"')
                    self.task.refresh_from_db()
                    self.assertEqual(self.task.status, "pendente")
                    self.assertFalse(ConferenciaRecolhimentoLocacao.objects.exists())
                finally:
                    chrome.stop()

    def test_counters_match_filters_mobile_and_desktop(self):
        self.client.force_login(self.user)
        helper = self.helper
        old = helper.criar_locacao(data_entrega=helper.hoje-timedelta(days=4))
        future = helper.criar_locacao(data_entrega=helper.hoje+timedelta(days=20),
                                      data_prevista_devolucao=helper.hoje+timedelta(days=21))
        for status in ("confirmada", "resolvida_admin"):
            rental = helper.criar_locacao(data_entrega=helper.hoje+timedelta(days=10),
                                          data_prevista_devolucao=helper.hoje+timedelta(days=11))
            task = obter_ou_criar_tarefa_operacional(rental,"entrega")
            type(task).objects.filter(pk=task.pk).update(status=status)
        painel = painel_operacional_rapido_locacoes(RequestFactory().get("/"))
        expected = {"entrega":2,"recolhimento":1,"tudo":2,"antigas":1}
        self.assertEqual(painel["total_pendencias"],3)
        for width in (390,1280):
            with self.subTest(width=width), TemporaryDirectory(prefix="panel-counters-") as profile:
                chrome = Chrome(CHROME,profile).start()
                try:
                    tab = chrome.tab()
                    tab.call("Emulation.setDeviceMetricsOverride",{"width":width,"height":844,"deviceScaleFactor":1,"mobile":width==390})
                    tab.call("Network.setCookie",{"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url})
                    tab.call("Page.navigate",{"url":self.live_server_url+"/vendas/"})
                    tab.wait("!!window.salesDraftUI?.ready")
                    self.assertEqual(tab.evaluate('document.querySelector(".locacoes-operacionais-badge").textContent.trim()'),"3")
                    self.click(tab,"#btnLocacoesOperacionaisVenda")
                    self.assertEqual(tab.evaluate('document.querySelectorAll("[data-locacoes-card]").length'),3)
                    for name, count in expected.items():
                        selector = '[data-locacoes-filtro="'+name+'"]'
                        self.assertTrue(tab.evaluate(f'document.querySelector({selector!r}).textContent.trim().endsWith(" {count}")'))
                        self.click(tab,selector)
                        self.assertEqual(tab.evaluate('Array.from(document.querySelectorAll("[data-locacoes-card]")).filter(c=>!c.hidden).length'),count)
                finally:
                    chrome.stop()
