// Apresentacao apenas: os controles, dados e eventos dos rascunhos permanecem originais.
(() => {
    const mobile = window.matchMedia('(max-width: 980px)');
    const layout = document.getElementById('layout-vendas');
    if (!layout) return;
    const controls = new Set([
        'sales-draft-discard', 'sales-official-sale-link', 'sales-operation-diagnostic',
        'sales-device-operations', 'sales-online-recover', 'sales-new-offline-sale',
        'sales-pending-count', 'sales-draft-date-note', 'sales-draft-recovery',
        'local-sale-query', 'local-sale-replay', 'local-new-assembly',
        'local-sales-recovery', 'local-sales-recover-button',
    ]);
    const routineMessages = new Set([
        '', 'Salvando rascunho local...', 'Salvando rascunho local…',
        'Rascunho local salvo neste navegador.',
        'Rascunho local salvo. Dados de referência a revalidar.',
        'Rascunho local descartado.', 'Venda confirmada. Recibo preservado.',
        'Venda online confirmada. Rascunho local removido.',
        'Venda online confirmada. Rascunho residual não restaurado.',
    ]);
    function routine(text) {
        // Mensagens novas ou desconhecidas ficam visiveis, mesmo se role="status".
        return routineMessages.has(text)
            || /^Rascunho restaurado\. Data (?:da venda )?preservada: \d{4}-\d{2}-\d{2}\.$/.test(text)
            || /^Venda #\d+ sincronizada\. Referência [a-zA-Z0-9-]+\.$/.test(text);
    }
    let currentBox, observer;
    function attach() {
        if (!mobile.matches) return;
        const box = document.getElementById('sales-draft-status');
        if (!box || box === currentBox) return;
        observer?.disconnect();
        currentBox = box;
        let open = false;
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.id = 'sales-draft-mobile-toggle';
        box.prepend(toggle);
        box.setAttribute('data-draft-mobile', '');
        function update() {
            if (!mobile.matches) return;
            const visible = (element) => element && !element.hidden;
            const pending = box.querySelector('#sales-pending-count');
            const intervention = (visible(pending) && pending.textContent.trim() !== '')
                || visible(box.querySelector('#sales-online-recover'))
                || visible(box.querySelector('#local-sale-query'))
                || visible(box.querySelector('#local-sale-replay'))
                || !!box.querySelector('.offline-operation-details');
            for (const element of box.children) {
                if (element === toggle) continue;
                const status = element.matches('[role="status"], [role="alert"]');
                const known = status || controls.has(element.id)
                    || (element.matches('a:not([id])') && element.textContent === 'Ver venda');
                element.toggleAttribute('data-draft-mobile-item', known);
                const criticalMessage = status && (element.getAttribute('role') === 'alert'
                    || !routine(element.textContent.trim()));
                const criticalAction = intervention && (status || [
                    'sales-pending-count', 'sales-operation-diagnostic', 'sales-device-operations',
                    'sales-online-recover', 'sales-new-offline-sale', 'sales-draft-recovery',
                    'local-sale-query', 'local-sale-replay', 'local-new-assembly',
                    'local-sales-recovery', 'local-sales-recover-button',
                ].includes(element.id));
                element.toggleAttribute('data-draft-mobile-alert', criticalMessage || criticalAction
                    || !!element.querySelector('[role="alert"]'));
            }
            const savedDrafts = visible(box.querySelector('#sales-draft-recovery'))
                || box.querySelector('#local-sales-recovery')?.options.length > 1;
            const label = 'Rascunhos e recuperação' + (savedDrafts ? ' (há salvos)' : '');
            if (toggle.textContent !== label) toggle.textContent = label;
            toggle.setAttribute('aria-expanded', String(open));
            box.dataset.draftMobileOpen = String(open);
        }
        toggle.addEventListener('click', (event) => {
            // Abrir o grupo nao deve chegar aos listeners de autosave do formulario.
            event.stopPropagation();
            open = !open;
            update();
        });
        toggle.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') event.stopPropagation();
        });
        observer = new MutationObserver(update);
        observer.observe(box, {
            childList: true, subtree: true, characterData: true,
            attributes: true, attributeFilter: ['hidden', 'role'],
        });
        update();
    }
    // Os dois adaptadores inserem o bloco de forma assincrona como filho do layout.
    new MutationObserver(attach).observe(layout, {childList: true});
    mobile.addEventListener('change', () => {
        if (mobile.matches && currentBox) {
            observer?.disconnect();
            currentBox = null;
            document.getElementById('sales-draft-mobile-toggle')?.remove();
        }
        attach();
    });
    attach();
})();
