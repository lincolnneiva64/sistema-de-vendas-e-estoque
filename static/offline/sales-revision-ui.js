import {openDB, Repository, validReceipt} from '/offline/assets/2-8e/core.js';
import {draftScope} from '/offline/assets/2-8e/sales-drafts.js';
import {carregarSnapshotComercial} from '/offline/assets/2-8e/commercial.js';
import {beginRevision,loadRevision,saveRevision,discardRevision,finalizeRevision,revisionPresentation,lookupOriginal,commercialConflict} from '/offline/assets/2-8e/sales-revisions.js';

const element = id => document.getElementById(id);
const form=element('revision-form'), message=element('revision-message'), fields=element('revision-fields');
const originalId=new URL(location.href).searchParams.get('operation_id');
let repo,scope,original,record,payload,catalog,blocked=false,pending=Promise.resolve(),busy=false;
let blockedResultChecked=null;
let blockedLookup=null;
let resultGeneration=0;
const channel='BroadcastChannel' in window ? new BroadcastChannel('sales-revisions') : null;
window.salesRevisionUI={ready:false,flush:()=>pending,get record(){return record;},get blocked(){return blocked;}};
function announce(){channel?.postMessage({originalId,scope,revision:record?.revision});}
function fail(error){message.textContent=error.message || String(error);if(error.recordId)official(error.recordId);}
function official(id){const link=element('revision-official');link.href=`/vendas/${id}/`;link.textContent=`Ver venda oficial #${id}`;link.hidden=false;}
function capture(){
    const result=structuredClone(payload);
    result.cliente_id=element('revision-customer').value || null;
    result.tipo_pagamento=element('revision-payment').value;result.data_vencimento=element('revision-due').value;
    const cash=element('revision-cash').value,bank=element('revision-bank').value;
    if(cash!==''||bank!=='')result.origem_recebimento={...(cash!==''?{caixa:cash}:{}),...(bank!==''?{banco:bank}:{})};
    else delete result.origem_recebimento;
    for(const input of element('revision-items').querySelectorAll('[data-item-field]'))result.itens[Number(input.dataset.index)][input.dataset.itemField]=input.value;
    return result;
}
function total(){const current=capture();const value=current.itens.reduce((sum,item)=>sum+Number(item.quantidade)*Number(item.preco_unitario),0);element('revision-total').textContent=Number.isFinite(value)?`Total de referência: ${value.toFixed(2)} — revalidado pelo servidor.`:'Total de referência indisponível: revise os valores.';}
function queueSave(){
    if(blocked||busy||!record||record.status!=='rascunho')return;
    payload=capture();const captured=structuredClone(payload);
    for(const [index,group] of Array.from(element('revision-items').children).entries()){
        const id=payload.itens[index].produto_id;
        const name=record.labels?.produtos?.find(product=>product.id===id)?.nome;
        group.querySelector('legend').textContent=`Item ${index+1} — ${name || 'Produto ID '+id}`;
    }
    pending=pending.then(async()=>{
        if(blocked)return;
        record=await saveRevision(repo,scope,originalId,captured,record.revision);
        message.textContent='Revisão salva neste aparelho. Original preservada.';announce();
    }).catch(error=>{blocked=true;fields.disabled=true;element('revision-save').disabled=true;fail(error);});
    total();
}
function renderItems(){
    const container=element('revision-items');container.replaceChildren();
    payload.itens.forEach((item,index)=>{
        const group=document.createElement('fieldset'),legend=document.createElement('legend');
        const originalName=record.labels?.produtos?.find(product=>product.id===item.produto_id)?.nome;
        legend.textContent=`Item ${index+1} — ${originalName || 'Produto ID '+item.produto_id}`;group.append(legend);
        for(const [field,label] of [['produto_id','Produto (ID)'],['quantidade','Quantidade'],['unidade','Unidade'],['preco_unitario','Preço unitário']]){
            const wrapper=document.createElement('label'),input=document.createElement('input');wrapper.textContent=label;
            input.value=item[field];input.dataset.index=index;input.dataset.itemField=field;wrapper.append(input);group.append(wrapper);
        }
        const reference=document.createElement('select');reference.append(new Option('Trocar produto explicitamente — catálogo de referência',''));
        for(const product of catalog?.produtos || [])reference.append(new Option(`${product.nome} (ID ${product.id}) — preço ref. ${product.preco_venda}; unidades ${product.unidade_venda_1}/${product.unidade_venda_2}; conversão ref. ${product.fator_conversao}`,product.id));
        reference.addEventListener('change',()=>{if(!reference.value)return;group.querySelector('[data-item-field="produto_id"]').value=reference.value;queueSave();});group.append(reference);
        const hint=document.createElement('p');hint.className='reference';
        hint.textContent=(catalog?.produtos || []).some(product=>product.id===item.produto_id)?'Catálogo apenas de referência. Unidade e preço não serão substituídos ao escolher produto.':'Produto ausente do catálogo disponível. Valores originais preservados; revise ou remova explicitamente.';group.append(hint);
        const remove=document.createElement('button');remove.type='button';remove.textContent='Remover este item';remove.addEventListener('click',()=>{payload=capture();payload.itens.splice(index,1);renderItems();queueSave();});group.append(remove);container.append(group);
    });
}
function render(){
    payload=structuredClone(record.payload);form.hidden=false;
    element('revision-fixed').textContent=`Data original: ${payload.data_venda} · Operador original: ${payload.operador} (não editáveis).`;
    element('revision-customer').value=payload.cliente_id || '';
    if(!Array.from(element('revision-payment').options).some(option=>option.value===payload.tipo_pagamento))element('revision-payment').add(new Option(payload.tipo_pagamento,payload.tipo_pagamento));
    element('revision-payment').value=payload.tipo_pagamento;
    element('revision-due').value=payload.data_vencimento || '';element('revision-cash').value=payload.origem_recebimento?.caixa ?? '';element('revision-bank').value=payload.origem_recebimento?.banco ?? '';
    element('revision-original').textContent=JSON.stringify({motivo:record.motivo_original,payload:original.payload},null,2);
    const select=element('revision-customer-reference');select.replaceChildren(new Option('Selecione para trocar explicitamente',''));
    for(const customer of catalog?.clientes || [])select.append(new Option(`${customer.nome} (ID ${customer.id})`,customer.id));
    element('revision-catalog').textContent=catalog?`Catálogo de referência de ${catalog.prepared_at || catalog.gerado_em}. Estoque, custo e conversões serão revalidados no servidor.`:'Catálogo não disponível. IDs e valores originais preservados. Não há autorização local de estoque ou preço.';
    renderItems();total();fields.disabled=record.status!=='rascunho'||blocked;
    element('revision-save').disabled=fields.disabled||!navigator.onLine;element('revision-discard').disabled=fields.disabled;
}
async function refreshResult(){
    const generation=++resultGeneration;
    if(!repo||!scope||!original)return;
    const identity=await repo.get('metadata','sales-identity'),device=await repo.get('metadata','device');
    if(generation!==resultGeneration)return;
    if(identity?.actor_id!==scope.actor_id||identity?.environment_id!==scope.environment_id||device?.id!==scope.device_id){blocked=true;form.hidden=true;fields.disabled=true;element('revision-save').disabled=true;element('revision-discard').disabled=true;element('revision-official').hidden=true;element('revision-origin').textContent='Revisão preservada para a identidade original.';message.textContent='Identidade mudou. Revisão preservada e bloqueada.';return;}
    if(record?.status==='concluida'){
        const child=await repo.get('operations',record.replacement_operation_id);
        if(child?.server_result?.code==='revisao_origem_bloqueada'){
            if(blockedResultChecked!==child.server_result.completed_at){
                blockedResultChecked=child.server_result.completed_at;
                blockedLookup=lookupOriginal(repo,original,scope).catch(fail);
            }
            // A newer UI generation must await the pending observation too.
            // Otherwise it can render before persistence and suppress the older
            // generation that actually recovered the official confirmation.
            await blockedLookup;
        }
    }
    const view=await revisionPresentation(repo,original,scope);
    if(generation!==resultGeneration)return;
    if(view?.recordId){blocked=true;fields.disabled=true;element('revision-save').disabled=true;element('revision-discard').disabled=true;official(view.recordId);message.textContent=view.originalConfirmed?'Original já confirmada. Revisão bloqueada.':`Conflito resolvido pela venda nº ${view.recordId}. Venda corrigida confirmada. Se você enviou ao cliente informações da versão anterior, envie novamente a nota correta.`;}
    if(record?.status==='concluida'){
        const operation=await repo.get('operations',record.replacement_operation_id);
        if(generation!==resultGeneration)return;
        if(operation?.status==='confirmada'&&validReceipt(operation.server_result,operation)){official(operation.server_result.record_id);message.textContent=`Venda corrigida confirmada. Se você enviou ao cliente informações da versão anterior, envie novamente a nota correta. Original: ${originalId}; substituta: ${operation.operation_id}.`;}
        else if(!view?.recordId){message.textContent=`Revisão concluída neste aparelho. Original: ${originalId}; substituta: ${record.replacement_operation_id}. Estado: ${operation?.status || 'indeterminado'}. Sincronização somente manual.`;
            if(operation?.last_error)message.append(' ',operation.last_error);
            if(commercialConflict(operation)){const link=document.createElement('a');link.href=`/offline/revisao/?operation_id=${encodeURIComponent(operation.operation_id)}`;link.textContent='Consultar e revisar a substituta em conflito';message.append(' ',link);}}
    }
}
form.addEventListener('input',queueSave);form.addEventListener('change',queueSave);
element('revision-customer-reference').addEventListener('change',()=>{const value=element('revision-customer-reference').value;if(value){element('revision-customer').value=value;queueSave();}});
element('revision-add').addEventListener('click',()=>{payload=capture();payload.itens.push({produto_id:'',quantidade:'',unidade:'',preco_unitario:''});renderItems();queueSave();});
form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||blocked||record?.status!=='rascunho')return;
    queueSave();await pending;if(blocked)return;
    if(!window.confirm('Concluir esta revisão como NOVA operação local? Confira cliente, itens, unidades, preços, pagamento, vencimento e origem. A original será preservada e o envio será somente manual.'))return;
    busy=true;fields.disabled=true;element('revision-save').disabled=true;element('revision-discard').disabled=true;
    try{record=await finalizeRevision(repo,scope,originalId,record.revision);announce();render();document.dispatchEvent(new Event('offline-operations-changed'));const queue=new BroadcastChannel('offline-pilot');queue.postMessage({type:'changed',actor:scope.actor_id,environment:scope.environment_id});queue.close();await refreshResult();}
    catch(error){fail(error);if(error.recordId)blocked=true;await refreshResult();}
    finally{busy=false;fields.disabled=blocked||record.status!=='rascunho';element('revision-save').disabled=fields.disabled||!navigator.onLine;element('revision-discard').disabled=fields.disabled;}
});
element('revision-discard').addEventListener('click',async()=>{
    if(busy||blocked||!window.confirm('Descartar somente este rascunho de revisão? A operação original e o rascunho normal serão preservados.'))return;
    await pending;if(blocked)return;try{await discardRevision(repo,scope,originalId,record.revision);blocked=true;fields.disabled=true;element('revision-save').disabled=true;element('revision-discard').disabled=true;message.textContent='Rascunho de revisão descartado. Original preservada.';announce();}catch(error){fail(error);}
});
if(channel)channel.onmessage=async event=>{
    if(event.data?.originalId!==originalId||JSON.stringify(event.data.scope)!==JSON.stringify(scope)||event.data.revision<=record?.revision)return;
    const latest=await loadRevision(repo,scope,originalId);blocked=true;fields.disabled=true;element('revision-save').disabled=true;element('revision-discard').disabled=true;
    if(latest.record?.status==='concluida'){record=latest.record;await refreshResult();}else message.textContent='Revisão atualizada em outra aba. Reabra esta tela para carregar a versão salva.';
};
for(const name of ['offline','online'])window.addEventListener(name,()=>{element('revision-save').disabled=busy||blocked||record?.status!=='rascunho'||!navigator.onLine;if(!navigator.onLine)message.textContent='Sem conexão: revisão preservada. Concluir exige nova consulta online.';});
document.addEventListener('offline-operation-updated',()=>{void refreshResult().catch(fail);});
const queue='BroadcastChannel' in window?new BroadcastChannel('offline-pilot'):null;if(queue)queue.onmessage=()=>{void refreshResult().catch(fail);};
const identityChannel='BroadcastChannel' in window?new BroadcastChannel('sales-identity'):null;if(identityChannel)identityChannel.onmessage=()=>{void refreshResult().catch(fail);};
window.addEventListener('focus',()=>{void refreshResult().catch(fail);});
try{
    repo=new Repository(await openDB());const identity=await repo.get('metadata','sales-identity');
    if(identity?.environment_id!==element('offline-global').dataset.environment)throw new Error('Ambiente da revisão incompatível.');
    scope=await draftScope(repo,identity);
    if(!scope)throw new Error('Abra Vendas autenticado neste dispositivo antes de revisar.');
    element('offline-global').dataset.actor=scope.actor_id;
    original=await repo.get('operations',originalId);element('revision-origin').textContent=`Operação original: ${originalId} · cliente original: ${original?.original_labels?.cliente?.nome || original?.payload?.cliente_id || 'não informado'}`;
    const loaded=await loadRevision(repo,scope,originalId);
    record=loaded.record?.status==='concluida' ? loaded.record
        : navigator.onLine ? await beginRevision(repo,scope,original) : loaded.record;
    if(!record)throw new Error('Conecte-se para iniciar a revisão e consultar a original.');
    catalog=await carregarSnapshotComercial(repo,scope).catch(()=>null);
    render();message.textContent='Revisão restaurada/salva separadamente. Ajustes não alteram a operação original.';await refreshResult();
    if(navigator.onLine && 'serviceWorker' in navigator)void navigator.serviceWorker.register('/service-worker.js',{scope:'/'}).catch(()=>{message.textContent+=' Não foi possível preparar a tela para reabertura sem rede.';});
}catch(error){blocked=true;fail(error);if(repo&&original&&scope)await refreshResult().catch(fail);}
finally{window.salesRevisionUI.ready=true;}
