import '/offline/assets/2-8f/sales.js';
import {operationDetails} from '/offline/assets/2-8f/operation-details.js';
import {revisionPresentation} from '/offline/assets/2-8f/sales-revisions.js';
import {Repository, openDB, saleOperationState, commandOf, hash, validReceipt} from '/offline/assets/2-8f/core.js';
import {draftScope, loadDraft, saveDraft, discardDraft, projectAssembly, prepareDraftSubmission, confirmDraftSubmission, releaseDraftSubmission, finalizeDraftOffline, releaseConfirmedDraft, lastConfirmedDraft, startNextOfflineDraft, lastLocalDraft, ambiguousSales} from '/offline/assets/2-8f/sales-drafts.js';

if (!window.salesDraftBridge) await new Promise(resolve => document.addEventListener('sales-draft-bridge-ready', resolve, {once:true}));
await window.salesOffline.ready;
const bridge = window.salesDraftBridge;
const box = document.createElement('div');
box.id = 'sales-draft-status';
box.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:4px 12px;font-size:12px;flex-shrink:0';
const message = document.createElement('span'); message.setAttribute('role', 'status');
const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Descartar rascunho';
discard.id = 'sales-draft-discard'; discard.disabled = true;
box.append(message, discard);
const officialLink = document.createElement('a');
officialLink.id = 'sales-official-sale-link'; officialLink.hidden = true;
officialLink.target = '_blank'; officialLink.rel = 'noopener noreferrer';
box.append(officialLink);
const diagnostics = document.createElement('div');
diagnostics.id = 'sales-operation-diagnostic'; diagnostics.style.width = '100%';
box.append(diagnostics);
const recover = document.createElement('button');
recover.type = 'button'; recover.id = 'sales-online-recover'; recover.hidden = true;
recover.textContent = 'Consultar resultado da venda'; box.append(recover);
recover.addEventListener('click', () => { void window.salesDraftUI.recoverOnline(); });
const nextSale = document.createElement('button');
nextSale.type = 'button'; nextSale.id = 'sales-new-offline-sale'; nextSale.textContent = 'Nova venda'; nextSale.hidden = true;
const queueCount = document.createElement('span'); queueCount.id = 'sales-pending-count';
box.append(nextSale,queueCount);
nextSale.addEventListener('click', () => { void window.salesDraftUI.startNextSale(); });
document.getElementById('layout-vendas').prepend(box);
box.hidden = !bridge.eligible;
let repo, scope, revision = 0, saved = '', blocked = false, suppress = true, ignoreReset = false;
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
const ambiguityControls = new Map();
const completionControls = new Map();
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-drafts') : null;
const observer = new MutationObserver(() => { if (!ignoreReset) schedule(0); });
const meaningful = assembly => assembly.cliente || assembly.operador || assembly.tipo_venda || assembly.itens.length || assembly.lancamento;
function announce() { channel?.postMessage({scope, revision}); }
function showError(error) { message.textContent = error.message || 'Não foi possível salvar o rascunho local. Último rascunho preservado.'; }
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
    for (const field of document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')) {
        if (!completionControls.has(field)) completionControls.set(field, field.disabled);
        field.disabled = true;
    }
    resetVisual(); discard.disabled = true;
    message.textContent = `Venda salva neste aparelho e aguardando sincronização. Sem número oficial. Referência ${marker.operation_id.slice(0,8)}. Será enviada manualmente quando a conexão estiver estável.`;
    offlineActions();
    void refreshCompletion().catch(showError);
}
async function refreshCompletion() {
    const generation = ++completionGeneration;
    const marker = localCompletion || historicalCompletion;
    if (!repo || !scope || !await checkIdentity()) return;
    const operations = await repo.all('operations');
    if (generation !== completionGeneration) return;
    const sales = operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id && op.environment_id === scope.environment_id);
    queueCount.textContent = `${sales.filter(op => op.status !== 'confirmada').length} venda(s) aguardando sincronização ou resolução.`;
    queueBlocked = ambiguousSales(operations,scope).length > 0;
    nextSale.hidden = true;
    if (!localCompletion) {
        if (queueBlocked) {
            for (const field of document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')) {
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
        if (queueBlocked) return;
    }
    if (!marker) return;
    const operation = await repo.get('operations', marker.operation_id);
    if (generation !== completionGeneration) return;
    if (operation && (operation.type !== 'criar_venda' || operation.actor_id !== scope.actor_id
        || operation.environment_id !== scope.environment_id || operation.device_id !== scope.device_id))
        throw new Error('Identidade da operação local incompatível. Dados preservados.');
    completionView = saleOperationState(operation);
    if (operation?.transport === 'online' && operation.status === 'enviando')
        completionView = {...completionView, label:'Enviando venda online. Confirmacao oficial ainda nao recebida.'};
    recover.hidden = !localCompletion || !operation || ['confirmada', 'conflito'].includes(operation.status);
    recover.disabled = recoveringOnline;
    const revisionView = await revisionPresentation(repo, operation, scope);
    if (generation !== completionGeneration) return;
    if (revisionView?.recordId && !completionView.recordId) completionView = {...completionView,recordId:revisionView.recordId,error:'',
        url:`/vendas/${revisionView.recordId}/`,label:revisionView.originalConfirmed ? 'Original confirmada oficialmente.'
            : `Conflito resolvido pela venda nº ${revisionView.recordId}. Venda corrigida confirmada. Se você enviou ao cliente informações da versão anterior, envie novamente a nota correta.`};
    const expanded = !!diagnostics.querySelector('details[open]');
    diagnostics.replaceChildren();
    const detail = operationDetails(operation, scope, revisionView);
    if (detail) { detail.open = expanded; diagnostics.append(detail); }
    message.textContent = `${completionView.label} Referência ${marker.operation_id.slice(0,8)}.`
        + (completionView.error ? ' ' + completionView.error : '');
    if (localCompletion && !completionView.recordId) {
        const payload = operation?.payload || {};
        const total = (payload.itens || []).reduce((sum, item) => sum + Number(item.quantidade) * Number(item.preco_unitario), 0);
        const transport = operation?.transport === 'online' ? 'online' : 'offline';
        message.textContent += ` Existe uma venda ${transport} pendente de resolução. Antes de continuar, revise e resolva essa venda. Cliente: ${operation?.original_labels?.cliente?.nome || payload.cliente_id || 'Não informado'}. Data: ${payload.data_venda || marker.finalized_at}. Total: ${total.toLocaleString('pt-BR', {style:'currency', currency:'BRL'})}. Status: ${completionView.status}. Consulte o resultado pelo botão abaixo. Para reenviar manualmente quando permitido, abra Status → Sincronizar agora.`;
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
    if (localCompletion && completionView.recordId && !queueBlocked) {
        const result = await releaseConfirmedDraft(repo, scope, marker.operation_id);
        if (generation !== completionGeneration) return;
        historicalCompletion = marker; localCompletion = null;
        window.salesOffline.completed = false;
        blocked = false; ignoreReset = false; saved = ''; revision = result.revision;
        for (const [field, disabled] of completionControls) field.disabled = disabled;
        completionControls.clear(); discard.disabled = true;
        document.getElementById('btnGravarVenda').textContent = 'Gravar Venda';
        document.getElementById('btnGravarVenda').disabled = false;
        document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
        window.atualizarSelectsBonitosVenda?.();
        channel?.postMessage({type:'draft-confirmed', scope, revision, operation_id:marker.operation_id});
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
    const assembly = bridge.capture(), fingerprint = JSON.stringify(assembly);
    if (fingerprint === saved || (!saved && !meaningful(assembly))) return pending;
    message.textContent = 'Salvando rascunho local…';
    pending = pending.then(async () => {
        if (blocked || fingerprint === saved) return;
        try {
            const result = await saveDraft(repo, scope, assembly, revision, replaceConfirmed);
            replaceConfirmed = false;
            revision = result.revision; saved = fingerprint;
            discard.disabled = false;
            message.textContent = 'Rascunho local salvo. Dados de referência a revalidar.';
            announce();
        } catch (error) {
            if (error.code === 'draft-finalized-offline') {
                const result = await loadDraft(repo, scope);
                if (result.finalization) { completed(result.finalization); return; }
            }
            if (error.message.includes('outra aba')) blocked = true;
            if (error.message.includes('já enviada')) blocked = true;
            if (error.message.includes('mudou')) { blocked = true; resetVisual(); }
            showError(error);
            window.salesDraftUI.lastError = error.message;
        }
    }).finally(offlineActions);
    return pending;
}
function resetVisual() {
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
        discard.disabled = true;
        officialLink.hidden = true;
        diagnostics.replaceChildren();
        return false;
    }
    return true;
}
window.salesDraftUI = {
    ready:false, flush, get revision() { return revision; }, get blocked() { return blocked; },
    get finalization() { return localCompletion; },
    refreshCompletion,
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
            revision = result.revision; blocked = false; ignoreReset = false; saved = '';
            resetVisual();
            for (const [field,disabled] of completionControls) field.disabled = disabled;
            completionControls.clear(); discard.disabled = true; nextSale.hidden = true;
            document.getElementById('btnGravarVenda').disabled = false;
            document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
            document.getElementById('btnGravarVenda').textContent = 'Gravar Venda';
            window.atualizarSelectsBonitosVenda?.();
            channel?.postMessage({type:'draft-next-offline',scope,revision,operation_id:marker.operation_id});
            await refreshCompletion();
            document.dispatchEvent(new Event('sales-completion-released'));
        } catch (error) { showError(error); }
        finally { startingNext = false; offlineActions(); }
    },
    async recoverOnline() {
        if (recoveringOnline || !localCompletion || !await checkIdentity()) return;
        recoveringOnline = true;
        recover.disabled = true;
        let confirmedId = null;
        try {
            const operation = await repo.get('operations', localCompletion.operation_id);
            if (!operation || await hash(commandOf(operation)) !== operation.payload_hash)
                throw new Error('Comando local divergente. Operacao preservada; revisao tecnica necessaria.');
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
            channel?.postMessage({type:'draft-finalized-offline', scope, revision, operation_id:result.finalization.operation_id});
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
            if (!(await revisionPresentation(repo,op,scope))?.recordId) return false;
        }
        return true;
    },
    async finalizeOffline(origin) {
        if (window.salesOffline.recovering || !window.salesOffline.active) return;
        if (finalizing) return finalizing;
        if (localCompletion) return {finalization:localCompletion, alreadyFinalized:true};
        if (!bridge.eligible || !scope || !repo) { bridge.error('Identidade local indisponível ou contexto de edição/Pedido. Rascunho preservado.'); return; }
        const editingControls = new Map(Array.from(document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button'), field => [field, field.disabled]));
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
                channel?.postMessage({type:'draft-finalized-offline', scope, revision, operation_id:result.finalization.operation_id});
                const queue = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
                queue?.postMessage({type:'changed', actor:scope.actor_id, environment:scope.environment_id}); queue?.close();
                document.dispatchEvent(new Event('offline-operations-changed'));
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
            revision = result.revision; saved = ''; discard.disabled = true;
            message.textContent = 'Venda online confirmada. Rascunho local removido.'; announce();
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
        if (identity?.environment_id === indicator.dataset.environment) scope = await draftScope(repo, identity);
        if (scope) {
            const result = await loadDraft(repo, scope);
            revision = result.revision;
            const previousLocal = await lastLocalDraft(repo, scope), previousOfficial = await lastConfirmedDraft(repo, scope);
            const previousLocalOperation = previousLocal && await repo.get('operations',previousLocal.operation_id);
            const previousOfficialOperation = previousOfficial && await repo.get('operations',previousOfficial.operation_id);
            historicalCompletion = previousLocal && (!previousOfficial || previousLocalOperation?.sequence > previousOfficialOperation?.sequence)
                ? previousLocal : previousOfficial;
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
                bridge.restore(result.draft);
                saved = JSON.stringify(bridge.capture()); discard.disabled = false;
                message.textContent = `Rascunho local restaurado. Última alteração: ${new Date(result.draft.atualizado_em).toLocaleString('pt-BR')}. Dados de referência a revalidar.`;
            }
        } else message.textContent = 'Autentique-se online para salvar rascunhos locais.';
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
    if (['dataVenda', 'vencimentoVenda', 'operadorVenda', 'tipoVenda', 'clienteBusca', 'quantidade', 'preco', 'unidade'].includes(event.target.id)) {
        ignoreReset = false; schedule();
    }
});
document.addEventListener('change', event => {
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
    if (scope && JSON.stringify(update?.scope) === JSON.stringify(scope) && update.type === 'draft-confirmed' && localCompletion?.operation_id === update.operation_id) {
        void refreshCompletion().catch(showError); return;
    }
    if (scope && JSON.stringify(update?.scope) === JSON.stringify(scope) && update.type === 'draft-next-offline' && localCompletion?.operation_id === update.operation_id) {
        // Adopt only an empty marker. A new draft belongs to whichever tab saved it.
        const current = await loadDraft(repo,scope);
        if (!current.draft && !current.finalization && current.revision === update.revision) {
            completionGeneration++; historicalCompletion = localCompletion; localCompletion = null;
            window.salesOffline.completed = false; revision = current.revision;
            blocked = false; ignoreReset = false; saved = ''; resetVisual();
            for (const [field,disabled] of completionControls) field.disabled = disabled;
            completionControls.clear(); discard.disabled = true;
            document.getElementById('btnGravarVenda').disabled = false;
            document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
            await refreshCompletion(); offlineActions(); return;
        }
    }
    if (scope && JSON.stringify(update?.scope) === JSON.stringify(scope) && update.revision > revision) {
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
        const result = await discardDraft(repo, scope, revision);
        revision = result.revision; saved = ''; resetVisual();
        ignoreReset = true; discard.disabled = true; message.textContent = 'Rascunho local descartado.'; announce();
    } catch (error) { showError(error); }
});
