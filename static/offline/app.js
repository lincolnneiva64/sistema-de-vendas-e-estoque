import {POLICY, CONNECTION_POLICY, connectionStatus, Repository, Stability, openDB, commandOf, hash, validHealth, validReceipt, indicatorState} from '/offline/assets/2-8f/core.js';
import {renderIndicator, formatOfflineTime, timeText, operationTimes} from '/offline/assets/2-8f/presentation.js';
import {operationDetails} from '/offline/assets/2-8g-close/operation-details.js';
import {revisionPresentation} from '/offline/assets/2-8g-close/sales-revisions.js';
import {closedLocally,closureKey,installClosureUI,mirrorClosure} from '/offline/assets/2-8g-close/sales-closures.js';

const pilot = !!document.getElementById('offline-pilot');
const globalIndicator = document.getElementById('offline-global');
const globalSync = document.getElementById('offline-global-sync');
let notice = null, remoteSync = null;
const checkingOperations = new Set();
let renderedHistoryKey = null;
let authenticated = false;
let salesIdentity = null;
let healthEnvironment = location.host;
const badge = document.getElementById('offline-status');
let repo, snapshot, csrf = '', inFlight, syncing = false, saving = false, preparing = false, syncOpening = false, stopped = false, progress = '', probePending = null, sessionGeneration = 0, sessionPending = null, leaving = false;
let observationGeneration = 0, observedSince = null;
let probeGeneration = -1, probeStartedAt = null, healthController = null, monitorTimer = null, confirmationTimer = null;
const stability = new Stability();
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
const modal = document.createElement('dialog');
modal.id = 'offline-sync-modal';
modal.className = 'offline-sync-modal';
modal.setAttribute('aria-labelledby', 'offline-modal-title');
modal.innerHTML = `<h2 id="offline-modal-title">Sincronizar operações?</h2><p id="offline-modal-count"></p><p id="offline-modal-text" role="status" aria-live="polite"></p><div class="offline-modal-actions"><button type="button" id="offline-modal-cancel">Agora não</button><button type="button" id="offline-modal-confirm">Sincronizar agora</button></div>`;
document.body.append(modal);
const modalText = modal.querySelector('#offline-modal-text');
const modalConfirm = modal.querySelector('#offline-modal-confirm');
const modalCancel = modal.querySelector('#offline-modal-cancel');
let modalResolve = null;
function closeModal() {
    if (syncing) return;
    modal.close(); modalResolve?.(false); modalResolve = null;
}
modalCancel.addEventListener('click', closeModal);
modal.addEventListener('cancel', event => { event.preventDefault(); closeModal(); });
modalConfirm.addEventListener('click', () => {
    if (!modalResolve) return;
    modalConfirm.disabled = true; modalCancel.disabled = true;
    const resolve = modalResolve; modalResolve = null; resolve(true);
});
function confirmSynchronization(count) {
    modal.querySelector('#offline-modal-title').textContent = 'Sincronizar operações?';
    modal.querySelector('#offline-modal-count').textContent = count + (count === 1 ? ' operação aguardando' : ' operações aguardando');
    modalText.textContent = 'A conexão está estável. Resultados desconhecidos serão consultados primeiro. Se não encontrados, o mesmo comando será reenviado nesta sincronização manual. As demais operações pendentes serão enviadas ao servidor.';
    modalConfirm.hidden = false; modalConfirm.disabled = false;
    modalCancel.disabled = false; modalCancel.textContent = 'Agora não';
    if (!modal.open) modal.showModal();
    return new Promise(resolve => { modalResolve = resolve; });
}
function finishModal(text) {
    modalText.textContent = text;
    modalConfirm.hidden = true; modalCancel.disabled = false; modalCancel.textContent = 'Fechar';
}
function busyButton(button, busy, label) {
    if (!button) return;
    button.textContent = label;
    button.setAttribute('aria-busy', String(busy));
}
function syncButton(button, disabled, count, active = syncing) {
    if (!button) return;
    button.disabled = disabled || syncOpening;
    button.classList.toggle('offline-sync-ready', !button.disabled);
    busyButton(button, active, active ? 'Sincronizando...' : syncOpening ? 'Aguardando confirmação...' : 'Sincronizar agora' + (!button.disabled ? ' — ' + count + (count === 1 ? ' operação' : ' operações') : ''));
}
function operationLabel(operation) {
    return ({
        criar_venda: 'Venda',
        observacao_operacional: 'Observação de tarefa',
        observacao_entrega_venda: 'Observação de entrega',
        venda_local: 'Venda local',
    })[operation.type] || 'Operação';
}
function saleTotal(operation) {
    const items = operation.payload?.itens;
    if (!Array.isArray(items) || !items.length) return null;
    let total = 0;
    for (const item of items) {
        const quantity = Number(item.quantidade), price = Number(item.preco_unitario);
        if (!['number', 'string'].includes(typeof item.quantidade) || !String(item.quantidade).trim()
            || !['number', 'string'].includes(typeof item.preco_unitario) || !String(item.preco_unitario).trim()
            || !Number.isFinite(quantity) || quantity < 0 || !Number.isFinite(price) || price < 0) return null;
        total += quantity * price;
    }
    return Number.isFinite(total) ? new Intl.NumberFormat('pt-BR', {style:'currency', currency:'BRL'}).format(total) : null;
}
function saleCustomer(operation) {
    const customer = operation.original_labels?.cliente;
    return customer && customer.id === operation.payload?.cliente_id && customer.nome
        ? customer.nome : operation.payload?.tipo_pagamento === 'consumo_proprio'
            ? 'Consumo próprio' : 'Não disponível nos dados locais';
}
function saleProducts(operation) {
    const items = operation.payload?.itens;
    if (!Array.isArray(items) || !items.length) return null;
    const names = items.map((item, index) => {
        const product = operation.original_labels?.produtos?.[index];
        return product && product.id === item.produto_id && product.nome
            ? `${item.quantidade} × ${product.nome}` : null;
    });
    return names.every(Boolean) ? names.join(', ') : `${items.length} ${items.length === 1 ? 'item' : 'itens'}; nomes não disponíveis`;
}
function attentionReason(operation) {
    if (operation.status === 'conflito') return 'O servidor identificou uma divergência. Não repita esta operação; confira os detalhes antes de agir.';
    if (operation.status === 'resultado_desconhecido' || operation.status === 'enviando')
        return 'A conexão pode ter sido interrompida durante o envio. A operação pode já estar registrada no servidor.';
    if (operation.status === 'erro') return 'O envio não foi confirmado. A operação continua guardada neste dispositivo.';
    return 'Esta operação ainda aguarda envio ao servidor.';
}
function operationStatusLabel(operation) {
    return ({
        pendente:'Aguardando envio',
        enviando:'Envio iniciado',
        erro:'Falha no envio',
        resultado_desconhecido:'Resultado ainda não confirmado',
        conflito:'Precisa de revisão',
    })[operation.status] || 'Precisa de atenção';
}
function lookupText(operation) {
    const lookup = operation.last_lookup;
    if (!lookup) return 'Ainda não consultado nesta tela.';
    if (lookup.outcome === 'found') return 'O servidor confirmou que a operação já está registrada.';
    if (lookup.outcome === 'not_found') return 'O servidor não encontrou a operação nesta consulta. Ela continua guardada; nenhuma venda foi reenviada.';
    return 'Não foi possível confirmar o resultado nesta consulta. A operação continua guardada.';
}
function safeNextStep(operation) {
    if (operation.status === 'conflito') return 'Não sincronize novamente. Peça ajuda para revisar a divergência.';
    if (operation.last_lookup?.outcome === 'not_found')
        return 'A operação foi mantida. Quando a conexão estiver estável, use “Sincronizar agora” para tentar com o mesmo identificador.';
    if (operation.status === 'resultado_desconhecido' || operation.status === 'enviando')
        return 'Verifique novamente no servidor antes de qualquer sincronização.';
    return 'Você pode verificar o servidor agora; a consulta não envia nem altera a venda.';
}
function localDateText(value) {
    if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) return 'Data não disponível';
    return formatOfflineTime(value);
}
function appendTextField(list, label, value) {
    const term = document.createElement('dt'), description = document.createElement('dd');
    term.textContent = label;
    description.textContent = value;
    list.append(term, description);
}
function message(text) {
    if (globalIndicator) { notice = {kind: 'error', label: text, until: Date.now() + 10000}; void render().catch(() => {}); }
    const target = document.getElementById('offline-message');
    if (target) target.textContent = text;
}
function broadcast() {
    const scope = communicationScope();
    channel?.postMessage({type: 'changed', syncing, progress, at: Date.now(), notice,
        actor: scope.actor_id, environment: scope.environment_id});
}
function changed() { broadcast(); document.dispatchEvent(new Event('offline-operation-updated')); return render(); }
function communicationScope() {
    // Health is meaningful before authentication or snapshot preparation on
    // both screens. An empty template identity must not reset that result.
    return {environment_id: (globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id) || healthEnvironment,
        actor_id: (globalIndicator ? globalIndicator.dataset.actor : snapshot?.actor.id)
            || (salesIdentity && globalIndicator && salesIdentity.environment_id === globalIndicator.dataset.environment
                ? salesIdentity.actor_id : null) || 'anonymous'};
}
function preparedSyncScope(scope, operations) {
    return scope.actor_id !== 'anonymous' && ((snapshot?.actor.id === scope.actor_id && snapshot.environment_id === scope.environment_id)
        || operations.some(op => op.type === 'criar_venda'));
}
async function refreshCommunication(event = null) {
    const scope = communicationScope();
    if (!scope.actor_id || !scope.environment_id) { stability.reset(); return; }
    const value = await repo.communication(scope, event);
    stability.restore(value, performance.now(), Date.now());
    clearTimeout(confirmationTimer);
    confirmationTimer = null;
    if ((value.suspect_id || value.connection?.kind === 'checking' && value.connection.failures) && !leaving) {
        confirmationTimer = setTimeout(() => probe().catch(error => message(error.message)),
            Math.max(0, Math.max(value.suspect_at || 0, value.connection?.checked_at || 0) + CONNECTION_POLICY.retry - Date.now()));
    }
    if (!stability.connected && syncing) { stopped = true; inFlight?.abort(); }
    if (event) broadcast();
}
async function render() {
    if (!repo) return;
    await refreshCommunication();
    const [operations, history] = await Promise.all([repo.all('operations'), pilot ? repo.all('history') : []]);
    const scope = communicationScope();
    const scoped = operations.filter(op => op.actor_id === scope.actor_id && op.environment_id === scope.environment_id);
    const scopedById = new Map(scoped.map(operation => [operation.operation_id, operation]));
    const closureMetadata=await repo.all('metadata');
    const revisions = new Map();
    for (const op of scoped.filter(op => op.status === 'conflito')) revisions.set(op.operation_id, await revisionPresentation(repo,op,scope));
    // Resolution is a presentation only: preserve historical conflict bytes.
    const unresolved = scoped.filter(op => !revisions.get(op.operation_id)?.recordId && !closedLocally(closureMetadata,op)
        && op.status !== 'substituida_manual');
    const pending = unresolved.filter(op => op.status !== 'confirmada');
    const completedById = new Map();
    for (const row of history) {
        if (row.actor_id !== scope.actor_id || row.environment_id !== scope.environment_id) continue;
        const current = scopedById.get(row.operation_id) || row;
        if (['confirmada', 'substituida_manual'].includes(current.status)
            || revisions.get(current.operation_id)?.recordId || closedLocally(closureMetadata, current))
            completedById.set(current.operation_id, {...current, synchronized_at:row.synchronized_at || current.synchronized_at});
    }
    for (const operation of scoped) {
        if (['confirmada', 'substituida_manual'].includes(operation.status)
            || revisions.get(operation.operation_id)?.recordId || closedLocally(closureMetadata, operation))
            completedById.set(operation.operation_id, {...completedById.get(operation.operation_id), ...operation});
    }
    const state = indicatorState({operations: unresolved, stability, now: performance.now(), wall: Date.now(),
        syncing: syncing || (remoteSync && Date.now() - remoteSync.at < POLICY.maxGap), progress: syncing ? progress : remoteSync?.progress, notice, authenticated});
    let resetLog = document.getElementById('offline-reset-log');
    if (!resetLog && badge) {
        resetLog = document.createElement('span'); resetLog.id = 'offline-reset-log';
        resetLog.setAttribute('role', 'status'); badge.after(resetLog);
    }
    if (resetLog) timeText(resetLog, stability.failedAt && !stability.ready(performance.now(), Date.now())
        ? 'Estabilidade reiniciada — horário do dispositivo: ' + formatOfflineTime(stability.failedAt) + ' — motivo: falha de comunicação · Horário de Belém (PA)' : '');
    renderIndicator(badge, state, Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000));
    if (syncing && modal.open) modalText.textContent = 'Sincronizando ' + progress.replace('/', ' de ') + '...';
    const observedConnection = connectionStatus(stability.connection, Date.now());
    const connectionKind = observedConnection === 'offline' ? 'offline'
        : observedConnection === 'online' && stability.valid(performance.now(), Date.now()) ? 'online' : 'checking';
    if (globalIndicator && globalIndicator.dataset.connection !== connectionKind) {
        globalIndicator.dataset.connection = connectionKind;
        document.dispatchEvent(new CustomEvent('offline-connectivity', {detail: connectionKind}));
    }
    if (globalSync) {
        globalSync.hidden = !pending.some(op => ['pendente', 'erro', 'resultado_desconhecido'].includes(op.status));
        syncButton(globalSync, !state.canSync || !navigator.locks || !preparedSyncScope(scope, scoped), state.count, state.kind === 'syncing');
    }
    if (!pilot) return;
    const stabilityText = document.getElementById('offline-stability');
    stabilityText.hidden = state.kind === 'waiting';
    stabilityText.dataset.state = state.kind;
    stabilityText.textContent = state.kind === 'waiting' ? 'Verificando estabilidade para sincronização segura · ' + Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000) + ' de 15 minutos · ' + state.count + (state.count === 1 ? ' operação aguardando' : ' operações aguardando') : stability.connected ? 'CONEXÃO ESTÁVEL HÁ ' + Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000) + ' MINUTOS · mínimo 15' : 'Comunicação com o servidor indisponível ou ainda não verificada.';
    syncButton(document.getElementById('offline-sync'), !state.canSync || !navigator.locks, state.count, state.kind === 'syncing');
    const saveButton = document.getElementById('offline-save');
    saveButton.disabled = !snapshot || syncing || saving;
    busyButton(saveButton, saving, saving ? 'Salvando...' : 'Salvar neste dispositivo');
    const prepareButton = document.getElementById('offline-prepare');
    prepareButton.disabled = syncing || preparing;
    busyButton(prepareButton, preparing, preparing ? 'Preparando...' : 'Preparar / atualizar dados online');
    const list = document.getElementById('offline-operations');
    const historyDetails = document.getElementById('offline-completed-history');
    const historyWasOpen = historyDetails.open;
    const focusedOperation = list.contains(document.activeElement)
        ? document.activeElement.closest('[data-operation-id]')?.dataset.operationId
            || document.activeElement.dataset.verifyOperation || null : null;
    const expandedOperations = new Set([...list.querySelectorAll('details[open]')]
        .map(detail => detail.dataset.operationId));
    list.replaceChildren();
    if (!pending.length) {
        const empty = document.createElement('li');
        empty.className = 'offline-empty-state';
        empty.textContent = 'Nenhuma operação precisa de atenção';
        list.append(empty);
    }
    for (const op of pending.sort((a, b) => b.sequence - a.sequence)) {
        const row = document.createElement('li');
        row.className = 'offline-operation-card';
        const title = document.createElement('h3');
        title.textContent = operationLabel(op);
        const status = document.createElement('p');
        status.className = 'offline-operation-status';
        status.textContent = operationStatusLabel(op);
        const details = document.createElement('dl');
        details.className = 'offline-operation-summary';
        appendTextField(details, 'Data e hora', localDateText(op.created_at));
        if (op.type === 'criar_venda' || op.type === 'venda_local') {
            if (op.payload?.data_venda) appendTextField(details, 'Data da venda', String(op.payload.data_venda));
            appendTextField(details, 'Cliente', saleCustomer(op));
            const total = saleTotal(op);
            if (total) appendTextField(details, 'Valor', total);
            const products = saleProducts(op);
            if (products) appendTextField(details, 'Produtos', products);
        } else {
            appendTextField(details, 'Tarefa', op.type === 'observacao_entrega_venda' ? 'Entrega de venda' : 'Tarefa operacional');
            if (op.payload?.observacao) appendTextField(details, 'Observação', op.payload.observacao);
        }
        const reason = document.createElement('p');
        reason.className = 'offline-operation-reason';
        reason.textContent = attentionReason(op);
        const lookup = document.createElement('p');
        lookup.className = 'offline-operation-lookup';
        lookup.textContent = 'Última consulta: ' + lookupText(op);
        const nextStep = document.createElement('p');
        nextStep.className = 'offline-operation-next-step';
        nextStep.textContent = 'Próximo passo seguro: ' + safeNextStep(op);
        row.append(title, status, details, reason, lookup, nextStep);
        if (op.type !== 'venda_local' && op.status !== 'conflito') {
            const verify = document.createElement('button');
            verify.type = 'button';
            verify.dataset.verifyOperation = op.operation_id;
            verify.disabled = checkingOperations.has(op.operation_id);
            verify.textContent = verify.disabled ? 'Consultando servidor…' : 'Verificar novamente no servidor';
            verify.addEventListener('click', () => { void verifyOperation(op.operation_id); });
            row.append(verify);
        }
        const revisionView = revisions.get(op.operation_id);
        const closure = closureMetadata.find(item => item.key === closureKey(op))?.receipt;
        const diagnostic = operationDetails(op, scope, revisionView, closure);
        if (diagnostic) { diagnostic.open = expandedOperations.has(op.operation_id); row.append(diagnostic); }
        list.append(row);
    }
    const historyList = document.getElementById('offline-history-operations');
    const focusedHistoryOperation = historyList.contains(document.activeElement)
        ? document.activeElement.closest('[data-operation-id]')?.dataset.operationId || null : null;
    const completed = [...completedById.values()].sort((a, b) =>
        Date.parse(b.synchronized_at || b.server_result?.completed_at || b.created_at || 0)
        - Date.parse(a.synchronized_at || a.server_result?.completed_at || a.created_at || 0));
    historyDetails.querySelector('summary').textContent = `Histórico de operações concluídas (${completed.length})`;
    const historyKey = JSON.stringify(completed.map(operation => [
        operation.operation_id, operation.status, operation.synchronized_at,
        operation.server_result?.record_id, operation.resolution?.source, operation.resolution?.resolved_at,
        operation.manual_replacement?.sale_id, operation.manual_replacement?.replaced_at, operation.manual_replacement?.reason,
    ]));
    if (!historyDetails.open) {
        historyList.replaceChildren();
        renderedHistoryKey = null;
    } else if (historyKey !== renderedHistoryKey) {
        const expandedHistoryOperations = new Set([...historyList.querySelectorAll('details[open]')]
            .map(detail => detail.dataset.operationId));
        historyList.replaceChildren();
        if (!completed.length) {
            const empty = document.createElement('li');
            empty.className = 'offline-empty-state';
            empty.textContent = 'Ainda não há operações concluídas.';
            historyList.append(empty);
        }
        for (const op of completed) {
            const row = document.createElement('li');
            row.className = 'offline-history-card';
            const status = document.createElement('strong');
            const revisionView = revisions.get(op.operation_id);
            const closure = closureMetadata.find(item => item.key === closureKey(op))?.receipt;
            if (op.status === 'substituida_manual') status.textContent = `Operação antiga substituída pela venda corrigida nº ${op.manual_replacement?.sale_id || 'não informada'}`;
            else if (closedLocally(closureMetadata, op)) status.textContent = 'Encerrada sem gerar venda';
            else if (revisionView?.recordId) status.textContent = `Conflito resolvido pela venda nº ${revisionView.recordId}`;
            else if (op.resolution?.source === 'automatic_reconciliation') status.textContent = 'Resolvida automaticamente';
            else if (op.resolution?.source === 'server_check') status.textContent = 'Confirmada após consulta no servidor';
            else status.textContent = operationLabel(op) + ' concluída';
            const summary = document.createElement('span');
            const recordId = op.server_result?.record_id;
            const replacementId = op.manual_replacement?.sale_id;
            summary.textContent = [recordId ? `Registro nº ${recordId}` : replacementId ? `Venda corrigida nº ${replacementId}` : '',
                localDateText(op.manual_replacement?.replaced_at || op.synchronized_at || op.server_result?.completed_at || op.created_at)]
                .filter(Boolean).join(' · ');
            row.append(status, summary);
            const technical = document.createElement('details');
            technical.dataset.operationId = op.operation_id;
            technical.open = expandedHistoryOperations.has(op.operation_id);
            const technicalSummary = document.createElement('summary');
            technicalSummary.textContent = 'Ver detalhes técnicos';
            const technicalList = document.createElement('dl');
            for (const [label, value] of Object.entries({
                UUID:op.operation_id, Hash:op.payload_hash, Usuário:op.actor_id, Dispositivo:op.device_id,
                Ambiente:op.environment_id, Tentativas:op.attempts ?? 0,
                'Resolvida em':localDateText(op.manual_replacement?.replaced_at || op.resolution?.resolved_at || op.server_result?.completed_at),
                'Motivo do encerramento':op.manual_replacement?.reason,
                'Venda corrigida':op.manual_replacement?.sale_id,
            })) appendTextField(technicalList, label, String(value ?? 'Não disponível'));
            technical.append(technicalSummary, technicalList);
            row.append(technical);
            historyList.append(row);
        }
        renderedHistoryKey = historyKey;
        if (focusedHistoryOperation)
            historyList.querySelector(`details[data-operation-id="${CSS.escape(focusedHistoryOperation)}"] summary`)?.focus({preventScroll:true});
    }
    historyDetails.open = historyWasOpen;
    if (focusedOperation) {
        const target = list.querySelector(`[data-verify-operation="${CSS.escape(focusedOperation)}"]`)
            || list.querySelector(`details[data-operation-id="${CSS.escape(focusedOperation)}"] summary`);
        target?.focus({preventScroll:true});
    }
    timeText(document.getElementById('offline-diagnostic'), snapshot ? `Usuário: ${snapshot.actor.name} · ambiente: ${snapshot.environment_id} · preparação informada pelo dispositivo: ${formatOfflineTime(snapshot.prepared_at)} · Horário de Belém (PA) · limite: 200 tarefas pendentes` : 'Prepare online após autenticar-se.');
}
async function verifyOperation(operationId) {
    if (checkingOperations.has(operationId)) return;
    checkingOperations.add(operationId);
    try {
        const operation = await repo.get('operations', operationId);
        const scope = communicationScope();
        const device = await repo.get('metadata', 'device');
        if (!operation || operation.actor_id !== scope.actor_id || operation.environment_id !== scope.environment_id
            || !device?.id || operation.device_id !== device.id || operation.type === 'venda_local'
            || !['pendente', 'enviando', 'erro', 'resultado_desconhecido'].includes(operation.status)) {
            message('Esta operação não corresponde ao usuário, dispositivo e ambiente ativos. Ela foi mantida sem consulta.');
            return;
        }
        try {
            const result = await reconcile(operation, {source:'server_check'});
            if (result === 'nao_encontrada') {
                await repo.updateAttempt(operation, {last_lookup:{outcome:'not_found', checked_at:new Date().toISOString()}});
                message('O servidor não encontrou a operação nesta consulta. Ela foi mantida e não foi reenviada.');
            } else {
                message('O servidor confirmou a operação. A pendência local foi atualizada sem reenviar a venda.');
            }
        } catch (error) {
            await repo.updateAttempt(operation, {last_lookup:{outcome:'inconclusive', checked_at:new Date().toISOString(),
                message:error.message, code:error.code || 'consulta_indeterminada'}});
            message('Não foi possível confirmar o resultado. A operação continua guardada; nenhum reenvio foi feito.');
        }
        document.dispatchEvent(new Event('offline-operations-changed'));
        channel?.postMessage({type:'changed', actor:scope.actor_id, environment:scope.environment_id});
    } catch (error) {
        message('Não foi possível concluir a consulta local. Confira os detalhes da operação; nenhum reenvio foi feito. ' + error.message);
    } finally {
        checkingOperations.delete(operationId);
        await render();
    }
}
async function failConnection(event) {
    stopped = true;
    if (syncing) notice = {kind: 'error', label: 'Sincroniza\u00e7\u00e3o interrompida — opera\u00e7\u00f5es preservadas', until: Date.now() + 10000}; inFlight?.abort();
    await refreshCommunication(event);
    await render();
}
async function fetchTimed(url, options = {}) {
    const controller = options.controller || new AbortController();
    if (options.synchronize) inFlight = controller;
    const timer = setTimeout(() => controller.abort(), POLICY.timeout);
    try {
        const response = await fetch(url, {...options, cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
        const body = await response.text(); // Timeout also covers a stalled response body.
        return new Response(body, {status: response.status, statusText: response.statusText, headers: response.headers});
    }
    finally { clearTimeout(timer); if (inFlight === controller) inFlight = null; }
}
export function probe() {
    if (probePending && probeGeneration === observationGeneration && probeStartedAt != null
        && Date.now() - probeStartedAt > POLICY.maxGap) {
        // An in-flight request can itself be frozen. Do not await it on wake.
        observationGeneration++;
        observedSince = null;
        healthController?.abort();
    }
    // Concurrent callers await the complete health/session/storage/render cycle.
    if (!probePending || probeGeneration !== observationGeneration) {
        probeGeneration = observationGeneration;
        probeStartedAt = Date.now();
        const pending = performProbe().finally(() => { if (probePending === pending) probePending = null; });
        probePending = pending;
    }
    return probePending;
}
async function performProbe() {
    if (leaving) return;
    const generation = observationGeneration;
    // Read the failure marker before starting: millisecond timestamp ties must not reject a genuinely new check.
    const previous = await repo.communication(communicationScope());
    if (leaving || generation !== observationGeneration) return;
    const started_at = Date.now();
    if (previous.suspect_id && started_at < previous.suspect_at + 5000) {
        await refreshCommunication();
        return;
    }
    let health;
    const controller = new AbortController();
    const startedMono = performance.now();
    // A timeout delivered after the browser slept is not evidence of a network failure.
    const interrupted = () => Date.now() - started_at > POLICY.timeout + 1000
        || performance.now() - startedMono > POLICY.timeout + 1000;
    const resume = () => {
        observedSince = null;
        setTimeout(() => { if (!leaving && generation === observationGeneration) void probe().catch(error => message(error.message)); }, 0);
    };
    healthController = controller;
    try {
        const response = await fetchTimed('/api/offline/health/', {controller});
        health = await response.json();
        const environment = globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id;
        if (!response.ok || typeof health.environment !== 'string' || !health.environment || !validHealth(health, environment || health.environment)) throw new Error('Health-check inv\u00e1lido.');
    } catch {
        if (!leaving && generation === observationGeneration) {
            if (interrupted()) { resume(); return; }
            observedSince = null;
            await failConnection(previous.suspect_id
                ? {type: 'confirmation', suspect_id: previous.suspect_id, ok: false, at: Date.now(), started_at}
                : {type: 'suspect', id: crypto.randomUUID(), at: Date.now(), started_at, failure_seen: previous.failed_at ?? null});
        }
        return;
    } finally {
        if (healthController === controller) healthController = null;
    }
    if (leaving || generation !== observationGeneration) return;
    if (interrupted()) { resume(); return; }
    healthEnvironment = health.environment;
    const at = Date.now();
    await refreshCommunication(previous.suspect_id
        ? {type: 'confirmation', suspect_id: previous.suspect_id, ok: true, at, started_at}
        : {type: 'success', at, started_at, observed_since: observedSince, failure_seen: previous.failed_at ?? null});
    if (!leaving && generation === observationGeneration) observedSince = at;
    await checkSession();
    await render();
    document.dispatchEvent(new CustomEvent('offline-connectivity', {detail: globalIndicator?.dataset.connection}));
}
function updateObservation() {
    clearTimeout(confirmationTimer);
    confirmationTimer = null;
    observationGeneration++;
    // Hidden pages may keep validating. Keep their last proven endpoint; the
    // repository caps every credited interval at maxGap. Visible resumption
    // always starts with a fresh health-check and no unobserved time credit.
    if (leaving || !document.hidden) observedSince = null;
    healthController?.abort();
    clearInterval(monitorTimer);
    monitorTimer = null;
    if (leaving) return;
    // A resumed page owns a fresh monitor and cannot join a suspended old probe.
    monitorTimer = setInterval(() => probe().catch(error => message(error.message)), POLICY.interval);
    if (!document.hidden) void probe().catch(error => message(error.message));
}
function showTasks() {
    if (!pilot) return;
    const select = document.getElementById('offline-task');
    select.replaceChildren();
    for (const task of snapshot?.tasks || []) {
        const option = document.createElement('option'); option.value = task.id; option.textContent = task.label; select.append(option);
    }
}
async function prepare() {
    if (preparing || syncing) return;
    preparing = true;
    const button = document.getElementById('offline-prepare');
    button.disabled = true; busyButton(button, true, 'Preparando...');
    try {
        if (!await checkSession()) throw new Error('Autentique-se novamente para preparar. Fila preservada.');
        const response = await fetchTimed('/api/offline/snapshot/');
        if ([401, 403].includes(response.status)) { requireAuthentication(); throw new Error('Autentique-se novamente e confira sua permissão. Fila preservada.'); }
        if (!response.ok) throw new Error('Preparação recusada. Confira sua permissão e conexão.');
        const data = await response.json();
        if (data.protocol_version !== 1 || !data.actor?.id || !Array.isArray(data.tasks)) throw new Error('Snapshot inválido.');
        if (snapshot && snapshot.environment_id !== data.environment_id) throw new Error('Ambiente mudou. A fila permanece vinculada ao ambiente anterior.');
        csrf = data.csrf_token;
        const {csrf_token, ...safe} = data; // Never persist CSRF or credentials.
        const next = {...safe, key: 'pilot', prepared_at: new Date().toISOString()};
        await repo.put('snapshots', next);
        snapshot = next; showTasks();
        let diagnostic = 'Persistência não suportada por este navegador.';
        if (navigator.storage?.persist) {
            try { diagnostic = await navigator.storage.persist() ? 'Armazenamento persistente concedido.' : 'Persistência não concedida; preserve os dados deste site e exporte a fila.'; }
            catch { diagnostic = 'Não foi possível solicitar persistência; dados locais continuam disponíveis.'; }
        }
        await repo.put('metadata', {key: 'persistence', diagnostic});
        document.getElementById('offline-storage').textContent = diagnostic;
        message('Dados preparados neste dispositivo. ' + diagnostic);
        if ('serviceWorker' in navigator) { await navigator.serviceWorker.register('/service-worker.js', {scope: '/'}); await navigator.serviceWorker.ready; }
        else message('Snapshot salvo, mas Service Worker indisponível. Use HTTPS ou localhost para abrir sem rede.');
        await probe(); await changed();
    } catch (error) { message('Não foi possível preparar os dados: ' + error.message); }
    finally { preparing = false; await render(); }
}
function publishSessionStatus(data) {
    const status = {authenticated: data.authenticated, actor_id: data.actor_id,
        environment_id: communicationScope().environment_id};
    window.salesSessionStatus = status;
    document.dispatchEvent(new CustomEvent('sales-session-status', {detail: status}));
    const channel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-session') : null;
    // A successful response can predate a login in another tab. Ownership
    // changes are announced only by the fresh page's sales-identity record.
    if (status.authenticated === false) channel?.postMessage(status);
    channel?.close();
}
function checkSession() {
    sessionPending = performSessionCheck(++sessionGeneration);
    return sessionPending;
}
async function performSessionCheck(generation) {
    const login = document.getElementById('offline-login');
    const status = document.getElementById('offline-session');
    try {
        const response = await fetchTimed('/api/offline/session/');
        if (!response.ok) throw new Error('Sessão indisponível.');
        const data = await response.json();
        if (generation !== sessionGeneration) return sessionPending;
        authenticated = data.authenticated === true && data.can_prepare === true;
        // Authentication loss must not replace the owner of existing drafts.
        publishSessionStatus(data);
        if (!login || !status) return authenticated;
        login.hidden = data.authenticated === true;
        status.textContent = data.authenticated
            ? `Autenticado como ${data.username}. ` + (data.can_prepare ? 'Pode preparar / atualizar dados online.' : 'Sem permissão para preparar dados do piloto.')
            : 'Autentique-se online para preparar / atualizar dados.';
    } catch {
        if (generation !== sessionGeneration) return sessionPending;
        requireAuthentication();
    }
    return authenticated;
}
function requireAuthentication() {
    authenticated = false;
    const login = document.getElementById('offline-login');
    const status = document.getElementById('offline-session');
    if (login) login.hidden = false;
    if (status) status.textContent = 'Autentique-se novamente e confira sua permissão. Dados locais preservados.';
}
async function save(event) {
    event.preventDefault();
    if (saving) return;
    saving = true;
    const button = document.getElementById('offline-save'); button.disabled = true; busyButton(button, true, 'Salvando...');
    try {
        const task = snapshot?.tasks.find(t => t.id === document.getElementById('offline-task').value);
        const text = document.getElementById('offline-note').value.trim();
        if (!task || !text || text.length > 2000) throw new Error('Escolha uma tarefa preparada e informe até 2000 caracteres.');
        const op = await repo.create(snapshot, task, text);
        message('Salvo neste dispositivo · ' + op.operation_id);
        document.getElementById('offline-note').value = ''; await changed();
    } catch (error) { message('Não foi possível salvar neste dispositivo: ' + error.message); }
    finally { saving = false; await render(); }
}
async function synchronize() {
    if (syncOpening || syncing) return;
    syncOpening = true;
    for (const button of [globalSync, document.getElementById('offline-sync')]) {
        if (button) { button.disabled = true; button.classList.remove('offline-sync-ready'); button.textContent = 'Aguardando confirmação...'; }
    }
    try { await runSynchronization(); }
    finally { syncOpening = false; await render(); }
}
async function runSynchronization() {
    const scope = communicationScope();
    if (scope.actor_id === 'anonymous') return;
    if (!navigator.locks || !stability.ready(performance.now(), Date.now()) || syncing) return;
    if (!await checkSession()) { message('Autentique-se novamente e confira sua permissão. Fila preservada.'); await render(); return; }
    await navigator.locks.request('offline-pilot-sync', {ifAvailable: true}, async lock => {
        if (!lock) { message('Outra aba está sincronizando.'); return; }
        await refreshCommunication();
        if (!stability.ready(performance.now(), Date.now())) return;
        const scoped = (await repo.all('operations')).filter(op => op.actor_id === scope.actor_id && op.environment_id === scope.environment_id);
        if (!preparedSyncScope(scope, scoped)) return;
        const closureMetadata=await repo.all('metadata');
        const operations = scoped.filter(op => !closedLocally(closureMetadata,op) && ['pendente', 'resultado_desconhecido', 'erro'].includes(op.status)).sort((a, b) => a.sequence - b.sequence);
        if (!operations.length || !await confirmSynchronization(operations.length)) return;
        syncing = true; stopped = false; notice = null; progress = '0/' + operations.length; await changed();
        let confirmed = 0;
        try {
            // Refresh session/CSRF online without overwriting the prepared snapshot.
            const session = await fetchTimed('/api/offline/snapshot/');
            if ([401, 403].includes(session.status)) requireAuthentication();
            if (!session.ok) throw new Error('Autentique-se novamente e confira sua permissão. Fila preservada.');
            const identity = await session.json();
            if (identity.actor?.id !== scope.actor_id || identity.environment_id !== scope.environment_id || identity.protocol_version !== 1) throw new Error('Usuário/ambiente diferente. Fila preservada.');
            csrf = identity.csrf_token;
            for (let index = 0; index < operations.length; index++) {
                await refreshCommunication();
                if (stopped || !stability.ready(performance.now(), Date.now())) { stopped = true; await probe(); break; }
                const current = await repo.get('operations', operations[index].operation_id);
                if (!current || ['confirmada', 'conflito'].includes(current.status)) continue;
                if (current.status === 'resultado_desconhecido') {
                    try {
                        const lookup = await reconcile(current);
                        if (lookup === 'encontrada') { await changed(); continue; }
                        // Only this user-confirmed manual batch may retry an absent UUID.
                        await refreshCommunication();
                        if (stopped || !stability.ready(performance.now(), Date.now())) break;
                    } catch (error) {
                        await repo.diagnose(current, error.message, error.code, error.response);
                        message(error.message); await changed();
                        if (error.code === 'consulta_autenticacao') { stopped = true; break; }
                        continue;
                    }
                }
                const operation = await repo.updateAttempt(current, {status:'enviando', attempts:current.attempts + 1});
                if (!operation) continue;
                progress = `${index + 1}/${operations.length}`; await changed();
                let receiptPersisted = false;
                try {
                    const response = await fetchTimed('/api/offline/observations/', {method: 'POST', synchronize: true,
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                        body: JSON.stringify({...commandOf(operation), payload_hash: operation.payload_hash})});
                    if ([401, 403].includes(response.status)) {
                        requireAuthentication();
                        await repo.updateAttempt(operation, {status: 'erro', last_error: 'Sessão/permissão/CSRF: autentique-se novamente. Dados preservados.'});
                        stopped = true; message('Autenticação necessária. Fila preservada.'); break;
                    }
                    if ([400, 413].includes(response.status)) {
                        await repo.updateAttempt(operation, {status: 'erro', last_error: 'Servidor recusou o formato. Revisão técnica necessária.'});
                        stopped = true; message('Erro de validação. Dados preservados.'); break;
                    }
                    if (![200, 409].includes(response.status)) throw new Error('Servidor indisponível.');
                    const result = await response.json();
                    if (response.status === 409 && result?.code === 'uuid_comando_divergente'
                        && result.operation_id === operation.operation_id && result.hash === operation.payload_hash) {
                        await repo.diagnose(operation, 'UUID já utilizado com outro comando. Resultado original precisa de reconciliação; revisão técnica necessária.', result.code, result);
                        await changed(); continue;
                    }
                    if (!validReceipt(result, operation, response.status)) throw new Error('Confirmação do servidor inválida.');
                    await repo.record(operation, result); receiptPersisted = true;
                    if (result.status === 'confirmada') confirmed++; await changed();
                } catch (error) {
                    if (!receiptPersisted) await repo.diagnose(operation, error.message + ' Consulte o resultado pela sincronização manual antes de repetir.');
                    // Preserve the existing new stability window after an uncertain send,
                    // but let the health-check decide whether the server is offline.
                    stopped = true;
                    await probe(); message('Sincronização interrompida — operações preservadas'); break;
                }
            }

        } catch (error) { message(error.message); }
        finally {
            const results = await repo.all('operations');
            const closures = await repo.all('metadata');
            let conflict = false;
            for (const op of results.filter(op => op.actor_id === scope.actor_id && op.environment_id === scope.environment_id && op.status === 'conflito')) {
                if (closedLocally(closures,op)) continue;
                if (!(await revisionPresentation(repo,op,scope))?.recordId) conflict = true;
            }
            const remaining = results.some(op => operations.some(original => original.operation_id === op.operation_id)
                && !['confirmada', 'substituida_manual'].includes(op.status) && !closedLocally(closures,op));
            const text = conflict ? 'Conflito de sincronização — revisão necessária' : stopped || remaining ? 'Sincronização interrompida — operações preservadas' : 'Sincronização concluída';
            finishModal(text);
            const feedback = document.getElementById('offline-message');
            if (feedback) feedback.textContent = text;
            notice = {kind: conflict ? 'conflict' : stopped || remaining ? 'error' : 'success', label: text, until: Date.now() + 10000};
            syncing = false; csrf = ''; await changed();
        }
    });
}
async function reconcile(operation, {source = null} = {}) {
    const fail = (message, code = 'consulta_indeterminada', response = null) => {
        const error = new Error(message); error.code = code; error.response = response; throw error;
    };
    if (await hash(commandOf(operation)) !== operation.payload_hash)
        fail('Comando local não corresponde ao hash preservado. Revisão técnica necessária.', 'comando_local_divergente');
    const query = new URLSearchParams({actor_id:operation.actor_id, environment_id:operation.environment_id,
        type:operation.type, device_id:operation.device_id, hash:operation.payload_hash});
    const response = await fetchTimed(`/api/offline/operations/${operation.operation_id}/?${query}`);
    if ([401,403].includes(response.status)) { requireAuthentication(); fail('Consulta recusada: autentique-se e confira permissão/CSRF.', 'consulta_autenticacao'); }
    let result;
    try { result = await response.json(); } catch { fail('Resposta da consulta inválida. Operação preservada.'); }
    if (response.status === 409 && result?.lookup === 'incompativel')
        fail('UUID encontrado com identidade ou comando incompatível. Revisão técnica necessária.', 'uuid_comando_divergente', result);
    if (response.status !== 200 || result?.operation_id !== operation.operation_id || result.hash !== operation.payload_hash)
        fail('Não foi possível determinar o resultado no servidor. Operação preservada.');
    if (result.lookup === 'nao_encontrada') return result.lookup;
    if (result.lookup !== 'encontrada' || result.actor_id !== operation.actor_id
        || result.environment_id !== operation.environment_id || result.type !== operation.type
        || result.device_id !== operation.device_id || !validReceipt(result.receipt, operation))
        fail('Resultado da consulta incompatível ou receipt inválido. Operação preservada.');
    const checkedOperation = {...operation, last_lookup:{outcome:'found', checked_at:new Date().toISOString()}};
    if (source) checkedOperation.resolution = {source, resolved_at:new Date().toISOString()};
    await repo.record(checkedOperation, result.receipt);
    if (result.closure) {
        const device=await repo.get('metadata','device');
        await mirrorClosure(repo,{...communicationScope(),device_id:device?.id},await repo.get('operations',operation.operation_id),result.closure);
    }
    return 'encontrada';
}
async function exportDiagnostic() {
    const data = {format: 'offline-pilot-diagnostic-v1', origin: location.origin, exported_at: new Date().toISOString(),
        metadata: await repo.all('metadata'), snapshots: await repo.all('snapshots'), operations: await repo.all('operations'), history: await repo.all('history')};
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'offline-piloto-diagnostico.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function start() {
    repo = new Repository(await openDB());
    installClosureUI(async()=>{
        const identity=await repo.get('metadata','sales-identity'),device=await repo.get('metadata','device');
        if(!identity?.actor_id || !device?.id)throw new Error('Identidade local indisponível.');
        return {repo,scope:{...identity,device_id:device.id}};
    });
    if (globalIndicator?.dataset.actor && !window.localSalesContext && !window.salesOffline?.shell && !document.getElementById('revision-form')) {
        await repo.put('metadata', {key: 'sales-identity', actor_id: globalIndicator.dataset.actor,
            environment_id: globalIndicator.dataset.environment});
        if ('BroadcastChannel' in window) {
            const identityChannel = new BroadcastChannel('sales-identity');
            identityChannel.postMessage('changed'); identityChannel.close();
        }
    }
    salesIdentity = await repo.get('metadata', 'sales-identity');
    await checkSession();
    snapshot = await repo.get('snapshots', 'pilot');
    if (!pilot && !globalIndicator) return;
    if (navigator.locks) await navigator.locks.request('offline-pilot-sync', {ifAvailable: true}, async lock => { if (lock) await repo.recover(); });
    document.dispatchEvent(new Event('offline-operation-updated'));
    if (pilot) {
        const identity = await repo.identity();
        document.getElementById('offline-device').textContent = identity.id;
        document.getElementById('offline-storage').textContent = (await repo.get('metadata', 'persistence'))?.diagnostic || 'Persistência ainda não solicitada.';
        showTasks();
        document.getElementById('offline-prepare').addEventListener('click', prepare);
        document.getElementById('offline-form').addEventListener('submit', save);
        document.getElementById('offline-sync').addEventListener('click', () => synchronize().catch(error => message(error.message)));
        document.getElementById('offline-export').addEventListener('click', () => exportDiagnostic().catch(error => message(error.message)));
        if (!navigator.locks) message('Sincronização indisponível neste navegador: Web Locks necessária. A fila pode ser salva e exportada.');
    }
    globalSync?.addEventListener('click', () => synchronize().catch(error => message(error.message)));
    channel?.addEventListener('message', event => {
        if (event.data?.type === 'changed' && event.data.actor === (globalIndicator ? globalIndicator.dataset.actor : snapshot?.actor.id) && event.data.environment === (globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id)) { remoteSync = event.data.syncing ? event.data : null; notice = event.data.notice; }
        repo.get('snapshots', 'pilot').then(value => { snapshot = value; return render(); }).catch(error => message(error.message));
    });
    window.addEventListener('offline', () => { void probe().catch(error => message(error.message)); });
    window.addEventListener('online', () => { void checkSession(); void probe(); });
    window.addEventListener('beforeunload', () => {
        // Navigation can cancel fetch before pagehide/visibilitychange. Invalidate
        // that observation before aborting, so its rejection cannot publish a failure.
        // Keep the timer alive here: a different handler may cancel navigation.
        clearTimeout(confirmationTimer);
        confirmationTimer = null;
        observationGeneration++;
        observedSince = null;
        healthController?.abort();
    });
    window.addEventListener('pagehide', () => { leaving = true; updateObservation(); inFlight?.abort(); });
    window.addEventListener('pageshow', () => { leaving = false; void checkSession(); updateObservation(); });
    document.addEventListener('visibilitychange', updateObservation);
    updateObservation();
    setInterval(() => { if (syncing) broadcast(); if (stopped === false && syncing && !stability.valid(performance.now(), Date.now())) void probe().catch(error => message(error.message)); void render().catch(error => message(error.message)); }, 1000);
    await render(); await probe(); await render();
}
export const initialized = start().catch(error => { if (badge) badge.textContent = 'Armazenamento offline indisponível'; message('Não foi possível abrir o armazenamento local: ' + error.message); });
document.addEventListener('offline-operations-changed', () => { void initialized.then(() => render()); });
document.addEventListener('offline-connectivity-suspect', () => { void initialized.then(() => probe()).catch(error => message(error.message)); });
