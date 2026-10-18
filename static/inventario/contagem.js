'use strict';
let requisicoesPendentes = 0;
let finalizando = false;
let conferenciaBloqueada = !!document.querySelector('form.contagem fieldset:disabled');
function aplicarEstadoConferencia(estado) {
    const finalizar = document.querySelector('#finalizar-conferencia');
    if (finalizar) finalizar.elements.versao.value = estado.versao;
    if (!estado.pode_escrever) {
        conferenciaBloqueada = true;
        document.querySelectorAll('form.contagem fieldset, form.sinalizacao fieldset').forEach(campo => { campo.disabled = true; });
        document.querySelectorAll('[data-editar-contagem]').forEach(botao => { botao.hidden = true; });
        document.querySelectorAll('form.contagem .mensagem').forEach(mensagem => {
            mensagem.textContent = 'Contagem enviada. Correções exigem recontagem autorizada.';
        });
        if (finalizar) finalizar.querySelector('button').disabled = true;
        document.querySelector('#estado-conferencia').textContent = estado.finalizada
            ? 'Conferência finalizada e enviada. Alterações somente após reabertura autorizada.'
            : estado.enviada ? 'Sua atribuição foi enviada. A conferência geral aguarda as demais atribuições.'
            : 'Este acesso não permite alterações. Solicite orientação ao responsável.';
    }
}
document.querySelectorAll('form.contagem').forEach(form => {
    let enviando = false;
    const valores = () => JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']);
    form.dataset.valoresSalvos = valores();
    form.dataset.confirmado = form.querySelector('.mensagem').dataset.confirmado;
    const cartao = form.closest('article');
    let jaConfirmado = form.dataset.confirmado === 'true';
    let protegido = form.querySelector('.mensagem').dataset.guardado === 'true';
    const editar = form.querySelector('[data-editar-contagem]');
    const atualizarEdicao = () => {
        const ocupado = enviando || finalizando || conferenciaBloqueada;
        [form.elements.embalagens, form.elements.avulsos].filter(Boolean).forEach(campo => {
            campo.readOnly = protegido || ocupado;
        });
        form.querySelector('[value=salvar]').disabled = ocupado || protegido;
        form.querySelector('[value=ok]').disabled = ocupado || (protegido && form.dataset.confirmado === 'true');
        editar.hidden = !protegido || conferenciaBloqueada;
        editar.disabled = ocupado;
        editar.textContent = form.dataset.confirmado === 'true' ? 'Recontar produto' : 'Continuar contagem';
    };
    const atualizarVisual = confirmado => {
        cartao.classList.toggle('produto-conferido', confirmado);
        cartao.classList.toggle('produto-pendente', !confirmado && jaConfirmado);
        cartao.querySelector('.estado-produto').textContent = confirmado ? 'Conferido'
            : jaConfirmado ? 'Alteração pendente de confirmação' : '';
        form.querySelector('[value=ok]').textContent = jaConfirmado ? 'Atualizar OK' : 'OK — Produto conferido';
        if (confirmado) {
            const principal = form.elements.embalagens;
            const avulsos = form.elements.avulsos;
            cartao.querySelector('.quantidade-confirmada').textContent = 'Quantidade confirmada: '
                + principal.value + ' ' + principal.dataset.unidade
                + (avulsos ? ' + ' + avulsos.value + ' ' + avulsos.dataset.unidade + ' avulsos' : '');
        }
    };
    atualizarVisual(form.dataset.confirmado === 'true');
    atualizarEdicao();
    editar.addEventListener('click', () => {
        if (enviando || conferenciaBloqueada || finalizando) return;
        protegido = false;
        form.dataset.confirmado = 'false';
        atualizarVisual(false);
        atualizarEdicao();
        form.querySelector('.mensagem').textContent = jaConfirmado
            ? 'Corrija o total contado e confirme o OK novamente antes de enviar.'
            : 'Continue a contagem. Guarde novamente ou confirme o OK quando terminar.';
        form.elements.embalagens.focus();
    });
    form.addEventListener('input', () => {
        if (protegido || enviando || conferenciaBloqueada || finalizando) return;
        form.dataset.confirmado = 'false';
        atualizarVisual(false);
        form.querySelector('.mensagem').textContent = 'Alterações ainda não confirmadas. Dê OK novamente antes de enviar.';
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (enviando || conferenciaBloqueada || finalizando) return;
        const acao = event.submitter?.value;
        if (!acao) return;
        if (protegido && (acao === 'salvar' || form.dataset.confirmado === 'true')) return;
        const mensagem = form.querySelector('.mensagem');
        const dados = new FormData(form);
        dados.set('acao', acao);
        enviando = true;
        requisicoesPendentes++;
        atualizarEdicao();
        mensagem.textContent = 'Enviando…';
        try {
            const response = await fetch(form.action, {method: 'POST', body: dados, headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
            if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Acesso indisponível. Atualize a página ou solicite um novo link.');
            const resultado = await response.json();
            if (!response.ok) throw new Error(resultado.erro || 'Não foi possível salvar. Atualize a página.');
            form.elements.versao.value = resultado.produto.versao;
            form.elements.embalagens.value = resultado.produto.embalagens;
            if (form.elements.avulsos) form.elements.avulsos.value = resultado.produto.avulsos;
            form.dataset.confirmado = String(resultado.produto.conferido);
            jaConfirmado = jaConfirmado || resultado.produto.conferido;
            protegido = true;
            atualizarVisual(resultado.produto.conferido);
            form.dataset.valoresSalvos = valores();
            aplicarEstadoConferencia(resultado.conferencia);
            mensagem.textContent = resultado.mensagem;
        } catch (erro) {
            mensagem.textContent = erro instanceof TypeError ? 'Sem confirmação do servidor. Preserve esta tela ou atualize para verificar o que ficou salvo.' : erro.message;
        } finally {
            enviando = false;
            requisicoesPendentes--;
            atualizarEdicao();
        }
    });
});
window.inventarioContagemPronta = true;

document.querySelectorAll('form.sinalizacao').forEach(form => {
    let enviando = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (enviando || conferenciaBloqueada || finalizando) return;
        enviando = true;
        requisicoesPendentes++;
        const button = form.querySelector('button');
        const mensagem = form.querySelector('.mensagem-sinalizacao');
        button.disabled = true;
        let concluido = false;
        try {
            const response = await fetch(form.action, {method: 'POST', body: new FormData(form), headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
            if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Acesso indisponível. Solicite orientação ao responsável.');
            const resultado = await response.json();
            if (!response.ok) throw new Error(resultado.erro || 'Não foi possível enviar.');
            mensagem.textContent = resultado.mensagem;
            form.elements.descricao.disabled = true;
            concluido = true;
        } catch (erro) {
            mensagem.textContent = erro instanceof TypeError ? 'Sem confirmação do servidor. Tente enviar novamente sem alterar a descrição.' : erro.message;
        } finally {
            enviando = false;
            requisicoesPendentes--;
            if (!concluido && !conferenciaBloqueada) button.disabled = false;
        }
    });
});

const finalizarConferencia = document.querySelector('#finalizar-conferencia');
if (finalizarConferencia) finalizarConferencia.addEventListener('submit', async event => {
    event.preventDefault();
    const mensagem = finalizarConferencia.querySelector('.mensagem-finalizacao');
    if (conferenciaBloqueada || requisicoesPendentes) {
        mensagem.textContent = 'Aguarde as gravações em andamento antes de finalizar.';
        return;
    }
    const pendente = [...document.querySelectorAll('form.contagem')].some(form =>
        form.dataset.confirmado !== 'true' || form.dataset.valoresSalvos !== JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']));
    if (pendente) {
        mensagem.textContent = 'Confirme o OK de todos os produtos e das alterações antes de enviar.';
        return;
    }
    const dados = new FormData(finalizarConferencia);
    requisicoesPendentes++;
    finalizando = true;
    const campos = [...document.querySelectorAll('form.contagem fieldset, form.sinalizacao fieldset')].map(campo => [campo, campo.disabled]);
    campos.forEach(([campo]) => { campo.disabled = true; });
    finalizarConferencia.querySelector('button').disabled = true;
    try {
        const resposta = await fetch(finalizarConferencia.action, {method: 'POST', body: dados,
            headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
        if (!resposta.headers.get('content-type')?.includes('application/json')) throw new Error('Acesso indisponível. Atualize a página.');
        const resultado = await resposta.json();
        if (!resposta.ok) throw new Error(resultado.erro || 'Não foi possível finalizar.');
        aplicarEstadoConferencia(resultado.conferencia);
        mensagem.textContent = resultado.mensagem;
    } catch (erro) {
        mensagem.textContent = erro instanceof TypeError ? 'Sem confirmação do servidor. Atualize a página para verificar o envio.' : erro.message;
    } finally {
        requisicoesPendentes--;
        finalizando = false;
        if (!conferenciaBloqueada) {
            campos.forEach(([campo, desabilitado]) => { campo.disabled = desabilitado; });
            finalizarConferencia.querySelector('button').disabled = false;
        }
    }
});
