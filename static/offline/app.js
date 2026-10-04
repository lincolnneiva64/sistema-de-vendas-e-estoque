import {POLICY, Repository, Stability, openDB, commandOf, validHealth, validReceipt} from './core.js';

const pilot = !!document.getElementById('offline-pilot');
const badge = document.getElementById('offline-status');
let repo, snapshot, csrf = '', inFlight, syncing = false, saving = false, stopped = false, progress = '', probing = false;
const stability = new Stability();
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
function message(text) {
    const target = document.getElementById('offline-message');
    if (target) target.textContent = text;
}
function changed() { channel?.postMessage('changed'); return render(); }
async function render() {
    if (!repo) return;
    const operations = await repo.all('operations');
    const pending = operations.filter(op => op.status !== 'confirmada');
    const ready = stability.ready(performance.now(), Date.now());
    let label;
    if (syncing) label = 'SINCRONIZANDO ' + progress;
    else if (!stability.valid(performance.now(), Date.now())) label = 'OFFLINE · ' + pending.length + ' operações aguardando sincronização';
    else if (pending.some(op => op.status === 'conflito')) label = 'CONFLITO · revisão necessária';
    else if (pending.some(op => ['erro', 'resultado_desconhecido'].includes(op.status))) label = 'ERRO DE SINCRONIZAÇÃO · dados preservados';
    else if (ready && pending.length) label = 'PRONTO PARA SINCRONIZAR · ' + pending.length + ' operações pendentes';
    else if (pending.length) label = 'CONEXÃO RESTABELECIDA · aguardando estabilidade (' + Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000) + '/15 min)';
    else label = 'ONLINE';
    if (badge) badge.textContent = snapshot ? label : 'Piloto offline · preparar';
    if (!pilot) return;
    document.getElementById('offline-stability').textContent = stability.connected ? 'CONEXÃO ESTÁVEL HÁ ' + Math.floor(stability.elapsed(performance.now(), Date.now()) / 60000) + ' MINUTOS · mínimo 15' : 'Comunicação com o servidor indisponível ou ainda não verificada.';
    const sendable = pending.filter(op => op.actor_id === snapshot?.actor.id && op.environment_id === snapshot?.environment_id && ['pendente', 'resultado_desconhecido', 'erro'].includes(op.status));
    document.getElementById('offline-sync').disabled = !ready || !sendable.length || syncing || !navigator.locks;
    document.getElementById('offline-save').disabled = !snapshot || syncing || saving;
    document.getElementById('offline-prepare').disabled = syncing;
    const list = document.getElementById('offline-operations');
    list.replaceChildren();
    for (const op of operations.sort((a, b) => b.sequence - a.sequence)) {
        const row = document.createElement('li');
        row.textContent = `${op.status} · ${op.payload.observacao} · tarefa #${op.aggregate_id} · usuário ${op.actor_id} · ${op.operation_id}` + (op.server_result?.record_id ? ` · evento #${op.server_result.record_id}` : '') + (op.last_error ? ' · ' + op.last_error : '');
        list.append(row);
    }
    document.getElementById('offline-diagnostic').textContent = snapshot ? `Usuário: ${snapshot.actor.name} · ambiente: ${snapshot.environment_id} · snapshot: ${snapshot.prepared_at} · limite: 200 tarefas pendentes` : 'Prepare online após autenticar-se.';
}
function failConnection() {
    stability.reset(); stopped = true; inFlight?.abort();
    void render().catch(error => message(error.message));
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
async function probe() {
    if (!snapshot || probing) return;
    probing = true;
    try {
        const response = await fetchTimed('/api/offline/health/');
        if (!response.ok || !validHealth(await response.json(), snapshot.environment_id)) throw new Error('Servidor indisponível ou ambiente/protocolo incompatível.');
        stability.success(performance.now(), Date.now());
    } catch { failConnection(); }
    finally { probing = false; await render(); }
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
    try {
        await checkSession();
        const response = await fetchTimed('/api/offline/snapshot/');
        if (response.status === 401) throw new Error('Faça login online e volte para preparar. A fila existente foi preservada.');
        if (!response.ok) throw new Error('Preparação recusada. Confira sua permissão e conexão.');
        const data = await response.json();
        if (data.protocol_version !== 1 || !data.actor?.id || !Array.isArray(data.tasks)) throw new Error('Snapshot inválido.');
        if (snapshot && snapshot.environment_id !== data.environment_id) throw new Error('Ambiente mudou. A fila permanece vinculada ao ambiente anterior.');
        csrf = data.csrf_token;
        const {csrf_token, ...safe} = data; // Never persist CSRF or credentials.
        const next = {...safe, key: 'pilot', prepared_at: new Date().toISOString()};
        await repo.put('snapshots', next);
        snapshot = next; stability.reset(); showTasks();
        let diagnostic = 'Persistência não suportada por este navegador.';
        if (navigator.storage?.persist) {
            try { diagnostic = await navigator.storage.persist() ? 'Armazenamento persistente concedido.' : 'Persistência não concedida; preserve os dados deste site e exporte a fila.'; }
            catch { diagnostic = 'Não foi possível solicitar persistência; dados locais continuam disponíveis.'; }
        }
        await repo.put('metadata', {key: 'persistence', diagnostic});
        document.getElementById('offline-storage').textContent = diagnostic;
        message('Preparação salva neste dispositivo. ' + diagnostic);
        if ('serviceWorker' in navigator) { await navigator.serviceWorker.register('/service-worker.js', {scope: '/'}); await navigator.serviceWorker.ready; }
        else message('Snapshot salvo, mas Service Worker indisponível. Use HTTPS ou localhost para abrir sem rede.');
        await probe(); await changed();
    } catch (error) { message(error.message); }
}
async function checkSession() {
    if (!pilot) return;
    const login = document.getElementById('offline-login');
    const status = document.getElementById('offline-session');
    if (!login || !status) return;
    try {
        const response = await fetchTimed('/api/offline/session/');
        if (!response.ok) throw new Error('Sessão indisponível.');
        const data = await response.json();
        login.hidden = data.authenticated === true;
        status.textContent = data.authenticated
            ? `Autenticado como ${data.username}. ` + (data.can_prepare ? 'Pode preparar / atualizar dados online.' : 'Sem permissão para preparar dados do piloto.')
            : 'Autentique-se online para preparar / atualizar dados.';
    } catch {
        login.hidden = false;
        status.textContent = 'Não foi possível verificar a autenticação online. Dados locais preservados.';
    }
}
async function save(event) {
    event.preventDefault();
    if (saving) return;
    saving = true;
    const button = document.getElementById('offline-save'); button.disabled = true;
    try {
        const task = snapshot?.tasks.find(t => t.id === document.getElementById('offline-task').value);
        const text = document.getElementById('offline-note').value.trim();
        if (!task || !text || text.length > 2000) throw new Error('Escolha uma tarefa preparada e informe até 2000 caracteres.');
        const op = await repo.create(snapshot, task, text);
        message('Observação salva neste dispositivo · ' + op.operation_id);
        document.getElementById('offline-note').value = ''; await changed();
    } catch (error) { message('Não foi possível salvar neste dispositivo: ' + error.message); }
    finally { saving = false; await render(); }
}
async function synchronize() {
    if (!navigator.locks || !stability.ready(performance.now(), Date.now()) || syncing) return;
    await navigator.locks.request('offline-pilot-sync', {ifAvailable: true}, async lock => {
        if (!lock) { message('Outra aba está sincronizando.'); return; }
        if (!stability.ready(performance.now(), Date.now())) return;
        const operations = (await repo.all('operations')).filter(op => op.actor_id === snapshot.actor.id && op.environment_id === snapshot.environment_id && ['pendente', 'resultado_desconhecido', 'erro'].includes(op.status)).sort((a, b) => a.sequence - b.sequence);
        if (!operations.length || !window.confirm(`Conexão estável. ${operations.length} operações aguardando sincronização. Deseja sincronizar agora?`)) return;
        syncing = true; stopped = false;
        try {
            // Refresh session/CSRF online without overwriting the prepared snapshot.
            const session = await fetchTimed('/api/offline/snapshot/');
            if (!session.ok) throw new Error('Autentique-se novamente e confira sua permissão. Fila preservada.');
            const identity = await session.json();
            if (identity.actor.id !== snapshot.actor.id || identity.environment_id !== snapshot.environment_id || identity.protocol_version !== 1) throw new Error('Usuário/ambiente diferente. Fila preservada.');
            csrf = identity.csrf_token;
            for (let index = 0; index < operations.length; index++) {
                if (stopped || !stability.ready(performance.now(), Date.now())) { failConnection(); break; }
                const operation = {...operations[index], status: 'enviando', attempts: operations[index].attempts + 1};
                await repo.put('operations', operation); progress = `${index + 1}/${operations.length}`; await changed();
                try {
                    const response = await fetchTimed('/api/offline/observations/', {method: 'POST', synchronize: true,
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                        body: JSON.stringify({...commandOf(operation), payload_hash: operation.payload_hash})});
                    if ([401, 403].includes(response.status)) {
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
                    await repo.record(operation, result); await changed();
                } catch (error) {
                    await repo.put('operations', {...operation, status: 'resultado_desconhecido', last_error: error.message + ' Reenviar o mesmo UUID após estabilidade.'});
                    failConnection(); message('Erro de sincronização. Dados preservados.'); break;
                }
            }
            if (!stopped) message('Sincronização concluída. Confira os resultados e eventuais conflitos no histórico.');
        } catch (error) { message(error.message); }
        finally { syncing = false; csrf = ''; await changed(); }
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
    if (!pilot && !snapshot) return;
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
    channel?.addEventListener('message', () => render().catch(error => message(error.message)));
    window.addEventListener('offline', failConnection);
    window.addEventListener('online', () => { void checkSession(); void probe(); });
    window.addEventListener('pagehide', () => { stability.reset(); inFlight?.abort(); });
    window.addEventListener('pageshow', () => { stability.reset(); void checkSession(); void probe(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { stability.valid(performance.now(), Date.now()); void probe(); } });
    setInterval(() => { void probe().catch(error => message(error.message)); }, POLICY.interval);
    setInterval(() => { if (stopped === false && syncing && !stability.valid(performance.now(), Date.now())) failConnection(); void render().catch(error => message(error.message)); }, 1000);
    await probe(); await render();
}
start().catch(error => { if (badge) badge.textContent = 'Armazenamento offline indisponível'; message('Não foi possível abrir o armazenamento local: ' + error.message); });
