// Mesmo estado aberto/aria-expanded do componente de Receber do cliente.
(() => {
    document.querySelectorAll('[data-atalhos-cabecalho]').forEach((menu) => {
        if (menu.dataset.atalhosPronto) return;
        menu.dataset.atalhosPronto = 'true';
        const toggle = menu.querySelector('button');
        const setOpen = (open) => {
            menu.classList.toggle('aberto', open);
            toggle.setAttribute('aria-expanded', String(open));
        };
        toggle.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            setOpen(!menu.classList.contains('aberto'));
        });
        menu.addEventListener('click', (event) => event.stopPropagation());
        document.addEventListener('click', () => setOpen(false));
        document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape' || !menu.classList.contains('aberto')) return;
            setOpen(false);
            toggle.focus();
        });
    });
})();
