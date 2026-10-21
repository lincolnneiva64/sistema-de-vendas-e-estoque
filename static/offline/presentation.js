// Presentation only: state selection and synchronization remain in core/app.
export function renderIndicator(target, state, elapsedMinutes) {
    if (!target) return;
    const global = target.closest('.offline-global');
    if (global?.querySelector('#offline-toggle')) {
        const labels = {checking: 'Reconectando', online: 'Online', offline: 'Offline'};
        const label = labels[state.connectionKind] || 'Reconectando';
        global.dataset.state = state.connectionKind;
        global.dataset.requiresAction = String(state.requiresAction);
        global.dataset.syncState = state.kind;
        global.querySelector('#offline-tab-label').textContent = label;
        const badge = global.querySelector('#offline-pending-badge');
        badge.textContent = String(state.count);
        badge.hidden = !state.count;
        global.querySelector('#offline-toggle').setAttribute('aria-label',
            label + '. ' + (state.requiresAction ? 'Há pendências que exigem atenção. ' : '')
            + state.label + '. ' + state.count + ' operações pendentes. Abrir ou recolher detalhes');
    }
    const quantity = state.count + (state.count === 1 ? ' operação' : ' operações');
    let title = state.label, detail = '', counter = '', pending = '';
    if (state.kind === 'waiting') {
        title = 'Conexão restabelecida';
        detail = 'Verificando estabilidade para sincronização segura';
        counter = elapsedMinutes + ' de 15 minutos';
        pending = quantity + ' aguardando';
    } else if (state.kind === 'ready') {
        title = 'Conexão estável';
        detail = quantity + (state.count === 1 ? ' pronta' : ' prontas') + ' para sincronizar';
    } else if (state.kind === 'offline') {
        title = 'Sem conexão — trabalhando localmente';
        pending = quantity + (state.count === 1 ? ' pendente' : ' pendentes');
    } else if (state.kind === 'online') {
        title = 'Conexão estável';
    }
    if (global) {
        title = state.connectionKind === 'online' ? 'Online — comunicação confirmada com o servidor'
            : state.connectionKind === 'offline' ? 'Offline — servidor inacessível'
            : 'Reconectando — aguardando confirmação do servidor';
        detail = state.label;
        if (state.kind === 'waiting') detail = 'Sincronização aguardando verificação de estabilidade (' + elapsedMinutes + ' de 15 minutos).';
        if (state.kind === 'online') detail = '';
        counter = ''; pending = '';
    }
    target.dataset.state = state.kind;
    target.classList.add('offline-status-card');
    const parts = [[title, 'offline-status-title'], [detail, 'offline-status-detail'],
        [counter, 'offline-status-counter'], [pending, 'offline-status-pending'],
        ...(state.actionDetails || []).map(text => [text, 'offline-status-action'])];
    const signature = JSON.stringify(parts);
    // Avoid replacing the live region every second when its content is unchanged.
    if (target.dataset.presentation === signature) return;
    target.dataset.presentation = signature;
    target.replaceChildren(...parts.filter(([text]) => text).map(([text, className], index) => {
        const node = document.createElement('span');
        node.className = className;
        node.textContent = (index ? ' ' : '') + text;
        return node;
    }));
}
