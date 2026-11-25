'use strict';
document.getElementById('busca-produtos')?.addEventListener('input', event => {
    const termo = event.target.value.toLocaleLowerCase('pt-BR');
    document.querySelectorAll('.produto-selecao').forEach(label => { label.hidden = !label.textContent.toLocaleLowerCase('pt-BR').includes(termo); });
});

document.querySelectorAll('[data-incluir-produto]').forEach(botao => {
    botao.addEventListener('click', () => {
        const linha = botao.closest('.produto-selecao');
        const campo = linha.querySelector('input[type=checkbox]');
        document.querySelector('#produtos-manuais').append(linha);
        campo.checked = true;
        botao.remove();
        campo.focus();
    });
});
