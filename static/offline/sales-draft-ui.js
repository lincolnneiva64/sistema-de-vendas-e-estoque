import '/offline/assets/2-8g-auth-link/sales.js';
import {operationDetails} from '/offline/assets/2-8g-close/operation-details.js';
import {revisionPresentation} from '/offline/assets/2-8g-close/sales-revisions.js';
import {Repository, openDB, saleOperationState, commandOf, hash, validReceipt} from '/offline/assets/2-8f/core.js';
import {draftScope, loadDraft, saveDraft, discardDraft, projectAssembly, prepareDraftSubmission, confirmDraftSubmission, releaseDraftSubmission, finalizeDraftOffline, releaseConfirmedDraft, lastConfirmedDraft, startNextOfflineDraft, lastLocalDraft, ambiguousSales, listRecoverableDrafts, recoverDraft} from '/offline/assets/2-8g-close/sales-drafts.js';
import {readStock,validateStock} from '/offline/assets/2-8g-close/sales-stock.js';
import {closedLocally,closureKey,mirrorClosure} from '/offline/assets/2-8g-close/sales-closures.js';

if (!window.salesDraftBridge) await new Promise(resolve => document.addEventListener('sales-draft-bridge-ready', resolve, {once:true}));
await window.salesOffline.ready;
const bridge = window.salesDraftBridge;
const box = document.createElement('div');
box.id = 'sales-draft-status';
box.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:4px 12px;font-size:12px;flex-shrink:0';
const message = document.createElement('span'); message.setAttribute('role', 'status');
const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Descartar rascunho';
discard.id = 'sales-draft-discard'; discard.disabled = true; discard.hidden = true;
box.append(message, discard);
const officialLink = document.createElement('a');
officialLink.id = 'sales-official-sale-link'; officialLink.hidden = true;
officialLink.target = '_blank'; officialLink.rel = 'noopener noreferrer';
box.append(officialLink);
const diagnostics = document.createElement('div');
diagnostics.id = 'sales-operation-diagnostic'; diagnostics.style.width = '100%';
box.append(diagnostics);
const deviceSales = document.createElement('details');
deviceSales.id = 'sales-device-operations';
const deviceSalesSummary = document.createElement('summary');
deviceSalesSummary.textContent = 'Vendas registradas neste dispositivo';
const deviceSalesList = document.createElement('div');
deviceSalesList.setAttribute('aria-live', 'polite');
deviceSales.append(deviceSalesSummary, deviceSalesList);
box.append(deviceSales);
const recover = document.createElement('button');
recover.type = 'button'; recover.id = 'sales-online-recover'; recover.hidden = true;
recover.textContent = 'Consultar resultado da venda'; box.append(recover);
recover.addEventListener('click', () => { void window.salesDraftUI.recoverOnline(); });
const nextSale = document.createElement('button');
nextSale.type = 'button'; nextSale.id = 'sales-new-offline-sale'; nextSale.textContent = 'Nova venda'; nextSale.hidden = true;
const queueCount = document.createElement('span'); queueCount.id = 'sales-pending-count';
queueCount.hidden = true;
const dateNote = document.createElement('span'); dateNote.id = 'sales-draft-date-note'; dateNote.hidden = true;
const recovery = document.createElement('details'); recovery.id = 'sales-draft-recovery';
recovery.hidden = true;
const recoverySummary = document.createElement('summary'); recoverySummary.textContent = 'Recuperar outro rascunho';
const recoveryPanel = document.createElement('div');
const recoverySelect = document.createElement('select'); recoverySelect.id = 'sales-draft-recovery-select';
recoverySelect.setAttribute('aria-label', 'Rascunhos disponíveis para recuperação');
const recoveryRefresh = document.createElement('button'); recoveryRefresh.type = 'button';
recoveryRefresh.textContent = 'Atualizar lista';
const recoveryButton = document.createElement('button'); recoveryButton.type = 'button';
recoveryButton.id = 'sales-draft-recovery-button';
recoveryButton.textContent = 'Recuperar selecionado'; recoveryButton.disabled = true;
recoveryPanel.append(recoverySelect, recoveryRefresh, recoveryButton); recovery.append(recoverySummary, recoveryPanel);
box.append(nextSale,queueCount,dateNote,recovery);
nextSale.addEventListener('click', () => { void window.salesDraftUI.startNextSale(); });
document.getElementById('layout-vendas').prepend(box);
box.hidden = !bridge.eligible;
let repo, scope, revision = 0, saved = '', activeDraftId = null, blocked = false, suppress = true, ignoreReset = false;
let timer, pending = Promise.resolve(), submitted = null;
let replaceConfirmed = false;
let preparedSubmission = false;
let localCompletion = null, finalizing = null;
let completionView = null;
let completionGeneration = 0;
let recoveringOnline = false;
let queriedConfirmationId = null;
let historicalCompletion = null;
let queueBlocked = false, startingNext = false;
let stockView = null;
let hasCurrentDraft = false, recoveryCandidates = [];
const navigation = '#btnConsultarVendas, [data-sales-navigation], #sales-draft-status, #modal-operador-obrigatorio';
const editingFields = () => [...document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')]
    .filter(field => !field.closest(navigation));
const ambiguityControls = new Map();
const completionControls = new Map();
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-drafts') : null;
const tabChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-draft-tabs') : null;
const instanceId = crypto.randomUUID();
const tabIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let currentTabId = null, probingTabId = null, tabCollision = false, releaseTabLock = null;
tabChannel?.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.instance_id === instanceId) return;
    if (data.type === 'probe' && data.tab_id === currentTabId) {
        if (probingTabId === currentTabId) tabCollision = true;
        tabChannel.postMessage({type:'occupied',tab_id:currentTabId,instance_id:instanceId});
    } else if (data.type === 'occupied' && data.tab_id === probingTabId) tabCollision = true;
});
async function claimTabId() {
    let tabId;
    try { tabId = sessionStorage.getItem('sales-draft-tab-id'); }
    catch { throw new Error('Não foi possível identificar esta aba para preservar o rascunho.'); }
    if (!tabIdPattern.test(tabId || '')) tabId = crypto.randomUUID();
    if (tabChannel) {
        currentTabId = tabId; probingTabId = tabId; tabCollision = false;
        tabChannel.postMessage({type:'probe',tab_id:tabId,instance_id:instanceId});
        await new Promise(resolve => setTimeout(resolve, 140));
        probingTabId = null;
        if (tabCollision) tabId = crypto.randomUUID();
    }
    if (navigator.locks) {
        let acquired = false;
        while (!acquired) {
            acquired = await new Promise((resolve, reject) => {
                navigator.locks.request('sales-draft-tab:' + tabId, {ifAvailable:true}, lock => {
                    resolve(!!lock);
                    if (!lock) return;
                    return new Promise(done => { releaseTabLock = done; });
                }).catch(reject);
            });
            if (!acquired) tabId = crypto.randomUUID();
        }
    }
    currentTabId = tabId;
    try { sessionStorage.setItem('sales-draft-tab-id', tabId); }
    catch { throw new Error('Não foi possível preservar a identidade desta aba. O rascunho foi mantido.'); }
    window.addEventListener('pagehide', event => {
        if (!event.persisted) releaseTabLock?.();
    }, {once:true});
    return tabId;
}
const observer = new MutationObserver(() => { if (!ignoreReset) schedule(0); });
const meaningful = assembly => assembly.cliente || assembly.operador || assembly.tipo_venda || assembly.itens.length || assembly.lancamento;
function announce(type = 'draft-updated', operationId = null, draftId = activeDraftId) {
    channel?.postMessage({type,scope,revision,tab_id:scope?.tab_id,draft_id:draftId,operation_id:operationId});
}
function showError(error) { message.textContent = error.message || 'Não foi possível salvar o rascunho local. Último rascunho preservado.'; }
function setDiscardEnabled(enabled) {
    discard.disabled = !enabled;
    discard.hidden = !enabled;
}
function renderDeviceSales(operations) {
    deviceSalesList.replaceChildren();
    deviceSales.hidden = operations.length === 0;
    deviceSalesSummary.textContent = `Vendas registradas neste dispositivo (${operations.length})`;
    for (const operation of [...operations].sort((left, right) => right.sequence - left.sequence)) {
        const row = document.createElement('div');
        const state = saleOperationState(operation);
        const payload = operation.payload || {};
        const total = (payload.itens || []).reduce((sum, item) =>
            sum + Number(item.quantidade) * Number(item.preco_unitario), 0);
        const customer = operation.original_labels?.cliente?.nome || payload.cliente_id || 'Cliente não informado';
        const summary = document.createElement('span');
        summary.textContent = `${state.label} ${customer} — ${payload.data_venda || operation.created_at} — `
            + `${total.toLocaleString('pt-BR', {style:'currency', currency:'BRL'})} — Ref. ${operation.operation_id}`;
        row.append(summary);
        if (state.recordId) {
            const link = document.createElement('a');
            link.href = state.url; link.textContent = ` Abrir venda #${state.recordId}`;
            link.target = '_blank'; link.rel = 'noopener noreferrer';
            row.append(link);
        } else if (ambiguousSales([operation], scope).length) {
            const query = document.createElement('button');
            query.type = 'button'; query.textContent = 'Consultar resultado';
            query.disabled = recoveringOnline;
            query.addEventListener('click', () => { void window.salesDraftUI.recoverOnline(operation.operation_id); });
            row.append(query);
        }
        deviceSalesList.append(row);
    }
}
function updateRecoveryButton() {
    const candidate = recoveryCandidates.find(item => item.key === recoverySelect.value);
    recoveryButton.disabled = !candidate || candidate.blocked || hasCurrentDraft || !!localCompletion
        || !!bridge.capture().itens.length || !!bridge.capture().cliente || !!bridge.capture().lancamento;
}
async function refreshRecoveryList() {
    if (!repo || !scope) return;
    recoveryCandidates = await listRecoverableDrafts(repo, scope);
    recovery.hidden = recoveryCandidates.length === 0;
    recoverySelect.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = ''; placeholder.textContent = recoveryCandidates.length
        ? 'Selecione um rascunho preservado' : 'Nenhum rascunho disponível';
    recoverySelect.append(placeholder);
    for (const item of recoveryCandidates) {
        const option = document.createElement('option');
        option.value = item.key;
        const total = Number(item.total || 0).toLocaleString('pt-BR', {style:'currency',currency:'BRL'});
        option.textContent = `${item.customer || 'Cliente não informado'} — ${item.item_count} item(ns) — ${total} — ${new Date(item.updated_at).toLocaleString('pt-BR')}`
            + (item.legacy ? ' — rascunho legado' : ' — transferir de outra aba')
            + (item.blocked ? ` — bloqueado: ${item.reason}` : '');
        option.disabled = item.blocked;
        recoverySelect.append(option);
    }
    const singleCandidate = recoveryCandidates.length === 1 ? recoveryCandidates[0] : null;
    if (singleCandidate) recoverySelect.value = singleCandidate.key;
    recoverySummary.textContent = singleCandidate
        ? singleCandidate.legacy ? 'Rascunho antigo preservado' : 'Rascunho em outra aba'
        : 'Recuperar outro rascunho';
    recoverySelect.hidden = !!singleCandidate;
    recoveryRefresh.hidden = !!singleCandidate;
    recoveryButton.textContent = singleCandidate ? 'Recuperar rascunho' : 'Recuperar selecionado';
    updateRecoveryButton();
}
recoveryRefresh.addEventListener('click', () => { void refreshRecoveryList().catch(showError); });
recoverySelect.addEventListener('change', updateRecoveryButton);
recoveryButton.addEventListener('click', async () => {
    const candidate = recoveryCandidates.find(item => item.key === recoverySelect.value);
    if (!candidate || candidate.blocked) return;
    try {
        await pending;
        if (!candidate.legacy && !await bridge.confirmRecover(false)) return;
        if (hasCurrentDraft || bridge.capture().itens.length || bridge.capture().cliente || bridge.capture().lancamento)
            throw new Error('Esta aba já contém dados de venda. Salve ou conclua essa montagem antes de recuperar outra.');
        const result = await recoverDraft(repo, scope, candidate);
        revision = result.revision; activeDraftId = result.draft.draft_id; hasCurrentDraft = true;
        bridge.restore(result.draft);
        saved = JSON.stringify(bridge.capture());
        setDiscardEnabled(true);
        message.textContent = result.source_preserved
            ? 'Rascunho legado recuperado nesta aba; a cópia original foi preservada. Revise os itens antes de concluir.'
            : 'Rascunho recuperado nesta aba. Revise os itens antes de concluir.';
        dateNote.textContent = 'Rascunho restaurado. Data preservada: ' + result.draft.data_venda + '.';
        dateNote.hidden = false; recovery.open = false;
        announce(); await refreshRecoveryList();
    } catch (error) { showError(error); }
});
function offlineActions() {
    if (!nextSale.hidden) nextSale.disabled = startingNext || !!finalizing;
    if (!window.salesOffline.active && !localCompletion && !queueBlocked) return;
    const button = document.getElementById('btnGravarVenda');
    const label = localCompletion ? completionView?.recordId ? 'Venda sincronizada'
        : completionView?.status === 'conflito' ? 'Venda offline em revisão' : 'Venda offline pendente' : 'Salvar venda offline';
    const disabled = !!localCompletion || !!finalizing || blocked || queueBlocked || !scope || !bridge.eligible;
    if (button.textContent !== label) button.textContent = label;
    if (button.disabled !== disabled) button.disabled = disabled;
    const confirm = document.getElementById('btnConfirmarFechamentoVenda');
    if (confirm.disabled !== disabled) confirm.disabled = disabled;
}
function completed(marker) {
    localCompletion = marker;
    window.salesOffline.completed = true;
    blocked = true; ignoreReset = true; clearTimeout(timer);
    revision = marker.revision;
    activeDraftId = marker.draft_id;
    for (const field of editingFields()) {
        if (!completionControls.has(field)) completionControls.set(field, field.disabled);
        field.disabled = true;
    }
    resetVisual(); setDiscardEnabled(false);
    message.textContent = `Venda salva neste aparelho e aguardando sincronização. Sem número oficial. Referência ${marker.operation_id.slice(0,8)}. Será enviada manualmente quando a conexão estiver estável.`;
    offlineActions();
    void refreshCompletion().catch(showError);
}
async function refreshCompletion() {
    const generation = ++completionGeneration;
    if (!repo || !scope || !await checkIdentity()) return;
    const operations = await repo.all('operations');
    const nextStock = await readStock(repo,scope);
    if (generation !== completionGeneration) return;
    stockView = nextStock;
    const sales = operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id && op.environment_id === scope.environment_id);
    const deviceOperations = sales.filter(op => op.device_id === scope.device_id);
    renderDeviceSales(deviceOperations);
    if (!localCompletion) {
        const latest = [...deviceOperations].sort((left, right) => right.sequence - left.sequence)[0];
        historicalCompletion = latest ? {operation_id:latest.operation_id, draft_id:latest.operation_id,
            finalized_at:latest.created_at, historical:true} : null;
    }
    const marker = localCompletion || historicalCompletion;
    const pendingCount = sales.filter(op => op.status !== 'confirmada' && !closedLocally(nextStock.closures,op)).length;
    queueCount.textContent = `${pendingCount} venda(s) aguardando sincronização ou resolução.`;
    queueCount.hidden = pendingCount === 0;
    queueBlocked = ambiguousSales(operations,scope).length > 0;
    nextSale.hidden = true;
    if (!localCompletion) {
        if (queueBlocked) {
            for (const field of editingFields()) {
                if (!ambiguityControls.has(field)) ambiguityControls.set(field,field.disabled);
                field.disabled = true;
            }
            message.textContent = 'Há venda com resultado desconhecido ou envio em andamento. Montagem preservada. Abra Status para consultar ou sincronizar manualmente quando permitido.';
            message.setAttribute('role','alert');
        } else if (ambiguityControls.size) {
            for (const [field,disabled] of ambiguityControls) field.disabled = disabled;
            ambiguityControls.clear();
            message.textContent = 'Resultado anterior resolvido. Montagem preservada; você pode continuar.';
            message.setAttribute('role','status');
        }
        offlineActions();
        if (queueBlocked && !marker) return;
    }
    if (!marker) return;
    const operation = await repo.get('operations', marker.operation_id);
    if (generation !== completionGeneration) return;
    if (operation && (operation.type !== 'criar_venda' || operation.actor_id !== scope.actor_id
        || operation.environment_id !== scope.environment_id || operation.device_id !== scope.device_id))
        throw new Error('Identidade da operação local incompatível. Dados preservados.');
    completionView = saleOperationState(operation);
    const closure=nextStock.closures?.find(row=>row.key===closureKey(operation))?.receipt;
    const closed=closedLocally(nextStock.closures,operation);
    if(closed)completionView={status:'encerrada_sem_venda',recordId:null,url:null,error:'',label:'Encerrada administrativamente — nenhuma venda gerada. Histórico preservado.'};
    if (operation?.transport === 'online' && operation.status === 'enviando')
        completionView = {...completionView, label:'Enviando venda online. Confirmacao oficial ainda nao recebida.'};
    recover.hidden = !operation || !ambiguousSales([operation],scope).length;
    recover.disabled = recoveringOnline;
    const revisionView = await revisionPresentation(repo, operation, scope);
    if (generation !== completionGeneration) return;
    if (revisionView?.recordId && !completionView.recordId) completionView = {...completionView,recordId:revisionView.recordId,error:'',
        url:`/vendas/${revisionView.recordId}/`,label:revisionView.originalConfirmed ? 'Original confirmada oficialmente.'
            : `Conflito resolvido pela venda nº ${revisionView.recordId}. Venda corrigida confirmada. Se você enviou ao cliente informações da versão anterior, envie novamente a nota correta.`};
    const expanded = !!diagnostics.querySelector('details[open]');
    diagnostics.replaceChildren();
    const detail = operationDetails(operation, scope, revisionView,closure);
    if (detail) { detail.open = expanded; diagnostics.append(detail); }
    message.textContent = `${completionView.label} Referência ${marker.operation_id.slice(0,8)}.`
        + (completionView.error ? ' ' + completionView.error : '');
    if (localCompletion && !completionView.recordId && !closed) {
        const payload = operation?.payload || {};
        const total = (payload.itens || []).reduce((sum, item) => sum + Number(item.quantidade) * Number(item.preco_unitario), 0);
        const transport = operation?.transport === 'online' ? 'online' : 'offline';
        message.textContent += ` Existe uma venda de origem ${transport} pendente de resolução. A conexão atual é indicada em Status. Cliente: ${operation?.original_labels?.cliente?.nome || payload.cliente_id || 'Não informado'}. Data: ${payload.data_venda || marker.finalized_at}. Total: ${total.toLocaleString('pt-BR', {style:'currency', currency:'BRL'})}. Status: ${completionView.status}. `
            + (completionView.status === 'conflito' ? 'Use Revisar venda no diagnóstico abaixo. Consultar Vendas e navegação continuam disponíveis; a montagem permanece protegida até a resolução.'
                : 'Consulte o resultado pelo botão abaixo. Para reenviar manualmente quando permitido, abra Status → Sincronizar agora.');
        message.setAttribute('role', 'alert');
    } else message.setAttribute('role', 'status');
    const canStartNext = localCompletion?.conclusion_mode === 'offline' && operation?.transport === 'offline'
        && operation.status === 'pendente' && operation.attempts === 0 && !operation.server_result && !queueBlocked;
    if (canStartNext) {
        message.textContent = `Venda salva neste dispositivo. Aguardando sincronização. Sem número oficial. Ainda não existe oficialmente no servidor. Identificação local: ${marker.operation_id}.`;
        message.setAttribute('role','status');
        nextSale.hidden = false; nextSale.disabled = startingNext || !!finalizing;
        recover.hidden = true;
    }
    officialLink.hidden = !completionView.recordId;
    if (completionView.recordId) {
        officialLink.href = completionView.url;
        officialLink.textContent = `Ver venda #${completionView.recordId}`;
    } else { officialLink.removeAttribute('href'); officialLink.textContent = ''; }
    if(localCompletion && closed && !queueBlocked){
        const result=await mirrorClosure(repo,scope,operation,closure);
        const current=await loadDraft(repo,scope);
        if(generation!==completionGeneration)return;
        if(result.released || !current.draft && !current.finalization && !current.confirmation){
            historicalCompletion=marker;localCompletion=null;window.salesOffline.completed=false;
            blocked=false;ignoreReset=false;saved='';activeDraftId=null;hasCurrentDraft=false;revision=current.revision;
            for(const [field,disabled] of completionControls)field.disabled=disabled;
            completionControls.clear();setDiscardEnabled(false);
            document.getElementById('btnGravarVenda').textContent='Gravar Venda';
            document.getElementById('btnGravarVenda').disabled=false;document.getElementById('btnConfirmarFechamentoVenda').disabled=false;
            announce('draft-confirmed',marker.operation_id,marker.draft_id);
            document.dispatchEvent(new Event('sales-completion-released'));
        }else message.textContent+=' Montagem posterior ou revisão local preservada; reabra a tela para conferir.';
    }
    if (localCompletion && completionView.recordId && !queueBlocked) {
        const result = await releaseConfirmedDraft(repo, scope, marker.operation_id);
        if (generation !== completionGeneration) return;
        historicalCompletion = marker; localCompletion = null;
        window.salesOffline.completed = false;
        blocked = false; ignoreReset = false; saved = ''; activeDraftId = null; hasCurrentDraft = false; revision = result.revision;
        for (const [field, disabled] of completionControls) field.disabled = disabled;
        completionControls.clear(); setDiscardEnabled(false);
        document.getElementById('btnGravarVenda').textContent = 'Gravar Venda';
        document.getElementById('btnGravarVenda').disabled = false;
        document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
        window.atualizarSelectsBonitosVenda?.();
        announce('draft-confirmed',marker.operation_id,marker.draft_id);
        document.dispatchEvent(new Event('sales-completion-released'));
    }
    offlineActions();
}
function schedule(delay = 250) {
    offlineActions();
    if (suppress || ignoreReset || blocked || queueBlocked || !scope || !bridge.eligible) return;
    clearTimeout(timer);
    timer = setTimeout(() => { void flush(); }, delay);
}
function flush() {
    offlineActions();
    clearTimeout(timer);
    if (suppress || ignoreReset || blocked || queueBlocked || !scope || !bridge.eligible) return pending;
    if (!saved && meaningful(bridge.capture())) {
        bridge.ensureNewDate?.();
        dateNote.textContent = 'Data da nova montagem: ' + bridge.capture().data_venda + '.'; dateNote.hidden = false;
    }
    const assembly = bridge.capture(), fingerprint = JSON.stringify(assembly);
    if (fingerprint === saved || (!saved && !meaningful(assembly))) return pending;
    message.textContent = 'Salvando rascunho local…';
    pending = pending.then(async () => {
        if (blocked || fingerprint === saved) return;
        try {
            const result = await saveDraft(repo, scope, assembly, revision, replaceConfirmed);
            replaceConfirmed = false;
            revision = result.revision; saved = fingerprint; activeDraftId = result.draft.draft_id;
            hasCurrentDraft = true;
            setDiscardEnabled(true);
            message.textContent = 'Rascunho local salvo. Dados de referência a revalidar.';
            announce();
        } catch (error) {
            if (error.code === 'draft-finalized-offline') {
                const result = await loadDraft(repo, scope);
                if (result.finalization) { completed(result.finalization); return; }
            }
            if (error.message.includes('outra aba')) blocked = true;
            if (error.message.includes('já enviada')) blocked = true;
            if (error.message.includes('mudou')) blocked = true;
            showError(error);
            window.salesDraftUI.lastError = error.message;
        }
    }).finally(offlineActions);
    return pending;
}
function resetVisual() {
    dateNote.hidden = true;
    suppress = true;
    bridge.clear(); observer.takeRecords();
    suppress = false;
}
async function checkIdentity() {
    if (!scope || !repo) return false;
    const identity = await repo.get('metadata', 'sales-identity'), device = await repo.get('metadata', 'device');
    if (identity?.actor_id !== scope.actor_id || identity?.environment_id !== scope.environment_id || device?.id !== scope.device_id) {
        blocked = true; clearTimeout(timer); resetVisual();
        completionGeneration++;
        message.textContent = 'Identidade mudou. Rascunho anterior preservado e montagem limpa.';
        setDiscardEnabled(false);
        officialLink.hidden = true;
        diagnostics.replaceChildren();
        return false;
    }
    return true;
}
window.salesDraftUI = {
    ready:false, flush, get revision() { return revision; }, get blocked() { return blocked; },
    get finalization() { return localCompletion; },
    get lastDeviceSale() { return historicalCompletion; },
    refreshCompletion,
    validateStockInclusion(item) {
        if (!window.salesOffline.active) return true;
        try {
            const assembly = bridge.capture();
            const items = assembly.itens.filter((_,index) => index !== assembly.lancamento?.indice_edicao);
            validateStock(stockView,scope,[...items,item],[...assembly.itens,item]);
            return true;
        } catch (error) { showError(error); bridge.error(error.message); return false; }
    },
    async startNextSale() {
        if (startingNext || finalizing || !localCompletion || !await checkIdentity()) return;
        startingNext = true; nextSale.disabled = true;
        const marker = localCompletion;
        try {
            await pending;
            const result = await startNextOfflineDraft(repo,scope,marker.operation_id,revision);
            completionGeneration++;
            historicalCompletion = marker; localCompletion = null;
            submitted = null;
            window.salesOffline.completed = false;
            revision = result.revision; blocked = false; ignoreReset = false; saved = ''; activeDraftId = null; hasCurrentDraft = false;
            resetVisual();
            for (const [field,disabled] of completionControls) field.disabled = disabled;
            completionControls.clear(); setDiscardEnabled(false); nextSale.hidden = true;
            document.getElementById('btnGravarVenda').disabled = false;
            document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
            document.getElementById('btnGravarVenda').textContent = 'Gravar Venda';
            window.atualizarSelectsBonitosVenda?.();
            announce('draft-next-offline',marker.operation_id,marker.draft_id);
            activeDraftId = null;
            await refreshCompletion();
            document.dispatchEvent(new Event('sales-completion-released'));
        } catch (error) { showError(error); }
        finally { startingNext = false; offlineActions(); }
    },
    async recoverOnline(operationId = null) {
        const marker = localCompletion || historicalCompletion;
        operationId ||= marker?.operation_id;
        if (recoveringOnline || !operationId || !await checkIdentity()) return;
        recoveringOnline = true;
        recover.disabled = true;
        let confirmedId = null;
        try {
            const operation = await repo.get('operations', operationId);
            if (!operation || await hash(commandOf(operation)) !== operation.payload_hash)
                throw new Error('Comando local divergente. Operacao preservada; revisao tecnica necessaria.');
            if (operation.actor_id !== scope.actor_id || operation.environment_id !== scope.environment_id
                || operation.device_id !== scope.device_id || !ambiguousSales([operation], scope).length)
                throw new Error('Esta operação não está aguardando consulta. Nenhum envio foi iniciado.');
            const query = new URLSearchParams({actor_id:operation.actor_id, environment_id:operation.environment_id,
                type:operation.type, device_id:operation.device_id, hash:operation.payload_hash});
            const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
            let response, result;
            try { response = await fetch(`/api/vendas/online/${operation.operation_id}/?${query}`,
                {credentials:'same-origin', cache:'no-store', signal:controller.signal});
                result = await response.json(); }
            finally { clearTimeout(timer); }
            if (!response.ok || result.operation_id !== operation.operation_id || result.hash !== operation.payload_hash)
                throw new Error('Consulta indisponivel ou incompatível. Venda preservada; autentique-se e consulte novamente.');
            if (result.lookup === 'nao_encontrada') {
                message.textContent = 'Operacao ainda nao encontrada. O envio anterior pode estar em andamento. Identidade preservada; consulte novamente ou use Status → Sincronizar agora quando permitido.';
                return;
            }
            if (result.lookup !== 'encontrada' || result.actor_id !== scope.actor_id || result.environment_id !== scope.environment_id
                || result.device_id !== operation.device_id || result.type !== operation.type || !validReceipt(result.receipt, operation))
                throw new Error('Resultado da consulta invalido. Venda preservada.');
            await repo.record(operation, result.receipt);
            if (result.receipt.status === 'confirmada') {
                confirmedId = result.receipt.record_id;
                queriedConfirmationId = operation.operation_id;
            }
            await refreshCompletion();
            document.dispatchEvent(new Event('offline-operations-changed'));
            operationChannel?.postMessage({type:'changed', actor:scope.actor_id, environment:scope.environment_id});
        } catch (error) {
            message.textContent = confirmedId
                ? `Venda #${confirmedId} confirmada oficialmente. Falha local; reabra a tela. Nao repita a venda.`
                : 'Resultado da venda ainda desconhecido. ' + error.message + ' Consulte novamente; nao repita a venda.';
        } finally { recoveringOnline = false; recover.disabled = false; }
    },
    async submitOnline(origin, csrf) {
        if (!scope || !repo || !bridge.eligible || blocked || queueBlocked || localCompletion || !navigator.locks)
            throw new Error('Gravacao protegida indisponivel. Reabra a tela em navegador compativel; montagem preservada.');
        return navigator.locks.request('offline-pilot-sync', {ifAvailable:true}, async lock => {
            if (!lock) throw new Error('Outra aba esta enviando. Aguarde e consulte o resultado.');
            await flush();
            if (!await checkIdentity() || blocked) throw new Error('Identidade ou montagem mudou. Dados preservados.');
            const current = await loadDraft(repo, scope);
            if (!current.draft || current.revision !== revision
                || JSON.stringify(projectAssembly(bridge.capture())) !== JSON.stringify(projectAssembly(current.draft)))
                throw new Error('Montagem nao persistida ou atualizada em outra aba. Reabra a tela.');
            const result = await finalizeDraftOffline(repo, scope, {revision, draft_id:current.draft.draft_id,
                origem_recebimento:origin, reference_snapshot:window.salesOffline.referenceSnapshot, transport:'online'});
            completed(result.finalization);
            submitted = null;
            activeDraftId = result.finalization.draft_id;
            announce('draft-finalized-offline',result.finalization.operation_id);
            if (result.alreadyFinalized) return null;
            const operation = await repo.updateAttempt(result.operation, {status:'enviando', attempts:1, transport:'online'});
            if (!operation) return null;
            await refreshCompletion();
            let persisted = false, officialData = null;
            try {
                const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
                let response, data;
                try { response = await fetch('/api/vendas/online/', {method:'POST', credentials:'same-origin', cache:'no-store',
                    signal:controller.signal, headers:{'Content-Type':'application/json', 'X-CSRFToken':csrf},
                    body:JSON.stringify({...commandOf(operation), payload_hash:operation.payload_hash})});
                    data = await response.json(); }
                finally { clearTimeout(timer); }
                if (!validReceipt(data.receipt, operation, response.status)) throw new Error('Resposta do envio indisponivel ou invalida.');
                await repo.record(operation, data.receipt); persisted = true; officialData = data;
                await refreshCompletion();
                document.dispatchEvent(new Event('offline-operations-changed'));
                operationChannel?.postMessage({type:'changed', actor:scope.actor_id, environment:scope.environment_id});
                // A query may already have released this assembly while the POST
                // response was in flight. Do not clear a newly edited assembly.
                if (queriedConfirmationId === operation.operation_id) return null;
                return data.sucesso && data.receipt.status === 'confirmada' ? data : null;
            } catch (error) {
                if (persisted) {
                    message.textContent = officialData.receipt.status === 'confirmada'
                        ? `Venda #${officialData.receipt.record_id} confirmada oficialmente. Falha ao liberar a montagem local; reabra a tela. Nao repita a venda.`
                        : 'Conflito oficial preservado. Reabra a tela para revisar a venda.';
                    return officialData.sucesso ? officialData : null;
                }
                if (!persisted) await repo.diagnose(operation, 'Resultado online desconhecido. ' + error.message);
                await refreshCompletion();
                if (saleOperationState(await repo.get('operations', operation.operation_id)).recordId) return null;
                if (!persisted) message.textContent = 'Resultado da venda desconhecido. Nao repita a venda. Use Consultar resultado da venda; reenvio somente por Status → Sincronizar agora quando permitido.';
                return null;
            }
        });
    },
    async canRecoverOnline() {
        if (!this.ready || !bridge.eligible || blocked || queueBlocked || localCompletion || finalizing || preparedSubmission || submitted || ignoreReset) return false;
        if (!await checkIdentity()) return false;
        await flush();
        const current = await loadDraft(repo, scope);
        if (blocked || localCompletion || finalizing || current.finalization || current.confirmation || current.revision !== revision) return false;
        const operations = await repo.all('operations');
        for (const op of operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id
            && op.environment_id === scope.environment_id && op.status !== 'confirmada')) {
            if(closedLocally(await repo.all('metadata'),op))continue;
            if (!(await revisionPresentation(repo,op,scope))?.recordId) return false;
        }
        return true;
    },
    async finalizeOffline(origin) {
        if (window.salesOffline.recovering || !window.salesOffline.active) return;
        if (finalizing) return finalizing;
        if (localCompletion) return {finalization:localCompletion, alreadyFinalized:true};
        if (!bridge.eligible || !scope || !repo) { bridge.error('Identidade local indisponível ou contexto de edição/Pedido. Rascunho preservado.'); return; }
        const editingControls = new Map(editingFields().map(field => [field, field.disabled]));
        for (const [field, disabled] of editingControls) if (!completionControls.has(field)) completionControls.set(field, disabled);
        finalizing = (async () => {
            try {
                await flush();
                await checkIdentity();
                if (blocked || queueBlocked) throw new Error('Montagem bloqueada. Consulte o resultado anterior antes de concluir.');
                const current = await loadDraft(repo, scope);
                if (current.finalization) { completed(current.finalization); return {finalization:current.finalization, alreadyFinalized:true}; }
                if (current.revision !== revision || JSON.stringify(projectAssembly(bridge.capture())) !== JSON.stringify(projectAssembly(current.draft)))
                    throw new Error('Não foi possível persistir a montagem atual. Rascunho anterior preservado.');
                const result = await finalizeDraftOffline(repo, scope, {
                    revision, draft_id:current.draft?.draft_id, origem_recebimento:origin,
                    reference_snapshot:window.salesOffline.referenceSnapshot,
                });
                completed(result.finalization);
                activeDraftId = result.finalization.draft_id;
                announce('draft-finalized-offline',result.finalization.operation_id);
                const queue = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
                queue?.postMessage({type:'changed', actor:scope.actor_id, environment:scope.environment_id}); queue?.close();
                document.dispatchEvent(new Event('offline-operations-changed'));
                await refreshCompletion();
                return result;
            } catch (error) { showError(error); bridge.error(error.message); window.salesDraftUI.lastError = error.message; }
            finally {
                finalizing = null;
                if (!localCompletion) for (const [field, disabled] of editingControls) field.disabled = disabled;
                offlineActions();
            }
        })();
        for (const field of editingControls.keys()) field.disabled = true;
        offlineActions();
        return finalizing;
    },
    async restoreReference(assembly) {
        try {
            await checkIdentity();
            if (blocked) return;
            suppress = true;
            bridge.restore(projectAssembly(assembly)); observer.takeRecords();
        } catch (error) { showError(error); }
        finally { suppress = false; }
    },
    restoreOnlineReference(assembly) {
        if (blocked || localCompletion || finalizing || submitted) throw new Error('Montagem local protegida.');
        suppress = true;
        try { bridge.restore(projectAssembly(assembly)); observer.takeRecords(); }
        finally { suppress = false; }
    },
    async beforeSubmit() {
        if (!bridge.eligible) return true;
        await flush();
        let record = null;
        if (scope && repo && !blocked) {
            try {
                const current = await loadDraft(repo, scope);
                if (current.confirmation) blocked = true;
                record = current.draft;
                if (current.revision !== revision) blocked = true;
            } catch (error) { blocked = true; showError(error); }
        }
        if (blocked) { bridge.error('Rascunho atualizado em outra aba. Reabra a tela antes de concluir.'); return false; }
        if (document.querySelector('#tabelaProdutos tr[data-draft-review]')) {
            bridge.error('Há produto do rascunho fora do catálogo atual. Revise ou remova o item antes de concluir.'); return false;
        }
        submitted = {revision, fingerprint:JSON.stringify(bridge.capture()), record};
        return true;
    },
    prepareSubmission() {
        if (scope && submitted?.record) {
            prepareDraftSubmission(scope, submitted.record);
            preparedSubmission = true;
        }
    },
    confirmSubmission(vendaId) {
        preparedSubmission = false;
        ignoreReset = true; clearTimeout(timer);
        if (scope && submitted?.record) confirmDraftSubmission(scope, submitted.record, vendaId);
    },
    failedSubmission() {
        if (preparedSubmission && scope && submitted?.record) {
            releaseDraftSubmission(scope, submitted.record);
            preparedSubmission = false;
        }
    },
    async confirmedSuccess() {
        if (!scope || !bridge.eligible || !submitted) return;
        clearTimeout(timer); await pending;
        ignoreReset = true;
        if (submitted.fingerprint !== JSON.stringify(bridge.capture()) || submitted.revision !== revision) {
            message.textContent = 'Venda confirmada. Alterações posteriores do rascunho foram preservadas.'; return;
        }
        try {
            const result = await discardDraft(repo, scope, submitted.revision);
            revision = result.revision; saved = ''; activeDraftId = null; hasCurrentDraft = false; setDiscardEnabled(false);
            message.textContent = 'Venda online confirmada. Rascunho local removido.';
            announce('draft-confirmed',null,submitted.record?.draft_id);
        } catch (error) {
            message.textContent = 'Venda online confirmada. Rascunho local preservado porque a limpeza não foi concluída: ' + error.message;
        }
    },
};
try {
    if (bridge.eligible) {
        repo = new Repository(await openDB());
        const indicator = document.getElementById('offline-global');
        const identity = window.salesOffline.shell ? await repo.get('metadata', 'sales-identity')
            : {actor_id:indicator.dataset.actor, environment_id:indicator.dataset.environment};
        if (identity?.environment_id === indicator.dataset.environment) scope = await draftScope(repo, identity, await claimTabId());
        if (scope) {
            const result = await loadDraft(repo, scope);
            revision = result.revision;
            const previousLocal = await lastLocalDraft(repo, scope), previousOfficial = await lastConfirmedDraft(repo, scope);
            const previousLocalOperation = previousLocal && await repo.get('operations',previousLocal.operation_id);
            const previousOfficialOperation = previousOfficial && await repo.get('operations',previousOfficial.operation_id);
            historicalCompletion = previousLocal && (!previousOfficial || previousLocalOperation?.sequence > previousOfficialOperation?.sequence)
                ? previousLocal : previousOfficial;
            const previousClosure=await repo.get('metadata','rascunho_venda:'+JSON.stringify([scope.environment_id,scope.actor_id,scope.device_id,scope.tab_id])+':last-closure');
            if(previousClosure){const closedOp=await repo.get('operations',previousClosure.operation_id),recent=historicalCompletion&&await repo.get('operations',historicalCompletion.operation_id);
                if(!recent || closedOp?.sequence>recent.sequence)historicalCompletion=previousClosure;}
            if (result.finalization) completed(result.finalization);
            else if (historicalCompletion) await refreshCompletion();
            if (result.confirmation) {
                replaceConfirmed = result.confirmation.estado === 'concluido';
                if (!replaceConfirmed) blocked = true;
                message.textContent = result.confirmation.estado === 'concluido'
                    ? 'Venda online confirmada. Rascunho residual não restaurado.'
                    : 'Rascunho de envio anterior não restaurado. Confira a venda online antes de repetir.';
            }
            if (result.draft && !result.confirmation) {
                activeDraftId = result.draft.draft_id;
                hasCurrentDraft = true;
                bridge.restore(result.draft);
                saved = JSON.stringify(bridge.capture()); setDiscardEnabled(true);
                message.textContent = `Rascunho restaurado. Data da venda preservada: ${result.draft.data_venda}.`;
            }
        } else message.textContent = 'Autentique-se online para salvar rascunhos locais.';
        if (scope) {
            window.salesDraftUI.scope = scope;
            await refreshRecoveryList();
        }
    }
} catch (error) { blocked = !!scope; showError(error); }
observer.observe(document.getElementById('tabelaProdutos'), {subtree:true, childList:true, attributes:true, characterData:true});
suppress = false;
window.salesDraftUI.ready = true;
await refreshCompletion().catch(showError);
offlineActions();
window.salesDraftResolve?.();
document.addEventListener('sales-offline-active', offlineActions);
new MutationObserver(offlineActions).observe(document.getElementById('btnGravarVenda'), {attributes:true, attributeFilter:['disabled'], childList:true});
document.addEventListener('sales-draft-change', () => schedule(0));
document.addEventListener('input', event => {
    if (event.target.id === 'dataVenda' && !localCompletion) {
        dateNote.textContent = 'Data informada para esta montagem: ' + event.target.value + '.'; dateNote.hidden = false;
    }
    if (['dataVenda', 'vencimentoVenda', 'operadorVenda', 'tipoVenda', 'clienteBusca', 'produtoBusca', 'quantidade', 'preco', 'unidade'].includes(event.target.id)) {
        ignoreReset = false; schedule();
    }
});
document.addEventListener('change', event => {
    if (event.target.id === 'dataVenda' && !localCompletion) {
        dateNote.textContent = 'Data informada para esta montagem: ' + event.target.value + '.'; dateNote.hidden = false;
    }
    if (['dataVenda', 'vencimentoVenda', 'operadorVenda', 'tipoVenda', 'produto', 'unidade'].includes(event.target.id)) {
        ignoreReset = false; schedule(0);
    }
});
for (const eventName of ['click', 'keydown']) document.addEventListener(eventName, event => {
    if (event.target.closest('#btnAdicionarItemVenda, #produtoBusca, #quantidade, #unidade, #preco, #clienteBusca')) ignoreReset = false;
}, true);
document.addEventListener('visibilitychange', () => { if (document.hidden) void flush(); });
window.addEventListener('pagehide', () => { if (!ignoreReset) void flush(); });
window.addEventListener('focus', () => { void checkIdentity(); });
window.addEventListener('focus', () => { void refreshCompletion().catch(showError); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshCompletion().catch(showError); });
document.addEventListener('offline-operation-updated', () => { void refreshCompletion().catch(showError); });
const operationChannel = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
if (operationChannel) operationChannel.onmessage = event => {
    if (scope && event.data?.type === 'changed' && event.data.actor === scope.actor_id
        && event.data.environment === scope.environment_id) void refreshCompletion().catch(showError);
};
const identityChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-identity') : null;
if (identityChannel) identityChannel.onmessage = () => { void checkIdentity(); };
if (channel) channel.onmessage = async event => {
    const update = event.data;
    const sameDraft = scope && update?.tab_id === scope.tab_id && update?.draft_id === activeDraftId
        && JSON.stringify(update?.scope) === JSON.stringify(scope);
    if (sameDraft && update.type === 'draft-confirmed' && localCompletion?.operation_id === update.operation_id) {
        void refreshCompletion().catch(showError); return;
    }
    if (sameDraft && update.type === 'draft-next-offline' && localCompletion?.operation_id === update.operation_id) {
        // Adopt only an empty marker. A new draft belongs to whichever tab saved it.
        const current = await loadDraft(repo,scope);
        if (!current.draft && !current.finalization && current.revision === update.revision) {
            completionGeneration++; historicalCompletion = localCompletion; localCompletion = null;
            window.salesOffline.completed = false; revision = current.revision;
            blocked = false; ignoreReset = false; saved = ''; resetVisual();
            for (const [field,disabled] of completionControls) field.disabled = disabled;
            completionControls.clear(); setDiscardEnabled(false);
            document.getElementById('btnGravarVenda').disabled = false;
            document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
            await refreshCompletion(); offlineActions(); return;
        }
    }
    if (sameDraft && update.revision > revision) {
        const current = await loadDraft(repo, scope);
        if (current.finalization) { completed(current.finalization); return; }
        blocked = true; clearTimeout(timer);
        message.textContent = 'Rascunho atualizado em outra aba. Reabra a tela para carregar a versão salva.';
    }
};
discard.addEventListener('click', async () => {
    if (!await bridge.confirmDiscard()) return;
    await pending;
    try {
        clearTimeout(timer);
        const discardedDraftId = activeDraftId;
        const result = await discardDraft(repo, scope, revision);
        revision = result.revision; saved = ''; activeDraftId = null; hasCurrentDraft = false; resetVisual();
        ignoreReset = true; setDiscardEnabled(false); message.textContent = 'Rascunho local descartado.';
        announce('draft-discarded',null,discardedDraftId);
    } catch (error) { showError(error); }
});
