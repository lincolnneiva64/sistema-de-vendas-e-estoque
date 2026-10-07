// Local assembly only. Never reads/writes operations or reserves a sequence.
export const DRAFT_SCHEMA = 1;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const id = value => typeof value === 'string' && /^[1-9]\d*$/.test(value);
const decimal = value => typeof value === 'string' && /^\d+(\.\d{1,6})?$/.test(value);
const string = (value, max = 500) => typeof value === 'string' && value.length <= max;
const date = value => value === '' || /^\d{4}-\d{2}-\d{2}$/.test(value);

export function draftKey(scope) {
    if (!scope || !id(scope.actor_id) || !string(scope.environment_id) || !scope.environment_id || !uuid(scope.device_id))
        throw new Error('Identidade local do rascunho inválida.');
    return 'rascunho_venda:' + JSON.stringify([scope.environment_id, scope.actor_id, scope.device_id]);
}
// Independent, small recovery marker: IDB cleanup may be unavailable after POST.
// No payload, queue, operation ID or sequence is stored here.
function confirmationKey(scope, record) {
    return draftKey(scope) + ':confirmation:' + record.draft_id + ':' + record.revision;
}
export function prepareDraftSubmission(scope, record) {
    if (!record) return;
    const key = confirmationKey(scope, record);
    if (localStorage.getItem(key)) throw new Error('Rascunho já enviado ou confirmado. Reabra a tela antes de repetir a gravação.');
    localStorage.setItem(key, JSON.stringify({estado:'enviando'}));
    if (!localStorage.getItem(key)) throw new Error('Não foi possível proteger o rascunho antes da gravação online.');
}
export function confirmDraftSubmission(scope, record, vendaId) {
    if (record) localStorage.setItem(confirmationKey(scope, record), JSON.stringify({estado:'concluido', venda_id:String(vendaId)}));
}
export function releaseDraftSubmission(scope, record) {
    if (record) localStorage.removeItem(confirmationKey(scope, record));
}
function submissionMarker(scope, record) {
    // If storage cannot be read, fail closed rather than restore a potentially sold draft.
    if (!record) return null;
    const value = localStorage.getItem(confirmationKey(scope, record));
    return value ? JSON.parse(value) : null;
}
export function projectAssembly(value) {
    if (!value || !Array.isArray(value.itens) || value.itens.length > 1000
        || !string(value.operador) || !['', 'À vista', 'A prazo', 'consumo_proprio'].includes(value.tipo_venda)
        || !date(value.data_venda) || !date(value.data_vencimento)) throw new Error('Montagem local inválida.');
    const cliente = value.cliente == null ? null : {
        id: value.cliente.id, nome: value.cliente.nome, prazo: value.cliente.prazo,
    };
    if (cliente && (!id(cliente.id) || !string(cliente.nome) || !Number.isSafeInteger(cliente.prazo) || cliente.prazo < 0))
        throw new Error('Cliente do rascunho inválido.');
    const itens = value.itens.map(item => {
        if (!id(item.produto_id) || !string(item.produto_nome) || !string(item.unidade, 50)
            || !decimal(item.quantidade) || Number(item.quantidade) <= 0 || !decimal(item.preco_unitario)
            || !decimal(item.fator) || !string(item.unidade1, 50) || !string(item.unidade2, 50)
            || typeof item.fracionado !== 'boolean') throw new Error('Item do rascunho inválido.');
        const subtotal = Number(item.quantidade) * Number(item.preco_unitario);
        if (!Number.isFinite(subtotal) || subtotal > 1e12) throw new Error('Valor local inválido.');
        return {produto_id: item.produto_id, produto_nome: item.produto_nome, quantidade: item.quantidade,
            unidade: item.unidade, preco_unitario: item.preco_unitario, fator: item.fator,
            unidade1: item.unidade1, unidade2: item.unidade2, fracionado: item.fracionado,
            subtotal_referencia: subtotal.toFixed(2)};
    });
    let lancamento = null;
    if (value.lancamento) {
        const editor = value.lancamento;
        if (!id(editor.produto_id) || !string(editor.produto_nome) || !string(editor.quantidade, 50)
            || !string(editor.unidade, 50) || !string(editor.preco, 50) || !decimal(editor.fator)
            || !string(editor.unidade1, 50) || !string(editor.unidade2, 50) || typeof editor.fracionado !== 'boolean'
            || !(editor.indice_edicao === null || (Number.isSafeInteger(editor.indice_edicao)
                && editor.indice_edicao >= 0 && editor.indice_edicao < itens.length))) throw new Error('Linha em montagem inválida.');
        lancamento = Object.fromEntries(['produto_id','produto_nome','quantidade','unidade','preco','fator',
            'unidade1','unidade2','fracionado','indice_edicao'].map(field => [field, editor[field]]));
    }
    return {cliente, operador: value.operador, tipo_venda: value.tipo_venda, lancamento,
        data_venda: value.data_venda, data_vencimento: value.data_vencimento, itens,
        total_referencia: itens.reduce((sum, item) => sum + Number(item.subtotal_referencia), 0).toFixed(2)};
}

