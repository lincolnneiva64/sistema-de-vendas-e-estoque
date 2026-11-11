"""Temporary Chrome profiles, isolated server, synthetic operations only."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import Permission
from . import tests_sale_sync_browser as sync
from . import tests_sale_release_browser as release
from . import tests_sale_creation as creation
from .browser_support import Chrome
from .models import AbandonoVendaOffline, OperacaoSincronizacao


@skipUnless(Path(sync.CHROME).is_file(),'Chrome unavailable')
class SaleResolutionBrowserTests(StaticLiveServerTestCase):
    reset_sequences=True
    def setUp(self):
        sync.SaleSyncBrowserTests.setUp(self)
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='estoque',
            codename__in=['add_venda','view_venda']))
    open_sales=sync.SaleSyncBrowserTests.open_sales
    reload=sync.SaleSyncBrowserTests.reload
    repository=sync.SaleSyncBrowserTests.repository
    select_customer=sync.SaleSyncBrowserTests.select_customer
    add_product=sync.SaleSyncBrowserTests.add_product
    prepare=sync.SaleSyncBrowserTests.prepare
    stable=sync.SaleSyncBrowserTests.stable
    start_manual=sync.SaleSyncBrowserTests.start_manual
    manual=sync.SaleSyncBrowserTests.manual
    original_conflict=release.SaleReleaseBrowserTests.original_conflict
    effects=creation.SaleCreationTests.effects

    def browser(self):
        profile=TemporaryDirectory(prefix='sale-resolution-fictional-',ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        chrome=Chrome(sync.CHROME,profile.name).start();self.addCleanup(chrome.stop)
        return chrome

    def load_module(self,t):
        t.evaluate("window.resolver=await import('/offline/assets/sale-resolution-v1/sales-resolutions.js');window.closures=await import('/offline/assets/sale-resolution-v1/sales-closures.js');true")

    def child_and_independent(self,t):
        t.evaluate('''window.parentOperation=await repo.get('operations',operation.operation_id);
            const command=core.commandOf(parentOperation),id=crypto.randomUUID();
            window.childCommand={...command,operation_id:id,aggregate_id:id,sequence:command.sequence+1,
                payload:{...command.payload,revisao:{original_operation_id:command.operation_id,original_hash:parentOperation.payload_hash,relacao:'revisao_de_conflito',revisada_em:new Date().toISOString()}}};
            window.child={...childCommand,payload_hash:await core.hash(childCommand),status:'pendente',attempts:0,server_result:null};
            await repo.put('operations',child);await repo.put('history',child);
            const otherId=crypto.randomUUID(),otherCommand={...command,operation_id:otherId,aggregate_id:otherId,sequence:command.sequence+2};
            window.independent={...otherCommand,payload_hash:await core.hash(otherCommand),status:'resultado_desconhecido',attempts:1,server_result:null};
            await repo.put('operations',independent);window.otherBefore=JSON.stringify(independent);
            window.historyBefore=JSON.stringify(await repo.all('history'));true''')

    def test_simple_ui_resolves_child_then_root_preserving_independent_and_history(self):
        chrome=self.browser();t=self.original_conflict(chrome);self.load_module(t)
        self.child_and_independent(t);before=self.effects()
        t.evaluate("document.dispatchEvent(new CustomEvent('sales-resolve-pending',{detail:{operation_id:parentOperation.operation_id}}));true")
        t.wait("document.getElementById('sales-resolution-dialog')?.open")
        t.wait("document.querySelector('#sales-resolution-dialog [role=status]').textContent.includes('primeiro')")
        self.assertEqual(AbandonoVendaOffline.objects.count(),0)
        t.evaluate("document.querySelector('#sales-resolution-dialog [data-reason]').value='Tentativa abandonada';document.querySelector('#sales-resolution-dialog [data-confirm]').checked=true;document.querySelector('#sales-resolution-dialog [data-resolve]').click();true")
        t.wait("!document.querySelector('#sales-resolution-dialog [data-back]').hidden")
        t.evaluate("document.querySelector('#sales-resolution-dialog [data-back]').click();document.querySelector('#sales-resolution-dialog [data-confirm]').checked=true;document.querySelector('#sales-resolution-dialog [data-resolve]').click();true")
        t.wait("document.querySelector('#sales-resolution-dialog [role=status]').textContent.includes('confirmado pelo servidor')")
        self.assertEqual(AbandonoVendaOffline.objects.count(),2)
        self.assertEqual(self.effects(),before)
        self.assertEqual(t.evaluate("JSON.stringify(await repo.get('operations',independent.operation_id))"),t.evaluate('otherBefore'))
        self.assertEqual(t.evaluate("JSON.stringify(await repo.all('history'))"),t.evaluate('historyBefore'))
        self.assertTrue(t.evaluate("closures.closedLocally(await repo.all('metadata'),child) && closures.closedLocally(await repo.all('metadata'),parentOperation)"))
        self.assertFalse(t.evaluate("closures.closedLocally(await repo.all('metadata'),independent)"))
        t.wait('!salesDraftUI.finalization')

    def test_lost_response_inconclusive_get_and_repeat_preserve_one_decision(self):
        chrome=self.browser();t=self.original_conflict(chrome);self.load_module(t);self.child_and_independent(t)
        t.evaluate("window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(String(u).includes('/resolution/')&&o?.method==='POST')throw TypeError('lost response');return r};window.failed=false;try{await resolver.resolvePending(repo,scope,child,'Abandono',true)}catch(e){failed=true};true")
        self.assertTrue(t.evaluate('failed'));self.assertEqual(AbandonoVendaOffline.objects.count(),1)
        self.assertFalse(t.evaluate("closures.closedLocally(await repo.all('metadata'),child)"))
        t.evaluate("window.fetch=async(u,o)=>String(u).includes('/resolution/')?new Response('{}',{status:503}):realFetch(u,o);try{await resolver.consultResolution(repo,scope,child)}catch(e){};true")
        self.assertFalse(t.evaluate("closures.closedLocally(await repo.all('metadata'),child)"))
        t.evaluate("window.fetch=realFetch;await resolver.consultResolution(repo,scope,child);await resolver.resolvePending(repo,scope,child,'Ignored repeat',true);true")
        self.assertEqual(AbandonoVendaOffline.objects.count(),1)
        self.assertTrue(t.evaluate("closures.closedLocally(await repo.all('metadata'),child)"))
        child=t.evaluate('child');command=t.evaluate('childCommand')
        independent=t.evaluate("await repo.get('operations',independent.operation_id)")
        history=t.evaluate("await repo.all('history')");before=self.effects()
        self.reload(t);self.repository(t);self.load_module(t)
        t.evaluate('window.child='+json.dumps(child)+';true')
        self.assertTrue(t.evaluate("closures.closedLocally(await repo.all('metadata'),child)"))
        self.assertEqual(t.evaluate("await repo.get('operations',"+json.dumps(independent['operation_id'])+")"),independent)
        self.assertEqual(t.evaluate("await repo.all('history')"),history)
        other=self.open_sales(chrome);self.load_module(other)
        self.assertTrue(other.evaluate("closures.closedLocally(await repo.all('metadata'),await repo.get('operations',"+json.dumps(child['operation_id'])+"))"))
        fresh=self.open_sales(self.browser())
        self.assertNotEqual(fresh.evaluate('scope.device_id'),t.evaluate('scope.device_id'))
        envelope={**command,'payload_hash':child['payload_hash']}
        fresh.evaluate("window.replay=await fetch('/api/offline/observations/',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(document.cookie.split('; ').find(c=>c.startsWith('csrftoken=')).split('=')[1])},body:JSON.stringify("+json.dumps(envelope)+")});window.replayed=await replay.json();true")
        self.assertEqual(fresh.evaluate('replay.status'),409)
        self.assertEqual(fresh.evaluate('replayed.code'),'encerrada_administrativamente')
        self.assertEqual(self.effects(),before)
        self.assertEqual(AbandonoVendaOffline.objects.count(),1)

    def test_two_tabs_do_not_send_during_resolution_and_absence_is_not_ack(self):
        chrome=self.browser();t=self.original_conflict(chrome);self.load_module(t);self.child_and_independent(t)
        other=self.open_sales(chrome)
        t.evaluate("window.lockEntered=false;window.hold=navigator.locks.request('offline-pilot-sync',async()=>{lockEntered=true;await new Promise(r=>window.unlock=r)});true")
        t.wait('lockEntered')
        other.evaluate("window.resolver=await import('/offline/assets/sale-resolution-v1/sales-resolutions.js');window.child=(await repo.all('operations')).find(o=>o.payload?.revisao);window.busy=false;try{await resolver.resolvePending(repo,scope,child,'Abandono',true)}catch(e){busy=e.message.includes('Outra aba')};true")
        self.assertTrue(other.evaluate('busy'));self.assertFalse(AbandonoVendaOffline.objects.exists())
        t.evaluate("unlock();await hold;window.state=await resolver.consultResolution(repo,scope,child);true")
        self.assertEqual(t.evaluate('state.lookup'),'nao_encontrada')
        self.assertFalse(AbandonoVendaOffline.objects.exists())
        t.evaluate("window.fetch=async(u,o)=>{if(String(u).includes('/resolution/')&&o?.method==='POST')throw TypeError('not sent');return realFetch(u,o)};try{await resolver.resolvePending(repo,scope,child,'Abandono',true)}catch(e){};true")
        self.assertTrue(t.evaluate("resolver.resolutionPending(await repo.all('metadata'),child)"))
        self.assertFalse(t.evaluate("closures.closedLocally(await repo.all('metadata'),child)"))

    def test_confirmed_replacement_shows_official_sale_without_abandonment(self):
        t=self.original_conflict(self.browser());self.load_module(t);self.child_and_independent(t)
        from .services import process_operation
        receipt,status=process_operation(t.evaluate('childCommand'),self.user)
        self.assertEqual(status,200)
        before=self.effects()
        t.evaluate("document.dispatchEvent(new CustomEvent('sales-resolve-pending',{detail:{operation_id:child.operation_id}}));true")
        t.wait("document.getElementById('sales-resolution-dialog')?.open")
        t.evaluate("document.querySelector('#sales-resolution-dialog [data-reason]').value='Verificar tentativa';document.querySelector('#sales-resolution-dialog [data-confirm]').checked=true;document.querySelector('#sales-resolution-dialog [data-resolve]').click();true")
        t.wait("!document.querySelector('#sales-resolution-dialog [data-official]').hidden")
        self.assertIn(str(receipt['record_id']),t.evaluate("document.querySelector('#sales-resolution-dialog [data-official]').href"))
        self.assertTrue(t.evaluate("document.querySelector('#sales-resolution-dialog [data-resolve]').hidden"))
        self.assertFalse(AbandonoVendaOffline.objects.exists())
        self.assertEqual(self.effects(),before)
