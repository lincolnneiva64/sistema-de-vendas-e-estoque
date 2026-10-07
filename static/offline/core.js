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

// May be used inside a transaction that ALSO persists the operation.
export function reserveSequence(store, device) {
    const value = device || {key:'device', id:crypto.randomUUID(), sequence:0};
    if (!Number.isSafeInteger(value.sequence) || value.sequence < 0 || value.sequence >= Number.MAX_SAFE_INTEGER)
        throw new Error('Sequência local inválida. Dados preservados.');
    const next = {...value, sequence:value.sequence + 1};
    store.put(next);
    return next;
}

export class Stability {
    constructor(policy = POLICY) { this.policy = policy; this.reset(); }
    reset() { this.started = null; this.last = null; this.lastWall = null; this.connected = false; this.accumulated = 0; }
    success(now, wall) {
        if (this.last !== null && now >= this.last && wall >= this.lastWall
            && now - this.last <= this.policy.maxGap && wall - this.lastWall <= this.policy.maxGap)
            this.accumulated += Math.min(now - this.last, wall - this.lastWall);
        this.started ??= now;
        this.last = now; this.lastWall = wall; this.connected = true;
    }
    valid(now, wall) {
        return this.connected && this.last !== null && now >= this.last && wall >= this.lastWall
            && now - this.last <= this.policy.maxGap && wall - this.lastWall <= this.policy.maxGap;
    }
    ready(now, wall) { return this.valid(now, wall) && this.accumulated >= this.policy.window; }
    elapsed() { return this.accumulated; }
    restore(value, now, wall) {
        this.reset();
        this.reconnecting = !!value?.reconnecting;
        this.accumulated = Math.max(0, value?.observed_ms ??
            (value?.last_success_at != null && value?.stable_since != null ? value.last_success_at - value.stable_since : 0));
        this.failedAt = value?.failed_at;
        this.suspect = !!value?.suspect_id;
        if (this.suspect || !value?.connected || !Number.isFinite(value.last_success_at)) return;
        this.lastWall = value.last_success_at;
        this.last = now - (wall - value.last_success_at);
        this.started = this.last - this.accumulated;
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
                // IDB serializes tabs; only a probe for this suspicion may resolve it.
                // confirmed_at fences off requests started before recovery.
                if (event?.type === 'suspect' && !value.suspect_id
                    && (value.confirmed_at == null || event.started_at >= value.confirmed_at)
                    && (value.failed_at == null || event.started_at > value.failed_at || event.failure_seen === value.failed_at)) {
                    value = {...value, connected: false, suspect_id: event.id, suspect_at: event.at,
                        observed_since: null, observed_until: null};
                } else if (event?.type === 'confirmation' && value.suspect_id === event.suspect_id
                    && event.started_at >= value.suspect_at + 5000) {
                    if (event.ok) {
                        value = {...value, suspect_id: null, suspect_at: null, confirmed_at: event.at, connected: true,
                            stable_since: value.stable_since ?? event.at, last_success_at: event.at,
                            observed_since: event.at, observed_until: event.at};
                    } else {
                        value = {...value, suspect_id: null, suspect_at: null, confirmed_at: event.at, connected: false,
                            stable_since: null, last_success_at: null, observed_ms: 0,
                            observed_since: null, observed_until: null, failed_at: event.at,
                            reset_reason: 'falha de comunica\u00e7\u00e3o', reconnecting: true};
                    }
                } else if (event?.type === 'failure' && (value.failed_at == null || event.at > value.failed_at)) {
                    value = {...value, connected: false, stable_since: null, last_success_at: null,
                        observed_ms: 0, observed_since: null, observed_until: null, failed_at: event.at, reset_reason: 'falha de comunica\u00e7\u00e3o', reconnecting: true};
                } else if (event?.type === 'success' && !value.suspect_id
                    && (value.confirmed_at == null || event.started_at >= value.confirmed_at) && (value.failed_at == null || event.started_at > value.failed_at
                    || event.failure_seen === value.failed_at)) {
                    // An older request cannot undo a failure observed by another tab.
                    if (value.last_success_at == null || event.at >= value.last_success_at) {
                        const accumulated = value.observed_ms ?? (value.connected ? value.last_success_at - value.stable_since : 0);
                        const legacyRecovery = value.reconnecting && value.failed_at != null && value.observed_since === undefined;
                        // Missing legacy event cursors may continue from the migrated cursor.
                        // Explicit null still means a new/resumed tab observed no interval.
                        const observedSince = event.observed_since === undefined && value.reconnecting && value.failed_at != null
                            ? value.observed_since : event.observed_since;
                        // Credit only an interval observed by this tab. A new/resumed tab has no predecessor.
                        // Serialized IDB transactions add the union of intervals, never one interval per tab.
                        const from = Math.max(value.observed_until ?? value.last_success_at ?? event.at, observedSince ?? event.at);
                        const delta = !legacyRecovery && value.connected && event.at >= from && event.at - from <= POLICY.maxGap
                            && observedSince != null && event.at - observedSince <= POLICY.maxGap
                            ? event.at - from : 0;
                        const observed = Math.max(0, accumulated) + delta;
                        value = {...value, connected: true, stable_since: value.stable_since ?? event.at,
                            observed_ms: observed, reconnecting: !!value.reconnecting && observed < POLICY.window,
                            observed_since: event.at,
                            observed_until: legacyRecovery || delta > 0 ? event.at : value.observed_until ?? value.last_success_at ?? event.at,
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
                try { done(reserveSequence(store, event.target.result)); }
                catch (_) { tx.abort(); }
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
