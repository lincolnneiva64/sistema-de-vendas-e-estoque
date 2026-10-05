export const POLICY = Object.freeze({interval: 30000, timeout: 5000, window: 900000, maxGap: 60000});
export const DB_NAME = 'vendas-offline-pilot';
export const COMMAND_FIELDS = ['operation_id', 'device_id', 'actor_id', 'environment_id', 'type', 'schema_version', 'aggregate_id', 'payload', 'created_at', 'sequence'];

export function canonical(value) {
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
    return JSON.stringify(value);
}
export function commandOf(operation) {
    return Object.fromEntries(COMMAND_FIELDS.map(key => [key, operation[key]]));
}
export async function hash(command) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(command)));
    return [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
}

export class Stability {
    constructor(policy = POLICY) { this.policy = policy; this.reset(); }
    reset() { this.started = null; this.last = null; this.lastWall = null; this.connected = false; }
    success(now, wall) {
        if (this.last === null || now - this.last > this.policy.maxGap || wall - this.lastWall > this.policy.maxGap || wall < this.lastWall) this.started = now;
        this.last = now; this.lastWall = wall; this.connected = true;
    }
    valid(now, wall) {
        if (this.last !== null && (now - this.last > this.policy.maxGap || wall - this.lastWall > this.policy.maxGap || wall < this.lastWall)) this.reset();
        return this.connected;
    }
    ready(now, wall) { return this.valid(now, wall) && this.last - this.started >= this.policy.window; }
    elapsed(now, wall) { return this.valid(now, wall) ? Math.max(0, this.last - this.started) : 0; }
    restore(value, now, wall) {
        this.reset();
        this.reconnecting = !!value?.reconnecting;
        if (!value?.connected || !Number.isFinite(value.stable_since) || !Number.isFinite(value.last_success_at)
            || value.stable_since > value.last_success_at || wall < value.last_success_at
            || wall - value.last_success_at > this.policy.maxGap) return;
        this.lastWall = value.last_success_at;
        this.last = now - (wall - value.last_success_at);
        this.started = this.last - (value.last_success_at - value.stable_since);
        this.connected = true;
    }
}

export function openDB() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => {
            const db = request.result;
            for (const [name, keyPath] of [['metadata', 'key'], ['operations', 'operation_id'], ['history', 'operation_id'], ['snapshots', 'key']]) {
                if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, {keyPath});
            }
        };
        request.onsuccess = () => {
            request.result.onversionchange = () => request.result.close();
            resolve(request.result);
        };
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error('Feche outras abas para atualizar o armazenamento local.'));
    });
}

export class Repository {
    constructor(db) { this.db = db; }
    transaction(stores, write, work) {
        return new Promise((resolve, reject) => {
            let result;
            const tx = this.db.transaction(stores, write ? 'readwrite' : 'readonly');
            tx.oncomplete = () => resolve(result);
            tx.onabort = () => reject(tx.error || new Error('Gravacao local interrompida.'));
            tx.onerror = () => {}; // onabort is the authoritative failure notification.
            try { work(tx, value => { result = value; }); } catch (error) { tx.abort(); reject(error); }
        });
    }
    get(store, key) {
        return this.transaction([store], false, (tx, done) => { tx.objectStore(store).get(key).onsuccess = event => done(event.target.result); });
    }
    all(store) {
        return this.transaction([store], false, (tx, done) => { tx.objectStore(store).getAll().onsuccess = event => done(event.target.result); });
    }
    put(store, value) { return this.transaction([store], true, tx => tx.objectStore(store).put(value)); }
    communication(scope, event = null) {
        const key = 'communication:' + JSON.stringify([scope.environment_id, scope.actor_id]);
        return this.transaction(['metadata'], true, (tx, done) => {
            const store = tx.objectStore('metadata');
            store.get(key).onsuccess = request => {
                let value = request.target.result || {key, ...scope, connected: false, stable_since: null, last_success_at: null};
                if (event?.type === 'failure') {
                    value = {...value, connected: false, stable_since: null, last_success_at: null,
                        failed_at: event.at, reconnecting: true};
                } else if (event?.type === 'success' && (value.failed_at == null || event.started_at > value.failed_at)) {
                    // An older request cannot undo a failure observed by another tab.
                    if (value.last_success_at == null || event.at >= value.last_success_at) {
                        const continuous = value.connected && event.at >= value.last_success_at
                            && event.at - value.last_success_at <= POLICY.maxGap;
                        value = {...value, connected: true, stable_since: continuous ? value.stable_since : event.at,
                            last_success_at: event.at};
                    }
                }
                if (event) store.put(value);
                done(value);
            };
        });
    }
    identity() {
        return this.transaction(['metadata'], true, (tx, done) => {
            const store = tx.objectStore('metadata');
            store.get('device').onsuccess = event => {
                const value = event.target.result || {key: 'device', id: crypto.randomUUID(), sequence: 0};
                value.sequence += 1;
                store.put(value); done(value);
            };
        });
    }
    async create(snapshot, task, text) {
        const identity = await this.identity();
        const command = {operation_id: crypto.randomUUID(), device_id: identity.id, actor_id: snapshot.actor.id,
            environment_id: snapshot.environment_id, type: task.kind === 'entrega_venda' ? 'observacao_entrega_venda' : 'observacao_operacional', schema_version: 1,
            aggregate_id: task.id, payload: task.kind === 'entrega_venda' ? {rota_id: task.rota_id, venda_id: task.venda_id, tarefa_status: task.status, observacao: text} : {locacao_id: task.locacao_id, tarefa_status: task.status, observacao: text},
            created_at: new Date().toISOString(), sequence: identity.sequence};
        const operation = {...command, payload_hash: await hash(command), status: 'pendente', attempts: 0, last_error: '', server_result: null};
        await this.transaction(['operations'], true, tx => tx.objectStore('operations').add(operation));
        return operation;
    }
    async recover() {
        return this.transaction(['operations'], true, tx => {
            tx.objectStore('operations').openCursor().onsuccess = event => {
                const cursor = event.target.result;
                if (!cursor) return;
                if (cursor.value.status === 'enviando') cursor.update({...cursor.value, status: 'resultado_desconhecido', last_error: 'Envio interrompido; reenviar o mesmo UUID.'});
                cursor.continue();
            };
        });
    }
    record(operation, result) {
        const updated = {...operation, status: result.status, server_result: result, last_error: result.erro || ''};
        return this.transaction(['operations', 'history'], true, tx => {
            tx.objectStore('operations').put(updated);
            tx.objectStore('history').put({...updated, synchronized_at: new Date().toISOString()});
        });
    }
}

