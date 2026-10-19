import {openDB, Repository} from '/offline/assets/2-8f/core.js';
import {carregarSnapshotComercial, obterClientesSnapshot, obterProdutosSnapshot} from '/offline/assets/2-8f-fix/commercial.js';

const state = window.salesOffline;
const indicator = document.getElementById('offline-global');
const rentalActions = '#locacoesOperacionaisVenda .locacoes-rapidas-resolver, '
    + '#locacoesOperacionaisVenda .locacoes-rapidas-confirmar, '
    + '#locacoesOperacionaisVenda [data-resolver-form], '
    + '#locacoesOperacionaisVenda [data-confirmar-envio-form]';
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
else if (scope) await repo?.put('metadata', {key: 'sales-identity', ...scope});
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
        await window.salesDraftUI?.refreshCompletion();
        window.atualizarSelectsBonitosVenda?.();
        document.dispatchEvent(new Event('sales-offline-active'));
    } finally {
        state.activating = false;
        // Reconnection may have arrived while activation still held its guard.
        if (indicator?.dataset.connection === 'online' && window.salesDraftUI?.ready) queueMicrotask(connectionChanged);
    }
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
    if (!scope) return; // An anonymous normal page owns no authenticated draft.
    const current = await repo?.get('metadata', 'sales-identity');
    const session = window.salesSessionStatus;
    if (scope && session?.authenticated === true && session.environment_id === scope.environment_id
        && session.actor_id && session.actor_id !== scope.actor_id) {
        blockIdentity(); return;
    }
    if (scope && (current?.actor_id !== scope.actor_id || current?.environment_id !== scope.environment_id)) {
        blockIdentity(); return;
    }
    if (!state.shell && !state.active && (session?.authenticated === false || state.authenticationRequired)) {
        state.authenticationRequired = true;
        banner.hidden = false;
        banner.replaceChildren(document.createTextNode('Sess\u00e3o ausente ou expirada. Entre novamente com seu usu\u00e1rio. Rascunho preservado. '));
        const login = document.createElement('a');
        login.id = 'sales-session-login'; login.textContent = 'Fazer login';
        login.href = '/offline/login/?next=' + encodeURIComponent(location.pathname + location.search);
        login.target = '_blank'; login.rel = 'noopener';
        const reopen = document.createElement('a'); reopen.textContent = 'Reabrir Vendas ap\u00f3s entrar';
        reopen.href = location.pathname + location.search;
        reopen.addEventListener('click', async event => {
            event.preventDefault(); await window.salesDraftUI?.flush(); location.assign(reopen.href);
        });
        banner.append(login, document.createTextNode(' · '), reopen);
        disableSalesFields();
        return;
    }
    if (current?.actor_id === scope?.actor_id && current?.environment_id === scope?.environment_id) return;
    if (!scope) return;
    blockIdentity();
}
function disableSalesFields() {
    for (const field of document.querySelectorAll('#layout-vendas input, #layout-vendas select, #layout-vendas button')) {
        if (!field.matches('[data-sales-navigation-toggle]') && !field.closest(rentalActions)) field.disabled = true;
    }
}
function blockIdentity() {
    // Freeze autosave synchronously before clearing the old user's assembly.
    state.identityBlocked = true;
    document.dispatchEvent(new Event('sales-identity-blocked'));
    scope = null;
    document.getElementById('tabelaProdutos').replaceChildren();
    document.getElementById('clienteBusca').value = '';
    document.getElementById('clienteId').value = '';
    banner.hidden = false;
    banner.textContent = 'Identidade mudou. Reabra Vendas após autenticar-se. Montagem anterior preservada no dispositivo.';
    disableSalesFields();
}
function sessionStatus(status) {
    if (status?.environment_id !== indicator?.dataset.environment) return;
    window.salesSessionStatus = status;
    void checkIdentity();
}
document.addEventListener('sales-session-status', event => sessionStatus(event.detail));
const sessionChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-session') : null;
if (sessionChannel) sessionChannel.onmessage = event => sessionStatus(event.data);
if (window.salesSessionStatus) sessionStatus(window.salesSessionStatus);
if (identityChannel) identityChannel.onmessage = checkIdentity;
window.addEventListener('focus', checkIdentity);
state.ready = state.active ? state.activate() : Promise.resolve();
state.suspect = () => document.dispatchEvent(new Event('offline-connectivity-suspect'));
function showProtectedOnline() {
    if (state.identityBlocked || indicator?.dataset.connection !== 'online') return;
    banner.hidden = false;
    banner.dataset.blockReason = window.salesDraftUI?.lastOnlineRecoveryBlock || '';
    if (window.salesSessionStatus?.authenticated === false) {
        banner.dataset.reason = 'authentication';
        banner.textContent = 'Servidor online. Sessão ausente ou expirada. Entre novamente com o mesmo usuário em Status. Venda local preservada; nenhum envio foi iniciado.';
    } else {
        banner.dataset.reason = 'protected-local-sale';
        banner.textContent = 'Servidor online. Montagem mantida com dados locais para preservar venda pendente, em revisão ou protegida. Consulte Status para verificar a pendência; nenhum envio foi iniciado.';
    }
    if (snapshot) banner.append(document.createTextNode(' Catálogo local: '
        + new Date(snapshot.prepared_at || snapshot.gerado_em).toLocaleString('pt-BR') + '.'));
}
state.deactivate = async () => {
    // A cached shell must not advertise a restored metadata observation as a fresh connection.
    // A successful session GET (including authenticated=false) proves contact on this page.
    if (state.shell && !window.salesSessionStatus) return;
    if (!state.active || state.activating || state.recovering || indicator?.dataset.connection !== 'online') return;
    if (!window.salesDraftUI?.ready) return;
    if (window.salesSessionStatus?.authenticated === false) { showProtectedOnline(); return; }
    if (!await window.salesDraftUI?.canRecoverOnline()) { showProtectedOnline(); return; }
    if (state.recovering || indicator?.dataset.connection !== 'online') return;
    state.recovering = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let previous = null;
    let recoveryMessage = 'Servidor online. Não foi possível recuperar o catálogo online; montagem local preservada. O sistema tentará novamente, sem enviar vendas.';
    let recoveryReason = 'catalog-unavailable';
    try {
        // Fetch a fresh server catalogue without navigating or executing its scripts.
        const response = await fetch('/vendas/', {signal: controller.signal, cache: 'no-store', credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}});
        if (response.redirected || [401, 403].includes(response.status)) {
            recoveryReason = 'authentication';
            recoveryMessage = 'Servidor respondeu, mas o acesso ao catálogo online precisa de autenticação ou permissão. Entre com o mesmo usuário em Status. Venda local preservada; nenhum envio foi iniciado.';
            throw new Error('catalog-authentication');
        }
        if (!response.ok) {
            recoveryMessage = `Servidor respondeu HTTP ${response.status}; catálogo online indisponível. Montagem local preservada. O sistema tentará novamente, sem enviar vendas.`;
            throw new Error('catalog-unavailable');
        }
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const identity = page.getElementById('offline-global');
        if (identity?.dataset.actor !== scope?.actor_id || identity?.dataset.environment !== scope?.environment_id
            || !page.getElementById('produto') || !page.getElementById('operadorVenda') || !page.getElementById('tipoVenda')) {
            recoveryReason = 'catalog-identity';
            recoveryMessage = 'Servidor respondeu, mas a identidade ou o catálogo online não pôde ser validado. Confira a sessão do mesmo usuário em Status. Montagem local preservada; nenhum envio foi iniciado.';
            throw new Error('catalog-identity');
        }
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
        banner.hidden = true; banner.textContent = ''; delete banner.dataset.reason;
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
        if (!state.identityBlocked) { banner.dataset.reason = recoveryReason; banner.textContent = recoveryMessage; }
    } finally { clearTimeout(timer); state.recovering = false; }
};
function connectionChanged() {
    if (state.identityBlocked) return;
    if (indicator?.dataset.connection === 'offline' && state.active && !state.activating) {
        delete banner.dataset.reason;
        banner.hidden = false;
        banner.textContent = snapshot
            ? `Modo offline — dados locais de referência. Catálogo: ${new Date(snapshot.prepared_at || snapshot.gerado_em).toLocaleString('pt-BR')}. Comunicação com o servidor indisponível. Venda local preservada; nenhum envio foi iniciado.`
            : 'Comunicação com o servidor indisponível. Dados locais não preparados; montagem preservada.';
    }
    if (indicator?.dataset.connection === 'offline' && !state.active) state.ready = state.activate();
    if (indicator?.dataset.connection === 'online' && state.active) void state.deactivate().catch(() => {});
}
document.addEventListener('offline-connectivity', connectionChanged);
document.addEventListener('offline-operation-updated', connectionChanged);
document.addEventListener('sales-completion-released', connectionChanged);
document.addEventListener('sales-draft-ready', connectionChanged);
connectionChanged();
// Official actions remain blocked; sale buttons use the local finalizer.
document.addEventListener('click', event => {
    // Authentication navigation never releases a protected sale assembly.
    if (event.target.closest('[data-operacional-login]')) return;
    // Rental actions have their own connection/authentication checks. A pending
    // sale may keep this assembly local even after the server is online again.
    if (event.target.closest(rentalActions)) return;
    if (event.target.closest('[data-sales-navigation-toggle]')) return;
    // Read-only navigation and closing a warning do not release the assembly.
    if (event.target.closest('#btnConsultarVendas, #btnFilaDepositoVendaGravada, #atalhosVenda a[href], #modal-operador-obrigatorio')) return;
    if ((state.completed && event.target.closest('#layout-vendas') && !event.target.closest('#sales-draft-status, #btnConsultarVendas, [data-sales-navigation], #modal-operador-obrigatorio')) || (state.active && event.target.closest('#vendaGravadaBloco, #locacoesOperacionaisVenda, .vendas-pendencias-lateral, .cobrancas-vendas-lateral, #revisaoPrecosPosterior, #atalho-despesa-global'))) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
document.addEventListener('submit', event => {
    if (event.target.matches('#locacoesOperacionaisVenda [data-resolver-form], #locacoesOperacionaisVenda [data-confirmar-envio-form]')) return;
    if (state.active) { event.preventDefault(); event.stopImmediatePropagation(); }
}, true);
