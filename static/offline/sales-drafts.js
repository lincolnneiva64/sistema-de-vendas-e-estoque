import {hash, reserveSequence, saleOperationState, canonical, commandOf} from '/offline/assets/2-8f/core.js';
import {prepareRevisionResolution, resolutionFromSnapshot, resolutionObservationKey} from '/offline/assets/2-8g-close/sales-revisions.js';
import {readStock,validateStock,stockSignature,stockBaselineKey} from '/offline/assets/sack-conversion-v1/sales-stock.js';
import {chaveSnapshotComercial} from '/offline/assets/2-8f-fix/commercial.js';
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
    if (scope.tab_id != null && !uuid(scope.tab_id)) throw new Error('Identidade da aba inválida.');
    return 'rascunho_venda:' + JSON.stringify([scope.environment_id, scope.actor_id, scope.device_id,
        ...(scope.tab_id ? [scope.tab_id] : [])]);
}
export const legacyDraftKey = scope => draftKey({...scope, tab_id:null});
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

export async function draftScope(repo, identity, tabId = null) {
    if (!identity?.actor_id || !identity.environment_id) return null;
    if (tabId != null && !uuid(tabId)) throw new Error('Identidade da aba inválida.');
    let error;
    return repo.transaction(['metadata'], true, (tx, done) => {
        const store = tx.objectStore('metadata');
        store.get('sales-identity').onsuccess = event => {
            const current = event.target.result;
            if (current?.actor_id !== identity.actor_id || current?.environment_id !== identity.environment_id) { done(null); return; }
            store.get('device').onsuccess = event => {
                try {
                    const device = event.target.result || {key:'device', id:crypto.randomUUID(), sequence:0};
                    const scope = {actor_id:identity.actor_id, environment_id:identity.environment_id, device_id:device.id,
                        ...(tabId ? {tab_id:tabId} : {})};
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
                    store.get(key).onsuccess = safe(record => {
                        const run = operations => {
                            const invoke = stock => done(action({tx, store, device, key, revisionKey, revision, record, operations,stock}));
                            if (!stores.includes('snapshots')) { invoke(null); return; }
                            tx.objectStore('snapshots').get(chaveSnapshotComercial(scope)).onsuccess = safe(snapshot => {
                                store.getAll().onsuccess = safe(meta => invoke({snapshot:snapshot || null,
                                    baseline:snapshot ? meta.find(row=>row.key===stockBaselineKey(snapshot.snapshot_id)) || null : null,
                                    operations,closures:meta.filter(row=>row.key.startsWith('sales-closure:') && row.receipt)}));
                            });
                        };
                        if (stores.includes('operations')) tx.objectStore('operations').getAll().onsuccess = safe(run);
                        else run(null);
                    });
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
const recoveryClaimKey = key => key + ':recovered';
function recoveryBlock(record, operations, scope) {
    if (ambiguousSales(operations, scope).length)
        return 'Há envio pendente ou resultado desconhecido; sincronize ou consulte antes de recuperar.';
    const operation = operations.find(item => item.operation_id === record.draft_id);
    if (operation) return 'Existe uma operação associada; consulte o resultado antes de recuperar.';
    if (submissionMarker({...scope, tab_id:record.tab_id || null}, record))
        return 'Existe um envio online iniciado ou concluído; recuperação bloqueada.';
    return '';
}
export async function listRecoverableDrafts(repo, scope) {
    const activeKey = draftKey(scope), legacyKey = legacyDraftKey(scope);
    const [metadata, operations] = await Promise.all([repo.all('metadata'), repo.all('operations')]);
    const claims = new Set(metadata.filter(item => item.key?.endsWith(':recovered')).map(item => item.key));
    return metadata.filter(record => record.tipo === 'rascunho_venda' && record.key !== activeKey
        && (record.key === legacyKey || record.key?.startsWith('rascunho_venda:'))
        && record.actor_id === scope.actor_id && record.environment_id === scope.environment_id
        && record.device_id === scope.device_id && Number.isSafeInteger(record.revision)
        && !claims.has(recoveryClaimKey(record.key)))
        .map(record => {
            const reason = recoveryBlock(record, operations, scope);
            return {key:record.key, draft_id:record.draft_id, revision:record.revision,
                updated_at:record.atualizado_em, customer:record.cliente?.nome || '',
                item_count:Array.isArray(record.itens) ? record.itens.length : 0,
                total:record.total_referencia || '', legacy:record.key === legacyKey, blocked:!!reason, reason};
        })
        .sort((left, right) => String(right.updated_at).localeCompare(String(left.updated_at)));
}
export async function recoverDraft(repo, scope, candidate) {
    if (!candidate?.key || candidate.key === draftKey(scope) || !scope.tab_id)
        throw new Error('Selecione um rascunho disponível para esta aba.');
    let error;
    return repo.transaction(['metadata','operations'], true, (tx, done) => {
        const store = tx.objectStore('metadata'), operationsStore = tx.objectStore('operations');
        const sourceRequest = store.get(candidate.key), destinationRequest = store.get(draftKey(scope));
        const sourceFenceRequest = store.get(candidate.key + ':revision');
        const destinationFenceRequest = store.get(draftKey(scope) + ':revision');
        const claimKey = recoveryClaimKey(candidate.key), claimRequest = store.get(claimKey);
        const operationsRequest = operationsStore.getAll();
        let source, sourceFence, destination, fence, claim, operations;
        const settle = () => {
            if (source === undefined || sourceFence === undefined || destination === undefined || fence === undefined
                || claim === undefined || operations === undefined) return;
            try {
                if (destination) throw new Error('Esta aba já possui um rascunho. Ele foi preservado.');
                if (!source || source.draft_id !== candidate.draft_id || source.revision !== candidate.revision
                    || source.actor_id !== scope.actor_id || source.environment_id !== scope.environment_id
                    || source.device_id !== scope.device_id || source.tipo !== 'rascunho_venda')
                    throw new Error('O rascunho mudou desde a listagem. Atualize as opções; nenhum dado foi removido.');
                if (sourceFence?.revision !== source.revision)
                    throw new Error('A revisão salva diverge da trava de concorrência. O rascunho foi preservado.');
                const destinationRevision = fence?.revision ?? 0;
                if (!Number.isSafeInteger(destinationRevision) || destinationRevision < 0
                    || destinationRevision >= Number.MAX_SAFE_INTEGER || source.revision >= Number.MAX_SAFE_INTEGER)
                    throw new Error('Revisão local inválida. O rascunho foi preservado.');
                validateRecord(source, {...scope, tab_id:source.tab_id || null}, source.revision);
                const reason = recoveryBlock(source, operations, scope);
                if (reason) throw new Error(reason);
                const revision = destinationRevision + 1;
                const recovered = {...source, key:draftKey(scope), tab_id:scope.tab_id, revision};
                if (candidate.key === legacyDraftKey(scope)) {
                    if (claim) throw new Error('O rascunho legado já foi recuperado em outra aba.');
                    store.add({key:claimKey, draft_id:source.draft_id, tab_id:scope.tab_id, recovered_at:new Date().toISOString()});
                } else {
                    store.delete(candidate.key);
                    const sourceFenceKey = candidate.key + ':revision';
                    store.put({key:sourceFenceKey, revision:source.revision + 1});
                }
                store.put(recovered);
                store.put({key:draftKey(scope) + ':revision', revision});
                done({revision, draft:validateRecord(recovered, scope, revision), source_preserved:candidate.key === legacyDraftKey(scope)});
            } catch (caught) { error = caught; tx.abort(); }
        };
        sourceRequest.onsuccess = () => { source = sourceRequest.result || null; settle(); };
        sourceFenceRequest.onsuccess = () => { sourceFence = sourceFenceRequest.result || null; settle(); };
        destinationRequest.onsuccess = () => { destination = destinationRequest.result || null; settle(); };
        destinationFenceRequest.onsuccess = () => { fence = destinationFenceRequest.result || null; settle(); };
        claimRequest.onsuccess = () => { claim = claimRequest.result || null; settle(); };
        operationsRequest.onsuccess = () => { operations = operationsRequest.result; settle(); };
    }).catch(caught => { throw error || caught; });
}
// A started upload may still commit. It must never unlock another assembly.
export function ambiguousSales(operations, scope) {
    return operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id
        && op.environment_id === scope.environment_id && (['enviando','resultado_desconhecido'].includes(op.status)
            || op.transport === 'online' && !['confirmada','conflito'].includes(op.status)));
}
function requireUnambiguous(operations, scope) {
    if (ambiguousSales(operations,scope).length) throw new Error('Há venda com resultado desconhecido ou envio em andamento. Consulte o resultado ou sincronize manualmente quando permitido. Operações preservadas.');
}
// Explicit operator action: release only the assembly marker, never the queue.
export async function startNextOfflineDraft(repo, scope, operationId, expectedRevision) {
    const operation = await repo.get('operations',operationId);
    if (!operation || await hash(commandOf(operation)) !== operation.payload_hash)
        throw new Error('Comando local divergente. Venda preservada.');
    const signature = canonical(operation);
    return access(repo,scope,true,({store,key,revisionKey,revision,record,operations}) => {
        const marker = finalized(record,scope,revision);
        const current = operations.find(op => op.operation_id === operationId);
        if (!marker || marker.operation_id !== operationId || revision !== expectedRevision)
            throw new Error('Montagem atualizada em outra aba. Reabra a tela para carregar a versão salva.');
        requireUnambiguous(operations,scope);
        if (marker.conclusion_mode !== 'offline' || !current || canonical(current) !== signature
            || current.type !== 'criar_venda' || current.transport !== 'offline' || current.status !== 'pendente'
            || current.attempts !== 0 || current.server_result || current.actor_id !== scope.actor_id
            || current.environment_id !== scope.environment_id || current.device_id !== scope.device_id
            || current.operation_id !== marker.draft_id || current.aggregate_id !== marker.draft_id)
            throw new Error('Esta montagem não é uma conclusão offline pendente segura. Consulte seu resultado.');
        store.put({key:key + ':last-local', operation_id:operationId});
        store.delete(key);
        store.put({key:revisionKey,revision:revision + 1});
        return {revision:revision + 1,released:true};
    },['metadata','operations']);
}
export function lastLocalDraft(repo,scope) {
    return access(repo,scope,false,({store,key}) => new Promise((resolve,reject) => {
        const request = store.get(key + ':last-local');
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error);
    }));
}
// Only a validated official receipt releases the assembly. Queue/history are read-only.
export async function releaseConfirmedDraft(repo, scope, operationId) {
    const prepared = await prepareRevisionResolution(repo,scope,operationId);
    if (!prepared.view?.recordId) throw new Error('Venda ainda não confirmada oficialmente.');
    return access(repo, scope, true, ({tx, store, key, revisionKey, revision, record}) => {
        const marker = finalized(record, scope, revision);
        if (marker && marker.operation_id !== operationId || !marker && record)
            throw new Error('Montagem atualizada em outra aba. Reabra a tela para carregar a versão salva.');
        if (!marker) return {revision, released:false};
        const request = tx.objectStore('operations').getAll();
        request.onsuccess = () => {
            const operations = request.result;
            store.get(resolutionObservationKey(scope,operationId)).onsuccess = event => {
              try {
                requireUnambiguous(operations,scope);
                const snapshot = {operations,observed:event.target.result || null};
                if (canonical(snapshot) !== prepared.signature) throw new Error('Operações atualizadas durante a liberação. Consulte novamente.');
                const operation = operations.find(op => op.operation_id === operationId);
                const resolved = resolutionFromSnapshot(snapshot,operationId,scope)?.view;
                if (operation?.type !== 'criar_venda' || operation.actor_id !== scope.actor_id
                    || operation.environment_id !== scope.environment_id || operation.device_id !== scope.device_id
                    || !resolved?.recordId || resolved.recordId !== prepared.view.recordId) throw new Error('Venda ainda não confirmada oficialmente.');
                store.put({key:key + ':last-confirmed', operation_id:operationId,
                    official_operation_id:resolved.replacementId || operationId,record_id:resolved.recordId});
                store.delete(key);
                store.put({key:revisionKey, revision:revision + 1});
              } catch (_) { tx.abort(); }
            };
        };
        return {revision:revision + 1, released:true};
    }, ['metadata','operations']);
}
export function lastConfirmedDraft(repo, scope) {
    return access(repo, scope, false, ({store, key}) => new Promise((resolve, reject) => {
        const request = store.get(key + ':last-confirmed');
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error);
    }));
}
export function saveDraft(repo, scope, assembly, expectedRevision, replaceConfirmed = false) {
    const safe = projectAssembly(assembly);
    return access(repo, scope, true, ({store, key, revisionKey, revision, record, operations}) => {
        requireUnambiguous(operations,scope);
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
            device_id:scope.device_id, ...(scope.tab_id ? {tab_id:scope.tab_id} : {}), revision:revision + 1, ...safe};
        store.put(next); store.put({key:revisionKey, revision:next.revision});
        return {revision:next.revision, draft:next};
    }, ['metadata','operations']);
}
export function discardDraft(repo, scope, expectedRevision) {
    return access(repo, scope, true, ({store, key, revisionKey, revision, record, operations}) => {
        requireUnambiguous(operations,scope);
        rejectFinalized(record, scope, revision);
        if (revision !== expectedRevision) throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de descartar.');
        store.delete(key);
        // Keep the revision fence after deletion so an old tab cannot resurrect it.
        store.put({key:revisionKey, revision:revision + 1});
        return {revision:revision + 1, draft:null};
    }, ['metadata','operations']);
}

function salePayload(record, origin) {
    const draft = projectAssembly(record);
    if (!draft.itens.length) throw new Error('Adicione pelo menos um produto à venda.');
    if (draft.itens.length > 200) throw new Error('A venda excede o limite de 200 itens.');
    if (!draft.data_venda) throw new Error('Informe a data da venda.');
    if (!draft.tipo_venda) throw new Error('Selecione a forma de pagamento.');
    if (draft.operador.length > 120) throw new Error('O nome do operador excede o limite permitido.');
    if (draft.lancamento) {
        const line = draft.lancamento;
        const product = line.produto_nome || 'Produto selecionado';
        if (!line.quantidade) throw new Error(`Informe a quantidade da linha pendente: ${product}.`);
        throw new Error(`A linha de ${product} ainda não foi adicionada ou atualizada. Use “Adicionar Item” ou “Atualizar Item”, ou cancele a linha.`);
    }
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

export async function finalizeDraftOffline(repo, scope, {revision:expectedRevision, draft_id:draftId, origem_recebimento:origin, reference_snapshot:referenceSnapshot = null, transport = 'offline'}) {
    if (!['offline','online'].includes(transport)) throw new Error('Origem da conclusão inválida.');
    // The draft already owns a randomUUID, persisted once at its creation.
    // Promote it to operation identity so even an aborted local retry reuses it.
    const operationId = draftId;
    for (;;) {
        const stock = transport === 'offline' ? await readStock(repo,scope) : null;
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
            if (stock) validateStock(stock,scope,draft.itens,draft.itens);
            if (!Number.isSafeInteger(device.sequence) || device.sequence < 0 || device.sequence >= Number.MAX_SAFE_INTEGER)
                throw new Error('Sequência local inválida.');
            return {revision, sequence:device.sequence, payload:salePayload(draft, origin),
                original_labels:{cliente:draft.cliente ? {id:draft.cliente.id, nome:draft.cliente.nome} : null,
                    produtos:draft.itens.map(item => ({id:item.produto_id, nome:item.produto_nome}))}};
        });
        if (prepared.finalization) return {finalization:prepared.finalization, alreadyFinalized:true};
        const command = {operation_id:operationId, device_id:scope.device_id, actor_id:scope.actor_id,
            environment_id:scope.environment_id, type:'criar_venda', schema_version:1,
            aggregate_id:operationId, payload:prepared.payload, created_at:new Date().toISOString(), sequence:prepared.sequence + 1};
        const operation = {...command, payload_hash:await hash(command), status:'pendente', attempts:0, last_error:'', server_result:null, transport,
            original_labels:prepared.original_labels,
            reference_snapshot:referenceSnapshot ? {snapshot_id:referenceSnapshot.snapshot_id, prepared_at:referenceSnapshot.prepared_at} : null};
        // Diagnostic metadata is never sent and must not reduce the existing command limit.
        const {reference_snapshot, original_labels, ...sizedOperation} = operation;
        if (new TextEncoder().encode(JSON.stringify(sizedOperation)).length > 20000) throw new Error('Venda local excede o limite do protocolo. Rascunho preservado.');
        const result = await access(repo, scope, true, ({tx, store, device, key, revisionKey, revision, record, operations,stock:currentStock}) => {
            const marker = finalized(record, scope, revision);
            if (marker) {
                if (marker.draft_id !== draftId) throw new Error('A conclusão pertence a outra montagem.');
                return {finalization:marker, alreadyFinalized:true};
            }
            const draft = validateRecord(record, scope, revision);
            requireUnambiguous(operations,scope);
            if (stock && stockSignature(stock) !== stockSignature(currentStock)) throw new Error('Estoque ou fila atualizados em outra aba. Revise antes de concluir.');
            if (!draft || draft.draft_id !== draftId || revision !== prepared.revision)
                throw new Error('Rascunho atualizado em outra aba. Reabra a tela antes de concluir.');
            if (submissionMarker(scope, draft)) throw new Error('Há envio online anterior. Confira seu resultado antes de concluir offline.');
            if (device.sequence !== prepared.sequence) return {retry:true};
            const identity = reserveSequence(store, device);
            if (identity.sequence !== command.sequence) throw new Error('Sequência mudou. Rascunho preservado.');
            tx.objectStore('operations').add(operation);
            const finalization = {key, tipo:'venda_concluida_offline', schema_version:DRAFT_SCHEMA,
                ...scope, draft_id:draftId, revision:revision + 1, operation_id:operationId, finalized_at:command.created_at, conclusion_mode:transport};
            store.put(finalization); store.put({key:revisionKey, revision:revision + 1});
            return {finalization, operation, alreadyFinalized:false};
        }, transport === 'offline' ? ['metadata','operations','snapshots'] : ['metadata','operations']);
        if (!result.retry) return result;
    }
}
