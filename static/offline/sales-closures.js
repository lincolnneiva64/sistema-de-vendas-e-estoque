import {canonical, commandOf, hash, validReceipt} from '/offline/assets/2-8f/core.js';
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
export const closureKey = op => 'sales-closure:' + (op ? JSON.stringify([op.environment_id,op.actor_id,op.operation_id]) : 'invalid');
export const intentKey = op => closureKey(op) + ':intent';
export function eligibleConflict(op) {
    return op?.type === 'criar_venda' && op.status === 'conflito' && validReceipt(op.server_result,op)
        && op.server_result.status === 'conflito' && !op.server_result.code
        && (op.server_result.conflict_kind || 'comercial') === 'comercial' && op.diagnostic_code !== 'uuid_comando_divergente';
}
export function validClosure(receipt,op) {
    return eligibleConflict(op) && receipt?.status === 'encerrada_sem_venda' && uuid(receipt.closure_id)
        && receipt.operation_id === op.operation_id && receipt.hash === op.payload_hash
        && receipt.actor_id === op.actor_id && receipt.environment_id === op.environment_id
        && receipt.original_device_id === op.device_id && uuid(receipt.device_id) && receipt.record_id === null
        && typeof receipt.motivo === 'string' && !!receipt.motivo.trim()
        && typeof receipt.closed_at === 'string' && /^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(receipt.closed_at)
        && Number.isFinite(Date.parse(receipt.closed_at)) && canonical(receipt.original_receipt) === canonical(op.server_result);
}
export const closedLocally = (metadata,op) => validClosure(metadata?.find(row => row.key === closureKey(op))?.receipt,op);
const ambiguous = (ops,scope) => ops.some(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id
    && op.environment_id === scope.environment_id && (['enviando','resultado_desconhecido'].includes(op.status)
        || op.transport === 'online' && !['confirmada','conflito'].includes(op.status)));
const hasRevision = (ops,meta,op) => !!op.payload?.revisao || ops.some(child => child.payload?.revisao?.original_operation_id === op.operation_id)
    || meta.some(row => row.original_operation_id === op.operation_id && row.relacao === 'revisao_de_conflito' && row.status !== 'descartada');

// Original operation/history are read-only; only administrative metadata and the
// matching assembly marker may be written. A later draft is never cleared.
async function access(repo,scope,op,work) {
    const signature = canonical(op);
    if (op.actor_id !== scope.actor_id || op.environment_id !== scope.environment_id
        || !eligibleConflict(op) || await hash(commandOf(op)) !== op.payload_hash)
        throw new Error('Somente conflito definitivo íntegro pode ser encerrado.');
    let error;
    return repo.transaction(['metadata','operations'],true,(tx,done) => {
        const store = tx.objectStore('metadata');
        store.getAll().onsuccess = event => {
            const meta = event.target.result;
            tx.objectStore('operations').getAll().onsuccess = event => {
                try {
                    const ops = event.target.result, current = ops.find(row => row.operation_id === op.operation_id);
                    const identity = meta.find(row => row.key === 'sales-identity'),device = meta.find(row => row.key === 'device');
                    if (canonical(current) !== signature || identity?.actor_id !== scope.actor_id
                        || identity?.environment_id !== scope.environment_id || device?.id !== scope.device_id)
                        throw new Error('Identidade ou operação mudou. Dados preservados.');
                    done(work({store,meta,ops}));
                } catch (caught) { error = caught; tx.abort(); }
            };
        };
    }).catch(caught => { throw error || caught; });
}
export async function mirrorClosure(repo,scope,op,receipt) {
    if (!validClosure(receipt,op)) throw new Error('Confirmação administrativa inválida. Montagem preservada.');
    return access(repo,scope,op,({store,meta,ops}) => {
        const previous = meta.find(row => row.key === closureKey(op));
        if (previous && canonical(previous.receipt) !== canonical(receipt)) throw new Error('Encerramento local divergente.');
        store.put({key:closureKey(op),receipt});
        const key = 'rascunho_venda:' + JSON.stringify([scope.environment_id,scope.actor_id,scope.device_id]);
        const marker = meta.find(row => row.key === key),fence = meta.find(row => row.key === key + ':revision');
        let revision = fence?.revision || 0,released = false;
        const pendingChild=ops.some(child=>child.payload?.revisao?.original_operation_id===op.operation_id
            && !(child.status==='conflito' && validReceipt(child.server_result,child) && child.server_result.code==='revisao_origem_bloqueada'));
        if (marker?.tipo === 'venda_concluida_offline' && marker.operation_id === op.operation_id && marker.draft_id === op.operation_id && op.device_id === scope.device_id && !ambiguous(ops,scope) && !pendingChild) {
            if (marker.revision !== revision || marker.actor_id !== scope.actor_id || marker.environment_id !== scope.environment_id
                || marker.device_id !== scope.device_id || !Number.isSafeInteger(revision) || revision >= Number.MAX_SAFE_INTEGER)
                throw new Error('Marcador da montagem incompatível.');
            store.delete(key); revision++;
            store.put({key:key + ':revision',revision}); released = true;
            store.put({key:key + ':last-closure',operation_id:op.operation_id,closure_id:receipt.closure_id});
        }
        return {receipt,released,revision};
    });
}
async function request(url,options) {
    const controller = new AbortController(),timer = setTimeout(() => controller.abort(),15000);
    try {
        const response = await fetch(url,{credentials:'same-origin',cache:'no-store',signal:controller.signal,...options});
        const data = await response.json();
        if (!response.ok) { const error = new Error(data.erro || 'Consulta indisponível.');error.refused=response.status===409 && data.code==='encerramento_recusado';throw error; }
        return data;
    } finally { clearTimeout(timer); }
}
function query(op) { return new URLSearchParams({actor_id:op.actor_id,environment_id:op.environment_id,
    original_device_id:op.device_id,hash:op.payload_hash}); }
