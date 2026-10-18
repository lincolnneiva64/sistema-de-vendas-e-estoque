'use strict';
let requisicoesPendentes = 0;
let finalizando = false;
let verificando = false;
const recuperarFormularios = new Map();
async function lerResposta(response) {
    if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Resposta inesperada do servidor (HTTP ' + response.status + '). Use Verificar acesso e último OK; preserve esta tela.');
    }
    const resultado = await response.json();
    if (!response.ok) throw new Error(resultado.erro || 'Não foi possível concluir (HTTP ' + response.status + '). Verifique o último OK.');
    return resultado;
}

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
    let precisaVerificar = false;
    const valores = () => JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']);
    form.dataset.valoresSalvos = valores();
    form.dataset.confirmado = form.querySelector('.mensagem').dataset.confirmado;
    const cartao = form.closest('article');
    let jaConfirmado = form.dataset.confirmado === 'true';
    let protegido = form.querySelector('.mensagem').dataset.guardado === 'true';
    const editar = form.querySelector('[data-editar-contagem]');
    let ultimoOK = form.dataset.ultimoEm ? {
        embalagens: form.dataset.ultimoPrincipal, avulsos: form.dataset.ultimoAvulsos,
        ok_em: form.dataset.ultimoEm,
    } : null;
    const quantidade = (principal, avulsos) => principal + ' ' + form.elements.embalagens.dataset.unidade
        + (form.elements.avulsos ? ' + ' + avulsos + ' ' + form.elements.avulsos.dataset.unidade + ' avulsos' : '');
    const pendencia = () => quantidade(form.elements.embalagens.value || 'Quantidade vazia', form.elements.avulsos?.value || '0')
        + ' não confirmados; último OK ' + (ultimoOK ? 'conhecido: ' + quantidade(ultimoOK.embalagens, ultimoOK.avulsos) : 'ainda não registrado') + '.';

    const atualizarEdicao = () => {
        const ocupado = enviando || finalizando || verificando || conferenciaBloqueada;
        [form.elements.embalagens, form.elements.avulsos].filter(Boolean).forEach(campo => {
            campo.readOnly = protegido || ocupado;
        });
        form.querySelector('[value=salvar]').disabled = ocupado || protegido || precisaVerificar;
        form.querySelector('[value=ok]').disabled = ocupado || precisaVerificar || (protegido && form.dataset.confirmado === 'true');
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
        cartao.querySelector('.quantidade-confirmada').textContent = ultimoOK
            ? (precisaVerificar ? 'Último OK conhecido: ' : 'Último OK salvo: ')
                + quantidade(ultimoOK.embalagens, ultimoOK.avulsos)
            : precisaVerificar ? 'Último OK ainda não verificado no servidor.' : 'Nenhum OK salvo.';
        if (!confirmado) cartao.querySelector('.estado-produto').textContent = pendencia();
    };
    atualizarVisual(form.dataset.confirmado === 'true');
    atualizarEdicao();
    editar.addEventListener('click', () => {
        if (enviando || conferenciaBloqueada || finalizando || verificando) return;
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
        if (protegido || enviando || conferenciaBloqueada || finalizando || verificando) return;
        form.dataset.confirmado = 'false';
        atualizarVisual(false);
        form.querySelector('.mensagem').textContent = 'Alterações ainda não confirmadas. Dê OK novamente antes de enviar.';
    });
    recuperarFormularios.set(Number(form.dataset.produtoId), produto => {
        const digitado = valores();
        const iguais = form.elements.embalagens.value === produto.embalagens
            && (!form.elements.avulsos || form.elements.avulsos.value === produto.avulsos);
        precisaVerificar = false;
        // A different server version is acknowledged explicitly, never retried automatically.
        form.elements.versao.value = produto.versao;
        ultimoOK = produto.ultimo_ok;
        jaConfirmado = !!ultimoOK;
        form.dataset.valoresSalvos = JSON.stringify([produto.embalagens, produto.avulsos || '']);
        form.dataset.confirmado = String(iguais && produto.conferido);
        protegido = iguais && produto.guardado;
        atualizarVisual(iguais && produto.conferido);
        form.querySelector('.mensagem').textContent = iguais && produto.conferido
            ? 'OK verificado no servidor. ' + cartao.querySelector('.quantidade-confirmada').textContent
            : pendencia() + ' Acesso verificado. Confira os números e confirme o OK para gravar.';
        if (valores() !== digitado) throw new Error('Os valores digitados devem ser preservados.');
        atualizarEdicao();
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (enviando || conferenciaBloqueada || finalizando || verificando) return;
        const acao = event.submitter?.value;
        if (!acao) return;
        if (precisaVerificar) return;
        if (protegido && (acao === 'salvar' || form.dataset.confirmado === 'true')) return;
        const mensagem = form.querySelector('.mensagem');
        const dados = new FormData(form);
        dados.set('acao', acao);
        enviando = true;
        requisicoesPendentes++;
        atualizarEdicao();
        mensagem.textContent = pendencia() + ' Enviando…';
        try {
            const response = await fetch(form.action, {method: 'POST', body: dados, headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
            const resultado = await lerResposta(response);
            form.elements.versao.value = resultado.produto.versao;
            form.elements.embalagens.value = resultado.produto.embalagens;
            if (form.elements.avulsos) form.elements.avulsos.value = resultado.produto.avulsos;
            form.dataset.confirmado = String(resultado.produto.conferido);
            jaConfirmado = jaConfirmado || resultado.produto.conferido;
            protegido = true;
            ultimoOK = resultado.produto.ultimo_ok;
            atualizarVisual(resultado.produto.conferido);
            form.dataset.valoresSalvos = valores();
            aplicarEstadoConferencia(resultado.conferencia);
            mensagem.textContent = resultado.mensagem;
        } catch (erro) {
            precisaVerificar = true;
            form.dataset.confirmado = 'false';
            atualizarVisual(false);
            mensagem.textContent = pendencia() + ' ' + (erro instanceof TypeError
                ? 'Sem confirmação do servidor. A gravação pode ter ocorrido; use Verificar acesso e último OK antes de reenviar.' : erro.message);
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
        if (enviando || conferenciaBloqueada || finalizando || verificando) return;
        enviando = true;
        requisicoesPendentes++;
        const button = form.querySelector('button');
        const mensagem = form.querySelector('.mensagem-sinalizacao');
        button.disabled = true;
        let concluido = false;
        try {
            const response = await fetch(form.action, {method: 'POST', body: new FormData(form), headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
            const resultado = await lerResposta(response);
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
    if (conferenciaBloqueada || requisicoesPendentes || verificando) {
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
        const resultado = await lerResposta(resposta);
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

const verificarAcesso = document.querySelector('#verificar-acesso');
if (verificarAcesso) verificarAcesso.addEventListener('click', async () => {
    const mensagem = document.querySelector('#mensagem-acesso');
    if (requisicoesPendentes || finalizando || verificando) {
        mensagem.textContent = 'Aguarde as requisições em andamento.';
        return;
    }
    verificando = true;
    verificarAcesso.disabled = true;
    mensagem.textContent = 'Consultando o acesso e o último OK no servidor…';
    // Freeze edits while consulting the server, without discarding the draft.
    const campos = [...document.querySelectorAll('form fieldset')].map(campo => [campo, campo.disabled]);
    campos.forEach(([campo]) => { campo.disabled = true; });
    try {
        const resultado = await lerResposta(await fetch(verificarAcesso.dataset.url, {
            headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store',
        }));
        if (String(resultado.acesso_id) !== document.querySelector('.mobile').dataset.acessoId) {
            throw new Error('O link aberto pertence a outra atribuição. Preserve esta tela e solicite o link correto ao responsável.');
        }
        document.querySelectorAll('[name=csrfmiddlewaretoken]').forEach(campo => { campo.value = resultado.csrf; });
        verificando = false;
        for (const [id, aplicar] of recuperarFormularios) {
            const produto = resultado.produtos.find(p => p.id === id);
            if (produto) aplicar(produto);
            else {
                const form = document.querySelector('form[data-produto-id="' + id + '"]');
                campos.forEach(par => { if (par[0] === form.querySelector('fieldset')) par[1] = true; });
                form.dataset.confirmado = 'false';
                form.querySelector('.mensagem').textContent = 'Produto fora da atribuição atual ou rodada encerrada. Preserve os números e solicite orientação.';
            }
        }
        aplicarEstadoConferencia(resultado.conferencia);
        mensagem.textContent = 'Acesso e último OK consultados no servidor. Valores digitados preservados. Nenhuma alteração foi reenviada.';
    } catch (erro) {
        mensagem.textContent = erro instanceof TypeError ? 'Sem conexão para verificar. Preserve esta tela e tente novamente.' : erro.message;
    } finally {
        verificando = false;
        verificarAcesso.disabled = false;
        if (!conferenciaBloqueada) campos.forEach(([campo, disabled]) => { campo.disabled = disabled; });
    }
});
window.addEventListener('beforeunload', event => {
    if ([...document.querySelectorAll('form.contagem')].some(form =>
        form.dataset.valoresSalvos !== JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']))) {
        event.preventDefault();
        event.returnValue = '';
    }
});
