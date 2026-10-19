'use strict';
// One-time presentation feedback across the existing POST/redirect/GET flow.
try {
    const stored = JSON.parse(sessionStorage.getItem('inventario-feedback') || 'null');
    if (stored) {
        sessionStorage.removeItem('inventario-feedback');
        if (stored.path === location.pathname) {
            const message = document.createElement('p');
            message.className = 'status';
            message.setAttribute('role', 'status');
            message.textContent = stored.text;
            document.querySelector('main').prepend(message);
        }
    }
} catch (_) { /* Storage may be unavailable; the persisted result remains visible. */ }
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
        const text = page.querySelector('main > .status[role=status]')?.textContent.trim()
            || 'Operação concluída. Confira o resultado abaixo.';
        try { sessionStorage.setItem('inventario-feedback', JSON.stringify({path:destination.pathname, text})); } catch (_) {}
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
