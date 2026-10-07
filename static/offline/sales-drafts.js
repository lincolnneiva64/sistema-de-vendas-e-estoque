import {hash, reserveSequence} from './core.js';
// Draft editing stays local; explicit finalization atomically creates one command.
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
async function access(repo, scope, write, action, stores = ['metadata']) {
    const key = draftKey(scope), revisionKey = key + ':revision';
    let error;
    return repo.transaction(stores, write, (tx, done) => {
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
                    store.get(key).onsuccess = safe(record => done(action({tx, store, device, key, revisionKey, revision, record})));
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
function finalized(record, scope, revision) {
    if (record?.tipo !== 'venda_concluida_offline') return null;
    if (record.schema_version !== DRAFT_SCHEMA || !uuid(record.draft_id) || !uuid(record.operation_id)
        || record.actor_id !== scope.actor_id || record.environment_id !== scope.environment_id
        || record.device_id !== scope.device_id || record.revision !== revision)
        throw new Error('Conclusão local incompatível. Operação preservada.');
    return record;
}
function rejectFinalized(record, scope, revision) {
    const marker = finalized(record, scope, revision);
    if (marker) {
        const error = new Error('Venda já concluída offline. Operação preservada: ' + marker.operation_id);
        error.code = 'draft-finalized-offline'; error.operation_id = marker.operation_id;
        throw error;
    }
}
export function loadDraft(repo, scope) {
    return access(repo, scope, false, ({revision, record}) => {
        const finalization = finalized(record, scope, revision);
        if (finalization) return {revision, draft:null, finalization};
        const safe = validateRecord(record, scope, revision), confirmation = submissionMarker(scope, safe);
        return {revision, draft:confirmation?.estado === 'concluido' ? null : safe, confirmation};
    });
}
export function saveDraft(repo, scope, assembly, expectedRevision, replaceConfirmed = false) {
    const safe = projectAssembly(assembly);
    return access(repo, scope, true, ({store, key, revisionKey, revision, record}) => {
        rejectFinalized(record, scope, revision);
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
    return access(repo, scope, true, ({store, key, revisionKey, revision, record}) => {
        rejectFinalized(record, scope, revision);
        if (revision !== expectedRevision) throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de descartar.');
        store.delete(key);
        // Keep the revision fence after deletion so an old tab cannot resurrect it.
        store.put({key:revisionKey, revision:revision + 1});
        return {revision:revision + 1, draft:null};
    });
}

function salePayload(record, origin) {
    const draft = projectAssembly(record);
    if (!draft.itens.length || draft.itens.length > 200 || !draft.data_venda || !draft.tipo_venda
        || draft.operador.length > 120 || draft.lancamento)
        throw new Error('Revise data, pagamento e itens. Adicione ou encerre a linha em montagem antes de concluir.');
    const payload = {schema_version:1, cliente_id:draft.cliente?.id || null,
        data_venda:draft.data_venda, data_vencimento:draft.data_vencimento,
        tipo_pagamento:draft.tipo_venda, operador:draft.operador,
        itens:draft.itens.map(item => {
            if (!item.unidade || item.unidade.length > 40 || !Number.isFinite(Number(item.preco_unitario)) || Number(item.preco_unitario) <= 0)
                throw new Error('Revise unidade e preço dos itens.');
            return Object.fromEntries(['produto_id','quantidade','unidade','preco_unitario'].map(key => [key,item[key]]));
        })};
    if (origin !== undefined) {
        if (!origin || Object.keys(origin).some(key => !['caixa','banco'].includes(key))
            || Object.values(origin).some(value => typeof value !== 'string' || value.length > 80
                || !/^(?:\d+(?:\.\d{1,6})?|\d+(?:\.\d{3})*,\d{1,2})$/.test(value))) throw new Error('Origem de recebimento inválida.');
        payload.origem_recebimento = {...origin};
    }
    return payload;
}

export async function finalizeDraftOffline(repo, scope, {revision:expectedRevision, draft_id:draftId, origem_recebimento:origin}) {
    // The draft already owns a randomUUID, persisted once at its creation.
    // Promote it to operation identity so even an aborted local retry reuses it.
    const operationId = draftId;
    for (;;) {
        const prepared = await access(repo, scope, false, ({revision, record, device}) => {
            const marker = finalized(record, scope, revision);
            if (marker) {
                if (marker.draft_id !== draftId) throw new Error('A conclusão pertence a outra montagem.');
                return {finalization:marker};
            }
            const draft = validateRecord(record, scope, revision);
            if (!draft || draft.draft_id !== draftId || revision !== expectedRevision)
                throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de concluir.');
            if (submissionMarker(scope, draft)) throw new Error('Há envio online anterior. Confira seu resultado antes de concluir offline.');
            if (!Number.isSafeInteger(device.sequence) || device.sequence < 0 || device.sequence >= Number.MAX_SAFE_INTEGER)
                throw new Error('Sequência local inválida.');
            return {revision, sequence:device.sequence, payload:salePayload(draft, origin)};
        });
        if (prepared.finalization) return {finalization:prepared.finalization, alreadyFinalized:true};
        const command = {operation_id:operationId, device_id:scope.device_id, actor_id:scope.actor_id,
            environment_id:scope.environment_id, type:'criar_venda', schema_version:1,
            aggregate_id:operationId, payload:prepared.payload, created_at:new Date().toISOString(), sequence:prepared.sequence + 1};
        const operation = {...command, payload_hash:await hash(command), status:'pendente', attempts:0, last_error:'', server_result:null};
        if (new TextEncoder().encode(JSON.stringify(operation)).length > 20000) throw new Error('Venda local excede o limite do protocolo. Rascunho preservado.');
        const result = await access(repo, scope, true, ({tx, store, device, key, revisionKey, revision, record}) => {
            const marker = finalized(record, scope, revision);
            if (marker) {
                if (marker.draft_id !== draftId) throw new Error('A conclusão pertence a outra montagem.');
                return {finalization:marker, alreadyFinalized:true};
            }
            const draft = validateRecord(record, scope, revision);
            if (!draft || draft.draft_id !== draftId || revision !== prepared.revision)
                throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de concluir.');
            if (submissionMarker(scope, draft)) throw new Error('Há envio online anterior. Confira seu resultado antes de concluir offline.');
            if (device.sequence !== prepared.sequence) return {retry:true};
            const identity = reserveSequence(store, device);
            if (identity.sequence !== command.sequence) throw new Error('Sequência mudou. Rascunho preservado.');
            tx.objectStore('operations').add(operation);
            const finalization = {key, tipo:'venda_concluida_offline', schema_version:DRAFT_SCHEMA,
                ...scope, draft_id:draftId, revision:revision + 1, operation_id:operationId, finalized_at:command.created_at};
            store.put(finalization); store.put({key:revisionKey, revision:revision + 1});
            return {finalization, operation, alreadyFinalized:false};
        }, ['metadata','operations']);
        if (!result.retry) return result;
    }
}
