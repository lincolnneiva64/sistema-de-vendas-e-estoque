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
    ready(now, wall) { return this.valid(now, wall) && now - this.started >= this.policy.window; }
    elapsed(now, wall) { return this.valid(now, wall) ? Math.max(0, now - this.started) : 0; }
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
            environment_id: snapshot.environment_id, type: 'observacao_operacional', schema_version: 1,
            aggregate_id: task.id, payload: {locacao_id: task.locacao_id, tarefa_status: task.status, observacao: text},
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
