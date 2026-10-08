"""IndexedDB real: conclusão atômica, multitab, falhas e reload sem servidor."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from estoque.models import Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_sales_drafts_browser as draft_tests
from .services import COMMAND_KEYS, validate_command

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class SaleFinalizationBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = draft_tests.SalesDraftBrowserTests.setUp
    open_sales = draft_tests.SalesDraftBrowserTests.open_sales
    reload = draft_tests.SalesDraftBrowserTests.reload
    select_customer = draft_tests.SalesDraftBrowserTests.select_customer
    add_product = draft_tests.SalesDraftBrowserTests.add_product
    repository = draft_tests.SalesDraftBrowserTests.repository

    def prepare(self, chrome):
        tab = self.open_sales(chrome)
        self.select_customer(tab, 'Cliente Comercial')
        self.add_product(tab, 'Produto Fracionado')
        self.repository(tab)
        tab.evaluate('window.app=await import([...document.scripts].find(s=>s.src.includes("/app.js")).src);await app.initialized;window.before=await drafts.loadDraft(repo,scope);window.sequenceBefore=(await repo.get("metadata","device")).sequence;true')
        return tab

    def finalize(self, tab, origin='undefined'):
        return tab.evaluate('await drafts.finalizeDraftOffline(repo,scope,{revision:before.revision,draft_id:before.draft.draft_id,origem_recebimento:' + origin + '})')

    def test_duas_conclusoes_concorrentes_hash_payload_scope_e_uma_sequence(self):
        models = [Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria, Produto, OperacaoSincronizacao]
        backend = {m: list(m.objects.order_by('pk').values()) for m in models}
        with TemporaryDirectory(prefix='sale-finalization-double-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                tab.evaluate('window.snapshotsBefore=await repo.all("snapshots");window.oldFetch=fetch;window.localFetches=0;window.fetch=()=>{localFetches++;throw Error("Servidor proibido")};window.parallel=await Promise.all([drafts.finalizeDraftOffline(repo,scope,{revision:before.revision,draft_id:before.draft.draft_id}),drafts.finalizeDraftOffline(new core.Repository(await core.openDB()),scope,{revision:before.revision,draft_id:before.draft.draft_id})]);true')
                results = tab.evaluate('parallel')
                self.assertEqual(results[0]['finalization']['operation_id'], results[1]['finalization']['operation_id'])
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore') + 1)
                operation = tab.evaluate('(await repo.all("operations"))[0]')
                command = {key: operation[key] for key in COMMAND_KEYS}
                validate_command({**command, 'payload_hash': operation['payload_hash']}, self.user, 'offline-isolated-tests')
                self.assertEqual(operation['aggregate_id'], operation['operation_id'])
                self.assertEqual(operation['operation_id'], tab.evaluate('before.draft.draft_id'))
                self.assertEqual(operation['status'], 'pendente')
                self.assertEqual(operation['actor_id'], str(self.user.pk))
                self.assertEqual(set(operation['payload']), {'schema_version','cliente_id','data_venda','data_vencimento','tipo_pagamento','operador','itens'})
                self.assertEqual(set(operation['payload']['itens'][0]), {'produto_id','quantidade','unidade','preco_unitario'})
                self.assertEqual(tab.evaluate('await core.hash(core.commandOf((await repo.all("operations"))[0]))'), operation['payload_hash'])
                self.assertEqual(tab.evaluate('localFetches'), 0)
                self.assertTrue(tab.evaluate('JSON.stringify(await repo.all("snapshots"))===JSON.stringify(snapshotsBefore)'))
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                self.assertEqual(self.finalize(tab)['finalization']['operation_id'], operation['operation_id'])
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore') + 1)
            finally:
                chrome.stop()
        self.assertEqual({m: list(m.objects.order_by('pk').values()) for m in models}, backend)

    def test_falhas_add_metadata_e_antes_commit_abortam_tudo_retry_seguro(self):
        with TemporaryDirectory(prefix='sale-finalization-abort-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                for mode in ['add', 'metadata', 'commit']:
                    tab.evaluate('window.mode=' + json.dumps(mode) + ';window.addBefore=IDBObjectStore.prototype.add;window.putBefore=IDBObjectStore.prototype.put;window.transactionBefore=repo.transaction;IDBObjectStore.prototype.add=function(value){if(mode==="add"&&this.name==="operations")throw new DOMException("Falha add","QuotaExceededError");return addBefore.call(this,value)};IDBObjectStore.prototype.put=function(value){if(mode==="metadata"&&value.tipo==="venda_concluida_offline")throw new DOMException("Falha marker","QuotaExceededError");return putBefore.call(this,value)};repo.transaction=function(stores,write,work){return transactionBefore.call(this,stores,write,(tx,done)=>work(tx,result=>{done(result);if(mode==="commit"&&write&&stores.includes("operations")){tx.objectStore("metadata").get("device").onsuccess=()=>tx.abort()}}))};true')
                    self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,scope,{revision:before.revision,draft_id:before.draft.draft_id});false}catch(e){true}finally{IDBObjectStore.prototype.add=addBefore;IDBObjectStore.prototype.put=putBefore;repo.transaction=transactionBefore}'))
                    self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                    self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore'))
                    self.assertTrue(tab.evaluate('JSON.stringify(await drafts.loadDraft(repo,scope))===JSON.stringify(before)'))
                result = self.finalize(tab)
                self.assertEqual(result['finalization']['operation_id'], tab.evaluate('before.draft.draft_id'))
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
            finally:
                chrome.stop()

    def test_multitab_aba_antiga_nao_ressuscita_e_ui_sem_fetch(self):
        with TemporaryDirectory(prefix='sale-finalization-tabs-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                other = self.open_sales(chrome)
                self.repository(other)
                other.evaluate('window.old=await drafts.loadDraft(repo,scope);true')
                tab.evaluate('document.getElementById("offline-global").dataset.connection="offline";await salesOffline.activate();await salesDraftUI.flush();window.fetchCount=0;window.originalFetch=fetch;window.fetch=()=>{fetchCount++;throw Error("Nao enviar")};btnGravarVenda.click();btnGravarVenda.click();true')
                tab.wait('!!salesDraftUI.finalization')
                other.wait('!!salesDraftUI.finalization')
                self.assertEqual(tab.evaluate('fetchCount'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore') + 1)
                self.assertTrue(other.evaluate('try{await drafts.saveDraft(repo,scope,old.draft,old.revision);false}catch(e){e.code==="draft-finalized-offline"}'))
                self.assertTrue(other.evaluate('try{await drafts.discardDraft(repo,scope,old.revision);false}catch(e){e.code==="draft-finalized-offline"}'))
                self.assertEqual(other.evaluate('salesDraftUI.finalization.operation_id'), tab.evaluate('salesDraftUI.finalization.operation_id'))
                self.assertTrue(tab.evaluate('btnGravarVenda.disabled && btnConfirmarFechamentoVenda.disabled && clienteBusca.disabled'))
                self.assertIn('Sem número oficial', tab.evaluate('document.getElementById("sales-draft-status").textContent'))
                tab.evaluate('window.fetch=originalFetch;true')
                self.reload(tab)
                self.repository(tab)
                self.assertEqual(tab.evaluate('document.querySelectorAll("#tabelaProdutos tr").length'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 1)
                self.assertTrue(tab.evaluate('btnGravarVenda.disabled && !!salesDraftUI.finalization'))
            finally:
                chrome.stop()
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)

    def test_reabrir_browser_preserva_operacao_e_indicador(self):
        with TemporaryDirectory(prefix='sale-finalization-restart-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                result = self.finalize(tab)
                operation_id = result['finalization']['operation_id']
            finally:
                chrome.stop()
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.open_sales(chrome)
                self.repository(tab)
                self.assertEqual(tab.evaluate('salesDraftUI.finalization.operation_id'), operation_id)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].operation_id'), operation_id)
                self.assertIsNone(tab.evaluate('(await drafts.loadDraft(repo,scope)).draft'))
                tab.wait('document.getElementById("offline-pending-badge").textContent==="1"')
            finally:
                chrome.stop()

    def test_vista_consumo_prazo_marker_online_e_validacao_minima(self):
        with TemporaryDirectory(prefix='sale-finalization-validation-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                for change in ['extra.itens=[]', 'extra.itens[0].produto_id=""', 'extra.data_venda=""', 'extra.tipo_venda=""', 'extra.itens[0].unidade=""']:
                    tab.evaluate('window.extra=structuredClone(before.draft);' + change + ';true')
                    # Missing ID is rejected by draft projection; invalid completion
                    # fields may be saved as editable draft but must not finalize.
                    self.assertTrue(tab.evaluate('try{const saved=await drafts.saveDraft(repo,scope,extra,(await drafts.loadDraft(repo,scope)).revision);await drafts.finalizeDraftOffline(repo,scope,{revision:saved.revision,draft_id:saved.draft.draft_id});false}catch(e){true}'))
                    tab.evaluate('window.before=await drafts.saveDraft(repo,scope,before.draft,(await drafts.loadDraft(repo,scope)).revision);true')
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                tab.evaluate('drafts.prepareDraftSubmission(scope,before.draft);true')
                self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,scope,{revision:before.revision,draft_id:before.draft.draft_id});false}catch(e){e.message.includes("envio online")}'))
                tab.evaluate('drafts.releaseDraftSubmission(scope,before.draft);true')
                for payment in ['À vista','consumo_proprio','A prazo']:
                    tab.evaluate('await repo.put("metadata",{key:"sales-identity",actor_id:scope.actor_id,environment_id:' + json.dumps(payment) + '});scope={...scope,environment_id:' + json.dumps(payment) + '};window.before=await drafts.saveDraft(repo,scope,{...before.draft,tipo_venda:' + json.dumps(payment) + '},0);true')
                    result = self.finalize(tab, '{caixa:"10.00",banco:"38.00"}' if payment == 'À vista' else 'undefined')
                    payload = result['operation']['payload']
                    self.assertEqual(payload['tipo_pagamento'], payment)
                    if payment == 'À vista':
                        self.assertEqual(payload['origem_recebimento'], {'caixa':'10.00','banco':'38.00'})
                    if payment == 'A prazo':
                        self.assertTrue(payload['data_vencimento'])
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 3)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), tab.evaluate('sequenceBefore') + 3)
            finally:
                chrome.stop()
        self.assertEqual(DespesaDiaria.objects.count(), 0)
        self.assertEqual(Venda.objects.count(), 0)

    def test_scope_e_sequence_concorrente_e_device_independente(self):
        with TemporaryDirectory(prefix='sale-finalization-sequence-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                for field, value in [('actor_id','"99999"'), ('environment_id','"other"'), ('device_id','crypto.randomUUID()')]:
                    self.assertTrue(tab.evaluate('try{await drafts.finalizeDraftOffline(repo,{...scope,' + field + ':' + value + '},{revision:before.revision,draft_id:before.draft.draft_id});false}catch(e){true}'))
                tab.evaluate('window.originalDigest=crypto.subtle.digest.bind(crypto.subtle);window.once=true;crypto.subtle.digest=async(...args)=>{if(once){once=false;await repo.identity()}return originalDigest(...args)};true')
                result = self.finalize(tab)
                self.assertEqual(result['operation']['sequence'], tab.evaluate('sequenceBefore') + 2)
                tab.evaluate('crypto.subtle.digest=originalDigest;window.deviceOld=await repo.get("metadata","device");await repo.put("metadata",{key:"device",id:crypto.randomUUID(),sequence:0});scope={...scope,device_id:(await repo.get("metadata","device")).id};window.before=await drafts.saveDraft(repo,scope,before.draft,0);true')
                next_result = self.finalize(tab)
                self.assertEqual(next_result['operation']['sequence'], 1)
                self.assertNotEqual(next_result['operation']['device_id'], result['operation']['device_id'])
            finally:
                chrome.stop()

    def test_shell_real_offline_vista_origem_sem_fetch_e_sem_autoenvio(self):
        with TemporaryDirectory(prefix='sale-finalization-shell-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = self.prepare(chrome)
                tab.evaluate('tipoVenda.value="À vista";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));await salesDraftUI.flush();await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                worker = chrome.worker(); worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                tab.call('Network.emulateNetworkConditions', dict(offline=True,latency=0,downloadThroughput=0,uploadThroughput=0))
                self.reload(tab)
                self.repository(tab)
                self.assertTrue(tab.evaluate('salesOffline.shell && salesOffline.active'))
                tab.evaluate('window.app=await import([...document.scripts].find(s=>s.src.includes("/app.js")).src);await app.initialized;window.realFetch=fetch;window.localFetches=0;window.fetch=()=>{localFetches++;throw Error("Sem backend")};btnGravarVenda.click();true')
                tab.wait('document.getElementById("vendaFechamentoOverlay").classList.contains("visivel")')
                tab.evaluate('document.getElementById("vendaOrigemCaixa").value="10,00";document.getElementById("vendaOrigemBanco").value="38,00";btnConfirmarFechamentoVenda.click();btnConfirmarFechamentoVenda.click();true')
                tab.wait('!!salesDraftUI.finalization')
                operation = tab.evaluate('(await repo.all("operations"))[0]')
                self.assertEqual(operation['payload']['origem_recebimento'], {'caixa':'10,00','banco':'38,00'})
                self.assertEqual(tab.evaluate('localFetches'), 0)
                # Online notification never submits operations; only the existing
                # manual synchronizer can be invoked explicitly in a later flow.
                tab.evaluate('window.posts=0;window.fetch=(url)=>{if(String(url).includes("observations"))posts++;return Promise.reject(Error("Teste offline"))};window.dispatchEvent(new Event("online"));true')
                self.assertEqual(tab.evaluate('posts'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("operations"))[0].status'), 'pendente')
                self.assertEqual(Venda.objects.count(), 0)
            finally:
                chrome.stop()
