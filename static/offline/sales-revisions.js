import {hash, commandOf, canonical, validReceipt, reserveSequence} from '/offline/assets/2-8f/core.js';
import {closureKey,intentKey,mirrorClosure} from '/offline/assets/2-8g-close/sales-closures.js';

const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
export function commercialConflict(operation) {
    return operation?.type === 'criar_venda' && operation.status === 'conflito'
        && validReceipt(operation.server_result, operation)
        && operation.server_result.status === 'conflito'
        && !operation.server_result.code && operation.diagnostic_code !== 'uuid_comando_divergente'
        && (operation.server_result.conflict_kind || 'comercial') === 'comercial';
}
const scoped = (operation, scope) => operation?.actor_id === scope.actor_id
    && operation.environment_id === scope.environment_id && operation.device_id === scope.device_id;
export function revisionKey(scope, originalId) {
    if (!uuid(originalId) || !uuid(scope.device_id) || !scope.actor_id || !scope.environment_id)
        throw new Error('Identidade da revisão inválida.');
    return 'revisao_venda:' + JSON.stringify([scope.environment_id, scope.actor_id, scope.device_id, originalId]);
}
const observationKey = operation => 'revisao_consulta:' + JSON.stringify([operation.environment_id, operation.actor_id, operation.operation_id]);
export async function lookupOriginal(repo, operation, scope) {
    if (!scoped(operation, scope) || !commercialConflict(operation)
        || await hash(commandOf(operation)) !== operation.payload_hash)
        throw new Error('Somente conflito comercial íntegro pode ser revisado.');
    if (!navigator.onLine) throw new Error('Conecte-se para consultar a operação original. Rascunho preservado.');
    const query = new URLSearchParams({actor_id:operation.actor_id, environment_id:operation.environment_id,
        device_id:operation.device_id, type:operation.type, hash:operation.payload_hash});
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    let data;
    try {
        const response = await fetch(`/api/offline/operations/${operation.operation_id}/?${query}`, {
            cache:'no-store', credentials:'same-origin', signal:controller.signal,
        });
        if (!response.ok) throw new Error(`Consulta da original recusada (${response.status}). Rascunho preservado.`);
        data = await response.json();
    } finally { clearTimeout(timer); }
    if (!navigator.onLine || data.lookup !== 'encontrada' || data.operation_id !== operation.operation_id
        || data.hash !== operation.payload_hash || data.actor_id !== scope.actor_id
        || data.environment_id !== scope.environment_id || data.device_id !== operation.device_id
        || data.type !== 'criar_venda' || !validReceipt(data.receipt, operation))
        throw new Error('Consulta ausente, indeterminada ou incompatível. Revisão bloqueada.');
    let parent = operation.operation_id, parentHash = operation.payload_hash;
    if (!Array.isArray(data.revisions)) throw new Error('Vínculo do servidor inválido.');
    const seen = new Set([parent]);
    for (const link of data.revisions) {
        if (link.original_operation_id !== parent || link.original_hash !== parentHash
            || !uuid(link.replacement_operation_id) || seen.has(link.replacement_operation_id)
            || link.actor_id !== scope.actor_id || link.environment_id !== scope.environment_id
            || link.relacao !== 'revisao_de_conflito'
            || !validReceipt(link.receipt, {type:'criar_venda', operation_id:link.replacement_operation_id, payload_hash:link.hash}))
            throw new Error('Cadeia de revisão inválida.');
        seen.add(link.replacement_operation_id); parent = link.replacement_operation_id; parentHash = link.hash;
    }
    // Store authoritative observations separately. Original bytes never change.
    if(data.closure){await mirrorClosure(repo,scope,operation,data.closure);throw new Error('Original encerrada administrativamente sem venda. Revisão bloqueada.');}
    const next = {key:observationKey(operation), original_hash:operation.payload_hash,
        checked_at:new Date().toISOString(), receipt:data.receipt, revisions:data.revisions};
    let observationError;
    const saved = await repo.transaction(['metadata'],true,(tx,done) => {
        const store=tx.objectStore('metadata');
        store.get(next.key).onsuccess=event => {
            try {
                const previous=event.target.result;
                if(previous?.original_hash===operation.payload_hash){
                    if(previous.receipt?.status==='confirmada' && validReceipt(previous.receipt,operation))next.receipt=previous.receipt;
                    const old=Array.isArray(previous.revisions)?previous.revisions:[];
                    for(let index=0;index<Math.min(old.length,next.revisions.length);index++){
                        const left=old[index],right=next.revisions[index];
                        if(left.original_operation_id!==right.original_operation_id || left.original_hash!==right.original_hash
                            || left.replacement_operation_id!==right.replacement_operation_id || left.hash!==right.hash)
                            throw new Error('Vínculo divergente entre consultas. Revisão bloqueada.');
                        if(left.receipt?.status==='confirmada' && validReceipt(left.receipt,{type:'criar_venda',operation_id:left.replacement_operation_id,payload_hash:left.hash}))
                            next.revisions[index]={...right,receipt:left.receipt};
                    }
                    if(old.length>next.revisions.length)next.revisions=old;
                }
                store.put(next);done(next);
            }catch(error){observationError=error;tx.abort();}
        };
    }).catch(error=>{throw observationError || error;});
    data.receipt=saved.receipt;data.revisions=saved.revisions;
    return data;
}
function assertRevisable(operation, lookup) {
    if (lookup.receipt.status === 'confirmada') {
        const error = new Error(`Original confirmada: venda #${lookup.receipt.record_id}. Revisão bloqueada.`);
        error.recordId = lookup.receipt.record_id; throw error;
    }
    if (!commercialConflict({...operation, server_result:lookup.receipt}) || lookup.revisions.length)
        throw new Error('Original não está disponível para nova revisão. Consulte a substituta existente.');
}
function copyPayload(operation) {
    const payload = operation.payload;
    const result = Object.fromEntries(['schema_version','cliente_id','data_venda','data_vencimento','tipo_pagamento','operador']
        .filter(key => key in payload).map(key => [key, payload[key]]));
    result.itens = payload.itens.map(item => Object.fromEntries(['produto_id','quantidade','unidade','preco_unitario'].map(key => [key,item[key]])));
    if (payload.origem_recebimento) result.origem_recebimento = structuredClone(payload.origem_recebimento);
    return result;
}
function validatePayload(payload, original) {
    const safe = copyPayload({payload});
    if (safe.data_venda !== original.payload.data_venda || safe.operador !== original.payload.operador)
        throw new Error('Data e operador originais são protegidos.');
    if (!['À vista','A vista','A prazo','consumo_proprio'].includes(safe.tipo_pagamento)
        || !Array.isArray(safe.itens) || !safe.itens.length || safe.itens.length > 200
        || (safe.cliente_id != null && safe.cliente_id !== '' && !/^[1-9]\d*$/.test(safe.cliente_id))
        || (safe.data_vencimento && !/^\d{4}-\d{2}-\d{2}$/.test(safe.data_vencimento)))
        throw new Error('Revise cliente, pagamento, vencimento e itens.');
    for (const item of safe.itens) {
        if (!/^[1-9]\d*$/.test(item.produto_id) || typeof item.unidade !== 'string' || !item.unidade.trim() || item.unidade.length > 40
            || ![item.quantidade,item.preco_unitario].every(value => typeof value === 'string'
                && /^\d+(?:\.\d{1,6})?$/.test(value) && Number(value) > 0 && Number.isFinite(Number(value))))
            throw new Error('Revise produto, quantidade, unidade e preço dos itens.');
        const subtotal = Number(item.quantidade) * Number(item.preco_unitario);
        if (!Number.isFinite(subtotal) || subtotal > 1e12) throw new Error('Valor local inválido. Revisão preservada.');
    }
    if (safe.origem_recebimento && (Object.keys(safe.origem_recebimento).some(key => !['caixa','banco'].includes(key))
        || Object.values(safe.origem_recebimento).some(value => typeof value !== 'string'
            || !/^(?:\d+(?:\.\d{1,6})?|\d+(?:\.\d{3})*,\d{1,2})$/.test(value))))
        throw new Error('Revise a origem de recebimento.');
    return safe;
}
async function access(repo, scope, originalId, work, write = true) {
    const key = revisionKey(scope, originalId);
    let caught;
    return repo.transaction(['metadata','operations'], write, (tx, done) => {
        const store = tx.objectStore('metadata');
        const safe = fn => event => {try {fn(event.target.result);} catch (error) {caught=error;tx.abort();}};
        store.get('device').onsuccess = safe(device => {
            if (device?.id !== scope.device_id) throw new Error('Dispositivo mudou. Revisão preservada.');
            store.get('sales-identity').onsuccess = safe(identity => {
                if (identity?.actor_id !== scope.actor_id || identity?.environment_id !== scope.environment_id)
                    throw new Error('Usuário/ambiente mudou. Revisão preservada.');
                tx.objectStore('operations').get(originalId).onsuccess = safe(original => {
                    if (!scoped(original, scope) || !commercialConflict(original)) throw new Error('Original não elegível para revisão.');
                    store.get(key + ':revision').onsuccess = safe(version => {
                        const revision = version?.revision || 0;
                        if (!Number.isSafeInteger(revision) || revision < 0 || revision >= Number.MAX_SAFE_INTEGER)
                            throw new Error('Revisão local inválida.');
                        store.get(key).onsuccess = safe(record => {
                            if (record && (record.original_hash !== original.payload_hash || record.revision !== revision
                                || !scoped(record, scope) || record.original_operation_id !== originalId || !uuid(record.draft_id)))
                                throw new Error('Rascunho de revisão incompatível.');
                            if (!write) { done(work({tx,store,key,original,record,revision,device})); return; }
                            store.get(closureKey(original)).onsuccess = safe(closure => {
                                if(closure)throw new Error('Original encerrada administrativamente. Revisão bloqueada.');
                                store.get(intentKey(original)).onsuccess = safe(intent => {
                                    if(intent && intent.state!=='recusada')throw new Error('Encerramento com resultado desconhecido. Consulte antes de revisar.');
                                    done(work({tx,store,key,original,record,revision,device}));
                                });
                            });
                        });
                    });
                });
            });
        });
    }).catch(error => {throw caught || error;});
}
export function loadRevision(repo, scope, originalId) {
    return access(repo, scope, originalId, ({record,revision}) => ({record,revision}), false);
}
export async function beginRevision(repo, scope, operation) {
    const lookup = await lookupOriginal(repo, operation, scope);
    assertRevisable(operation, lookup);
    return access(repo, scope, operation.operation_id, ({store,key,original,record,revision}) => {
        if (canonical(commandOf(original)) !== canonical(commandOf(operation)) || original.payload_hash !== operation.payload_hash)
            throw new Error('Original mudou durante a consulta. Revisão bloqueada.');
        if (record) return record;
        const now = new Date().toISOString();
        const next = {key,...scope, original_operation_id:original.operation_id, original_hash:original.payload_hash,
            relacao:'revisao_de_conflito', motivo_original:lookup.receipt.erro, draft_id:crypto.randomUUID(),
            revision:revision+1, status:'rascunho', criado_em:now, atualizado_em:now, payload:copyPayload(original),
            labels:structuredClone(original.original_labels || {}), reference_snapshot:original.reference_snapshot || null};
        store.add(next);store.put({key:key+':revision',revision:next.revision});return next;
    });
}
export function saveRevision(repo, scope, originalId, payload, expectedRevision) {
    return access(repo, scope, originalId, ({store,key,original,record,revision}) => {
        if (!record || record.status !== 'rascunho' || revision !== expectedRevision)
            throw new Error('Revisão atualizada/concluída em outra aba. Reabra a tela.');
        // Partial editing may contain invalid values. Validation belongs to explicit conclusion.
        const copied = copyPayload({payload});
        if (copied.data_venda !== original.payload.data_venda || copied.operador !== original.payload.operador)
            throw new Error('Data e operador originais são protegidos.');
        const next = {...record,payload:copied,revision:revision+1,atualizado_em:new Date().toISOString()};
        store.put(next);store.put({key:key+':revision',revision:next.revision});return next;
    });
}
export function discardRevision(repo, scope, originalId, expectedRevision) {
    return access(repo, scope, originalId, ({store,key,record,revision}) => {
        if (!record || record.status !== 'rascunho' || revision !== expectedRevision)
            throw new Error('Revisão atualizada/concluída em outra aba. Descarte bloqueado.');
        store.delete(key);store.put({key:key+':revision',revision:revision+1});
    });
}
export async function finalizeRevision(repo, scope, originalId, expectedRevision) {
    const original = await repo.get('operations',originalId);
    const loaded = await loadRevision(repo,scope,originalId);
    if (loaded.record?.status === 'concluida') return loaded.record;
    const lookup = await lookupOriginal(repo,original,scope);assertRevisable(original,lookup);
    for (;;) {
        const prepared = await access(repo,scope,originalId,({record,revision,device,original}) => {
            if (record?.status === 'concluida') return {completed:record};
            if (!record || revision !== expectedRevision) throw new Error('Revisão mudou em outra aba. Reabra a tela.');
            return {record,sequence:device.sequence,payload:validatePayload(record.payload,original)};
        },false);
        if (prepared.completed) return prepared.completed;
        const createdAt = new Date().toISOString();
        const command = {operation_id:prepared.record.draft_id, aggregate_id:prepared.record.draft_id,
            ...scope,type:'criar_venda',schema_version:1,created_at:createdAt,sequence:prepared.sequence+1,
            payload:{...prepared.payload,revisao:{original_operation_id:originalId,original_hash:original.payload_hash,
                relacao:'revisao_de_conflito',revisada_em:createdAt}}};
        const operation = {...command,payload_hash:await hash(command),status:'pendente',attempts:0,last_error:'',server_result:null,
            original_labels:prepared.record.labels, reference_snapshot:prepared.record.reference_snapshot};
        if (command.operation_id === originalId || new TextEncoder().encode(JSON.stringify({...command,payload_hash:operation.payload_hash})).length > 20000)
            throw new Error('Identidade/limite da substituta inválido.');
        const result = await access(repo,scope,originalId,({tx,store,key,original:current,record,revision,device}) => {
            if (record?.status === 'concluida') return record;
            if (!navigator.onLine) throw new Error('Conexão caiu. Rascunho preservado, conclusão bloqueada.');
            if (revision !== expectedRevision || record?.draft_id !== prepared.record.draft_id
                || canonical(commandOf(current)) !== canonical(commandOf(original))) throw new Error('Revisão/original mudou.');
            if (device.sequence !== prepared.sequence) return {retry:true};
            if (reserveSequence(store,device).sequence !== command.sequence) throw new Error('Sequência inválida.');
            tx.objectStore('operations').add(operation);
            const next = {...record,status:'concluida',replacement_operation_id:operation.operation_id,
                finalized_at:createdAt,revision:revision+1};
            store.put(next);store.put({key:key+':revision',revision:next.revision});return next;
        });
        if (!result.retry) return result;
    }
}
export function resolutionObservationKey(scope, operationId) {
    return observationKey({...scope, operation_id:operationId});
}

