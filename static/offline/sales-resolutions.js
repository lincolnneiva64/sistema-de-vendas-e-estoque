import {commandOf,hash,canonical} from '/offline/assets/2-8f/core.js';
// Imported lazily: closures installs this UI and shares its receipt validator.
const closures=()=>import('/offline/assets/sale-resolution-v1/sales-closures.js');
export const resolutionIntentKey=op=>'sales-resolution:'+JSON.stringify([op.environment_id,op.actor_id,op.operation_id]);
export const resolutionPending=(metadata,op)=>metadata.some(row=>row.key===resolutionIntentKey(op));
async function request(op,body) {
    const query=new URLSearchParams({actor_id:op.actor_id,environment_id:op.environment_id,
        original_device_id:op.device_id,hash:op.payload_hash});
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try {
        const csrf=decodeURIComponent(document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1]||'');
        const response=await fetch(`/api/offline/operations/${op.operation_id}/resolution/?${query}`,{
            credentials:'same-origin',cache:'no-store',signal:controller.signal,
            ...(body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(body)}:{})});
        const data=await response.json();
        if(!response.ok || data.operation_id!==op.operation_id || data.hash!==op.payload_hash)
            throw Error(data.erro||'Não foi possível confirmar. Consulte novamente; a venda foi preservada.');
        return data;
    } finally {clearTimeout(timer);}
}
async function check(repo,scope,op) {
    if(op?.type!=='criar_venda' || op.actor_id!==scope.actor_id || op.environment_id!==scope.environment_id
        || await hash(commandOf(op))!==op.payload_hash)throw Error('Não foi possível conferir esta venda. Dados preservados.');
    const current=await repo.get('operations',op.operation_id);
    if(op.server_verified && !current)return;
    if(!current || (op.server_verified ? canonical(commandOf(current))!==canonical(commandOf(op))
        : canonical(current)!==canonical(op)))throw Error('A venda mudou em outra aba. Reabra a resolução.');
}
function notify(scope) {
    document.dispatchEvent(new Event('offline-operation-updated'));
    document.dispatchEvent(new Event('offline-operations-changed'));
    const channel=new BroadcastChannel('offline-pilot');
    channel.postMessage({type:'changed',actor:scope.actor_id,environment:scope.environment_id});channel.close();
}
export async function consultResolution(repo,scope,op) {
    await check(repo,scope,op);
    const state=await request(op);
    if(state.lookup==='encontrada') {
        await mirrorResolution(repo,scope,op,state.closure);
    }
    return state;
}
async function mirrorResolution(repo,scope,op,receipt) {
    const {mirrorClosure,validClosure,closureKey}=await closures();
    if(!validClosure(receipt,op))throw Error('Confirmação administrativa incompatível. Venda preservada.');
    if(op.server_verified && !await repo.get('operations',op.operation_id))
        await repo.put('metadata',{key:closureKey(op),receipt});
    else await mirrorClosure(repo,scope,op,receipt);
    notify(scope);
}
export async function resolvePending(repo,scope,op,reason,confirmed) {
    if(confirmed!==true || !reason?.trim() || reason.trim().length>2000)throw Error('Informe o motivo e confirme o encerramento.');
    if(!navigator.locks)throw Error('Proteção entre abas indisponível. Venda preservada.');
    return navigator.locks.request('offline-pilot-sync',{ifAvailable:true},async lock=>{
        if(!lock)throw Error('Outra aba está trabalhando. Aguarde e tente novamente.');
        await check(repo,scope,op);
        const {closedLocally}=await closures();
        const meta=await repo.all('metadata'),ops=await repo.all('operations');
        const child=ops.find(row=>row.actor_id===scope.actor_id && row.environment_id===scope.environment_id
            && row.payload?.revisao?.original_operation_id===op.operation_id && !closedLocally(meta,row));
        if(child)return {lookup:'substituta',operation:child};
        const draft=meta.find(row=>row.relacao==='revisao_de_conflito' && row.original_operation_id===op.operation_id
            && row.status!=='descartada' && !row.replacement_operation_id);
        if(draft)throw Error('Há uma revisão em montagem. Abra a revisão e descarte somente esse rascunho antes de encerrar.');
        let intent=await repo.get('metadata',resolutionIntentKey(op));
        if(!intent) {
            intent={key:resolutionIntentKey(op),body:{command:commandOf(op),payload_hash:op.payload_hash,
                closure_id:crypto.randomUUID(),device_id:scope.device_id,motivo:reason.trim(),confirm:true}};
            await repo.put('metadata',intent);
        }
        // Absence is never acknowledgement. Only POST can reserve a missing UUID.
        const previous=await consultResolution(repo,scope,op);
        if(previous.lookup==='encontrada' || previous.lookup==='oficial' || previous.lookup==='aguardando')return previous;
        const state=await request(op,intent.body);
        if(state.lookup==='encontrada') {
            await mirrorResolution(repo,scope,op,state.closure);
        }
        return state;
    });
}
let provider,installed=false;
export function installResolutionUI(context) {
    provider=context;if(installed)return;installed=true;
    document.addEventListener('sales-resolve-pending',async event=>{
        let dialog;
        try {
            const {repo,scope}=await provider();
            let op=await repo.get('operations',event.detail.operation_id);
            const parents=[];
            await check(repo,scope,op);
            if(document.getElementById('sales-resolution-dialog'))return;
            const {closedLocally}=await closures();
            const meta=await repo.all('metadata'),operations=await repo.all('operations'),seen=new Set([op.operation_id]);
            let child;
            while((child=operations.find(row=>row.actor_id===scope.actor_id && row.environment_id===scope.environment_id
                && row.payload?.revisao?.original_operation_id===op.operation_id && !closedLocally(meta,row)))) {
                if(seen.has(child.operation_id))throw Error('Não foi possível conferir a cadeia. Vendas preservadas.');
                seen.add(child.operation_id);parents.push(op);op=child;
            }
            dialog=document.createElement('dialog');dialog.id='sales-resolution-dialog';
            dialog.style.cssText='width:min(460px,calc(100vw - 24px));box-sizing:border-box;max-height:85dvh;overflow:auto;border:1px solid #cbd5e1;border-radius:16px;padding:18px;color:#0f172a';
            dialog.innerHTML='<h2>Resolver venda pendente</h2><p data-summary></p><p>Encerrar esta tentativa sem gerar outra venda. As vendas oficiais e o histórico serão preservados.</p><label>Motivo<textarea data-reason maxlength="2000" style="display:block;width:100%;box-sizing:border-box"></textarea></label><label style="display:block;margin:12px 0"><input type="checkbox" data-confirm>Confirmo que esta tentativa deve ser encerrada sem gerar venda.</label><p role="status"></p><a data-official hidden>Ver venda oficial</a><button type="button" data-resolve>Confirmar encerramento</button><button type="button" data-query>Consultar resultado</button><button type="button" data-back hidden>Voltar à venda original</button><button type="button" data-exit>Fechar</button>';
            document.body.append(dialog);dialog.showModal();
            const message=dialog.querySelector('[role=status]'),submit=dialog.querySelector('[data-resolve]'),back=dialog.querySelector('[data-back]');
            const describe=()=>{
                const name=op.original_labels?.cliente?.nome||'Venda pendente';
                const total=(op.payload?.itens||[]).reduce((sum,i)=>sum+Number(String(i.quantidade).replace(',','.'))*Number(String(i.preco_unitario).replace(',','.')),0);
                dialog.querySelector('[data-summary]').textContent=name+' · '+total.toLocaleString('pt-BR',{style:'currency',currency:'BRL'});
                dialog.querySelector('[data-confirm]').checked=false;
                submit.hidden=false;dialog.querySelector('[data-official]').hidden=true;
            };
            describe();
            if(parents.length)message.textContent='Resolva primeiro esta tentativa substituta. Depois volte à venda original.';
            dialog.querySelector('[data-exit]').onclick=()=>{dialog.close();dialog.remove();};
            const action=async query=>{
                const buttons=[...dialog.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
                try {
                    const state=query?await consultResolution(repo,scope,op):await resolvePending(repo,scope,op,
                        dialog.querySelector('[data-reason]').value,dialog.querySelector('[data-confirm]').checked);
                    if(state.lookup==='substituta') {
                        parents.push(op);op=state.operation;describe();
                        message.textContent='Resolva primeiro esta tentativa substituta. Depois volte à venda original.';
                    }else if(state.lookup==='dependencias') {
                        const command=state.children[0].command;
                        const local=await repo.get('operations',command.operation_id);
                        parents.push(op);op=local||{...command,payload_hash:state.children[0].payload_hash,
                            status:'resultado_desconhecido',server_verified:true};
                        describe();message.textContent='Resolva primeiro esta tentativa substituta.';
                    }else if(state.lookup==='oficial') {
                        submit.hidden=true;
                        const link=dialog.querySelector('[data-official]');link.hidden=false;link.href=`/vendas/${state.record_id}/`;
                        message.textContent=`Esta tentativa já gerou a venda #${state.record_id}. Nenhum encerramento sem venda foi feito.`;
                    }else if(state.lookup==='encontrada') {
                        submit.hidden=true;back.hidden=!parents.length;
                        message.textContent='Encerramento confirmado pelo servidor. Esta tentativa não será enviada. Histórico preservado.';
                    }else message.textContent='Resultado ainda não confirmado. A venda permanece preservada. Consulte novamente; não refaça a venda.';
                }catch(error){message.textContent=error.message+' Nenhuma venda foi criada por esta resolução.';}
                finally{buttons.forEach(b=>b.disabled=false);}
            };
            submit.onclick=()=>void action(false);
            dialog.querySelector('[data-query]').onclick=()=>void action(true);
            back.onclick=()=>{op=parents.pop();back.hidden=true;describe();message.textContent='A substituta foi encerrada. Confirme agora o encerramento da original.';};
        }catch(error){dialog?.remove();window.alert(error.message);}
    });
}
