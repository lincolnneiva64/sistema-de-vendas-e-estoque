"""Controlled revision through the real page, IndexedDB and manual synchronizer."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from uuid import uuid4

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao, RevisaoVendaOffline
from .services import COMMAND_KEYS, command_hash, process_operation
from .browser_support import Chrome
from . import tests_sale_sync_browser as sync


@skipUnless(Path(sync.CHROME).is_file(), 'Chrome indisponível')
class SaleRevisionBrowserTests(StaticLiveServerTestCase):
    reset_sequences=True
    setUp=sync.SaleSyncBrowserTests.setUp
    open_sales=sync.SaleSyncBrowserTests.open_sales
    repository=sync.SaleSyncBrowserTests.repository
    select_customer=sync.SaleSyncBrowserTests.select_customer
    add_product=sync.SaleSyncBrowserTests.add_product
    stable=sync.SaleSyncBrowserTests.stable
    start_manual=sync.SaleSyncBrowserTests.start_manual
    manual=sync.SaleSyncBrowserTests.manual

    def seed(self,chrome):
        tab=self.open_sales(chrome);self.select_customer(tab,'Cliente Comercial');self.add_product(tab,'Produto Fracionado')
        tab.evaluate('await (await import("/offline/assets/2-8d/commercial.js")).atualizarSnapshotComercial(repo,scope);window.normalBefore=await drafts.loadDraft(repo,scope);true')
        normal=tab.evaluate('normalBefore.draft');scope=tab.evaluate('scope');identifier=str(uuid4())
        command={'operation_id':identifier,'aggregate_id':identifier,**scope,'type':'criar_venda','schema_version':1,
                 'created_at':timezone.now().isoformat(),'sequence':1,
                 'payload':{'schema_version':1,'cliente_id':str(self.customer.pk),'data_venda':normal['data_venda'],
                            'data_vencimento':normal['data_vencimento'],'operador':self.operator.nome,'tipo_pagamento':'A prazo',
                            'itens':[{'produto_id':str(self.product.pk),'quantidade':'1000','unidade':'CX','preco_unitario':'24.00'}]}}
        receipt,status=process_operation(command,self.user);self.assertEqual(status,409)
        operation={**command,'payload_hash':command_hash(command),'status':'conflito','attempts':1,
                   'last_error':receipt['erro'],'server_result':receipt,
                   'original_labels':{'cliente':{'id':str(self.customer.pk),'nome':self.customer.nome},'produtos':[{'id':str(self.product.pk),'nome':self.product.nome}]}}
        tab.evaluate('window.original='+json.dumps(operation)+';await repo.put("operations",original);await repo.put("metadata",{key:"device",id:scope.device_id,sequence:1});true')
        self.original_history={**operation,'synchronized_at':command['created_at']}
        tab.evaluate('await repo.put("history",'+json.dumps(self.original_history)+');true')
        self.command=command;self.original=operation;self.normal=normal;self.scope=scope
        self.before_server=OperacaoSincronizacao.objects.values().get(operation_id=identifier)
        self.assertEqual(Venda.objects.count(),0)
        return tab

    def revision_tab(self,chrome,identifier=None):
        tab=chrome.tab();tab.call('Network.enable')
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Page.addScriptToEvaluateOnNewDocument',{'source':'''const wall=Date.now;window.offset=0;Date.now=()=>wall()+offset;window.setInterval=()=>0;window.clearInterval=()=>{};window.confirm=()=>true;window.requests=[];const real=fetch;window.fetch=(url,options)=>{requests.push({url:String(url),method:options?.method||'GET'});return real(url,options)};'''})
        tab.call('Page.navigate',{'url':self.live_server_url+'/offline/revisao/?operation_id='+(identifier or self.command['operation_id'])})
        tab.wait('window.salesRevisionUI?.ready')
        self.assertTrue(tab.evaluate('!!salesRevisionUI.record'),tab.evaluate('document.getElementById("revision-message").textContent'))
        tab.evaluate('window.core=await import("/offline/assets/2-8d/core.js");window.revisions=await import("/offline/assets/2-8d/sales-revisions.js");window.drafts=await import("/offline/assets/2-8d/sales-drafts.js");window.repo=new core.Repository(await core.openDB());window.scope=await drafts.draftScope(repo,await repo.get("metadata","sales-identity"));window.app=await import("/offline/assets/2-8d/app.js");await app.initialized;window.original=await repo.get("operations",'+json.dumps(identifier or self.command['operation_id'])+');true')
        return tab

    def quantity(self,tab,value):
        tab.evaluate('const q=document.querySelector("[data-item-field=quantidade]");q.value='+json.dumps(value)+';q.dispatchEvent(new Event("input",{bubbles:true}));await salesRevisionUI.flush();true')

    def conclude(self,tab):
        tab.evaluate('document.getElementById("revision-form").requestSubmit();true')
        tab.wait('salesRevisionUI.record?.status==="concluida"')
        return tab.evaluate('await repo.get("operations",salesRevisionUI.record.replacement_operation_id)')

    def unchanged(self,tab):
        self.assertEqual(tab.evaluate('await repo.get("operations",'+json.dumps(self.command['operation_id'])+')'),self.original)
        self.assertEqual(tab.evaluate('await repo.get("history",'+json.dumps(self.command['operation_id'])+')'),self.original_history)
        self.assertEqual(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'),self.normal)
        self.assertEqual(OperacaoSincronizacao.objects.values().get(operation_id=self.command['operation_id']),self.before_server)

    def test_original_normal_draft_catalog_values_and_manual_confirmation(self):
        with TemporaryDirectory(prefix='revision-manual-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                normal=self.seed(chrome)
                normal.evaluate('const commercial=await import("/offline/assets/2-8d/commercial.js");const snapshot=await commercial.carregarSnapshotComercial(repo,scope);snapshot.produtos[0].preco_venda="99.00";snapshot.produtos[0].fator_conversao="99.00";snapshot.clientes[0].prazo_padrao_dias=99;await repo.put("snapshots",snapshot);true')
                tab=self.revision_tab(chrome)
                self.assertEqual(tab.evaluate('salesRevisionUI.record.payload'),self.command['payload'])
                self.assertGreaterEqual(tab.evaluate('requests.filter(r=>r.url.includes("/api/offline/operations/")).length'),1)
                self.assertEqual(tab.evaluate('requests.filter(r=>r.method!=="GET").length'),0)
                self.assertEqual(tab.evaluate('document.querySelector("[data-item-field=preco_unitario]").value'),'24.00')
                self.assertEqual(tab.evaluate('document.querySelector("[data-item-field=unidade]").value'),'CX')
                self.assertEqual(tab.evaluate('document.getElementById("revision-due").value'),self.command['payload']['data_vencimento'])
                self.quantity(tab,'2');self.unchanged(tab)
                count=tab.evaluate('requests.filter(r=>r.url.includes("/api/offline/operations/")).length')
                operation=self.conclude(tab)
                self.assertGreater(tab.evaluate('requests.filter(r=>r.url.includes("/api/offline/operations/")).length'),count)
                self.assertNotEqual(operation['operation_id'],self.command['operation_id'])
                self.assertNotEqual(operation['created_at'],self.command['created_at'])
                self.assertNotEqual(operation['payload_hash'],self.original['payload_hash'])
                self.assertEqual(operation['payload_hash'],command_hash({key:operation[key] for key in COMMAND_KEYS}))
                self.assertEqual(operation['sequence'],2)
                self.assertEqual(operation['payload']['revisao']['original_operation_id'],self.command['operation_id'])
                self.assertEqual(operation['payload']['revisao']['original_hash'],self.original['payload_hash'])
                self.assertEqual(tab.evaluate('requests.filter(r=>r.method!=="GET").length'),0)
                self.assertEqual(Venda.objects.count(),0);self.assertEqual(ItemVenda.objects.count(),0);self.assertEqual(ContaReceber.objects.count(),0);self.assertEqual(MovimentoFinanceiro.objects.count(),0)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                self.stable(tab);self.manual(tab)
                tab.wait('document.getElementById("revision-message").textContent.includes("envie novamente a nota correta")')
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(ItemVenda.objects.count(),1);self.assertEqual(ContaReceber.objects.count(),1)
                self.assertEqual(RevisaoVendaOffline.objects.count(),1);self.unchanged(tab)
                self.assertIn('Sincronização concluída',tab.evaluate('document.getElementById("offline-modal-text").textContent'))
                normal.evaluate('await salesDraftUI.flush();true');self.assertEqual(normal.evaluate('(await drafts.loadDraft(repo,scope)).draft'),self.normal)
                view=tab.evaluate('await revisions.revisionPresentation(repo,original,scope)');self.assertEqual(view['recordId'],Venda.objects.get().pk)
            finally:chrome.stop()

    def test_two_tabs_same_draft_stale_write_and_simultaneous_conclusion(self):
        with TemporaryDirectory(prefix='revision-tabs-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);first=self.revision_tab(chrome);second=self.revision_tab(chrome)
                self.assertEqual(first.evaluate('salesRevisionUI.record.draft_id'),second.evaluate('salesRevisionUI.record.draft_id'))
                old_revision=second.evaluate('salesRevisionUI.record.revision');self.quantity(first,'2')
                second.wait('salesRevisionUI.blocked')
                self.assertTrue(second.evaluate('try{await revisions.saveRevision(repo,scope,original.operation_id,original.payload,'+str(old_revision)+');false}catch(e){true}'))
                second.call('Page.reload');second.wait('window.salesRevisionUI?.ready && !salesRevisionUI.blocked');second.evaluate('window.core=await import("/offline/assets/2-8d/core.js");window.revisions=await import("/offline/assets/2-8d/sales-revisions.js");window.drafts=await import("/offline/assets/2-8d/sales-drafts.js");window.repo=new core.Repository(await core.openDB());window.scope=await drafts.draftScope(repo,await repo.get("metadata","sales-identity"));window.original=await repo.get("operations",'+json.dumps(self.command['operation_id'])+');true')
                for tab in (first,second):tab.evaluate('window.finalResult=revisions.finalizeRevision(repo,scope,original.operation_id,salesRevisionUI.record.revision);true')
                a=first.evaluate('await finalResult');b=second.evaluate('await finalResult')
                self.assertEqual(a['replacement_operation_id'],b['replacement_operation_id'])
                self.assertEqual(first.evaluate('(await repo.all("operations")).length'),2)
                self.assertEqual(first.evaluate('(await repo.get("metadata","device")).sequence'),2)
                self.assertEqual(Venda.objects.count(),0);self.unchanged(first)
            finally:chrome.stop()

    def test_noncommercial_states_and_lookup_failures_never_open(self):
        with TemporaryDirectory(prefix='revision-eligibility-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                tab=self.seed(chrome)
                tab.evaluate('window.revisions=await import("/offline/assets/2-8d/sales-revisions.js");window.baseFetch=fetch;window.lookups=0;window.fetch=(u,o)=>{if(String(u).includes("/api/offline/operations/"))lookups++;return baseFetch(u,o)};true')
                for status in ('resultado_desconhecido','pendente','erro','confirmada'):
                    self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,{...original,status:'+json.dumps(status)+'});false}catch(e){true}'))
                self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,{...original,diagnostic_code:"uuid_comando_divergente"});false}catch(e){true}'))
                self.assertEqual(tab.evaluate('lookups'),0)
                for status,body in [(401,{}),(403,{}),(503,{}),(200,{'lookup':'nao_encontrada'}),(200,{'lookup':'encontrada','receipt':{}}),(409,{'lookup':'incompativel'})]:
                    tab.evaluate('window.fetch=async()=>new Response('+json.dumps(json.dumps(body))+',{status:'+str(status)+',headers:{"Content-Type":"application/json"}});true')
                    self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,original);false}catch(e){true}'))
                    self.assertIsNone(tab.evaluate('(await revisions.loadRevision(repo,scope,original.operation_id)).record'))
                tab.evaluate('window.fetch=async()=>new Response("invalid json",{status:200});true')
                self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,original);false}catch(e){true}'))
                tab.evaluate('window.fetch=baseFetch;true');self.unchanged(tab);self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_offline_edit_refresh_reopen_and_late_confirmation(self):
        with TemporaryDirectory(prefix='revision-offline-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);tab=self.revision_tab(chrome)
                tab.evaluate('await navigator.serviceWorker.ready;await app.probe();true');tab.wait('!!navigator.serviceWorker.controller')
                worker=chrome.worker();worker.call('Network.enable')
                conditions=dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0)
                worker.call('Network.emulateNetworkConditions',conditions);tab.call('Network.emulateNetworkConditions',conditions)
                tab.wait('!navigator.onLine');self.quantity(tab,'1')
                self.assertTrue(tab.evaluate('try{await revisions.finalizeRevision(repo,scope,original.operation_id,salesRevisionUI.record.revision);false}catch(e){true}'))
                before_origin=tab.evaluate('performance.timeOrigin');tab.call('Page.reload');tab.wait('performance.timeOrigin!=='+str(before_origin)+' && window.salesRevisionUI?.ready')
                self.assertEqual(tab.evaluate('salesRevisionUI.record.payload.itens[0].quantidade'),'1')
                self.assertTrue(tab.evaluate('document.getElementById("revision-save").disabled'))
                conditions['offline']=False;worker.call('Network.emulateNetworkConditions',conditions);tab.call('Network.emulateNetworkConditions',conditions)
                chrome.stop();chrome=Chrome(sync.CHROME,profile).start();tab=self.revision_tab(chrome)
                self.assertEqual(tab.evaluate('salesRevisionUI.record.payload.itens[0].quantidade'),'1')
                row=OperacaoSincronizacao.objects.get(operation_id=self.command['operation_id']);receipt={**row.resultado,'status':'confirmada','record_id':123};row.status='confirmada';row.resultado=receipt;row.save(update_fields=['status','resultado'])
                tab.evaluate('document.getElementById("revision-form").requestSubmit();true')
                tab.wait('document.getElementById("revision-message").textContent.includes("Original já confirmada")')
                self.assertFalse(tab.evaluate('document.getElementById("revision-official").hidden'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),1)
                self.assertEqual(tab.evaluate('await repo.get("operations",original.operation_id)'),self.original)
                self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_discard_and_atomic_idb_failure_preserve_both_original_and_normal(self):
        with TemporaryDirectory(prefix='revision-idb-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);tab=self.revision_tab(chrome);self.quantity(tab,'2')
                tab.evaluate('window.transactionBefore=core.Repository.prototype.transaction;core.Repository.prototype.transaction=function(stores,write,work){return transactionBefore.call(this,stores,write,(tx,done)=>{work(tx,done);if(write&&stores.includes("operations")&&stores.includes("metadata"))tx.abort()})};true')
                self.assertTrue(tab.evaluate('try{await revisions.finalizeRevision(repo,scope,original.operation_id,salesRevisionUI.record.revision);false}catch(e){true}'))
                tab.evaluate('core.Repository.prototype.transaction=transactionBefore;true')
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'),1)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'),1)
                self.assertEqual(tab.evaluate('(await revisions.loadRevision(repo,scope,original.operation_id)).record.status'),'rascunho')
                self.unchanged(tab)
                tab.evaluate('document.getElementById("revision-discard").click();true')
                tab.wait('document.getElementById("revision-message").textContent.includes("descartado")')
                self.assertIsNone(tab.evaluate('(await revisions.loadRevision(repo,scope,original.operation_id)).record'))
                self.unchanged(tab);self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_stale_lookup_cannot_regress_official_confirmation_or_create_revision(self):
        with TemporaryDirectory(prefix='revision-stale-receipt-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                tab=self.seed(chrome)
                tab.evaluate('window.revisions=await import("/offline/assets/2-8d/sales-revisions.js");window.baseFetch=fetch;window.lookup={lookup:"encontrada",operation_id:original.operation_id,hash:original.payload_hash,actor_id:scope.actor_id,environment_id:scope.environment_id,device_id:scope.device_id,type:"criar_venda",receipt:original.server_result,revisions:[]};window.confirmed={...lookup,receipt:{...lookup.receipt,status:"confirmada",record_id:123}};window.fetch=async()=>new Response(JSON.stringify(confirmed),{status:200});true')
                self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,original);false}catch(e){e.recordId===123}'))
                tab.evaluate('window.fetch=async()=>new Response(JSON.stringify(lookup),{status:200});true')
                self.assertTrue(tab.evaluate('try{await revisions.beginRevision(repo,scope,original);false}catch(e){e.recordId===123}'))
                self.assertIsNone(tab.evaluate('(await revisions.loadRevision(repo,scope,original.operation_id)).record'))
                self.assertEqual(tab.evaluate('(await revisions.revisionPresentation(repo,original,scope)).recordId'),123)
                tab.evaluate('window.fetch=baseFetch;true');self.unchanged(tab);self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_identity_change_hides_and_blocks_revision_without_altering_records(self):
        with TemporaryDirectory(prefix='revision-identity-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);tab=self.revision_tab(chrome)
                tab.evaluate('await repo.put("metadata",{key:"sales-identity",actor_id:"999",environment_id:scope.environment_id});const privacyChannel=new BroadcastChannel("sales-identity");privacyChannel.postMessage("changed");privacyChannel.close();true')
                tab.wait('salesRevisionUI.blocked && document.getElementById("revision-form").hidden')
                self.assertTrue(tab.evaluate('document.getElementById("revision-save").disabled'))
                self.assertTrue(tab.evaluate('document.getElementById("revision-official").hidden'))
                tab.evaluate('await repo.put("metadata",{key:"sales-identity",actor_id:scope.actor_id,environment_id:scope.environment_id});true')
                self.unchanged(tab);self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_original_confirmed_after_local_conclusion_blocks_server_sale_and_recovers_original(self):
        with TemporaryDirectory(prefix='revision-late-queued-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);tab=self.revision_tab(chrome);self.quantity(tab,'2');child=self.conclude(tab)
                row=OperacaoSincronizacao.objects.get(operation_id=self.command['operation_id'])
                row.status='confirmada';row.resultado={**row.resultado,'status':'confirmada','record_id':123};row.save(update_fields=['status','resultado'])
                # Two UI refreshes must await the same original lookup, even when
                # its response arrives after a second queue notification.
                tab.evaluate('window.queryTransport=fetch;window.fetch=async(u,o)=>{const response=await queryTransport(u,o);if(String(u).includes("/api/offline/operations/"+original.operation_id+"/"))return new Promise(resolve=>window.releaseBlockedLookup=()=>resolve(response));return response};true')
                self.stable(tab);self.manual(tab)
                tab.wait('typeof window.releaseBlockedLookup==="function"')
                tab.evaluate('document.dispatchEvent(new Event("offline-operation-updated"));document.dispatchEvent(new Event("offline-operation-updated"));true')
                self.assertTrue(tab.evaluate('document.getElementById("revision-official").hidden'))
                tab.evaluate('releaseBlockedLookup();true')
                try:
                    tab.wait('!document.getElementById("revision-official").hidden')
                except AssertionError:
                    self.fail(tab.evaluate('JSON.stringify({message:document.getElementById("revision-message").textContent,observations:(await repo.all("metadata")).filter(value=>value.key.startsWith("revisao_consulta:")),linkHidden:document.getElementById("revision-official").hidden})'))
                self.assertIn('/vendas/123/',tab.evaluate('document.getElementById("revision-official").href'))
                result=tab.evaluate('await repo.get("operations",'+json.dumps(child['operation_id'])+')')
                self.assertEqual(result['server_result']['code'],'revisao_origem_bloqueada')
                self.assertEqual(tab.evaluate('await repo.get("operations",original.operation_id)'),self.original)
                self.assertEqual(Venda.objects.count(),0);self.assertEqual(RevisaoVendaOffline.objects.count(),0)
            finally:chrome.stop()

    def test_missing_product_and_explicit_commercial_changes_do_not_reset_other_fields(self):
        with TemporaryDirectory(prefix='revision-fields-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                seed=self.seed(chrome)
                seed.evaluate('const commercial=await import("/offline/assets/2-8d/commercial.js");const snapshot=await commercial.carregarSnapshotComercial(repo,scope);snapshot.produtos=[];snapshot.contagens.produtos=0;await repo.put("snapshots",snapshot);true')
                tab=self.revision_tab(chrome)
                self.assertIn('Produto ausente',tab.evaluate('document.getElementById("revision-items").textContent'))
                self.assertEqual(tab.evaluate('salesRevisionUI.record.payload'),self.command['payload'])
                tab.evaluate('document.querySelector("[data-item-field=unidade]").value="UN";document.querySelector("[data-item-field=unidade]").dispatchEvent(new Event("change",{bubbles:true}));await salesRevisionUI.flush();true')
                self.assertEqual(tab.evaluate('document.querySelector("[data-item-field=preco_unitario]").value'),'24.00')
                tab.evaluate('document.getElementById("revision-payment").value="À vista";document.getElementById("revision-payment").dispatchEvent(new Event("change",{bubbles:true}));document.getElementById("revision-cash").value="12.00";document.getElementById("revision-bank").value="12.00";document.getElementById("revision-bank").dispatchEvent(new Event("input",{bubbles:true}));await salesRevisionUI.flush();true')
                self.assertEqual(tab.evaluate('document.getElementById("revision-due").value'),self.command['payload']['data_vencimento'])
                saved=tab.evaluate('salesRevisionUI.record.payload')
                self.assertEqual(saved['origem_recebimento'],{'caixa':'12.00','banco':'12.00'})
                self.assertEqual(saved['data_venda'],self.command['payload']['data_venda'])
                self.assertEqual(saved['operador'],self.command['payload']['operador'])
                self.unchanged(tab);self.assertEqual(Venda.objects.count(),0)
            finally:chrome.stop()

    def test_chain_and_unknown_replacement_reconcile_without_second_post(self):
        with TemporaryDirectory(prefix='revision-chain-') as profile:
            chrome=Chrome(sync.CHROME,profile).start()
            try:
                self.seed(chrome);tab=self.revision_tab(chrome);child=self.conclude(tab)
                self.stable(tab);self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status'),'conflito')
                other=self.revision_tab(chrome,child['operation_id']);self.quantity(other,'1');grandchild=self.conclude(other)
                other.evaluate('window.realFetch=fetch;window.posts=0;window.fetch=async(u,o)=>{const response=await realFetch(u,o);if(String(u)==="/api/offline/observations/"){posts++;throw TypeError("lost response")}return response};true')
                self.stable(other);self.manual(other)
                self.assertEqual(other.evaluate('(await repo.get("operations",salesRevisionUI.record.replacement_operation_id)).status'),'resultado_desconhecido')
                self.assertEqual(Venda.objects.count(),1)
                other.evaluate('window.fetch=realFetch;true');self.stable(other);self.manual(other)
                other.wait('document.getElementById("revision-message").textContent.includes("envie novamente")')
                self.assertEqual(other.evaluate('posts'),1)
                self.assertEqual(Venda.objects.count(),1);self.assertEqual(RevisaoVendaOffline.objects.count(),2)
                self.assertEqual(other.evaluate('await revisions.revisionPresentation(repo,await repo.get("operations",'+json.dumps(self.command['operation_id'])+'),scope)')['recordId'],Venda.objects.get().pk)
                self.assertNotEqual(grandchild['operation_id'],child['operation_id']);self.unchanged(other)
            finally:chrome.stop()