// Shared by presentation and the atomic release. A revision link alone is never a receipt.
export function resolutionFromSnapshot(snapshot, operationId, scope) {
    const operation = snapshot.operations.find(op => op.operation_id === operationId);
    if (!operation || operation.type !== 'criar_venda' || operation.actor_id !== scope.actor_id
        || operation.environment_id !== scope.environment_id || !uuid(operation.operation_id)) return null;
    if (operation.status === 'confirmada' && operation.server_result?.status === 'confirmada' && validReceipt(operation.server_result,operation))
        return {view:{recordId:operation.server_result.record_id,originalConfirmed:true}, nodes:[operation]};
    if (!commercialConflict(operation)) return null;
    const observed = snapshot.observed;
    if (observed?.original_hash === operation.payload_hash && validReceipt(observed.receipt,operation)) {
        if (observed.receipt.status === 'confirmada')
            return {view:{recordId:observed.receipt.record_id,originalConfirmed:true}, nodes:[operation]};
        let parent = operation.operation_id, parentHash = operation.payload_hash, receipt = observed.receipt;
        const seen = new Set([parent]);
        const observedNodes = [operation];
        const links = observed.revisions;
        let valid = Array.isArray(links) && links.length > 0;
        for (const link of Array.isArray(links) ? links : []) {
            if (receipt.status !== 'conflito' || receipt.code || (receipt.conflict_kind || 'comercial') !== 'comercial'
                || link.original_operation_id !== parent || link.original_hash !== parentHash
                || !uuid(link.replacement_operation_id) || seen.has(link.replacement_operation_id)
                || link.actor_id !== scope.actor_id || link.environment_id !== scope.environment_id
                || link.relacao !== 'revisao_de_conflito'
                || !validReceipt(link.receipt,{type:'criar_venda',operation_id:link.replacement_operation_id,payload_hash:link.hash})) {
                valid = false; break;
            }
            const localChild = snapshot.operations.find(op => op.operation_id === link.replacement_operation_id);
            if (localChild) {
                if (localChild.type !== 'criar_venda' || localChild.actor_id !== scope.actor_id
                    || localChild.environment_id !== scope.environment_id || localChild.payload_hash !== link.hash
                    || localChild.payload?.revisao?.original_operation_id !== parent
                    || localChild.payload.revisao.original_hash !== parentHash
                    || localChild.payload.revisao.relacao !== 'revisao_de_conflito'
                    || localChild.status !== link.receipt.status || !validReceipt(localChild.server_result,localChild)
                    || (link.receipt.status === 'confirmada' && localChild.server_result.record_id !== link.receipt.record_id)
                    || (link.receipt.status === 'conflito' && !commercialConflict(localChild))) { valid = false; break; }
                observedNodes.push(localChild);
            }
            seen.add(link.replacement_operation_id); parent = link.replacement_operation_id;
            parentHash = link.hash; receipt = link.receipt;
        }
        if (valid && receipt.status === 'confirmada')
            return {view:{recordId:receipt.record_id,replacementId:parent}, nodes:observedNodes};
    }
    let current = operation, replacementId = null; const seen = new Set([current.operation_id]), nodes = [operation];
    for (;;) {
        if (!commercialConflict(current)) break;
        const children = snapshot.operations.filter(op => op.payload?.revisao?.original_operation_id === current.operation_id);
        if (children.length !== 1 || seen.has(children[0].operation_id)) break;
        const child = children[0];
        if (child.type !== 'criar_venda' || child.actor_id !== scope.actor_id || child.environment_id !== scope.environment_id
            || !uuid(child.operation_id) || child.payload.revisao.original_hash !== current.payload_hash
            || child.payload.revisao.relacao !== 'revisao_de_conflito') break;
        current = child; replacementId = child.operation_id; seen.add(replacementId); nodes.push(child);
        if (current.status === 'confirmada' && current.server_result?.status === 'confirmada' && validReceipt(current.server_result,current))
            return {view:{recordId:current.server_result.record_id,replacementId}, nodes};
    }
    return replacementId ? {view:{replacementId}, nodes} : null;
}

export async function prepareRevisionResolution(repo, scope, operationId) {
    const snapshot = await repo.transaction(['metadata','operations'],false,(tx,done) => {
        tx.objectStore('operations').getAll().onsuccess = event => {
            const operations = event.target.result;
            tx.objectStore('metadata').get(resolutionObservationKey(scope,operationId)).onsuccess = observed =>
                done({operations,observed:observed.target.result || null});
        };
    });
    const resolved = resolutionFromSnapshot(snapshot,operationId,scope);
    if (!resolved) return {view:null, signature:canonical(snapshot)};
    for (const node of resolved.nodes) {
        if (await hash(commandOf(node)) !== node.payload_hash) return {view:null, signature:canonical(snapshot)};
    }
    return {view:resolved.view,signature:canonical(snapshot)};
}

export async function revisionPresentation(repo, operation, scope) {
    if (!operation) return null;
    return (await prepareRevisionResolution(repo,scope,operation.operation_id)).view;
}
