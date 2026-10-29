'use strict';
(() => {
    const root = document.querySelector('[data-ajustes-diretos]');
    if (!root) return;
    const abrir = root.querySelector('[data-revisar-estoque]');
    const estado = root.querySelector('[data-estado-ajustes]');
    const previa = document.querySelector('#modal-previa-estoque');
    const sucesso = document.querySelector('#modal-sucesso-estoque');
    const conteudo = previa.querySelector('[data-conteudo-previa]');
    const endpoint = root.dataset.url;
    let enviando = false;
    let abrindo = false;
    const headers = {Accept:'application/json', 'X-Inventario-Ajuste':'1'};
    const request = async (url, data) => {
        const response = await fetch(url, {method:data ? 'POST' : 'GET', body:data,
            credentials:'same-origin', cache:'no-store', headers});
        const payload = response.headers.get('content-type')?.includes('application/json') ? await response.json() : null;
        if (!response.ok || !payload) {
            const erro = new Error(payload?.erro || 'O servidor não confirmou a operação.');
            erro.rejeitado = payload?.estado === 'rejeitado' && response.status < 500;
            throw erro;
        }
        return payload;
    };
    const atualizarEstado = resumo => {
        abrir.disabled = false;
        estado.textContent = resumo.erro || (resumo.aptos > 0
            ? resumo.aptos + ' produto(s) apto(s) com diferença. '
            : resumo.sem_divergencias ? 'Conferência sem divergências. Nenhum ajuste necessário.'
            : resumo.concluidos ? 'Todos os ajustes de estoque foram concluídos.'
            : resumo.pendentes ? resumo.pendentes + ' ajuste(s) pendente(s). Nenhum produto apto para atualizar agora. '
            : 'Revisão pendente. Consulte a situação dos produtos. ');
        if (resumo.bloqueados.length) estado.textContent += ' Bloqueados (' + resumo.bloqueados.length + '): ' + resumo.bloqueados.map(p => p.nome).join(', ') + '.';
    };
    const mostrarSucesso = payload => {
        if (!payload.ok || payload.estado !== 'aplicado' || !Number.isInteger(payload.quantidade) || payload.quantidade < 1) {
            throw new Error('Resultado sem comprovação integral da aplicação. Consulte o histórico.');
        }
        atualizarEstado(payload.resumo);
        sucesso.querySelector('[data-conferencia-sucesso]').textContent = 'Conferência nº ' + payload.conferencia_id + ' — ' + payload.conferencia_nome;
        sucesso.querySelector('[data-quantidade-sucesso]').textContent = payload.quantidade + ' produto(s) tiveram seus saldos atualizados nesta operação.';
        sucesso.querySelector('[data-pendencias-sucesso]').textContent = payload.resumo.pendentes + ' ajuste(s) pendente(s); ' + payload.resumo.bloqueados.length + ' produto(s) bloqueado(s).';
        if (previa.open) previa.close();
        sucesso.showModal();
    };
    const apresentarRecibo = payload => {
        // A one-use lookup key, never a client-generated success certificate.
        history.replaceState({inventarioRecibo:{path:root.dataset.detalhe, previa:payload.previa}},
            '', root.dataset.detalhe + '#titulo-resumo');
        location.reload();
    };
    sucesso.querySelector('a.secondary').addEventListener('click', () => sucesso.close());
    previa.addEventListener('cancel', event => { if (enviando) event.preventDefault(); });
    previa.addEventListener('click', event => {
        if (event.target.closest('[data-cancelar-ajustes]') && !enviando) previa.close();
    });
    abrir.addEventListener('click', async () => {
        if (abrindo || enviando) return;
        abrindo = true;
        conteudo.textContent = 'Preparando a prévia...';
        previa.showModal();
        const data = new FormData();
        data.set('acao','prever');
        data.set('csrfmiddlewaretoken',root.querySelector('[name=csrfmiddlewaretoken]').value);
        try {
            const payload = await request(endpoint, data);
            conteudo.innerHTML = payload.html; // Escaped Django template from this same-origin endpoint.
            atualizarEstado(payload.resumo);
        } catch (erro) {
            conteudo.textContent = erro.message + ' Feche a janela e revise novamente.';
            const cancelar = document.createElement('button');
            cancelar.type = 'button'; cancelar.dataset.cancelarAjustes = ''; cancelar.textContent = 'Cancelar';
            conteudo.append(cancelar);
        } finally { abrindo = false; }
    });
    previa.addEventListener('submit', async event => {
        if (!event.target.matches('[data-confirmar-ajustes-modal]')) return;
        event.preventDefault(); event.stopPropagation();
        if (enviando) return;
        const form = event.target;
        if (!form.reportValidity()) return;
        enviando = true;
        const data = new FormData(form);
        const mensagem = form.querySelector('[data-erro-ajustes]');
        const botoes = [...form.querySelectorAll('button')];
        botoes.forEach(b => { b.disabled = true; });
        form.setAttribute('aria-busy','true');
        mensagem.textContent = 'Atualizando... Aguarde a confirmação do servidor.';
        try {
            const payload = await request(endpoint, data);
            if (!payload.ok || payload.estado !== 'aplicado') throw new Error(payload.erro || 'Resultado não comprovado.');
            apresentarRecibo({...payload, previa:data.get('previa')});
            return;
        } catch (erro) {
            if (!erro.rejeitado) {
                // A lost response can follow a commit. Query the receipt; never repeat the POST.
                try {
                    const url = new URL(endpoint,location.origin);
                    url.searchParams.set('resultado_atualizacao',data.get('previa'));
                    const payload = await request(url);
                    if (payload.ok && payload.estado === 'aplicado') {
                        apresentarRecibo({...payload, previa:data.get('previa')});
                        return;
                    }
                } catch (_) { /* Uncertain result stays an error, never success. */ }
            }
            mensagem.textContent = erro.message + ' Sem confirmação desta operação, não considere o estoque atualizado. Feche e revise novamente; em caso de resposta perdida, consulte o histórico.';
            mensagem.setAttribute('role','alert');
            form.removeAttribute('aria-busy');
            enviando = false;
            // A stale preview cannot be confirmed again. Cancel opens the way to a fresh review.
            form.querySelector('[data-cancelar-ajustes]').disabled = false;
        }
    });
    const iniciar = async () => {
        try { atualizarEstado((await request(endpoint)).resumo); }
        catch (erro) { abrir.disabled = false; estado.textContent = 'Não foi possível verificar os ajustes. Revise a prévia: ' + erro.message; }
        let recibo = history.state?.inventarioRecibo;
        if (recibo) {
            history.replaceState(null, '', location.href);
            if (recibo.path !== location.pathname) recibo = null;
        }
        if (recibo) {
            try {
                const url = new URL(endpoint,location.origin);
                url.searchParams.set('resultado_atualizacao',recibo.previa);
                mostrarSucesso(await request(url));
            } catch (erro) { estado.textContent = 'Não foi possível comprovar o resultado. Consulte o histórico. ' + erro.message; }
        }
        window.inventarioAjustesModalPronto = true;
    };
    iniciar();
})();
