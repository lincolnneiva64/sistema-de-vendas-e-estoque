// Local transport adapter. Same UUID/command/hash/receipt as operation services.
// Existing authenticated drafts, operations, snapshots and owner metadata are untouched.
import {Repository, openDB, hash, validReceipt} from '/offline/assets/local-controlled-v1/core.js';
if (!window.salesDraftBridge) await new Promise(resolve => document.addEventListener('sales-draft-bridge-ready', resolve, {once:true}));
const bridge = window.salesDraftBridge, context = window.localSalesContext;
const repo = new Repository(await openDB());
const box = document.createElement('div'); box.id = 'sales-draft-status';
const status = document.createElement('span'); status.setAttribute('role','status');
const query = document.createElement('button'); query.type='button'; query.id='local-sale-query'; query.textContent='Consultar resultado';
const replay = document.createElement('button'); replay.type='button'; replay.id='local-sale-replay'; replay.textContent='Reenviar mesmo comando';
const next = document.createElement('a'); next.textContent='Abrir nova montagem'; next.id='local-new-assembly';
const newAssembly=new URL(location.href);newAssembly.searchParams.set('nova_local',crypto.randomUUID());next.href=newAssembly.href;
const link = document.createElement('a'); link.textContent='Ver venda'; link.hidden=true;
box.append(status, query, replay, link, next); document.getElementById('layout-vendas').prepend(box);
const channel = new BroadcastChannel('local-normal-sales');
let blocked=false, busy=false, restoring=false, savePending=Promise.resolve();
let tab = sessionStorage.getItem('local-normal-sales-tab') || crypto.randomUUID();
// Browser duplicate-tab copies sessionStorage. A held Web Lock prevents sharing a draft.
await new Promise(resolve => {
    const claim = () => navigator.locks.request('local-normal-sales-tab:'+tab, {ifAvailable:true}, async lock => {
        if (!lock) { tab=crypto.randomUUID(); return claim(); }
        sessionStorage.setItem('local-normal-sales-tab',tab); resolve();
        await new Promise(release => window.addEventListener('pagehide',release,{once:true}));
    }); claim();
});
const prefix='normal-sale-v1:'+JSON.stringify([context.environment_id,context.owner])+':';
const key=prefix+tab+':'+location.pathname+location.search;
let draftVersion=0, savedVersion=0, saveTimer, saveError=false;
let record=await repo.get('metadata',key) || {key, revision:context.revision, url:location.href, tab};
// Recovery resumes the original scope only when no live tab holds its lease.
const recovery=document.createElement('select'), recover=document.createElement('button');
recovery.id='local-sales-recovery';recover.id='local-sales-recover-button';recover.type='button';recover.textContent='Retomar montagem salva';
recovery.add(new Option('Montagens deste navegador',''));
for (const saved of await repo.all('metadata')) {
    if (saved.key!==key && saved.key.startsWith(prefix) && saved.url && saved.tab
        && !saved.result?.sucesso && (saved.command || saved.assembly?.itens?.length || saved.assembly?.lancamento)) {
        const option=new Option((saved.data?.operador || 'Montagem')+' - '+(saved.data?.data_venda || ''),saved.key);
        recovery.add(option);
    }
}
box.append(recovery,recover);
recover.onclick=async()=>{
    const saved=await repo.get('metadata',recovery.value);
    if (!saved || !saved.key.startsWith(prefix) || !saved.tab) return;
    await navigator.locks.request('local-normal-sales-tab:'+saved.tab,{ifAvailable:true},async lock=>{
        if (!lock) {bridge.error('Esta montagem esta aberta em outra aba.');return;}
        const url=new URL(saved.url);
        if (url.origin!==location.origin || url.pathname!=='/vendas/') return;
        sessionStorage.setItem('local-normal-sales-tab',saved.tab);location.assign(url.href);
        await new Promise(resolve=>window.addEventListener('pagehide',resolve,{once:true}));
    });
};
function pending() { return !!record.command && record.result?.receipt?.status !== 'confirmada'; }
const disabledBeforeFreeze=new Map();
function freeze(value) {
    for (const element of document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')) {
        if (!box.contains(element) && !element.matches('[data-sales-navigation-toggle]')) {
            if (value) {if (!disabledBeforeFreeze.has(element)) disabledBeforeFreeze.set(element,element.disabled);element.disabled=true;}
            else if (disabledBeforeFreeze.has(element)) {element.disabled=disabledBeforeFreeze.get(element);disabledBeforeFreeze.delete(element);}
        }
    }
}
function render() {
    query.hidden=replay.hidden=!pending();
    const result=record.result || record.lastReceipt;
    link.hidden=result?.receipt?.status!=='confirmada';
    if (!link.hidden) link.href=result.visualizar_url;
    next.hidden=blocked || (pending() && record.result?.receipt?.status!=='conflito');
    if (saveError) status.textContent='Nao foi possivel salvar o rascunho neste navegador. Montagem preservada; nao recarregue a pagina.';
    else if (blocked) status.textContent='Identidade ou ambiente mudou. Montagem anterior preservada. Reabra Vendas.';
    else if (pending()) status.textContent=record.result?.receipt?.status==='conflito'
        ? 'Venda nao gravada: '+(record.result.receipt.erro || 'conflito')+'. Comando preservado; abra uma nova montagem para corrigir.'
        : 'Resultado ainda nao confirmado. Consulte ou reenvie o mesmo comando. Nao repita a venda.';
    else status.textContent=record.result ? 'Venda confirmada. Recibo preservado.' : record.assembly && savedVersion===draftVersion ? 'Rascunho local salvo neste navegador.' : 'Salvando rascunho local...';
    freeze(blocked || pending());
}
async function persist() { await repo.put('metadata',record); }
async function flush() {
    savePending=savePending.then(async()=>{
        if (blocked || busy || restoring || record.command) return;
        // A reserved command (including its receipt) is immutable to autosave.
        const version=draftVersion, assembly=bridge.capture(), data=bridge.getData();
        const updated={...record, assembly, data};
        // Repository.put resolves on transaction completion, not on request success.
        await repo.put('metadata',updated);
        record=updated; savedVersion=version; render();
    });
    return savePending;
}
async function consume(data, httpStatus) {
    if (!validReceipt(data?.receipt,record.command,httpStatus) || data.sucesso !== (data.receipt.status==='confirmada')) throw Error('Recibo invalido; comando preservado.');
    record.result=data; await persist(); render(); return data.sucesso ? data : null;
}
async function verifyOwner() {
    const response=await fetch(location.href,{cache:'no-store',credentials:'same-origin'});
    if (!response.ok) throw Error('Servidor local indisponivel; montagem preservada.');
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    const current=JSON.parse(page.getElementById('local-sales-context')?.textContent || 'null');
    if (!current || current.owner!==context.owner || current.environment_id!==context.environment_id) {
        blocked=true; render(); channel.postMessage({changed:true}); throw Error('Identidade ou ambiente mudou.');
    }
    context.csrf=current.csrf;
}
async function send() {
    await verifyOwner();
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
    try {
        const response=await fetch('/api/vendas/online/',{method:'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,
            headers:{'Content-Type':'application/json','X-CSRFToken':context.csrf,'X-Local-Sales':'1'},body:JSON.stringify(record.command)});
        return await consume(await response.json(),response.status);
    } finally { clearTimeout(timer); }
}
async function exclusive(work) {
    return navigator.locks.request('normal-sale-send:'+context.owner,{ifAvailable:true},async lock=>{
        if (!lock || busy || blocked) throw Error('Outra aba esta enviando ou a identidade mudou.');
        busy=true;
        try { return await work(); }
        catch(error) { render(); bridge.error(error.message+' Montagem e comando preservados.'); return null; }
        finally {busy=false;render();}
    });
}
window.localSales={
    async startNew() {
        if (pending() || blocked) {bridge.error('Resolva o envio pendente antes de iniciar outra venda.');return;}
        await flush(); location.assign('/vendas/?nova_local='+crypto.randomUUID());
    },
    async afterSuccess() {
        if (!record.result?.sucesso || record.command.payload.dados.venda_id || record.command.payload.dados.pedido_id) return;
        await savePending;
        const previous=record;
        await repo.put('metadata',{...previous,key:key+':receipt:'+previous.command.operation_id});
        record={key,revision:'',url:location.href,tab,lastReceipt:previous.result};
        await persist();render();
    },
    async submit(data) {
        await flush();
        return exclusive(async()=>{
            if (record.command) {
                if (record.result?.receipt?.status==='confirmada') throw Error('Esta montagem ja foi gravada. Abra uma nova venda.');
                return send();
            }
            await verifyOwner();
            const id=crypto.randomUUID();
            const command={operation_id:id,aggregate_id:id,device_id:tab,actor_id:context.actor_id,
                environment_id:context.environment_id,type:'venda_local',schema_version:1,sequence:1,
                created_at:new Date().toISOString(),payload:{schema_version:1,dados:structuredClone(data),expected_revision:record.revision}};
            command.payload_hash=await hash(command);
            record.command=command; await persist(); render();
            return send();
        });
    },
    flush, get record(){return record;}, get key(){return key;}
};
query.onclick=()=>exclusive(async()=>{
    await verifyOwner();
    const response=await fetch('/api/vendas/online/'+record.command.operation_id+'/?hash='+record.command.payload_hash,
        {headers:{'X-Local-Sales':'1'},cache:'no-store',credentials:'same-origin'});
    const data=await response.json();
    if (data.lookup==='nao_encontrada') {status.textContent='Comando ainda nao encontrado. Reenvie o mesmo comando.';return;}
    return consume(data);
});
replay.onclick=()=>exclusive(send);
window.salesDraftUI={ready:false,flush,beforeSubmit:async()=>{try{await flush();await verifyOwner();return !blocked&&!pending();}catch(error){bridge.error(error.message);return false;}},
    prepareSubmission(){},confirmSubmission(){},failedSubmission(){},confirmedSuccess:async()=>{},
    get finalization(){return pending()?record.command:null;}};
