'use strict';
let requisicoesPendentes = 0;
let finalizando = false;
let verificando = false;
const recuperarFormularios = new Map();
function ocupado(botao, texto) {
    const anterior = botao.textContent;
    botao.textContent = texto;
    botao.setAttribute('aria-busy', 'true');
    return () => { botao.textContent = anterior; botao.removeAttribute('aria-busy'); };
}

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
const camposContagem = form => [form.elements.embalagens, form.elements.avulsos].filter(Boolean);
const camposFaltantes = form => camposContagem(form).filter(campo => campo.value.trim() === '');
const produtosPendentes = () => [...document.querySelectorAll('form.contagem')].filter(form =>
    camposContagem(form).some(campo => !campo.value.trim() || !campo.validity.valid)
    || form.dataset.confirmado !== 'true'
    || form.dataset.valoresSalvos !== JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']));
let navegandoPendencias = false;
const proximaPendencia = document.querySelector('#proxima-pendencia');
const listaPendentes = document.querySelector('#produtos-pendentes');
const listaConferidos = document.querySelector('#lista-produtos-conferidos');
document.querySelectorAll('form.contagem').forEach(form => {
    form.dataset.conferidoServidor = form.querySelector('.mensagem').dataset.confirmado;
});
function organizarProduto(form) {
    const destino = form.dataset.conferidoServidor === 'true' ? listaConferidos : listaPendentes;
    const cartao = form.closest('article');
    if (!destino || cartao.parentElement === destino) return;
    const seguinte = [...destino.querySelectorAll(':scope > article')].find(outro =>
        Number(outro.dataset.ordem) > Number(cartao.dataset.ordem));
    destino.insertBefore(cartao, seguinte || destino.querySelector('#nenhum-produto-pendente'));
}
function atualizarPendencias() {
    const formularios = [...document.querySelectorAll('form.contagem')];
    const conferidos = formularios.filter(form => form.dataset.conferidoServidor === 'true').length;
    document.querySelectorAll('[data-total-conferidos]').forEach(total => { total.textContent = conferidos; });
    document.querySelectorAll('[data-total-pendentes]').forEach(total => { total.textContent = formularios.length - conferidos; });
    const vazio = document.querySelector('#nenhum-produto-pendente');
    if (vazio) vazio.hidden = !formularios.length || conferidos !== formularios.length;
    if (!proximaPendencia) return;
    const pendentes = produtosPendentes();
    proximaPendencia.hidden = !navegandoPendencias || !pendentes.length || conferenciaBloqueada;
    proximaPendencia.textContent = 'Ir para o próximo pendente (' + pendentes.length + ')';
    document.querySelectorAll('.pendencia-destacada').forEach(cartao => {
        if (!pendentes.includes(cartao.querySelector('form.contagem'))) cartao.classList.remove('pendencia-destacada');
    });
}
function irParaPendente() {
    const form = produtosPendentes()[0];
    if (!form) return;
    document.querySelectorAll('.pendencia-destacada').forEach(cartao => cartao.classList.remove('pendencia-destacada'));
    const cartao = form.closest('article');
    cartao.classList.add('pendencia-destacada');
    const grupo = cartao.closest('#produtos-conferidos');
    if (grupo) grupo.open = true;
    const campo = camposFaltantes(form)[0] || camposContagem(form).find(campo => !campo.validity.valid);
    if (campo?.readOnly && !conferenciaBloqueada) form.querySelector('[data-editar-contagem]').click();
    const alvo = campo && !campo.readOnly ? campo : form.querySelector('[value=ok]:not(:disabled)') || form.querySelector('[data-editar-contagem]');
    alvo.focus({preventScroll: true});
    alvo.scrollIntoView({block: 'center', behavior: 'smooth'});
}
proximaPendencia?.addEventListener('click', irParaPendente);
const avisoPendencias = document.querySelector('#aviso-pendencias');
document.querySelector('#ir-primeiro-pendente')?.addEventListener('click', () => {
    avisoPendencias.close();
    irParaPendente();
});
document.querySelector('#fechar-pendencias')?.addEventListener('click', () => avisoPendencias.close());
function mostrarPendencias() {
    const pendentes = produtosPendentes();
    document.querySelector('#quantidade-pendentes').textContent = pendentes.length + ' produto(s) pendente(s). Confirme todos os campos e o OK.';
    const lista = document.querySelector('#lista-pendentes');
    lista.replaceChildren(...pendentes.map(form => {
        const item = document.createElement('li');
        item.textContent = form.closest('article').querySelector('h2').textContent;
        return item;
    }));
    navegandoPendencias = true;
    atualizarPendencias();
    avisoPendencias.showModal();
}

document.querySelectorAll('form.contagem').forEach(form => {
    form.noValidate = true;
    let enviando = false;
    let precisaVerificar = false;
    const valores = () => JSON.stringify([form.elements.embalagens.value, form.elements.avulsos?.value || '']);
    form.dataset.valoresSalvos = valores();
    form.dataset.confirmado = form.querySelector('.mensagem').dataset.confirmado;
    const cartao = form.closest('article');
    const quadro = form.querySelector('[data-conversao-url]');
    let consulta;
    let sequenciaConversao = 0;
    function totalServidor(produto) {
        if (!quadro) return;
        sequenciaConversao++;
        consulta?.abort();
        quadro.querySelector('output').textContent = produto.total_convertido ? produto.total_convertido + ' PCT' : '—';
        quadro.querySelector('[data-erro-conversao]').textContent = '';
        quadro.removeAttribute('aria-busy');
    }
    async function atualizarTotal() {
        if (!quadro) return;
        consulta?.abort();
        const atual = ++sequenciaConversao;
        consulta = new AbortController();
        const principal = form.elements.embalagens.value;
        const avulsos = form.elements.avulsos.value;
        const output = quadro.querySelector('output');
        const erro = quadro.querySelector('[data-erro-conversao]');
        erro.textContent = '';
        if (!principal || !avulsos) { output.textContent = '—'; quadro.removeAttribute('aria-busy'); return; }
        output.textContent = 'Calculando...';
        quadro.setAttribute('aria-busy', 'true');
        try {
            const url = new URL(quadro.dataset.conversaoUrl, location.href);
            url.search = new URLSearchParams({embalagens: principal, avulsos});
            const resultado = await lerResposta(await fetch(url, {credentials:'same-origin', cache:'no-store', headers:{Accept:'application/json'}, signal:consulta.signal}));
            if (atual === sequenciaConversao) output.textContent = resultado.total + ' PCT';
        } catch (falha) {
            if (atual !== sequenciaConversao || falha.name === 'AbortError') return;
            output.textContent = '—';
            erro.textContent = falha instanceof TypeError ? 'Sem conexão para calcular. Confira o total após guardar a contagem.' : falha.message;
        } finally {
            if (atual === sequenciaConversao) quadro.removeAttribute('aria-busy');
        }
    }
    form.addEventListener('input', atualizarTotal);

    let jaConfirmado = form.dataset.confirmado === 'true';
    let protegido = form.querySelector('.mensagem').dataset.guardado === 'true';
    const editar = form.querySelector('[data-editar-contagem]');
    let ultimoOK = form.dataset.ultimoEm ? {
        embalagens: form.dataset.ultimoPrincipal, avulsos: form.dataset.ultimoAvulsos,
        ok_em: form.dataset.ultimoEm,
    } : null;
    const quantidade = (principal, avulsos) => principal + ' ' + form.elements.embalagens.dataset.unidade
        + (form.elements.avulsos ? ' + ' + avulsos + ' ' + form.elements.avulsos.dataset.unidade + ' avulsos' : '');
    const pendencia = () => quantidade(form.elements.embalagens.value || 'Quantidade vazia', form.elements.avulsos?.value || (form.elements.avulsos ? 'Quantidade vazia' : '0'))
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
        const horarioOK = cartao.querySelector('[data-horario-ultimo-ok]');
        if (horarioOK) {
            const iso = ultimoOK?.ok_em;
            const data = iso && /(Z|[+-]\d{2}:\d{2})$/.test(iso) ? new Date(iso) : null;
            horarioOK.textContent = data && !Number.isNaN(data.getTime())
                ? new Intl.DateTimeFormat('pt-BR', {timeZone:'America/Belem', day:'2-digit', month:'2-digit', year:'numeric',
                    hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}).format(data).replace(',', ' às')
                : 'Horário não registrado';
        }
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
        organizarProduto(form);
        atualizarPendencias();
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
        camposContagem(form).forEach(campo => campo.removeAttribute('aria-invalid'));
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
        form.dataset.conferidoServidor = String(produto.conferido);
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
        if (acao === 'ok') {
            const faltantes = camposFaltantes(form);
            camposContagem(form).forEach(campo => campo.setAttribute('aria-invalid', String(!campo.value.trim() || !campo.validity.valid)));
            if (faltantes.length) {
                if (faltantes[0].readOnly) editar.click();
                mensagem.textContent = 'Falta informar a quantidade de: ' + faltantes.map(campo =>
                    (campo.name === 'avulsos' ? 'avulsos em ' : '') + campo.dataset.unidade).join(', ')
                    + '. Se não houver quantidade, digite 0 para confirmar.';
                faltantes[0].focus();
                faltantes[0].scrollIntoView({block: 'center', behavior: 'smooth'});
                return;
            }
            if (!form.reportValidity()) return;
        }
        const dados = new FormData(form);
        dados.set('acao', acao);
        enviando = true;
        const restaurar = ocupado(event.submitter, acao === 'salvar' ? 'Salvando...' : 'Confirmando...');
        requisicoesPendentes++;
        atualizarEdicao();
        mensagem.textContent = pendencia() + ' Enviando…';
        let proximoFoco = null;
        try {
            const response = await fetch(form.action, {method: 'POST', body: dados, headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
            const resultado = await lerResposta(response);
            totalServidor(resultado.produto);
            form.elements.versao.value = resultado.produto.versao;
            form.elements.embalagens.value = resultado.produto.embalagens;
            if (form.elements.avulsos) form.elements.avulsos.value = resultado.produto.avulsos;
            form.dataset.confirmado = String(resultado.produto.conferido);
            jaConfirmado = jaConfirmado || resultado.produto.conferido;
            protegido = true;
            ultimoOK = resultado.produto.ultimo_ok;
            if (resultado.produto.conferido && cartao.parentElement === listaPendentes
                    && (cartao.contains(document.activeElement) || document.activeElement === document.body)) {
                const seguinte = cartao.nextElementSibling?.querySelector('form.contagem')
                    || [...listaPendentes.querySelectorAll('form.contagem')].find(outro => outro !== form);
                proximoFoco = {form: seguinte, topo: Math.max(16,
                    Math.min(form.elements.embalagens.getBoundingClientRect().top, innerHeight / 3))};
            }
            form.dataset.conferidoServidor = String(resultado.produto.conferido);
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
            restaurar();
            atualizarVisual(form.dataset.confirmado === 'true');
            atualizarEdicao();
            if (proximoFoco && (cartao.contains(document.activeElement) || document.activeElement === document.body)) {
                const proximo = proximoFoco.form;
                const alvo = proximo && (camposContagem(proximo).find(campo => !campo.readOnly && !campo.matches(':disabled'))
                    || proximo.querySelector('[value=ok]:not(:disabled), [data-editar-contagem]:not(:disabled)'))
                    || document.querySelector('#nenhum-produto-pendente');
                if (alvo) {
                    alvo.focus({preventScroll: true});
                    window.scrollBy(0, alvo.getBoundingClientRect().top - proximoFoco.topo);
                }
            }
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
        const restaurar = ocupado(button, 'Enviando...');
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
            restaurar();
            if (!concluido && !conferenciaBloqueada) button.disabled = false;
        }
    });
});

const finalizarConferencia = document.querySelector('#finalizar-conferencia');
if (finalizarConferencia) finalizarConferencia.addEventListener('submit', async event => {
    event.preventDefault();
    const mensagem = finalizarConferencia.querySelector('.mensagem-finalizacao');
    if (conferenciaBloqueada || finalizando || requisicoesPendentes || verificando) {
        mensagem.textContent = 'Aguarde as gravações em andamento antes de finalizar.';
        return;
    }
    if (produtosPendentes().length) {
        mensagem.textContent = 'Conferência incompleta. Confira os produtos pendentes.';
        mostrarPendencias();
        return;
    }
    const dados = new FormData(finalizarConferencia);
    requisicoesPendentes++;
    finalizando = true;
    const campos = [...document.querySelectorAll('form.contagem fieldset, form.sinalizacao fieldset')].map(campo => [campo, campo.disabled]);
    campos.forEach(([campo]) => { campo.disabled = true; });
    finalizarConferencia.querySelector('button').disabled = true;
    const restaurar = ocupado(finalizarConferencia.querySelector('button'), 'Confirmando envio...');
    mensagem.textContent = 'Confirmando envio...';
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
        restaurar();
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
    const restaurar = ocupado(verificarAcesso, 'Verificando...');
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
        restaurar();
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
