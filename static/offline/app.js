import {POLICY, Repository, Stability, openDB, commandOf, validHealth, validReceipt, indicatorState} from './core.js';
import {renderIndicator} from './presentation.js';

const pilot = !!document.getElementById('offline-pilot');
const globalIndicator = document.getElementById('offline-global');
const globalSync = document.getElementById('offline-global-sync');
let notice = null, remoteSync = null;
let authenticated = false;
let healthEnvironment = location.host;
const badge = document.getElementById('offline-status');
let repo, snapshot, csrf = '', inFlight, syncing = false, saving = false, preparing = false, syncOpening = false, stopped = false, progress = '', probePending = null, sessionGeneration = 0, sessionPending = null, leaving = false;
let observationGeneration = 0, observedSince = null;
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
    modalText.textContent = 'A conexão está estável. As operações pendentes serão enviadas agora para o servidor.';
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
function changed() { broadcast(); return render(); }
function communicationScope() {
    // Health is meaningful before authentication or snapshot preparation on
    // both screens. An empty template identity must not reset that result.
    return {environment_id: (globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id) || healthEnvironment,
        actor_id: (globalIndicator ? globalIndicator.dataset.actor : snapshot?.actor.id) || 'anonymous'};
}
async function refreshCommunication(event = null) {
    const scope = communicationScope();
    if (!scope.actor_id || !scope.environment_id) { stability.reset(); return; }
    const value = await repo.communication(scope, event);
    stability.restore(value, performance.now(), Date.now());
    if (!stability.connected && syncing) { stopped = true; inFlight?.abort(); }
    if (event) broadcast();
}
async function render() {
    if (!repo) return;
    await refreshCommunication();
    const operations = await repo.all('operations');
    const scoped = operations.filter(op => op.actor_id === (globalIndicator ? globalIndicator.dataset.actor : snapshot?.actor.id)
        && op.environment_id === (globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id));
    const pending = scoped.filter(op => op.status !== 'confirmada');
    const state = indicatorState({operations: scoped, stability, now: performance.now(), wall: Date.now(),
        syncing: syncing || (remoteSync && Date.now() - remoteSync.at < POLICY.maxGap), progress: syncing ? progress : remoteSync?.progress, notice, authenticated});
    let resetLog = document.getElementById('offline-reset-log');
    if (!resetLog && badge) {
        resetLog = document.createElement('span'); resetLog.id = 'offline-reset-log';
        resetLog.setAttribute('role', 'status'); badge.after(resetLog);
    }
    if (resetLog) resetLog.textContent = stability.failedAt
        ? 'Estabilidade reiniciada \u00e0s ' + new Date(stability.failedAt).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'}) + ' \u2014 motivo: falha de comunica\u00e7\u00e3o' : '';
    renderIndicator(badge, state, Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000));
    if (syncing && modal.open) modalText.textContent = 'Sincronizando ' + progress.replace('/', ' de ') + '...';
    if (globalIndicator) globalIndicator.dataset.state = state.kind;
    if (globalSync) {
        globalSync.hidden = !pending.some(op => ['pendente', 'erro', 'resultado_desconhecido'].includes(op.status));
        syncButton(globalSync, !state.canSync || !navigator.locks || !snapshot
            || snapshot.actor.id !== globalIndicator.dataset.actor || snapshot.environment_id !== globalIndicator.dataset.environment, state.count, state.kind === 'syncing');
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
    list.replaceChildren();
    for (const op of operations.sort((a, b) => b.sequence - a.sequence)) {
        const row = document.createElement('li');
        row.textContent = `${op.status} · ${op.payload.observacao} · tarefa #${op.aggregate_id} · usuário ${op.actor_id} · ${op.operation_id}` + (op.server_result?.record_id ? ` · evento #${op.server_result.record_id}` : '') + (op.last_error ? ' · ' + op.last_error : '');
        list.append(row);
    }
    document.getElementById('offline-diagnostic').textContent = snapshot ? `Usuário: ${snapshot.actor.name} · ambiente: ${snapshot.environment_id} · snapshot: ${snapshot.prepared_at} · limite: 200 tarefas pendentes` : 'Prepare online após autenticar-se.';
}
async function failConnection() {
    stability.reset(); stability.reconnecting = true; stopped = true;
    if (syncing) notice = {kind: 'error', label: 'Sincroniza\u00e7\u00e3o interrompida — opera\u00e7\u00f5es preservadas', until: Date.now() + 10000}; inFlight?.abort();
    await refreshCommunication({type: 'failure', at: Date.now()});
    await render();
}
async function fetchTimed(url, options = {}) {
    const controller = new AbortController();
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
    // Concurrent callers await the complete health/session/storage/render cycle.
    if (!probePending) probePending = performProbe().finally(() => { probePending = null; });
    return probePending;
}
async function performProbe() {
    if (leaving || document.hidden) return;
    const generation = observationGeneration;
    // Read the failure marker before starting: millisecond timestamp ties must not reject a genuinely new check.
    const previous = await repo.communication(communicationScope());
    const started_at = Date.now();
    let health;
    try {
        const response = await fetchTimed('/api/offline/health/');
        health = await response.json();
        const environment = globalIndicator ? globalIndicator.dataset.environment : snapshot?.environment_id;
        if (!response.ok || typeof health.environment !== 'string' || !health.environment || !validHealth(health, environment || health.environment)) throw new Error('Health-check inv\u00e1lido.');
    } catch {
        if (!leaving && !document.hidden && generation === observationGeneration) {
            observedSince = null;
            await failConnection();
        }
        return;
    }
    if (leaving || document.hidden || generation !== observationGeneration) return;
    healthEnvironment = health.environment;
    const at = Date.now();
    await refreshCommunication({type: 'success', at, started_at, observed_since: observedSince, failure_seen: previous.failed_at ?? null});
    observedSince = at;
    await checkSession();
    await render();
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
    if (!snapshot || (globalIndicator && (snapshot.actor.id !== globalIndicator.dataset.actor || snapshot.environment_id !== globalIndicator.dataset.environment))) return;
    if (!navigator.locks || !stability.ready(performance.now(), Date.now()) || syncing) return;
    if (!await checkSession()) { message('Autentique-se novamente e confira sua permissão. Fila preservada.'); await render(); return; }
    await navigator.locks.request('offline-pilot-sync', {ifAvailable: true}, async lock => {
        if (!lock) { message('Outra aba está sincronizando.'); return; }
        await refreshCommunication();
        if (!stability.ready(performance.now(), Date.now())) return;
        const operations = (await repo.all('operations')).filter(op => op.actor_id === snapshot.actor.id && op.environment_id === snapshot.environment_id && ['pendente', 'resultado_desconhecido', 'erro'].includes(op.status)).sort((a, b) => a.sequence - b.sequence);
        if (!operations.length || !await confirmSynchronization(operations.length)) return;
        syncing = true; stopped = false; notice = null; progress = '0/' + operations.length; await changed();
        let confirmed = 0;
        try {
            // Refresh session/CSRF online without overwriting the prepared snapshot.
            const session = await fetchTimed('/api/offline/snapshot/');
            if ([401, 403].includes(session.status)) requireAuthentication();
            if (!session.ok) throw new Error('Autentique-se novamente e confira sua permissão. Fila preservada.');
            const identity = await session.json();
            if (identity.actor.id !== snapshot.actor.id || identity.environment_id !== snapshot.environment_id || identity.protocol_version !== 1) throw new Error('Usuário/ambiente diferente. Fila preservada.');
            csrf = identity.csrf_token;
            for (let index = 0; index < operations.length; index++) {
                await refreshCommunication();
                if (stopped || !stability.ready(performance.now(), Date.now())) { stopped = true; await probe(); break; }
                const operation = {...operations[index], status: 'enviando', attempts: operations[index].attempts + 1};
                await repo.put('operations', operation); progress = `${index + 1}/${operations.length}`; await changed();
                try {
                    const response = await fetchTimed('/api/offline/observations/', {method: 'POST', synchronize: true,
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                        body: JSON.stringify({...commandOf(operation), payload_hash: operation.payload_hash})});
                    if ([401, 403].includes(response.status)) {
                        requireAuthentication();
                        await repo.put('operations', {...operation, status: 'erro', last_error: 'Sessão/permissão/CSRF: autentique-se novamente. Dados preservados.'});
                        stopped = true; message('Autenticação necessária. Fila preservada.'); break;
                    }
                    if ([400, 413].includes(response.status)) {
                        await repo.put('operations', {...operation, status: 'erro', last_error: 'Servidor recusou o formato. Revisão técnica necessária.'});
                        stopped = true; message('Erro de validação. Dados preservados.'); break;
                    }
                    if (![200, 409].includes(response.status)) throw new Error('Servidor indisponível.');
                    const result = await response.json();
                    if (!validReceipt(result, operation)) throw new Error('Confirmação do servidor inválida.');
                    await repo.record(operation, result); if (result.status === 'confirmada') confirmed++; await changed();
                } catch (error) {
                    await repo.put('operations', {...operation, status: 'resultado_desconhecido', last_error: error.message + ' Reenviar o mesmo UUID após estabilidade.'});
                    // Preserve the existing new stability window after an uncertain send,
                    // but let the health-check decide whether the server is offline.
                    stopped = true;
                    await probe(); message('Sincronização interrompida — operações preservadas'); break;
                }
            }

        } catch (error) { message(error.message); }
        finally {
            const results = await repo.all('operations');
            const conflict = results.some(op => op.actor_id === snapshot.actor.id && op.environment_id === snapshot.environment_id && op.status === 'conflito');
            const remaining = results.some(op => operations.some(original => original.operation_id === op.operation_id) && op.status !== 'confirmada');
            const text = conflict ? 'Conflito de sincronização — revisão necessária' : stopped || remaining ? 'Sincronização interrompida — operações preservadas' : 'Sincronização concluída';
            finishModal(text);
            const feedback = document.getElementById('offline-message');
            if (feedback) feedback.textContent = text;
            notice = {kind: conflict ? 'conflict' : stopped || remaining ? 'error' : 'success', label: text, until: Date.now() + 10000};
            syncing = false; csrf = ''; await changed();
        }
    });
}
async function exportDiagnostic() {
    const data = {format: 'offline-pilot-diagnostic-v1', origin: location.origin, exported_at: new Date().toISOString(),
        metadata: await repo.all('metadata'), snapshots: await repo.all('snapshots'), operations: await repo.all('operations'), history: await repo.all('history')};
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'offline-piloto-diagnostico.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function start() {
    await checkSession();
    repo = new Repository(await openDB());
    snapshot = await repo.get('snapshots', 'pilot');
    if (!pilot && !globalIndicator) return;
    if (navigator.locks) await navigator.locks.request('offline-pilot-sync', {ifAvailable: true}, async lock => { if (lock) await repo.recover(); });
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
    window.addEventListener('pagehide', () => { leaving = true; observationGeneration++; observedSince = null; inFlight?.abort(); });
    window.addEventListener('pageshow', () => { leaving = false; void checkSession(); void probe().then(() => probe()).catch(error => message(error.message)); });
    document.addEventListener('visibilitychange', () => { observationGeneration++; observedSince = null; if (!document.hidden) void probe().then(() => probe()).catch(error => message(error.message)); });
    setInterval(() => { void probe().catch(error => message(error.message)); }, POLICY.interval);
    setInterval(() => { if (syncing) broadcast(); if (stopped === false && syncing && !stability.valid(performance.now(), Date.now())) void probe().catch(error => message(error.message)); void render().catch(error => message(error.message)); }, 1000);
    await render(); await probe(); await render();
}
export const initialized = start().catch(error => { if (badge) badge.textContent = 'Armazenamento offline indisponível'; message('Não foi possível abrir o armazenamento local: ' + error.message); });
