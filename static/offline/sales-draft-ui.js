import './sales.js';
import {Repository, openDB} from './core.js';
import {draftScope, loadDraft, saveDraft, discardDraft, projectAssembly, prepareDraftSubmission, confirmDraftSubmission, releaseDraftSubmission} from './sales-drafts.js';

if (!window.salesDraftBridge) await new Promise(resolve => document.addEventListener('sales-draft-bridge-ready', resolve, {once:true}));
await window.salesOffline.ready;
const bridge = window.salesDraftBridge;
const box = document.createElement('div');
box.id = 'sales-draft-status';
box.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:4px 12px;font-size:12px;flex-shrink:0';
const message = document.createElement('span'); message.setAttribute('role', 'status');
const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Descartar rascunho';
discard.id = 'sales-draft-discard'; discard.disabled = true;
box.append(message, discard);
document.getElementById('layout-vendas').prepend(box);
box.hidden = !bridge.eligible;
let repo, scope, revision = 0, saved = '', blocked = false, suppress = true, ignoreReset = false;
let timer, pending = Promise.resolve(), submitted = null;
let replaceConfirmed = false;
let preparedSubmission = false;
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-drafts') : null;
const observer = new MutationObserver(() => { if (!ignoreReset) schedule(0); });
const meaningful = assembly => assembly.cliente || assembly.operador || assembly.tipo_venda || assembly.itens.length || assembly.lancamento;
function announce() { channel?.postMessage({scope, revision}); }
function showError(error) { message.textContent = error.message || 'Não foi possível salvar o rascunho local. Último rascunho preservado.'; }
function schedule(delay = 250) {
    if (suppress || ignoreReset || blocked || !scope || !bridge.eligible) return;
    clearTimeout(timer);
    timer = setTimeout(() => { void flush(); }, delay);
}
function flush() {
    clearTimeout(timer);
    if (suppress || ignoreReset || blocked || !scope || !bridge.eligible) return pending;
    const assembly = bridge.capture(), fingerprint = JSON.stringify(assembly);
    if (fingerprint === saved || (!saved && !meaningful(assembly))) return pending;
    message.textContent = 'Salvando rascunho local…';
    pending = pending.then(async () => {
        if (blocked || fingerprint === saved) return;
        try {
            const result = await saveDraft(repo, scope, assembly, revision, replaceConfirmed);
            replaceConfirmed = false;
            revision = result.revision; saved = fingerprint;
            discard.disabled = false;
            message.textContent = 'Rascunho local salvo. Dados de referência a revalidar.';
            announce();
        } catch (error) {
            if (error.message.includes('outra aba')) blocked = true;
            if (error.message.includes('já enviada')) blocked = true;
            if (error.message.includes('mudou')) { blocked = true; resetVisual(); }
            showError(error);
            window.salesDraftUI.lastError = error.message;
        }
    });
    return pending;
}
function resetVisual() {
    suppress = true;
    bridge.clear(); observer.takeRecords();
    suppress = false;
}
async function checkIdentity() {
    if (!scope || !repo) return;
    const identity = await repo.get('metadata', 'sales-identity'), device = await repo.get('metadata', 'device');
    if (identity?.actor_id !== scope.actor_id || identity?.environment_id !== scope.environment_id || device?.id !== scope.device_id) {
        blocked = true; clearTimeout(timer); resetVisual();
        message.textContent = 'Identidade mudou. Rascunho anterior preservado e montagem limpa.';
        discard.disabled = true;
    }
}
window.salesDraftUI = {
    ready:false, flush, get revision() { return revision; }, get blocked() { return blocked; },
    async restoreReference(assembly) {
        try {
            await checkIdentity();
            if (blocked) return;
            suppress = true;
            bridge.restore(projectAssembly(assembly)); observer.takeRecords();
        } catch (error) { showError(error); }
        finally { suppress = false; }
    },
    async beforeSubmit() {
        if (!bridge.eligible) return true;
        await flush();
        let record = null;
        if (scope && repo && !blocked) {
            try {
                const current = await loadDraft(repo, scope);
                if (current.confirmation) blocked = true;
                record = current.draft;
                if (current.revision !== revision) blocked = true;
            } catch (error) { blocked = true; showError(error); }
        }
        if (blocked) { bridge.error('Rascunho atualizado em outra aba. Reabra a tela antes de concluir.'); return false; }
        if (document.querySelector('#tabelaProdutos tr[data-draft-review]')) {
            bridge.error('Há produto do rascunho fora do catálogo atual. Revise ou remova o item antes de concluir.'); return false;
        }
        submitted = {revision, fingerprint:JSON.stringify(bridge.capture()), record};
        return true;
    },
    prepareSubmission() {
        if (scope && submitted?.record) {
            prepareDraftSubmission(scope, submitted.record);
            preparedSubmission = true;
        }
    },
    confirmSubmission(vendaId) {
        preparedSubmission = false;
        ignoreReset = true; clearTimeout(timer);
        if (scope && submitted?.record) confirmDraftSubmission(scope, submitted.record, vendaId);
    },
    failedSubmission() {
        if (preparedSubmission && scope && submitted?.record) {
            releaseDraftSubmission(scope, submitted.record);
            preparedSubmission = false;
        }
    },
    async confirmedSuccess() {
        if (!scope || !bridge.eligible || !submitted) return;
        clearTimeout(timer); await pending;
        ignoreReset = true;
        if (submitted.fingerprint !== JSON.stringify(bridge.capture()) || submitted.revision !== revision) {
            message.textContent = 'Venda confirmada. Alterações posteriores do rascunho foram preservadas.'; return;
        }
        try {
            const result = await discardDraft(repo, scope, submitted.revision);
            revision = result.revision; saved = ''; discard.disabled = true;
            message.textContent = 'Venda online confirmada. Rascunho local removido.'; announce();
        } catch (error) {
            message.textContent = 'Venda online confirmada. Rascunho local preservado porque a limpeza não foi concluída: ' + error.message;
        }
    },
};
try {
    if (bridge.eligible) {
        repo = new Repository(await openDB());
        const indicator = document.getElementById('offline-global');
        const identity = window.salesOffline.shell ? await repo.get('metadata', 'sales-identity')
            : {actor_id:indicator.dataset.actor, environment_id:indicator.dataset.environment};
        if (identity?.environment_id === indicator.dataset.environment) scope = await draftScope(repo, identity);
        if (scope) {
            const result = await loadDraft(repo, scope);
            revision = result.revision;
            if (result.confirmation) {
                replaceConfirmed = true;
                message.textContent = result.confirmation.estado === 'concluido'
                    ? 'Venda online confirmada. Rascunho residual não restaurado.'
                    : 'Rascunho de envio anterior não restaurado. Confira a venda online antes de repetir.';
            }
            if (result.draft && !result.confirmation) {
                bridge.restore(result.draft);
                saved = JSON.stringify(bridge.capture()); discard.disabled = false;
                message.textContent = `Rascunho local restaurado. Última alteração: ${new Date(result.draft.atualizado_em).toLocaleString('pt-BR')}. Dados de referência a revalidar.`;
            }
        } else message.textContent = 'Autentique-se online para salvar rascunhos locais.';
    }
} catch (error) { blocked = !!scope; showError(error); }
observer.observe(document.getElementById('tabelaProdutos'), {subtree:true, childList:true, attributes:true, characterData:true});
suppress = false;
window.salesDraftUI.ready = true;
window.salesDraftResolve?.();
document.addEventListener('sales-draft-change', () => schedule(0));
document.addEventListener('input', event => {
    if (['dataVenda', 'vencimentoVenda', 'operadorVenda', 'tipoVenda', 'clienteBusca', 'quantidade', 'preco', 'unidade'].includes(event.target.id)) {
        ignoreReset = false; schedule();
    }
});
document.addEventListener('change', event => {
    if (['dataVenda', 'vencimentoVenda', 'operadorVenda', 'tipoVenda', 'produto', 'unidade'].includes(event.target.id)) {
        ignoreReset = false; schedule(0);
    }
});
for (const eventName of ['click', 'keydown']) document.addEventListener(eventName, event => {
    if (event.target.closest('#btnAdicionarItemVenda, #produtoBusca, #quantidade, #unidade, #preco, #clienteBusca')) ignoreReset = false;
}, true);
document.addEventListener('visibilitychange', () => { if (document.hidden) void flush(); });
window.addEventListener('pagehide', () => { if (!ignoreReset) void flush(); });
window.addEventListener('focus', () => { void checkIdentity(); });
const identityChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sales-identity') : null;
if (identityChannel) identityChannel.onmessage = () => { void checkIdentity(); };
if (channel) channel.onmessage = async event => {
    const update = event.data;
    if (scope && JSON.stringify(update?.scope) === JSON.stringify(scope) && update.revision > revision) {
        blocked = true; clearTimeout(timer);
        message.textContent = 'Rascunho atualizado em outra aba. Reabra a tela para carregar a versão salva.';
    }
};
discard.addEventListener('click', async () => {
    if (!await bridge.confirmDiscard()) return;
    await pending;
    try {
        clearTimeout(timer);
        const result = await discardDraft(repo, scope, revision);
        revision = result.revision; saved = ''; resetVisual();
        ignoreReset = true; discard.disabled = true; message.textContent = 'Rascunho local descartado.'; announce();
    } catch (error) { showError(error); }
});
