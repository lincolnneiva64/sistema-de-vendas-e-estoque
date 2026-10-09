"""Sync manual real: recibo, resposta perdida, conflitos e fila mista."""
import copy
import json
import os
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from uuid import uuid4

from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from estoque.models import (
    Venda, ItemVenda, EventoVenda, ContaReceber, MovimentoFinanceiro,
    DespesaDiaria, Produto, EntregaRota, EntregaRotaItem,
)
from locacoes.models import FaixaPrecoLocacao, Locacao, TarefaOperacionalLocacao, EventoLocacao
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sales_drafts_browser as draft_tests
from .services import COMMAND_KEYS, process_operation

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class SaleSyncBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = draft_tests.SalesDraftBrowserTests.setUp
    select_customer = draft_tests.SalesDraftBrowserTests.select_customer
    add_product = draft_tests.SalesDraftBrowserTests.add_product
    repository = draft_tests.SalesDraftBrowserTests.repository
    reload = draft_tests.SalesDraftBrowserTests.reload

    def open_sales(self, chrome):
        tab = chrome.tab()
        tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': '''
            const wall=Date.now; window.offset=0; Date.now=()=>wall()+offset;
            window.timers=new Map();let timerId=0;
            window.setInterval=(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId};
            window.clearInterval=id=>timers.delete(id);
        '''})
        tab.call('Network.enable')
        tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Page.navigate', {'url':self.live_server_url+'/vendas/'})
        tab.wait('window.salesDraftUI?.ready')
        self.repository(tab)
        tab.evaluate('window.app=await import([...document.scripts].find(s=>s.src.includes("/app.js")).src);await app.initialized;window.realFetch=fetch;window.sent=[];window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};true')
        return tab

    def prepare(self, chrome, payment='A prazo', quantity='2'):
        tab = self.open_sales(chrome)
        self.select_customer(tab, 'Cliente Comercial')
        self.add_product(tab, 'Produto Fracionado', quantity)
        tab.evaluate('await (await import("/offline/assets/2-8f/commercial.js")).atualizarSnapshotComercial(repo,scope);true')
        tab.evaluate('tipoVenda.value='+json.dumps(payment)+';tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();window.fetch=(url,options)=>String(url)==="/api/offline/health/"?Promise.reject(TypeError("offline")):realFetch(url,options);await app.probe();offset+=5001;await app.probe();offset+=5001;await app.probe();await salesOffline.ready;await salesDraftUI.flush();window.snapshotsBefore=await repo.all("snapshots");true')
        origin = '{caixa:"10.00",banco:"38.00"}' if payment == 'À vista' else 'undefined'
        tab.evaluate('await salesDraftUI.finalizeOffline('+origin+');window.operation=(await repo.all("operations"))[0];true')
        tab.wait('!!salesDraftUI.finalization')
        tab.evaluate('window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};offset+=5001;await app.probe();true')
        self.assertIsNone(tab.evaluate('await repo.get("snapshots","pilot")'))
        return tab

    def stable(self, tab, ticks=30):
        tab.evaluate('for(let i=0;i<'+str(ticks)+';i++){offset+=30000;await app.probe()}true')

    def start_manual(self, tab):
        tab.evaluate('if(document.getElementById("offline-sync-modal").open)document.getElementById("offline-modal-cancel").click();await app.probe();true')
        tab.wait('!document.getElementById("offline-global-sync").disabled')
        tab.evaluate('document.getElementById("offline-global-sync").click();true')
        tab.wait('document.getElementById("offline-sync-modal").open && !document.getElementById("offline-modal-confirm").hidden')
        tab.evaluate('document.getElementById("offline-modal-confirm").click();true')

    def manual(self, tab):
        self.start_manual(tab)
        tab.wait('document.getElementById("offline-modal-confirm").hidden', timeout=15)

    def assert_command(self, operation, sent):
        self.assertEqual(sent, {**{key:operation[key] for key in COMMAND_KEYS}, 'payload_hash':operation['payload_hash']})

    def test_so_acao_manual_apos_900000_envia_sem_snapshot_de_tarefas_e_atualiza_abas(self):
        with TemporaryDirectory(prefix='sale-sync-manual-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                other = self.open_sales(chrome)
                for page in [tab, other]:
                    page.wait('salesDraftUI.blocked && salesOffline.completed && clienteBusca.disabled')
                    page.wait('document.getElementById("sales-draft-status").textContent.includes("Venda salva neste dispositivo. Aguardando sincronização.")')
                    self.assertTrue(page.evaluate('!document.getElementById("sales-new-offline-sale").hidden'))
                tab.evaluate('window.dispatchEvent(new Event("online"));await app.probe();true')
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                self.stable(tab, 29)
                self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").disabled'))
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.stable(tab, 1)
                self.assertEqual(tab.evaluate('sent.length'), 0)
                operation = tab.evaluate('operation')
                tab.evaluate('window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/"){sent.push(JSON.parse(options.body));const response=await realFetch(url,options);return new Promise(resolve=>window.releaseSync=()=>resolve(response))}return realFetch(url,options)};true')
                self.start_manual(tab)
                tab.wait('typeof window.releaseSync==="function"')
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("Enviando venda offline")')
                other.wait('document.getElementById("sales-draft-status").textContent.includes("Enviando venda offline")')
                self.assertTrue(tab.evaluate('document.getElementById("sales-official-sale-link").hidden'))
                tab.evaluate('releaseSync();true')
                tab.wait('document.getElementById("offline-modal-confirm").hidden')
                updated = tab.evaluate('await repo.get("operations",operation.operation_id)')
                self.assertEqual(updated['status'], 'confirmada')
                self.assertEqual(updated['attempts'], 1)
                self.assertEqual(updated['last_error'], '')
                self.assertGreater(len(tab.evaluate('snapshotsBefore')),0)
                self.assert_command(operation, tab.evaluate('sent[0]'))
                sale_id = updated['server_result']['record_id']
                self.assertTrue(Venda.objects.filter(pk=sale_id).exists())
                tab.wait('document.getElementById("sales-official-sale-link").getAttribute("href")==="/vendas/'+str(sale_id)+'/"')
                other.wait('document.getElementById("sales-draft-status").textContent.includes("Venda #'+str(sale_id)+' sincronizada")')
                for page in [tab, other]:
                    page.wait('!salesDraftUI.blocked && !salesDraftUI.finalization && !salesOffline.completed && !clienteBusca.disabled && !btnGravarVenda.disabled')
                    self.assertNotEqual(page.evaluate('btnGravarVenda.textContent'), 'Venda sincronizada')
                self.assertTrue(tab.evaluate('await drafts.releaseConfirmedDraft(repo,scope,operation.operation_id);!(await drafts.loadDraft(repo,scope)).finalization'))
                self.assertTrue(other.evaluate('try{await drafts.saveDraft(repo,scope,{...salesDraftBridge.capture(),itens:[]},0);false}catch(e){e.message.includes("outra aba")}'))
                self.assertEqual(tab.evaluate('document.getElementById("offline-pending-badge").textContent'), '0')
                self.assertEqual(tab.evaluate('(await repo.all("history"))[0].server_result'), updated['server_result'])
                self.assertEqual(updated['payload_hash'], operation['payload_hash'])
                self.assertEqual(updated['operation_id'], operation['operation_id'])
                self.assertTrue(tab.evaluate('JSON.stringify(await repo.all("snapshots"))===JSON.stringify(snapshotsBefore)'))
                self.reload(tab); self.repository(tab)
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("Venda #'+str(sale_id)+' sincronizada")')
                tab.wait('!salesDraftUI.blocked && !salesOffline.completed && !clienteBusca.disabled && !btnGravarVenda.disabled')
                self.assertTrue(tab.evaluate('!btnGravarVenda.disabled && !clienteBusca.disabled && !salesOffline.completed'))
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
            finally:
                chrome.stop()
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("Venda #'+str(sale_id)+' sincronizada")')
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.assertTrue(tab.evaluate('!btnGravarVenda.disabled && !clienteBusca.disabled && !salesDraftUI.blocked'))
            finally:
                chrome.stop()

    def test_resposta_perdida_apos_commit_retry_mesmo_comando_sem_duplica_efeitos(self):
        for payment in ['A prazo','À vista','consumo_proprio']:
            # Each iteration uses independent browser scope/UUID; counts below
            # compare all official effects before/after its own manual replay.
            Produto.objects.filter(pk=self.product.pk).update(quantidade=10)
            with TemporaryDirectory(prefix='sale-sync-lost-') as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = self.prepare(chrome, payment)
                    self.stable(tab)
                    tab.evaluate('window.lose=true;window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/"){sent.push(JSON.parse(options.body));const response=await realFetch(url,options);if(lose){lose=false;await response.text();throw Error("Resposta perdida apos commit")}return response}return realFetch(url,options)};true')
                    self.manual(tab)
                    operation = tab.evaluate('operation')
                    self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'), 'resultado_desconhecido')
                    self.assertEqual(OperacaoSincronizacao.objects.get(operation_id=operation['operation_id']).status, 'confirmada')
                    tab.wait('document.getElementById("sales-draft-status").textContent.includes("ainda não confirmado")')
                    self.assertTrue(tab.evaluate('salesDraftUI.blocked && salesOffline.completed && clienteBusca.disabled'))
                    models = [Venda, ItemVenda, EventoVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto]
                    before = {model: list(model.objects.order_by('pk').values()) for model in models}
                    self.manual(tab)
                    updated = tab.evaluate('await repo.get("operations",operation.operation_id)')
                    self.assertEqual(updated['status'], 'confirmada')
                    self.assertEqual(updated['attempts'], 1)
                    for sent in tab.evaluate('sent'):
                        self.assert_command(operation, sent)
                    self.assertEqual(len(tab.evaluate('sent')), 1)
                    self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)
                    official = OperacaoSincronizacao.objects.get(operation_id=operation['operation_id'])
                    self.assertEqual(updated['server_result'], official.resultado)
                    self.assertEqual(ItemVenda.objects.filter(venda_id=official.resultado['record_id']).count(), 1)
                    self.assertTrue(tab.evaluate('JSON.stringify(await repo.all("snapshots"))===JSON.stringify(snapshotsBefore)'))
                finally:
                    chrome.stop()
        self.assertEqual(Venda.objects.count(), 3)
        self.assertEqual(ContaReceber.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        self.assertEqual(DespesaDiaria.objects.count(), 1)

    def test_conflito_estoque_e_preco_persistem_na_ui_sem_retry_e_sem_html(self):
        for cause in ['estoque','preco']:
            Produto.objects.filter(pk=self.product.pk).update(quantidade=10, preco_compra=12)
            with TemporaryDirectory(prefix='sale-sync-conflict-') as profile:
                chrome = Chrome(CHROME, profile).start()
                try:
                    tab = self.prepare(chrome)
                    original = tab.evaluate('operation')
                    if cause == 'estoque':
                        Produto.objects.filter(pk=self.product.pk).update(quantidade=1)
                    else:
                        Produto.objects.filter(pk=self.product.pk).update(preco_compra=99)
                    self.stable(tab); self.manual(tab)
                    updated = tab.evaluate('await repo.get("operations",operation.operation_id)')
                    self.assertEqual(updated['status'], 'conflito')
                    self.assertEqual(updated['payload'], original['payload'])
                    self.assertIsNone(updated['server_result']['record_id'])
                    self.assertIn('insuficiente' if cause == 'estoque' else 'abaixo do custo', updated['last_error'])
                    tab.wait('document.getElementById("sales-draft-status").textContent.includes("precisa de revisão")')
                    self.assertTrue(tab.evaluate('document.getElementById("sales-official-sale-link").hidden'))
                    self.assertFalse(Venda.objects.exists())
                    self.assertTrue(tab.evaluate('document.getElementById("offline-global-sync").hidden'))
                    self.reload(tab); self.repository(tab)
                    tab.wait('document.getElementById("sales-draft-status").textContent.includes("precisa de revisão")')
                    self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'conflito')
                    tab.evaluate('window.dispatchEvent(new Event("online"));await salesDraftUI.refreshCompletion();true')
                    self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].attempts'), 1)
                    # Unsafe server text is rendered as text, never interpreted.
                    tab.evaluate('window.op=(await repo.all("operations"))[0];op.last_error="<img src=x onerror=window.injected=true>";await repo.put("operations",op);await salesDraftUI.refreshCompletion();true')
                    tab.wait('document.getElementById("sales-draft-status").textContent.includes("<img")')
                    self.assertFalse(tab.evaluate('!!document.querySelector("#sales-draft-status img") || !!window.injected'))
                finally:
                    chrome.stop()

    def test_receipts_falsos_status_hash_id_data_nao_confirmam(self):
        with TemporaryDirectory(prefix='sale-sync-receipt-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                cases = [
                    (200,{'operation_id':str(uuid4())}), (200,{'hash':'0'*64}), (200,{'record_id':0}),
                    (200,{'record_id':'123'}), (200,{'record_id':True}), (200,{'completed_at':'invalida'}),
                    (200,{'completed_at':'2026'}), (200,{'status':'conflito','record_id':None,'erro':'Falso'}),
                    (409,{}), (409,{'status':'conflito','record_id':None,'erro':''}),
                    (409,{'status':'conflito','record_id':None,'erro':'Falha','completed_at':None}),
                ]
                for status, changes in cases:
                    tab.evaluate('window.fakeStatus='+str(status)+';window.fake={operation_id:operation.operation_id,hash:operation.payload_hash,status:"confirmada",record_id:123,completed_at:new Date().toISOString(),...'+json.dumps(changes)+'};window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/"){sent.push(JSON.parse(options.body));return new Response(JSON.stringify(fake),{status:fakeStatus})}return realFetch(url,options)};true')
                    self.manual(tab)
                    self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'), 'resultado_desconhecido')
                    self.assertTrue(tab.evaluate('document.getElementById("sales-official-sale-link").hidden'))
                    self.assertEqual(tab.evaluate('(await repo.all("history")).length'), 0)
                self.assertFalse(Venda.objects.exists())
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
                operation = tab.evaluate('operation')
                for sent in tab.evaluate('sent'):
                    self.assert_command(operation, sent)
                self.reload(tab); self.repository(tab)
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("ainda não confirmado")')
            finally:
                chrome.stop()

    def test_400_auth_5xx_fetch_timeout_preservam_comando_recuperavel(self):
        with TemporaryDirectory(prefix='sale-sync-errors-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                original = tab.evaluate('operation')
                for status in [400,401,403,500,502,503]:
                    tab.evaluate('window.code='+str(status)+';window.fetch=async(url,options)=>{if(String(url)==="/api/offline/observations/"){sent.push(JSON.parse(options.body));return new Response("Erro",{status:code})}return realFetch(url,options)};true')
                    self.manual(tab)
                    op = tab.evaluate('await repo.get("operations",operation.operation_id)')
                    self.assertEqual(op['status'], 'erro' if status in [400,401,403] else 'resultado_desconhecido')
                    self.assertEqual(op['payload'], original['payload'])
                    self.assertIsNone(op['server_result'])
                for kind in ['fetch','timeout','json']:
                    tab.evaluate('window.kind='+json.dumps(kind)+';window.fetch=(url,options)=>{if(String(url)==="/api/offline/observations/"){sent.push(JSON.parse(options.body));if(kind==="timeout")return new Promise((resolve,reject)=>options.signal.addEventListener("abort",()=>reject(new DOMException("Timeout","AbortError"))));if(kind==="json")return Promise.resolve(new Response("not-json",{status:200}));return Promise.reject(Error("Fetch rejeitado"))}return realFetch(url,options)};true')
                    self.manual(tab)
                    self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'), 'resultado_desconhecido')
                for sent in tab.evaluate('sent'):
                    self.assert_command(original, sent)
                self.assertEqual(tab.evaluate('(await repo.all("history")).length'), 0)
                self.assertFalse(OperacaoSincronizacao.objects.exists())
                self.reload(tab); self.repository(tab)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'resultado_desconhecido')
                # Startup recovery of a browser closed during sending.
                tab.evaluate('window.op=(await repo.all("operations"))[0];await repo.put("operations",{...op,status:"enviando"});true')
                self.reload(tab); self.repository(tab)
                tab.wait('(await repo.all("operations"))[0].status==="resultado_desconhecido"')
            finally:
                chrome.stop()

    def test_duas_vendas_fila_mista_conflito_nao_impede_observacoes(self):
        faixa, _ = FaixaPrecoLocacao.objects.get_or_create(codigo='centro_perto',defaults={'nome':'Teste','preco_jogo_diaria':'10'})
        rental = Locacao.objects.create(tipo_pessoa='avulsa',pessoa_avulsa_nome='Teste',endereco_entrega='Teste',
            data_entrega=date.today(),horario_entrega=time(10),data_evento=date.today(),horario_evento=time(12),
            data_prevista_devolucao=date.today(),faixa_preco=faixa,faixa_preco_nome_snapshot=faixa.nome)
        task = TarefaOperacionalLocacao.objects.create(locacao=rental,tipo='entrega',data_agendada=date.today())
        delivery_sale = Venda.objects.create(data_venda=date.today())
        route = EntregaRota.objects.create(tipo='unitaria')
        delivery = EntregaRotaItem.objects.create(rota=route,venda=delivery_sale)
        with TemporaryDirectory(prefix='sale-sync-mixed-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome,quantity='1')
                second = tab.evaluate('(()=>{const op=structuredClone(operation);op.operation_id=crypto.randomUUID();op.aggregate_id=op.operation_id;return op})()')
                # Seed a second independent, already-concluded operation as a queue
                # fixture. No UI for editing/replacing the first tombstone is added.
                tab.evaluate('window.second='+json.dumps(second)+';second.sequence=(await repo.identity()).sequence;second.payload_hash=await core.hash(core.commandOf(second));await repo.put("operations",second);true')
                conflict = copy.deepcopy(second)
                conflict['operation_id'] = conflict['aggregate_id'] = str(uuid4())
                conflict['payload']['itens'][0]['quantidade'] = '100'
                tab.evaluate('window.bad='+json.dumps(conflict)+';bad.sequence=(await repo.identity()).sequence;bad.payload_hash=await core.hash(core.commandOf(bad));await repo.put("operations",bad);true')
                observations = [
                    ('observacao_operacional',str(task.pk),{'locacao_id':str(rental.pk),'tarefa_status':task.status,'observacao':'Fila mista'}),
                    ('observacao_entrega_venda',str(delivery.pk),{'rota_id':str(route.pk),'venda_id':str(delivery_sale.pk),'tarefa_status':delivery.status,'observacao':'Entrega mista'}),
                ]
                for kind, aggregate, payload in observations:
                    tab.evaluate('window.note={...core.commandOf(operation),operation_id:crypto.randomUUID(),type:'+json.dumps(kind)+',aggregate_id:'+json.dumps(aggregate)+',payload:'+json.dumps(payload)+'};note.sequence=(await repo.identity()).sequence;await repo.put("operations",{...note,payload_hash:await core.hash(note),status:"pendente",attempts:0,last_error:"",server_result:null});true')
                self.stable(tab); self.manual(tab)
                ops = tab.evaluate('await repo.all("operations")')
                sales = [op for op in ops if op['type']=='criar_venda' and op['status']=='confirmada']
                self.assertEqual(len(sales), 2)
                self.assertEqual(len({op['server_result']['record_id'] for op in sales}), 2)
                self.assertEqual([op['status'] for op in ops if op['operation_id']==conflict['operation_id']], ['conflito'])
                self.assertEqual(len([op for op in ops if op['type'].startswith('observacao') and op['status']=='confirmada']), 2)
                self.assertEqual(EventoLocacao.objects.filter(tipo='observacao_offline').count(), 1)
                self.assertEqual(EventoVenda.objects.filter(tipo_evento='observacao_offline').count(), 1)
                self.assertEqual(Venda.objects.exclude(pk=delivery_sale.pk).count(), 2)
                self.assertEqual(ContaReceber.objects.count(), 2)
                for op in sales:
                    self.assertEqual(op['server_result'], OperacaoSincronizacao.objects.get(operation_id=op['operation_id']).resultado)
                receipts = {op['operation_id']:op['server_result'] for op in sales}
                effects = {model:list(model.objects.order_by('pk').values()) for model in [Venda,ItemVenda,ContaReceber,EventoVenda,EventoLocacao,Produto]}
                tab.evaluate('for(const op of await repo.all("operations"))if(op.type==="criar_venda"&&op.status==="confirmada")await repo.put("operations",{...op,status:"resultado_desconhecido",server_result:null});true')
                self.manual(tab)
                for op in tab.evaluate('await repo.all("operations")'):
                    if op['operation_id'] in receipts:
                        self.assertEqual(op['server_result'], receipts[op['operation_id']])
                        self.assertEqual(op['attempts'], 1)
                    else:
                        self.assertEqual(op['attempts'], 1)
                self.assertEqual({model:list(model.objects.order_by('pk').values()) for model in effects},effects)
                self.assertEqual(tab.evaluate('document.getElementById("offline-pending-badge").textContent'), '1')
                tab.wait('document.getElementById("sales-draft-status").textContent.includes("Venda #'+str(next(op for op in sales if op['operation_id']==tab.evaluate('operation.operation_id'))['server_result']['record_id'])+' sincronizada")')
            finally:
                chrome.stop()

    def test_falha_render_apos_commit_local_nao_regride_receipt_confirmado(self):
        with TemporaryDirectory(prefix='sale-sync-render-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                tab.evaluate('window.baseRecord=core.Repository.prototype.record;window.baseCommunication=core.Repository.prototype.communication;window.failRead=false;core.Repository.prototype.record=function(...args){return baseRecord.apply(this,args).then(result=>{failRead=true;return result})};core.Repository.prototype.communication=function(...args){if(failRead){failRead=false;return Promise.reject(Error("Falha render apos commit local"))}return baseCommunication.apply(this,args)};true')
                self.manual(tab)
                op = tab.evaluate('await repo.get("operations",operation.operation_id)')
                self.assertEqual(op['status'],'confirmada')
                self.assertEqual(op['last_error'],'')
                self.assertEqual(op['server_result'],OperacaoSincronizacao.objects.get(operation_id=op['operation_id']).resultado)
                self.assertEqual(tab.evaluate('(await repo.all("history"))[0].server_result'),op['server_result'])
                tab.wait('document.getElementById("sales-official-sale-link").getAttribute("href")==="/vendas/'+str(op['server_result']['record_id'])+'/"')
                self.assertEqual(Venda.objects.count(),1)
            finally:
                chrome.stop()

    def test_identidade_online_diferente_preserva_fila_sem_post(self):
        with TemporaryDirectory(prefix='sale-sync-identity-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                for change in [ {'actor':{'id':'99999'}}, {'environment_id':'outro'}, {'protocol_version':2} ]:
                    tab.evaluate('window.mismatch='+json.dumps(change)+';window.fetch=async(url,options)=>{if(String(url)==="/api/offline/snapshot/")return new Response(JSON.stringify({actor:{id:scope.actor_id},environment_id:scope.environment_id,protocol_version:1,csrf_token:"unused",...mismatch}),{status:200});if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};true')
                    self.manual(tab)
                    self.assertEqual(tab.evaluate('sent.length'),0)
                    op=tab.evaluate('await repo.get("operations",operation.operation_id)')
                    self.assertEqual(op['status'],'pendente')
                    self.assertEqual(op['attempts'],0)
                    self.assertIsNone(op['server_result'])
                self.assertFalse(Venda.objects.exists())
            finally:
                chrome.stop()

    def test_lookup_absent_refresh_diagnostics_and_identical_manual_retry(self):
        with TemporaryDirectory(prefix='sale-lookup-absent-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                original = tab.evaluate('operation')
                tab.evaluate('await repo.diagnose(operation,"Resposta perdida");await salesDraftUI.refreshCompletion();true')
                tab.evaluate('document.querySelector("#sales-operation-diagnostic summary").click();true')
                self.assertTrue(tab.evaluate('document.querySelector("#sales-operation-diagnostic details").open'))
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.assertIn(original['operation_id'], tab.evaluate('document.getElementById("sales-operation-diagnostic").textContent'))
                self.assertIn(original['payload']['itens'][0]['preco_unitario'], tab.evaluate('document.getElementById("sales-operation-diagnostic").textContent'))
                clock_offset = tab.evaluate('offset')
                self.reload(tab); self.repository(tab)
                tab.evaluate('offset='+str(clock_offset)+';window.app=await import([...document.scripts].find(s=>s.src.includes("/app.js")).src);await app.initialized;window.realFetch=fetch;window.calls=[];window.sent=[];window.fetch=async(url,options)=>{calls.push(String(url));if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};true')
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'resultado_desconhecido')
                self.assertEqual(tab.evaluate('sent.length'), 0)
                self.stable(tab); self.manual(tab)
                calls = tab.evaluate('calls')
                lookup = next(i for i,url in enumerate(calls) if '/api/offline/operations/' in url)
                self.assertLess(lookup, calls.index('/api/offline/observations/'))
                self.assert_command(original, tab.evaluate('sent[0]'))
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'confirmada')
            finally:
                chrome.stop()

    def test_lookup_failures_never_post_and_persistence_failure_preserves_unknown(self):
        with TemporaryDirectory(prefix='sale-lookup-failures-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                tab.evaluate('await repo.diagnose(operation,"Incerto");true')
                cases = [(401,{}),(403,{}),(503,{}),(200,None),
                         (409,{'lookup':'incompativel'}),
                         (200,{'lookup':'encontrada','receipt':{}}),
                         (200,{'lookup':'encontrada','actor_id':'999'}),
                         (200,{'lookup':'encontrada','environment_id':'other'}),
                         (200,{'lookup':'encontrada','type':'other'}),
                         (200,{'lookup':'nao_encontrada','hash':'0'*64})]
                for status, body in cases:
                    tab.evaluate('window.lookupStatus='+str(status)+';window.lookupBody='+json.dumps(body)+';window.fetch=async(url,options)=>{if(String(url).includes("/api/offline/operations/"))return new Response(lookupBody===null?"invalid-json":JSON.stringify({operation_id:operation.operation_id,hash:operation.payload_hash,actor_id:operation.actor_id,environment_id:operation.environment_id,type:operation.type,device_id:operation.device_id,...lookupBody}),{status:lookupStatus});if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};true')
                    self.manual(tab)
                    self.assertEqual(tab.evaluate('sent.length'),0)
                    self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'),'resultado_desconhecido')
                tab.evaluate('window.fetch=realFetch;await repo.put("operations",{...operation,status:"pendente"});true')
                self.manual(tab)
                official = OperacaoSincronizacao.objects.get()
                tab.evaluate('await repo.put("operations",{...operation,status:"resultado_desconhecido"});window.core=await import("/offline/assets/2-8f/core.js");window.recordBefore=core.Repository.prototype.record;core.Repository.prototype.record=()=>Promise.reject(Error("IndexedDB indisponível"));window.posts=0;window.fetch=(url,options)=>{if(String(url)==="/api/offline/observations/")posts++;return realFetch(url,options)};true')
                self.manual(tab)
                self.assertEqual(tab.evaluate('posts'),0)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'),'resultado_desconhecido')
                tab.evaluate('core.Repository.prototype.record=recordBefore;true')
                self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].server_result'),official.resultado)
                tab.evaluate('await repo.diagnose(operation,"Resposta antiga");await repo.record(operation,{operation_id:operation.operation_id,hash:operation.payload_hash,status:"conflito",record_id:null,completed_at:new Date().toISOString(),erro:"Antigo"});true')
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'),'confirmada')
            finally:
                chrome.stop()

    def test_lookup_persisted_conflict_and_two_tabs_no_retry(self):
        with TemporaryDirectory(prefix='sale-lookup-conflict-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab=self.prepare(chrome); self.stable(tab)
                Produto.objects.filter(pk=self.product.pk).update(quantidade=0)
                self.manual(tab)
                receipt=OperacaoSincronizacao.objects.get().resultado
                tab.evaluate('await repo.put("operations",{...operation,status:"resultado_desconhecido"});true')
                other=self.open_sales(chrome)
                self.manual(tab)
                other.wait('document.getElementById("sales-draft-status").textContent.includes("precisa de revis")')
                self.assertEqual(tab.evaluate('sent.length'),1)
                self.assertEqual(other.evaluate('sent.length'),0)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].server_result'),receipt)
                self.assertFalse(Venda.objects.exists())
            finally:
                chrome.stop()

    def test_uuid_divergent_post_and_lookup_are_explicit_and_never_duplicate(self):
        with TemporaryDirectory(prefix='sale-lookup-divergent-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                operation = tab.evaluate('operation')
                command = {key:operation[key] for key in COMMAND_KEYS}
                process_operation(command, self.user)
                tab.evaluate('window.core=await import("/offline/assets/2-8f/core.js");operation.payload.itens[0].quantidade="3";operation.payload_hash=await core.hash(core.commandOf(operation));await repo.put("operations",operation);true')
                self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].diagnostic_code'), 'uuid_comando_divergente')
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'resultado_desconhecido')
                self.manual(tab)
                self.assertEqual(tab.evaluate('sent.length'), 1)
                self.assertEqual(Venda.objects.count(), 1)
                self.assertTrue(tab.evaluate('document.getElementById("sales-operation-diagnostic").textContent.includes("uuid_comando_divergente")'))
                tab.evaluate('operation.payload.itens[0].quantidade="4";await repo.put("operations",{...operation,status:"resultado_desconhecido"});window.lookups=0;window.lookupFetch=fetch;window.fetch=(url,options)=>{if(String(url).includes("/api/offline/operations/"))lookups++;return lookupFetch(url,options)};true')
                self.manual(tab)
                self.assertEqual(tab.evaluate('lookups'),0)
                self.assertEqual(tab.evaluate('sent.length'),1)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].diagnostic_code'),'comando_local_divergente')
            finally:
                chrome.stop()

    def test_mixed_batch_lookup_failure_keeps_unknown_and_sends_other_operation(self):
        with TemporaryDirectory(prefix='sale-lookup-mixed-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome); self.stable(tab)
                tab.evaluate('await repo.diagnose(operation,"Incerto");window.core=await import("/offline/assets/2-8f/core.js");window.second={...core.commandOf(operation),operation_id:crypto.randomUUID(),sequence:operation.sequence+1};second.aggregate_id=second.operation_id;await repo.put("operations",{...second,payload_hash:await core.hash(second),status:"pendente",attempts:0});window.fetch=async(url,options)=>{if(String(url).includes("/api/offline/operations/"))return new Response("Falha",{status:503});if(String(url)==="/api/offline/observations/")sent.push(JSON.parse(options.body));return realFetch(url,options)};true')
                self.manual(tab)
                self.assertEqual(tab.evaluate('(await repo.get("operations",operation.operation_id)).status'),'resultado_desconhecido')
                self.assertEqual(tab.evaluate('(await repo.get("operations",second.operation_id)).status'),'confirmada')
                self.assertEqual(tab.evaluate('sent.map(op=>op.operation_id)'),[tab.evaluate('second.operation_id')])
                self.assertEqual(Venda.objects.count(),1)
            finally:
                chrome.stop()

    def test_versioned_modules_bypass_stale_unversioned_core_cache(self):
        with TemporaryDirectory(prefix='sale-lookup-cache-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                tab.evaluate('window.cache=await caches.open("offline-pilot-shell-v28-2-8f");await cache.put("/static/offline/core.js",new Response("throw Error(\\"Stale core\\")",{headers:{"Content-Type":"application/javascript"}}));true')
                self.reload(tab)
                tab.evaluate('window.core=await import("/offline/assets/2-8f/core.js");window.repo=new core.Repository(await core.openDB());true')
                self.assertTrue(tab.evaluate('typeof core.Repository.prototype.diagnose==="function" && typeof core.Repository.prototype.updateAttempt==="function"'))
                self.assertTrue(tab.evaluate('[...document.scripts].some(s=>s.src.includes("/offline/assets/2-8f/app.js"))'))
                self.assertTrue(tab.evaluate('performance.getEntriesByType("resource").some(e=>e.name.includes("/offline/assets/2-8f/core.js"))'))
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'),'pendente')
            finally:
                chrome.stop()
