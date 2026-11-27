/* Report presentation only. All financial values and sorting come from Django. */
(() => {
    const root = document.querySelector('.analise-consolidada');
    if (!root) return;
    const table = root.querySelector('.ac-tabela table');
    if (table?.tHead) {
        // Horizontal overflow creates a scroll ancestor even without a height limit,
        // so CSS sticky cannot follow the page. Move the original header instead;
        // column widths and horizontal scrolling stay aligned without a duplicate.
        let pending = false;
        function updateHeader() {
            pending = false;
            const bounds = table.getBoundingClientRect();
            const headerHeight = table.tHead.getBoundingClientRect().height;
            const footerHeight = table.tFoot?.getBoundingClientRect().height || 0;
            const limit = Math.max(0, bounds.height - headerHeight - footerHeight);
            const offset = Math.min(Math.max(0, -bounds.top), limit);
            table.style.setProperty('--ac-cabecalho-y', `${offset}px`);
        }
        function scheduleHeader() {
            if (pending) return;
            pending = true;
            requestAnimationFrame(updateHeader);
        }
        window.addEventListener('scroll', scheduleHeader, {passive: true});
        window.addEventListener('resize', scheduleHeader);
        // Includes class disclosure, unit details and changes above the table.
        new ResizeObserver(scheduleHeader).observe(root);
        updateHeader();
    }
    const buttons = [...root.querySelectorAll('[data-ac-classe-botao]')];
    for (const button of root.querySelectorAll('[data-ac-produto-botao]')) {
        const rows = button.getAttribute('aria-controls').trim().split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
        function disclose(open) {
            button.setAttribute('aria-expanded', String(open));
            for (const row of rows) row.hidden = !open;
        }
        disclose(false);
        button.hidden = false;
        button.addEventListener('click', () => disclose(button.getAttribute('aria-expanded') !== 'true'));
    }
    function toggle(button, open) {
        const rows = document.getElementById(button.getAttribute('aria-controls'));
        if (!rows) return;
        button.setAttribute('aria-expanded', String(open));
        rows.hidden = !open;
    }
    for (const button of buttons) {
        button.addEventListener('click', () => toggle(button, button.getAttribute('aria-expanded') !== 'true'));
    }
    for (const button of root.querySelectorAll('[data-ac-todos]')) {
        button.hidden = false;
        button.addEventListener('click', () => {
            for (const item of buttons) toggle(item, button.dataset.acTodos === 'abrir');
        });
    }
    for (const link of root.querySelectorAll('[data-ac-ir-classe]')) {
        link.addEventListener('click', event => {
            const button = buttons.find(item => item.dataset.acChave === link.dataset.acIrClasse);
            if (!button) return;
            event.preventDefault();
            toggle(button, true);
            button.scrollIntoView({block: 'center', behavior: 'auto'});
            button.focus({preventScroll: true});
        });
    }
    for (const detail of root.querySelectorAll('[data-ac-insight]')) {
        detail.addEventListener('toggle', () => {
            if (detail.open) for (const other of root.querySelectorAll('[data-ac-insight]')) {
                if (other !== detail) other.open = false;
            }
        });
        const close = detail.querySelector('[data-ac-fechar-insight]');
        close.hidden = false;
        close.addEventListener('click', () => {
            detail.open = false;
            detail.querySelector('summary').focus();
        });
    }
    let highlightTimer;
    for (const link of root.querySelectorAll('[data-ac-localizar]')) {
        link.addEventListener('click', event => {
            const target = document.getElementById(link.dataset.acLocalizar) ||
                [...root.querySelectorAll('[data-ac-alvo]')].find(row => row.dataset.acAlvo === link.dataset.acLocalizar);
            if (!target) return;
            event.preventDefault();
            const group = target.closest('[data-ac-produtos]');
            const parent = buttons.find(button => button.getAttribute('aria-controls') === group?.id);
            if (parent) toggle(parent, true);
            if (target.matches('[data-ac-apresentacao]')) {
                const disclosure = [...group.querySelectorAll('[data-ac-produto-botao]')].find(button =>
                    button.getAttribute('aria-controls').trim().split(/\s+/).includes(target.id));
                if (disclosure?.getAttribute('aria-expanded') !== 'true') disclosure?.click();
            }
            clearTimeout(highlightTimer);
            for (const row of root.querySelectorAll('.ac-localizado')) row.classList.remove('ac-localizado');
            target.classList.add('ac-localizado');
            target.scrollIntoView({block: 'center', inline: 'start', behavior: 'auto'});
            target.focus({preventScroll: true});
            highlightTimer = setTimeout(() => target.classList.remove('ac-localizado'), 5000);
        });
    }
    const source = root.querySelector('a[href="#ac-origem-custos"]');
    if (source) source.addEventListener('click', () => {
        const details = document.getElementById('ac-origem-custos');
        if (details) details.open = true;
    });
})();
