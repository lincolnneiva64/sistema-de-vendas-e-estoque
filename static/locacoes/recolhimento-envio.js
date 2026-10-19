'use strict';
(() => {
    const pendentes = new Set();
    document.addEventListener('change', event => {
        if (!event.target.matches('.funcionario-select')) return;
        const cartao = event.target.closest('[data-tarefa-id]') || event.target.closest('[data-envio-operacional]');
        if (!cartao?.querySelector('[data-enviar-recolhimento]')) return;
        const mensagem = cartao.querySelector('[data-envio-recolhimento-feedback]');
        if (mensagem) mensagem.textContent = '';
    }, true);
    document.addEventListener('click', async event => {
        const botao = event.target.closest('[data-enviar-recolhimento]');
        if (!botao) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const url = botao.dataset.enviarRecolhimento;
        if (pendentes.has(url) || botao.getAttribute('aria-disabled') === 'true') return;
        const cartao = botao.closest('[data-tarefa-id]') || botao.closest('[data-envio-operacional]');
        const select = cartao?.querySelector('.funcionario-select');
        let mensagem = cartao?.querySelector('[data-envio-recolhimento-feedback]');
        if (!select?.value) return;
        if (!mensagem) {
            mensagem = document.createElement('p');
            mensagem.dataset.envioRecolhimentoFeedback = '';
            mensagem.setAttribute('role', 'status');
            mensagem.setAttribute('aria-live', 'polite');
            cartao.append(mensagem);
        }
        const texto = botao.textContent;
        const desabilitado = select.disabled;
        const dados = new URLSearchParams({funcionario: select.value});
        pendentes.add(url);
        botao.setAttribute('aria-busy', 'true');
        botao.setAttribute('aria-disabled', 'true');
        botao.textContent = 'Preparando link...';
        select.disabled = true;
        mensagem.textContent = 'Preparando o link individual. Aguarde...';
        // Reserve the tab within the actual click gesture to avoid mobile popup blocking.
        let janela;
        try {
            janela = window.open('about:blank', '_blank');
            if (janela) janela.opener = null;
            const csrf = decodeURIComponent(document.cookie.split('; ').find(c => c.startsWith('csrftoken='))?.slice(10) || '')
                || document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
            const response = await fetch(url, {method:'POST', credentials:'same-origin', cache:'no-store',
                headers:{Accept:'application/json', 'X-CSRFToken':csrf}, body:dados});
            const resultado = response.headers.get('content-type')?.includes('application/json') ? await response.json() : null;
            if (!response.ok || !resultado?.whatsapp_url) throw new Error(resultado?.erro
                || 'Não foi possível validar o acesso. Verifique sua sessão e tente novamente.');
            const destino = new URL(resultado.whatsapp_url);
            if (destino.protocol !== 'https:' || destino.hostname !== 'web.whatsapp.com') throw new Error('Destino de envio inválido.');
            if (janela && !janela.closed) {
                janela.location.replace(destino.href);
                mensagem.textContent = 'WhatsApp aberto com a mensagem e o link. Confira e envie ao funcionário.';
            } else {
                mensagem.textContent = 'O navegador bloqueou a nova janela. ';
                const abrir = document.createElement('a');
                abrir.href = destino.href; abrir.target = '_blank'; abrir.rel = 'noopener noreferrer';
                abrir.textContent = 'Abrir WhatsApp com o link pronto';
                mensagem.append(abrir);
            }
        } catch (erro) {
            if (janela && !janela.closed) janela.close();
            mensagem.textContent = erro instanceof TypeError
                ? 'Sem resposta do servidor. Tente novamente; o sistema reutiliza a autorização válida sem duplicar links.' : erro.message;
        } finally {
            pendentes.delete(url);
            botao.removeAttribute('aria-busy');
            botao.removeAttribute('aria-disabled');
            botao.textContent = texto;
            select.disabled = desabilitado;
        }
    }, true);
})();