if (record.assembly && !record.result?.sucesso) {
    restoring=true;
    bridge.restore(record.assembly);
    // The ordinary bridge projects away item IDs; edits must keep their originals.
    if (record.data?.venda_id) [...document.querySelectorAll('#tabelaProdutos tr')].forEach((row,i)=>{
        if (record.data.itens[i]?.item_id) row.dataset.itemId=record.data.itens[i].item_id;
    });
    restoring=false;
}
channel.onmessage=()=>verifyOwner().catch(()=>{});
window.addEventListener('focus',()=>verifyOwner().catch(()=>{}));
function scheduleSave() {
    if (blocked || busy || restoring || record.command) return;
    draftVersion++; render();
    clearTimeout(saveTimer);
    saveTimer=setTimeout(()=>flush().catch(error=>{
        saveError=true; blocked=true; render(); bridge.error(error.message);
    }),0);
}
for (const event of ['input','change','click']) document.getElementById('layout-vendas').addEventListener(event,scheduleSave);
document.addEventListener('sales-draft-change',scheduleSave);
// Enter on price updates the confirmed table without a bubbling input/change.
// Observe the actual result, independently of keyboard or button entry.
const confirmedItems=document.getElementById('tabelaProdutos');
new MutationObserver(scheduleSave).observe(confirmedItems,{subtree:true,childList:true,attributes:true,characterData:true});
// Unload cannot guarantee completion of an asynchronous IndexedDB transaction.
// Autosave above must finish while the page is still active.
window.salesDraftUI.ready=true; window.salesDraftResolve(); render();
