'use strict';
document.querySelectorAll('a[data-detalhe]').forEach(link => {
    link.addEventListener('click', () => {
        const details = document.getElementById(link.dataset.detalhe);
        if (details) details.open = true;
    });
});
window.inventarioRevisaoPronta = true;
const formularioAtribuicao = document.querySelector('#form-atribuicao');
const selecionarTodos = document.querySelector('#selecionar-todos');
const confirmarAtribuicao = document.querySelector('#confirmar-atribuicao');
let processando = false;

function atualizarSelecao() {
    if (!formularioAtribuicao) return;
    const campos = [...formularioAtribuicao.querySelectorAll('[name=rodadas]:not(:disabled)')];
    const selecionados = campos.filter(campo => campo.checked).length;
    selecionarTodos.disabled = processando || campos.length === 0;
    selecionarTodos.checked = campos.length > 0 && selecionados === campos.length;
    selecionarTodos.indeterminate = selecionados > 0 && selecionados < campos.length;
    confirmarAtribuicao.disabled = processando || selecionados === 0;
}

selecionarTodos?.addEventListener('change', event => {
    formularioAtribuicao.querySelectorAll('[name=rodadas]:not(:disabled)').forEach(campo => { campo.checked = event.target.checked; });
    atualizarSelecao();
});
formularioAtribuicao?.addEventListener('change', atualizarSelecao);

const formularios = [...document.querySelectorAll('form[data-processamento]')];
const botoes = formularios.flatMap(form => [...form.querySelectorAll('button')]);
const estadoBotoes = botoes.map(botao => ({botao, texto: botao.textContent, desabilitado: botao.disabled}));
formularios.forEach(form => form.addEventListener('submit', event => {
    if (processando) {
        event.preventDefault();
        return;
    }
    if (form === formularioAtribuicao && !form.querySelector('[name=rodadas]:not(:disabled):checked')) {
        event.preventDefault();
        form.querySelector('[data-feedback]').textContent = 'Selecione pelo menos um produto disponível para atribuir.';
        atualizarSelecao();
        return;
    }
    processando = true;
    form.setAttribute('aria-busy', 'true');
    form.querySelector('[data-feedback]').textContent = 'Processando. Aguarde a confirmação nesta página.';
    // Actions are hidden inputs, so disabling buttons cannot remove the POST action.
    botoes.forEach(botao => { botao.disabled = true; });
    form.querySelectorAll('button').forEach(botao => {
        botao.textContent = botao.dataset.processando || 'Processando…';
    });
}));

window.addEventListener('pageshow', () => {
    processando = false;
    estadoBotoes.forEach(({botao, texto, desabilitado}) => {
        botao.textContent = texto;
        botao.disabled = desabilitado;
    });
    formularios.forEach(form => {
        form.removeAttribute('aria-busy');
        form.querySelector('[data-feedback]').textContent = '';
    });
    atualizarSelecao();
});
atualizarSelecao();

const resultado = document.querySelector('#erro-acao') || document.querySelector('#compartilhar-acesso');
if (resultado) {
    resultado.focus({preventScroll: true});
    resultado.scrollIntoView({block: 'start'});
}
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
