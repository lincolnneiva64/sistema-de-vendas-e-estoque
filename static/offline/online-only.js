// Temporary online-only UI. Reuses the existing command/hash/receipt endpoints.
// Never opens IndexedDB or reads, changes or sends the suspended offline queues.
import {canonical, hash, validReceipt} from '/offline/assets/local-controlled-v1/core.js';
import {ensureSaleCosts} from '/offline/assets/sale-cost-check-v1/sales-costs.js';

const indicator = document.getElementById('offline-global');
const environment = indicator?.dataset.environment;
let connected = false, probing = null;
function showConnection(ok) {
    connected = ok;
    if (indicator) {
        indicator.dataset.connection = indicator.dataset.state = ok ? 'online' : 'offline';
        const label = document.getElementById('offline-status');
        if (label) label.textContent = ok ? 'Online' : 'Offline';
    }
}
async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.method === 'POST' ? 15000 : 5000);
    try {
        return await fetch(url, {...options, credentials:'same-origin', cache:'no-store', signal:controller.signal});
    } catch (error) {
        showConnection(false);
        throw error;
    } finally { clearTimeout(timer); }
}
async function probe() {
    if (probing) return probing;
    probing = (async () => {
        try {
            const response = await request('/api/offline/health/');
            const data = await response.json();
            showConnection(response.ok && data.ok === true && data.environment === environment && data.protocol_version === 1);
        } catch (_) { showConnection(false); }
        return connected;
    })();
    try { return await probing; } finally { probing = null; }
}
window.addEventListener('offline', () => showConnection(false));
window.addEventListener('online', probe);
window.addEventListener('focus', probe);
document.addEventListener('offline-connectivity-suspect', probe);
setInterval(probe, 15000);
void probe();
// Keep the worker installed; the server supplies its reversible network-only policy.
if ('serviceWorker' in navigator) void navigator.serviceWorker.register('/service-worker.js', {updateViaCache:'none'}).catch(() => {});

