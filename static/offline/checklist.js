import {Repository, openDB} from '/offline/assets/2-8e/core.js';
const indicator = document.getElementById('offline-global');
const scope = {actor: indicator.dataset.actor, environment: indicator.dataset.environment};
const local = document.body.dataset.localChecklist === 'true';
// Install the guard before preparation or storage can stall.
function blocked() { return local || ['offline', 'checking'].includes(indicator.dataset.state); }
document.addEventListener('submit', event => {
    if (!event.target.matches('[data-local-note]') && blocked()) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
document.addEventListener('click', event => {
    const form = event.target.closest('form');
    if (form && !form.matches('[data-local-note]') && blocked()) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
const repo = new Repository(await openDB());
const key = 'checklist:' + location.pathname + location.search;
let snapshot = local ? (await repo.get('snapshots', key))?.snapshot : await repo.get('snapshots', 'pilot');
const panels = [...document.querySelectorAll('[data-checklist-task]')];
const identityMatches = s => s?.actor.id === scope.actor && s.environment_id === scope.environment;
if (!local) {
    try {
        const sale = panels.find(p => p.dataset.checklistKind === 'entrega_venda');
        const task = sale ? '?rota=' + encodeURIComponent(sale.dataset.rotaId) : panels.length === 1 ? '?task=' + encodeURIComponent(panels[0].dataset.checklistTask) : '';
        const response = await fetch('/api/offline/snapshot/' + task, {cache: 'no-store', signal: AbortSignal.timeout(5000)});
        if (!response.ok) {
            if ([401, 403].includes(response.status)) {
                snapshot = null;
                await repo.put('metadata', {key: 'checklist-identity', actor: null});
            }
            throw new Error('Sem permissão para preparar observações offline.');
        }
        const {csrf_token, ...data} = await response.json();
        if (!identityMatches(data)) throw new Error('Identidade incompatível.');
        snapshot = {...data, key: 'pilot', prepared_at: new Date().toISOString()};
        await repo.put('snapshots', snapshot);
        if ('BroadcastChannel' in window) {
            const channel = new BroadcastChannel('offline-pilot');
            channel.postMessage({type: 'changed', actor: scope.actor, environment: scope.environment}); channel.close();
        }
        await repo.put('metadata', {key: 'checklist-identity', ...scope});
        const clone = document.body.cloneNode(true);
        clone.querySelectorAll('script, input[name="csrfmiddlewaretoken"], #offline-sync-modal').forEach(e => e.remove());
        await repo.put('snapshots', {key, ...scope, snapshot, html: clone.innerHTML,
            styles: [...document.querySelectorAll('style')].map(e => e.outerHTML).join('')});
        await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
        await navigator.serviceWorker.ready;
        await navigator.storage?.persist?.();
    } catch (error) { panels.forEach(p => { p.textContent = error.message; }); }
}
for (const panel of panels) {
    const kind = panel.dataset.checklistKind || 'observacao_operacional';
    const task = identityMatches(snapshot) && snapshot.tasks.find(t => t.id === panel.dataset.checklistTask && (t.kind || 'observacao_operacional') === kind);
    if (!task) { panel.textContent = 'Observações offline indisponíveis: prepare esta tarefa online com permissão.'; continue; }
    panel.innerHTML = '<form data-local-note><label>Observação adicional (não confirma entrega/recolhimento)<textarea required maxlength="2000" rows="3"></textarea></label><button type="submit">Salvar neste dispositivo</button><p role="status"></p></form><details><summary>Pendências locais</summary><ul></ul></details>';
    const form = panel.querySelector('form');
    let saving = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (saving) return;
        saving = true;
        const button = form.querySelector('button'); button.disabled = true;
        try {
            const text = form.querySelector('textarea').value.trim();
            if (!text) throw new Error('Informe a observação.');
            await repo.create(snapshot, task, text);
            form.querySelector('textarea').value = '';
            form.querySelector('[role="status"]').textContent = 'Salvo neste dispositivo';
            if ('BroadcastChannel' in window) {
                const channel = new BroadcastChannel('offline-pilot');
                channel.postMessage({type: 'changed', actor: scope.actor, environment: scope.environment}); channel.close();
            }
            await render();
        } catch (error) { form.querySelector('[role="status"]').textContent = error.message; }
        finally { saving = false; button.disabled = false; }
    });
}
async function render() {
    const operations = (await repo.all('operations')).filter(o => o.actor_id === scope.actor && o.environment_id === scope.environment);
    for (const panel of panels) {
        const pending = operations.filter(o => o.aggregate_id === panel.dataset.checklistTask && o.type === (panel.dataset.checklistKind === 'entrega_venda' ? 'observacao_entrega_venda' : 'observacao_operacional') && o.status !== 'confirmada');
        const summary = panel.querySelector('summary'), list = panel.querySelector('ul');
        if (!list) continue;
        summary.textContent = pending.length + ' pendência(s) local(is) — detalhes'; list.replaceChildren();
        for (const op of pending) {
            const li = document.createElement('li');
            li.textContent = `${op.status}: ${op.payload.observacao} · ${op.operation_id} ${op.last_error || ''}`;
            list.append(li);
        }
    }
    for (const form of document.querySelectorAll('form:not([data-local-note])')) {
        for (const button of form.querySelectorAll('input, select, textarea, button')) {
            if (blocked()) {
                if (!button.hasAttribute('data-offline-blocked')) button.dataset.wasDisabled = String(button.disabled);
                button.dataset.offlineBlocked = 'true'; button.disabled = true;
                button.title = local ? 'Abra esta tela online novamente para confirmar.' : 'Esta ação exige conexão com o servidor.';
            } else if (button.dataset.offlineBlocked) {
                button.disabled = button.dataset.wasDisabled === 'true'; delete button.dataset.offlineBlocked; button.title = '';
            }
        }
    }
}
await render();
setInterval(() => { void render(); }, 1000);
