'use strict';
// Only choose which date filter to submit; balances remain server-side.
const formPeriodo = document.querySelector('.filtros-extrato form');
function atualizarPeriodo() {
    if (!formPeriodo) return;
    const modo = formPeriodo.querySelector('[name=periodo]').value;
    for (const nome of ['compra', 'intervalo', 'data_inicial', 'data_final']) {
        const campo = formPeriodo.querySelector(`[name=${nome}]`);
        campo.closest('label').hidden = !((modo === 'compra' && ['compra','intervalo'].includes(nome)) ||
            (modo === 'personalizado' && nome.startsWith('data_')));
    }
    const nav = document.querySelector('.filtros-extrato nav');
    if (nav) nav.hidden = modo !== 'compra';
    document.querySelector('.ajuda-periodo').hidden = modo !== 'compra';
}
formPeriodo?.querySelector('[name=periodo]')?.addEventListener('change', atualizarPeriodo);
formPeriodo?.querySelector('[name=compra]')?.addEventListener('change', () => {
    formPeriodo.querySelector('[name=periodo]').value = 'compra';
    atualizarPeriodo();
});
formPeriodo?.querySelector('[name=intervalo]')?.addEventListener('change', () => {
    formPeriodo.querySelector('[name=periodo]').value = 'compra';
    atualizarPeriodo();
});
for (const campo of formPeriodo?.querySelectorAll('input[type=date]') || []) {
    campo.addEventListener('change', () => {
        formPeriodo.querySelector('[name=periodo]').value = 'personalizado';
        atualizarPeriodo();
    });
}
atualizarPeriodo();