if (document.getElementById('layout-vendas')) {
    if (!window.salesDraftBridge) await new Promise(resolve => document.addEventListener('sales-draft-bridge-ready', resolve, {once:true}));
    const bridge = window.salesDraftBridge, local = window.localSalesContext;
    const actor = local?.actor_id || indicator?.dataset.actor;
    // This key belongs only to an actual online submission, not an offline draft/queue.
    const key = 'online-only-submission-v1:' + JSON.stringify([environment, actor, location.pathname, location.search]);
    let pending = null, busy = false, storageFailed = false;
    try { pending = JSON.parse(sessionStorage.getItem(key) || 'null'); }
    catch (_) { storageFailed = true; bridge.error('Não foi possível conferir o envio desta aba. Reabra a página antes de gravar.'); }
    const box = document.createElement('div'); box.id = 'online-only-sale-status'; box.setAttribute('role','status');
    box.style.cssText = 'padding:6px 12px;font-size:13px';
    const message = document.createElement('span');
    const query = document.createElement('button'); query.type = 'button'; query.textContent = 'Consultar resultado deste envio';
    box.append(message, query); document.getElementById('layout-vendas').prepend(box);
    function render() {
        query.hidden = !pending; query.disabled = busy;
        message.textContent = pending ? 'Este envio aguarda confirmação. Consulte o resultado antes de repetir a venda. ' : '';
        if (pending) document.getElementById('btnGravarVenda').disabled = true;
    }
    function clearSubmission() { sessionStorage.removeItem(key); pending = null; render(); }
    function csrf() { return decodeURIComponent(document.cookie.split('; ').find(row => row.startsWith('csrftoken='))?.slice(10) || ''); }
    async function identity() {
        const response = await request('/api/offline/session/');
        const data = await response.json();
        if (!response.ok || data.environment_id !== environment || (local ? data.local_sales_owner !== local.owner : !data.authenticated || data.actor_id !== actor))
            throw Error('Sessão ou identidade mudou. Entre novamente e reabra Vendas.');
    }
    query.onclick = async () => {
        if (!pending || busy) return;
        busy = true; render();
        try {
            await identity();
            const command = pending.command;
            const params = new URLSearchParams({hash:command.payload_hash, actor_id:command.actor_id,
                environment_id:command.environment_id, type:command.type, device_id:command.device_id});
            const response = await request('/api/vendas/online/' + command.operation_id + '/?' + params,
                {headers:local ? {'X-Local-Sales':'1'} : {}});
            const data = await response.json();
            if (data.lookup !== 'encontrada' || !validReceipt(data.receipt, command))
                throw Error('Resultado ainda não confirmado. Nenhum reenvio foi feito; não repita esta venda.');
            const receipt = data.receipt;
            if (receipt.status === 'confirmada') {
                // Preserve the confirmation in this tab until explicitly opening a fresh page.
                pending.receipt = receipt; sessionStorage.setItem(key, JSON.stringify(pending));
                message.textContent = 'Venda #' + receipt.record_id + ' confirmada. Abra a venda antes de iniciar outra montagem. ';
                const link = document.createElement('a'); link.href = '/vendas/' + receipt.record_id + '/'; link.textContent = 'Ver venda';
                box.append(link);
                const next = document.createElement('button'); next.type = 'button'; next.textContent = 'Nova venda';
                next.onclick = () => { clearSubmission(); location.assign('/vendas/?nova_online=' + crypto.randomUUID()); };
                box.append(next);
            } else {
                clearSubmission(); bridge.error(receipt.erro || 'Venda rejeitada pelo servidor. Confira os dados.');
                document.getElementById('btnGravarVenda').disabled = false;
            }
        } catch (error) { bridge.error(error.message); }
        finally { busy = false; query.disabled = false; }
    };
    window.onlineOnlySales = {
        get pending() { return pending; },
        async submit(data) {
            if (storageFailed) throw Error('Não foi possível conferir o envio desta aba. Nenhum envio foi iniciado.');
            if (busy || pending) throw Error('Consulte o resultado do envio desta montagem antes de repetir.');
            busy = true;
            try {
                if (!await probe()) throw Error('Offline. Conecte-se ao servidor para gravar. Nenhuma venda foi salva localmente.');
                await identity();
                await ensureSaleCosts(data.itens);
                const current = bridge.getData();
                // The closing dialog may add only the receiving origin to the submitted sale.
                if (canonical({...current, origem_recebimento:data.origem_recebimento}) !== canonical({...data, origem_recebimento:data.origem_recebimento}))
                    throw Error('A montagem mudou. Confira a venda antes de gravar.');
                if (!local && !bridge.eligible) throw Error('Use o fluxo normal de edição ou pedido.');
                const id = crypto.randomUUID();
                const payload = local ? {schema_version:1, dados:structuredClone(data), expected_revision:local.revision} : {
                    schema_version:1, ...(data.cliente_id ? {cliente_id:String(data.cliente_id)} : {}), data_venda:data.data_venda,
                    tipo_pagamento:data.tipo_pagamento, operador:data.operador,
                    itens:data.itens.map(item => Object.fromEntries(['produto_id','quantidade','unidade','preco_unitario'].map(k => [k,String(item[k])]))),
                    ...(data.data_vencimento ? {data_vencimento:data.data_vencimento} : {}),
                    ...(data.origem_recebimento ? {origem_recebimento:data.origem_recebimento} : {})};
                const command = {operation_id:id, aggregate_id:id, device_id:crypto.randomUUID(), actor_id:actor,
                    environment_id:environment, type:local ? 'venda_local' : 'criar_venda', schema_version:1,
                    sequence:1, created_at:new Date().toISOString(), payload};
                command.payload_hash = await hash(command);
                pending = {command};
                try { sessionStorage.setItem(key, JSON.stringify(pending)); }
                catch (error) { pending = null; throw Error('Não foi possível proteger este envio contra repetição. Nenhum envio foi iniciado.'); }
                render();
                const response = await request('/api/vendas/online/', {method:'POST',
                    headers:{'Content-Type':'application/json', 'X-CSRFToken':csrf(), 'X-Online-Only':'1', ...(local ? {'X-Local-Sales':'1'} : {})}, body:JSON.stringify(command)});
                const result = await response.json();
                if (!validReceipt(result.receipt, command, response.status) || result.sucesso !== (result.receipt.status === 'confirmada'))
                    throw Error('Resultado desconhecido. Consulte este envio; não repita a venda.');
                if (!result.sucesso) { clearSubmission(); throw Error(result.mensagem || result.receipt.erro); }
                pending.receipt = result.receipt;
                try { sessionStorage.setItem(key, JSON.stringify(pending)); }
                catch (_) { /* The original protected submission still supports a result lookup. */ }
                return result;
            } catch (error) {
                if (pending && !pending.receipt) throw Error('Não foi possível confirmar este envio. Consulte o resultado antes de repetir a venda.');
                throw error;
            } finally { busy = false; render(); }
        },
        afterSuccess() { clearSubmission(); },
    };
    window.salesOffline.active = false;
    window.salesOffline.ready = Promise.resolve();
    window.salesOffline.suspect = probe;
    // Unauthenticated editing/import still uses its existing server endpoint, never an offline queue.
    window.onlineOnlySales.handles = !!local || bridge.eligible;
    window.salesDraftResolve?.();
    render();
}
