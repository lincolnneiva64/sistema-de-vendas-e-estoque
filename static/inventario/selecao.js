'use strict';
document.getElementById('busca-produtos')?.addEventListener('input', event => {
    const termo = event.target.value.toLocaleLowerCase('pt-BR');
    document.querySelectorAll('.produto-selecao').forEach(label => { label.hidden = !label.textContent.toLocaleLowerCase('pt-BR').includes(termo); });
});
