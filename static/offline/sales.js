import {openDB, Repository} from '/offline/assets/2-8e/core.js';
import {carregarSnapshotComercial, obterClientesSnapshot, obterProdutosSnapshot} from '/offline/assets/2-8e/commercial.js';

const state = window.salesOffline;
const indicator = document.getElementById('offline-global');
let repo;
try { repo = new Repository(await openDB()); }
catch (_) { /* The missing-catalog guidance below also covers blocked IndexedDB. */ }
let scope = indicator?.dataset.actor ? {
    actor_id: indicator.dataset.actor, environment_id: indicator.dataset.environment,
} : null;
if (state.shell) {
    scope = await repo?.get('metadata', 'sales-identity');
    if (scope?.environment_id !== indicator?.dataset.environment) scope = null;
}
else await repo?.put('metadata', {key: 'sales-identity', ...scope});
const banner = document.createElement('div');
banner.id = 'sales-offline-notice'; banner.setAttribute('role', 'status');
banner.style.cssText = 'padding:6px 12px;background:#fff4d6;color:#624800;font-size:12px;overflow-wrap:anywhere;flex-shrink:0';
banner.hidden = true;
document.getElementById('layout-vendas').prepend(banner);
let snapshot = null;
function options(id, items, value, label) {
    const select = document.getElementById(id), selected = select.value;
    select.replaceChildren(new Option('Selecione', ''));
    for (const item of items) select.add(new Option(label(item), value(item)));
    select.value = selected;
}
state.activate = async () => {
    if (!state.shell && indicator?.dataset.connection !== 'offline') return;
    if (state.activating || state.recovering) return;
    state.activating = true;
    try {
        const mountedDraft = window.salesDraftUI?.ready && window.salesDraftBridge?.eligible
            ? window.salesDraftBridge.capture() : null;
        state.active = true;
        state.available = false;
        document.body.dataset.salesOffline = 'true';
        document.getElementById('btnGravarVenda').disabled = true;
        document.getElementById('btnConfirmarFechamentoVenda').disabled = true;
        banner.hidden = false;
        try {
            const current = await repo?.get('metadata', 'sales-identity');
            if (current?.actor_id !== scope?.actor_id || current?.environment_id !== scope?.environment_id) scope = null;
            snapshot = repo && scope && await carregarSnapshotComercial(repo, scope);
            if (!snapshot) throw new Error('missing');
            const products = await obterProdutosSnapshot(repo, scope);
            options('produto', products, p => p.nome, p => p.nome);
            const mapping = {produtoId:'id', codigo:'codigo', unidade:'unidade_venda_1', unidade1:'unidade_venda_1',
                unidade2:'unidade_venda_2', preco:'preco_venda', preco2:'preco_venda_fracionado',
                estoque:'estoque_referencia', estoqueConferido:'estoque_conferido', precoConferido:'preco_conferido',
                fracionado:'vende_fracionado', fator:'fator_conversao', custo:'custo_referencia', custo2:'custo_fracionado_referencia'};
            [...document.getElementById('produto').options].slice(1).forEach((option, i) => {
                for (const [key, field] of Object.entries(mapping)) option.dataset[key] = String(products[i][field]);
            });
            options('operadorVenda', snapshot.operadores, o => o.nome, o => o.nome);
            options('tipoVenda', snapshot.formas_pagamento, f => f.valor, f => f.label);
            state.available = true;
            state.referenceSnapshot = {snapshot_id:snapshot.snapshot_id, prepared_at:snapshot.prepared_at || snapshot.gerado_em};
            banner.textContent = `Modo offline — dados locais de referência. Catálogo: ${new Date(snapshot.prepared_at || snapshot.gerado_em).toLocaleString('pt-BR')}. Estoque/preços serão revalidados na futura sincronização. A venda pode ser salva neste aparelho, sem envio ao servidor.`;
        } catch (_) {
            snapshot = null;
            state.referenceSnapshot = null;
            for (const id of ['produto', 'operadorVenda', 'tipoVenda']) options(id, [], () => '', () => '');
            banner.textContent = 'Dados de vendas não preparados neste dispositivo. Conecte-se e use Status → Preparar dados de vendas. Rascunho válido já salvo pode ser concluído localmente; o servidor revalidará os dados no envio futuro.';
        }
        if (mountedDraft && scope) await window.salesDraftUI.restoreReference(mountedDraft);
        window.atualizarSelectsBonitosVenda?.();
        document.dispatchEvent(new Event('sales-offline-active'));
    } finally { state.activating = false; }
};
state.clients = async term => {
    await checkIdentity();
    if (!state.active) return [];
    if (!snapshot) return [];
    const normalize = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    const query = normalize(term);
    try {
        return (await obterClientesSnapshot(repo, scope)).filter(c =>
            normalize([c.nome, c.apelido_nome_conhecido, c.telefone].join(' ')).includes(query))
            .map(c => ({...c, prazo: c.prazo_padrao_dias, whatsapp: c.telefone}));
    } catch (_) { return []; }
};
const identityChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-identity') : null;
async function checkIdentity() {
    const current = await repo?.get('metadata', 'sales-identity');
    if (current?.actor_id === scope?.actor_id && current?.environment_id === scope?.environment_id) return;
    scope = null;
    document.getElementById('tabelaProdutos').replaceChildren();
    document.getElementById('clienteBusca').value = '';
    document.getElementById('clienteId').value = '';
    banner.hidden = false;
    banner.textContent = 'Identidade mudou. Reabra Vendas após autenticar-se. Montagem anterior preservada no dispositivo.';
    for (const field of document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')) field.disabled = true;
}
if (identityChannel) identityChannel.onmessage = checkIdentity;
window.addEventListener('focus', checkIdentity);
state.ready = state.active ? state.activate() : Promise.resolve();
state.suspect = () => document.dispatchEvent(new Event('offline-connectivity-suspect'));
state.deactivate = async () => {
    if (!state.active || state.activating || state.recovering || indicator?.dataset.connection !== 'online') return;
    if (!await window.salesDraftUI?.canRecoverOnline()) return;
    if (state.recovering || indicator?.dataset.connection !== 'online') return;
    state.recovering = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let previous = null;
    try {
        // Fetch a fresh server catalogue without navigating or executing its scripts.
        const response = await fetch('/vendas/', {signal: controller.signal, cache: 'no-store', credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}});
        if (!response.ok) throw new Error('Catálogo online indisponível');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const identity = page.getElementById('offline-global');
        if (identity?.dataset.actor !== scope?.actor_id || identity?.dataset.environment !== scope?.environment_id
            || !page.getElementById('produto') || !page.getElementById('operadorVenda') || !page.getElementById('tipoVenda')) throw new Error('Identidade ou catálogo online inválido');
        if (indicator.dataset.connection !== 'online' || !await window.salesDraftUI.canRecoverOnline()) return;
        const assembly = window.salesDraftBridge.capture();
        previous = {shell: state.shell, available: state.available, snapshot,
            options: new Map(['produto', 'operadorVenda', 'tipoVenda'].map(id => [id,
                Array.from(document.getElementById(id).options, option => option.cloneNode(true))]))};
        for (const id of ['produto', 'operadorVenda', 'tipoVenda']) {
            document.getElementById(id).replaceChildren(...Array.from(page.getElementById(id).options, option => document.importNode(option, true)));
        }
        state.active = false; state.shell = false; state.available = false; snapshot = null;
        delete document.body.dataset.salesOffline;
        banner.hidden = true; banner.textContent = '';
        window.salesDraftUI.restoreOnlineReference(assembly);
        document.getElementById('btnGravarVenda').textContent = 'Gravar Venda';
        document.getElementById('btnGravarVenda').disabled = false;
        document.getElementById('btnConfirmarFechamentoVenda').disabled = false;
        window.atualizarSelectsBonitosVenda?.();
        document.dispatchEvent(new Event('sales-online-active'));
    } catch (_) {
        if (previous) {
            state.active = true; state.shell = previous.shell; state.available = previous.available; snapshot = previous.snapshot;
            document.body.dataset.salesOffline = 'true';
            for (const [id, options] of previous.options) document.getElementById(id).replaceChildren(...options);
        }
        banner.hidden = false;
        banner.textContent = 'Conexão restabelecida. Não foi possível recuperar o catálogo online; montagem local preservada. Reabra Vendas para tentar novamente.';
    } finally { clearTimeout(timer); state.recovering = false; }
};
function connectionChanged() {
    if (indicator?.dataset.connection === 'offline' && !state.active) state.ready = state.activate();
    if (indicator?.dataset.connection === 'online' && state.active) void state.deactivate().catch(() => {});
}
document.addEventListener('offline-connectivity', connectionChanged);
document.addEventListener('offline-operation-updated', connectionChanged);
document.addEventListener('sales-completion-released', connectionChanged);
connectionChanged();
// Official actions remain blocked; sale buttons use the local finalizer.
document.addEventListener('click', event => {
    if ((state.completed && event.target.closest('#layout-vendas') && !event.target.closest('#sales-official-sale-link, #sales-operation-diagnostic, #sales-online-recover')) || (state.active && event.target.closest('#vendaGravadaBloco, #locacoesOperacionaisVenda, .vendas-pendencias-lateral, .cobrancas-vendas-lateral, #revisaoPrecosPosterior, #atalho-despesa-global'))) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
document.addEventListener('submit', event => {
    if (state.active) { event.preventDefault(); event.stopImmediatePropagation(); }
}, true);