function notify(scope) {
    document.dispatchEvent(new Event('offline-operation-updated'));
    const channel = 'BroadcastChannel' in window ? new BroadcastChannel('offline-pilot') : null;
    channel?.postMessage({type:'changed',actor:scope.actor_id,environment:scope.environment_id});channel?.close();
}
export async function recoverClosure(repo,scope,op) {
    const data = await request(`/api/offline/operations/${op.operation_id}/closure/?${query(op)}`);
    if (data.operation_id !== op.operation_id || data.hash !== op.payload_hash) throw new Error('Consulta incompatível.');
    if (data.lookup === 'nao_encontrada') return {unknown:true}; // earlier POST may still hold the parent lock
    if (data.lookup !== 'encontrada') throw new Error('Consulta indeterminada.');
    const result = await mirrorClosure(repo,scope,op,data.closure);notify(scope);return result;
}
export async function closeOperation(repo,scope,op,motivo,confirm) {
    if (confirm !== true || typeof motivo !== 'string' || !motivo.trim() || motivo.trim().length > 2000)
        throw new Error('Informe o motivo e confirme que nenhuma venda será criada.');
    if (!navigator.locks) throw new Error('Proteção entre abas indisponível.');
    return navigator.locks.request('offline-pilot-sync',{ifAvailable:true},async lock => {
        if (!lock) throw new Error('Outra aba está processando operações. Aguarde.');
        const intent = await access(repo,scope,op,({store,meta,ops}) => {
            if (hasRevision(ops,meta,op) || ambiguous(ops,scope)) throw new Error('Há revisão ou resultado desconhecido. Encerramento bloqueado.');
            const existing = meta.find(row => row.key === intentKey(op));
            if (existing && existing.state !== 'recusada') return existing;
            if (existing) store.put({...existing,key:intentKey(op) + ':' + existing.body.closure_id});
            const next = {key:intentKey(op),state:'resultado_desconhecido',requested_at:new Date().toISOString(),
                body:{closure_id:crypto.randomUUID(),hash:op.payload_hash,actor_id:scope.actor_id,environment_id:scope.environment_id,
                    original_device_id:op.device_id,device_id:scope.device_id,motivo:motivo.trim(),confirm:true}};
            store.put(next);return next;
        });
        try {
            const csrf = decodeURIComponent(document.cookie.split('; ').find(c => c.startsWith('csrftoken='))?.split('=')[1] || '');
            const data = await request(`/api/offline/operations/${op.operation_id}/close/`,{method:'POST',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(intent.body)});
            const result = await mirrorClosure(repo,scope,op,data.closure);notify(scope);return result;
        } catch (error) {
            if (error.refused) await access(repo,scope,op,({store}) => store.put({...intent,state:'recusada',refusal:error.message}));
            notify(scope);throw error;
        }
    });
}

let provider,installed=false;
export function installClosureUI(context) {
    provider=context;if(installed)return;installed=true;
    document.addEventListener('sales-close-operation',async event => {
        let dialog;
        try {
            const {repo,scope}=await provider();
            const op=await repo.get('operations',event.detail.operation_id);
            if (!eligibleConflict(op)) throw new Error('Operação não elegível.');
            const intent=await repo.get('metadata',intentKey(op));
            dialog=document.createElement('dialog');dialog.id='sales-close-dialog';
            dialog.innerHTML='<h2>Encerrar sem gerar venda</h2><p>Nenhuma venda será criada. A original e seu histórico serão preservados.</p><label>Motivo<textarea id="sales-close-reason" maxlength="2000" required></textarea></label><label><input id="sales-close-confirm" type="checkbox">Confirmo o abandono desta venda, sem gerar venda oficial.</label><p role="status"></p><button type="button" data-close-submit>Confirmar encerramento</button><button type="button" data-close-query>Consultar encerramento</button><button type="button" data-close-cancel>Fechar</button>';
            document.body.append(dialog);dialog.showModal();
            const reason=dialog.querySelector('textarea'),confirm=dialog.querySelector('input'),message=dialog.querySelector('[role=status]');
            if(intent && intent.state!=='recusada'){reason.value=intent.body.motivo;reason.readOnly=true;message.textContent='Solicitação anterior preservada. Consulte o servidor ou repita a mesma solicitação; ausência na consulta não prova falha.';}
            dialog.querySelector('[data-close-cancel]').onclick=()=>{dialog.close();dialog.remove();};
            const action=async queryOnly=>{
                const buttons=[...dialog.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
                try {
                    const result=queryOnly?await recoverClosure(repo,scope,op):await closeOperation(repo,scope,op,reason.value,confirm.checked);
                    message.textContent=result.unknown?'Resultado ainda desconhecido. A solicitação anterior pode estar em andamento. UUID preservado; consulte novamente ou repita a mesma solicitação.'
                        :'Encerrada administrativamente — nenhuma venda gerada. Histórico preservado.';
                } catch(error){message.textContent=error.message+' Encerramento não presumido. Consulte o resultado; não apague os dados.';}
                finally{buttons.forEach(b=>b.disabled=false);}
            };
            dialog.querySelector('[data-close-submit]').onclick=()=>{void action(false);};
            dialog.querySelector('[data-close-query]').onclick=()=>{void action(true);};
        }catch(error){dialog?.remove();window.alert(error.message);}
    });
}
