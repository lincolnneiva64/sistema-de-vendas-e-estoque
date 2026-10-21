'use strict';
// One-time presentation feedback across the existing POST/redirect/GET flow.
try {
    const stored = JSON.parse(sessionStorage.getItem('inventario-feedback') || 'null');
    if (stored) {
        sessionStorage.removeItem('inventario-feedback');
        if (stored.version === 2 && stored.path === location.pathname && !document.querySelector('[data-ajustes-pendentes]')) {
            const message = document.createElement('p');
            message.className = 'status';
            message.setAttribute('role', 'status');
            message.textContent = stored.text;
            document.querySelector('main').prepend(message);
        }
    }
} catch (_) { /* Storage may be unavailable; the persisted result remains visible. */ }

async function confirmarEstoque(form, button, data) {
    const buttons = [...document.querySelectorAll('form button')].map(b => [b, b.disabled, b.textContent]);
    document.querySelectorAll('main > .status[data-mensagem-servidor]').forEach(node => node.remove());
    let feedback = form.querySelector('[data-feedback]');
    if (!feedback) {
        feedback = document.createElement('p');
        feedback.dataset.feedback = '';
        form.append(feedback);
    }
    const informar = (text, failed = false) => {
        feedback.textContent = text;
        feedback.setAttribute('role', failed ? 'alert' : 'status');
        feedback.className = failed ? 'erro' : 'status';
    };
    const restore = () => {
        form.removeAttribute('aria-busy');
        buttons.forEach(([b, disabled, label]) => { b.disabled = disabled; b.textContent = label; });
    };
    const mostrar = payload => {
        const destination = new URL(payload.destino, location.origin);
        if (destination.origin !== location.origin || !destination.pathname.startsWith('/inventario/')) {
            throw new Error('Destino de resultado inválido. Consulte o histórico.');
        }
        informar(payload.mensagem);
        // The GET verifies the exact receipt again; sessionStorage never certifies an adjustment.
        destination.searchParams.set('resultado_atualizacao', data.get('previa'));
        location.assign(destination.href);
    };
    const headers = {Accept:'application/json', 'X-Inventario-Ajuste':'1'};
    form.setAttribute('aria-busy', 'true');
    buttons.forEach(([b]) => { b.disabled = true; });
    if (button) button.textContent = 'Atualizando...';
    informar('Atualizando...');
    try {
        const response = await fetch(form.action, {method:'POST', body:data, credentials:'same-origin', headers});
        const payload = response.headers.get('content-type')?.includes('application/json') ? await response.json() : null;
        if (response.ok && payload?.ok && payload.estado === 'aplicado') {
            mostrar(payload);
            return;
        }
        if (payload?.estado === 'rejeitado' && response.status < 500) {
            restore();
            informar(payload.erro, true);
            return;
        }
        throw new Error('Resposta sem resultado verificável.');
    } catch (_) {
        // Never repeat the POST automatically. Read only the audit of this signed lot.
        try {
            const receipt = new URL(form.action);
            receipt.search = '';
            receipt.searchParams.set('resultado_atualizacao', data.get('previa'));
            const response = await fetch(receipt, {credentials:'same-origin', headers});
            const payload = await response.json();
            if (response.ok && payload.ok && payload.estado === 'aplicado') {
                mostrar(payload);
                return;
            }
            restore();
            informar(payload.estado === 'nao_registrado'
                ? 'Resposta da confirmação indisponível. Nenhuma aplicação deste lote foi encontrada nesta consulta; a operação ainda não está comprovada. Consulte o histórico antes de tentar novamente.'
                : payload.erro || 'Não foi possível verificar o resultado. Consulte o histórico antes de tentar novamente.', true);
        } catch (_) {
            restore();
            informar('Não foi possível verificar a resposta. A operação pode ter sido registrada. Consulte o histórico antes de tentar novamente.', true);
        }
    }
}
// Only administrative inventory forms. Counting forms keep their version-aware handler.
document.addEventListener('submit', async event => {
    const form = event.target;
    if (event.defaultPrevented || form.hasAttribute('data-processamento') || form.method.toLowerCase() !== 'post'
        || document.querySelector('.mobile') || !document.querySelector('.navegacao-contextual')) return;
    event.preventDefault();
    if (document.querySelector('form[aria-busy="true"]')) return;
    const button = event.submitter || form.querySelector('button[type=submit],button:not([type])');
    const data = new FormData(form);
    if (button?.name) data.append(button.name, button.value);
    if (['atualizar', 'aplicar'].includes(data.get('acao'))) {
        await confirmarEstoque(form, button, data);
        return;
    }
    const buttons = [...document.querySelectorAll('form button')].map(b => [b, b.disabled, b.textContent]);
    const action = data.get('acao');
    const text = button?.dataset.processando || (action === 'atualizar' || action === 'aplicar'
        ? 'Atualizando...' : action === 'recontar' ? 'Confirmando recontagem...' : 'Confirmando...');
    let feedback = form.querySelector('[data-feedback]');
    if (!feedback) {
        feedback = document.createElement('p');
        feedback.dataset.feedback = '';
        feedback.setAttribute('role', 'status');
        form.append(feedback);
    }
    feedback.textContent = text;
    form.setAttribute('aria-busy', 'true');
    buttons.forEach(([b]) => { b.disabled = true; });
    if (button) button.textContent = text;
    try {
        const response = await fetch(form.action, {method:'POST', body:data,
            credentials:'same-origin', headers:{Accept:'text/html'}});
        if (!response.headers.get('content-type')?.includes('text/html')) throw new Error('Resposta inesperada do servidor.');
        const html = await response.text();
        if (response.status >= 500) throw new Error('O servidor não confirmou a operação.');
        const page = new DOMParser().parseFromString(html, 'text/html');
        if (!response.ok || !response.redirected) {
            throw new Error(page.querySelector('.erro,[role=alert]')?.textContent.trim()
                || 'A operação não foi confirmada. Verifique seu acesso e os campos preenchidos.');
        }
        const destination = new URL(response.url);
        if (destination.origin !== location.origin || !destination.pathname.startsWith('/inventario/')) {
            throw new Error('Acesso expirado. Entre novamente em outra aba e preserve esta tela.');
        }
        const text = page.querySelector('main > .status[data-mensagem-servidor]')?.textContent.trim();
        if (text && action !== 'revisar_ajustes_historicos') {
            try { sessionStorage.setItem('inventario-feedback', JSON.stringify({version:2, path:destination.pathname, text})); } catch (_) {}
        }
        // A fresh GET preserves the native page lifecycle and its existing scripts.
        // Access-link generation forms retain their own handler and one-time response.
        location.assign(destination.href);
    } catch (error) {
        form.removeAttribute('aria-busy');
        buttons.forEach(([b, disabled, label]) => { b.disabled = disabled; b.textContent = label; });
        feedback.textContent = 'Não foi possível confirmar a resposta. A operação pode ter sido registrada. Consulte o histórico antes de tentar novamente. ' + error.message;
        feedback.setAttribute('role', 'alert');
    }
});
window.inventarioFeedbackPronto = true;