export function validHealth(data, environment) {
    return data && data.ok === true && data.environment === environment && data.protocol_version === 1;
}
export function validReceipt(result, operation) {
    return result && result.operation_id === operation.operation_id && result.hash === operation.payload_hash
        && (result.status === 'conflito' || (result.status === 'confirmada' && Number.isSafeInteger(result.record_id)
            && result.record_id > 0 && typeof result.completed_at === 'string' && Number.isFinite(Date.parse(result.completed_at))));
}

// Both the detailed panel and the global indicator consume this projection.
export function indicatorState({operations, stability, now, wall, syncing = false, progress = '', notice = null, authenticated = true}) {
    const pending = operations.filter(op => op.status !== 'confirmada');
    const count = pending.length;
    const connected = stability.valid(now, wall);
    const ready = stability.ready(now, wall);
    const sendable = pending.some(op => ['pendente', 'erro', 'resultado_desconhecido'].includes(op.status));
    const quantity = count + (count === 1 ? ' operação' : ' operações');
    let kind, label;
    if (!connected) { kind = 'offline'; label = count ? 'OFFLINE — ' + quantity + ' aguardando sincronização' : 'OFFLINE — trabalhando localmente'; }
    else if (!authenticated) { kind = 'auth'; label = 'ONLINE — autenticação necessária para preparar/sincronizar' + (count ? ' · ' + quantity + ' preservadas' : ''); }
    else if (syncing) { kind = 'syncing'; label = 'Sincronizando ' + progress.replace('/', ' de ') + '\u2026'; }
    else if (pending.some(op => op.status === 'conflito')) { kind = 'conflict'; label = 'Conflito de sincronização — revisão necessária'; }
    else if (notice && notice.until > wall) { kind = notice.kind; label = notice.label; }
    else if (ready && sendable) { kind = 'ready'; label = 'Conexão estável — ' + quantity + (count === 1 ? ' pronta' : ' prontas') + ' para sincronizar'; }
    else if (ready && pending.some(op => ['erro', 'resultado_desconhecido'].includes(op.status))) { kind = 'error'; label = 'Erro de sincronização — operações preservadas'; }
    else if (!ready && (count || stability.reconnecting)) { kind = 'waiting'; label = 'Conexão restabelecida — Verificando estabilidade para sincronização segura (' + Math.floor(stability.elapsed(now, wall) / 60000) + ' de 15 minutos)' + (count ? ' \u00b7 ' + quantity + ' aguardando' : ''); }
    else { kind = 'online'; label = 'ONLINE'; }
    return {kind, label, count, canSync: ready && authenticated && sendable && !syncing};
}
