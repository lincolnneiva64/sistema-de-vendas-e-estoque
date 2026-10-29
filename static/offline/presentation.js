// Presentation only: state selection and synchronization remain in core/app.
const belemClock = new Intl.DateTimeFormat('pt-BR', {timeZone:'America/Belem',
    day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});

export function formatOfflineTime(value) {
    // Never interpret a date-only or a timezone-free legacy string as an instant.
    if (!(typeof value === 'number' && Number.isFinite(value))
        && !(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value))) return 'Horário não registrado';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return 'Horário não registrado';
    if (typeof value === 'string') {
        const [year, month, day] = value.slice(0, 10).split('-').map(Number);
        if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return 'Horário não registrado';
    }
    const parts = Object.fromEntries(belemClock.formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.day}/${parts.month}/${parts.year} às ${parts.hour}:${parts.minute}:${parts.second}`;
}

export function timeText(target, text) {
    // Only for presentation nodes containing text; never rewrite a command/receipt.
    const parts = String(text).split(/(\d{2}\/\d{2}\/\d{4} às \d{2}:\d{2}:\d{2})/g);
    target.replaceChildren(...parts.map((part, index) => {
        if (index % 2 === 0) return document.createTextNode(part);
        const strong = document.createElement('strong');
        strong.className = 'offline-timestamp'; strong.textContent = part;
        strong.style.cssText = 'color:#1e293b;font-weight:700;overflow-wrap:anywhere;white-space:normal';
        strong.title = 'Horário de Belém (PA) · America/Belem';
        return strong;
    }));
}

export function catalogTimes(snapshot) {
    return `Preparação informada pelo dispositivo: ${formatOfflineTime(snapshot?.prepared_at)} · `
        + `Geração do catálogo no servidor: ${formatOfflineTime(snapshot?.gerado_em)} · Horário de Belém (PA)`;
}

export function operationTimes(operation) {
    const box = document.createElement('div');
    box.className = 'offline-operation-times';
    box.style.cssText = 'color:#334155;font-size:12px;line-height:1.6;overflow-wrap:anywhere;white-space:normal';
    for (const [label, value] of [
        ['Criação informada pelo dispositivo', operation.created_at],
        ['Conclusão do processamento no servidor (resultado recebido)', operation.server_result?.completed_at],
        ...(operation.synchronized_at ? [['Confirmação salva neste dispositivo', operation.synchronized_at]] : []),
    ]) {
        const row = document.createElement('div');
        const text = value == null && label.includes('servidor')
            ? 'Horário não disponível nesta consulta' : formatOfflineTime(value);
        timeText(row, `${label}: ${text}`); box.append(row);
    }
    const legend = document.createElement('div');
    legend.textContent = 'Horário de Belém (PA) · America/Belem. Os registros não comprovam o horário de execução física.';
    box.append(legend);
    return box;
}

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
