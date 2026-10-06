import {openDB, Repository} from './core.js';
import {carregarSnapshotComercial, obterClientesSnapshot, obterProdutosSnapshot} from './commercial.js';

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
        banner.textContent = `Modo offline — dados locais de referência. Catálogo: ${new Date(snapshot.prepared_at || snapshot.gerado_em).toLocaleString('pt-BR')}. Estoque/preços serão revalidados na futura sincronização. Venda offline ainda não pode ser concluída nesta etapa.`;
    } catch (_) {
        snapshot = null;
        for (const id of ['produto', 'operadorVenda', 'tipoVenda']) options(id, [], () => '', () => '');
        banner.textContent = 'Dados de vendas não preparados neste dispositivo. Conecte-se e use Status → Preparar dados de vendas. Venda offline ainda não pode ser concluída nesta etapa.';
    }
    window.atualizarSelectsBonitosVenda?.();
    document.dispatchEvent(new Event('sales-offline-active'));
};
state.clients = async term => {
    await checkIdentity();
    if (!state.active) await state.activate();
    if (!snapshot) return [];
    const normalize = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    const query = normalize(term);
    try {
        return (await obterClientesSnapshot(repo, scope)).filter(c =>
            normalize([c.nome, c.apelido_nome_conhecido, c.telefone].join(' ')).includes(query))
            .map(c => ({...c, prazo: c.prazo_padrao_dias, whatsapp: c.telefone}));
    } catch (_) { await state.activate(); return []; }
};
const identityChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-identity') : null;
async function checkIdentity() {
    const current = await repo?.get('metadata', 'sales-identity');
    if (current?.actor_id === scope?.actor_id && current?.environment_id === scope?.environment_id) return;
    scope = null;
    document.getElementById('tabelaProdutos').replaceChildren();
    document.getElementById('clienteBusca').value = '';
    document.getElementById('clienteId').value = '';
    await state.activate();
}
if (identityChannel) identityChannel.onmessage = checkIdentity;
window.addEventListener('focus', checkIdentity);
state.ready = state.active ? state.activate() : Promise.resolve();
window.addEventListener('offline', () => { state.ready = state.activate(); });
let observedOnline = indicator?.dataset.state === 'online';
if (indicator) new MutationObserver(() => {
    if (indicator.dataset.state === 'online') observedOnline = true;
    if (observedOnline && !state.active && indicator.dataset.state === 'offline') state.ready = state.activate();
}).observe(indicator, {attributes: true, attributeFilter: ['data-state']});
window.addEventListener('online', () => {
    if (!state.active) return;
    const link = document.createElement('a'); link.href = '/vendas/';
    link.textContent = ' Reabrir online (a montagem local não será mantida).';
    banner.append(link);
});
// A mounted local sale must never reach an official action, even via keyboard.
document.addEventListener('click', event => {
    if (state.active && event.target.closest('#btnGravarVenda, #btnConfirmarFechamentoVenda, #vendaGravadaBloco, #locacoesOperacionaisVenda, .vendas-pendencias-lateral, .cobrancas-vendas-lateral, #revisaoPrecosPosterior, #atalho-despesa-global')) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
document.addEventListener('submit', event => {
    if (state.active) { event.preventDefault(); event.stopImmediatePropagation(); }
}, true);
