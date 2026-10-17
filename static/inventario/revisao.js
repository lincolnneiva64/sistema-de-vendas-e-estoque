'use strict';
document.querySelectorAll('a[data-detalhe]').forEach(link => {
    link.addEventListener('click', () => {
        const details = document.getElementById(link.dataset.detalhe);
        if (details) details.open = true;
    });
});
window.inventarioRevisaoPronta = true;
document.querySelector('#selecionar-todos')?.addEventListener('change', event => {
    document.querySelectorAll('#distribuicao [name=rodadas]:not(:disabled)').forEach(campo => { campo.checked = event.target.checked; });
});
document.querySelector('#copiar-link')?.addEventListener('click', async () => {
    const campo = document.querySelector('#link-funcionario');
    const feedback = document.querySelector('#feedback-link');
    try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(campo.value);
        else {
            campo.select();
            if (!document.execCommand('copy')) throw new Error('copy');
        }
        feedback.textContent = 'Link copiado.';
    } catch (_) {
        campo.select();
        feedback.textContent = 'Selecione e copie o link exibido acima.';
    }
});