export async function draftScope(repo, identity) {
    if (!identity?.actor_id || !identity.environment_id) return null;
    let error;
    return repo.transaction(['metadata'], true, (tx, done) => {
        const store = tx.objectStore('metadata');
        store.get('sales-identity').onsuccess = event => {
            const current = event.target.result;
            if (current?.actor_id !== identity.actor_id || current?.environment_id !== identity.environment_id) { done(null); return; }
            store.get('device').onsuccess = event => {
                try {
                    const device = event.target.result || {key:'device', id:crypto.randomUUID(), sequence:0};
                    const scope = {actor_id:identity.actor_id, environment_id:identity.environment_id, device_id:device.id};
                    draftKey(scope);
                    if (!event.target.result) store.put(device);
                    done(scope);
                } catch (caught) { error = caught; tx.abort(); }
            };
        };
    }).catch(caught => { throw error || caught; });
}

// All identity checks and compare-and-swap happen in the SAME IDB transaction.
async function access(repo, scope, write, action) {
    const key = draftKey(scope), revisionKey = key + ':revision';
    let error;
    return repo.transaction(['metadata'], write, (tx, done) => {
        const store = tx.objectStore('metadata');
        function safe(callback) { return event => { try { callback(event.target.result); } catch (caught) { error = caught; tx.abort(); } }; }
        store.get('device').onsuccess = safe(device => {
            if (device?.id !== scope.device_id) throw new Error('Dispositivo do rascunho mudou.');
            store.get('sales-identity').onsuccess = safe(identity => {
                if (identity?.actor_id !== scope.actor_id || identity?.environment_id !== scope.environment_id)
                    throw new Error('Usuário ou ambiente do rascunho mudou.');
                store.get(revisionKey).onsuccess = safe(version => {
                    const revision = version?.revision || 0;
                    if (!Number.isSafeInteger(revision) || revision < 0 || revision >= Number.MAX_SAFE_INTEGER)
                        throw new Error('Revisão local inválida. O rascunho foi preservado.');
                    store.get(key).onsuccess = safe(record => done(action({store, key, revisionKey, revision, record})));
                });
            });
        });
    }).catch(caught => { throw error || caught; });
}
function validateRecord(record, scope, revision) {
    if (!record) return null;
    if (record.tipo !== 'rascunho_venda' || record.schema_version !== DRAFT_SCHEMA || !uuid(record.draft_id)
        || record.environment_id !== scope.environment_id || record.actor_id !== scope.actor_id
        || record.device_id !== scope.device_id || record.revision !== revision
        || !Number.isFinite(Date.parse(record.criado_em)) || !Number.isFinite(Date.parse(record.atualizado_em)))
        throw new Error('Rascunho local incompatível. O conteúdo salvo foi preservado.');
    return {key:record.key, tipo:record.tipo, schema_version:record.schema_version,
        draft_id:record.draft_id, criado_em:record.criado_em, atualizado_em:record.atualizado_em,
        environment_id:record.environment_id, actor_id:record.actor_id, device_id:record.device_id,
        revision, ...projectAssembly(record)};
}
export function loadDraft(repo, scope) {
    return access(repo, scope, false, ({revision, record}) => {
        const safe = validateRecord(record, scope, revision), confirmation = submissionMarker(scope, safe);
        return {revision, draft:confirmation?.estado === 'concluido' ? null : safe, confirmation};
    });
}
export function saveDraft(repo, scope, assembly, expectedRevision, replaceConfirmed = false) {
    const safe = projectAssembly(assembly);
    return access(repo, scope, true, ({store, key, revisionKey, revision, record}) => {
        if (revision !== expectedRevision) throw new Error('Rascunho atualizado em outra aba. Reabra a tela para carregar a versão salva.');
        const existing = validateRecord(record, scope, revision);
        const confirmation = submissionMarker(scope, existing);
        if (confirmation?.estado === 'concluido' && !replaceConfirmed) throw new Error('Venda do rascunho já enviada ou confirmada. Reabra a tela antes de montar outra venda.');
        const previous = confirmation && replaceConfirmed ? null : existing;
        const now = new Date().toISOString();
        const next = {key, tipo:'rascunho_venda', schema_version:DRAFT_SCHEMA,
            draft_id:previous?.draft_id || crypto.randomUUID(), criado_em:previous?.criado_em || now,
            atualizado_em:now, environment_id:scope.environment_id, actor_id:scope.actor_id,
            device_id:scope.device_id, revision:revision + 1, ...safe};
        store.put(next); store.put({key:revisionKey, revision:next.revision});
        return {revision:next.revision, draft:next};
    });
}
export function discardDraft(repo, scope, expectedRevision) {
    return access(repo, scope, true, ({store, key, revisionKey, revision}) => {
        if (revision !== expectedRevision) throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de descartar.');
        store.delete(key);
        // Keep the revision fence after deletion so an old tab cannot resurrect it.
        store.put({key:revisionKey, revision:revision + 1});
        return {revision:revision + 1, draft:null};
    });
}
