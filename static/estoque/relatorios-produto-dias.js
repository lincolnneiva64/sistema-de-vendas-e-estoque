/* Only an expanded date fetches its documents; reopening reuses the result. */
(() => {
    const relatorio = document.querySelector('.relatorios-produto');
    if (!relatorio) return;
    async function carregar(link, linha, url, paginacao = false) {
        const conteudo = linha.querySelector('[data-dia-conteudo]');
        if (conteudo.dataset.carregando) return;
        conteudo.dataset.carregando = 'true';
        conteudo.setAttribute('aria-busy', 'true');
        if (!paginacao) conteudo.textContent = 'Carregando notas…';
        try {
            const resposta = await fetch(url, {credentials:'same-origin', headers:{'Accept':'text/html'}});
            if (!resposta.ok || resposta.redirected) throw new Error('notas');
            const html = await resposta.text();
            const documento = new DOMParser().parseFromString(html, 'text/html');
            const detalhe = documento.querySelector('.produto-dia-detalhe');
            if (!detalhe) throw new Error('notas');
            conteudo.replaceChildren(detalhe);
            conteudo.dataset.carregado = 'true';
            if (paginacao && !linha.hidden) {
                conteudo.tabIndex = -1;
                conteudo.focus({preventScroll:true});
                conteudo.scrollIntoView({block:'nearest'});
            }
        } catch (_) {
            const mensagem = document.createElement('p');
            mensagem.textContent = 'Não foi possível carregar as notas. ';
            const tentar = document.createElement('button');
            tentar.type = 'button';
            tentar.textContent = 'Tentar novamente';
            tentar.addEventListener('click', () => carregar(link, linha, url, paginacao));
            mensagem.append(tentar);
            if (!paginacao) conteudo.replaceChildren(mensagem);
            else conteudo.append(mensagem);
        } finally {
            delete conteudo.dataset.carregando;
            conteudo.removeAttribute('aria-busy');
        }
    }
    relatorio.addEventListener('click', evento => {
        const link = evento.target.closest('a[data-produto-dia],a[data-dia-pagina]');
        if (!link || evento.button !== 0 || evento.ctrlKey || evento.metaKey || evento.shiftKey || evento.altKey) return;
        evento.preventDefault();
        if (link.hasAttribute('data-produto-dia')) {
            const linha = document.getElementById(link.getAttribute('aria-controls'));
            linha.hidden = !linha.hidden;
            link.setAttribute('aria-expanded', String(!linha.hidden));
            if (!linha.hidden && !linha.querySelector('[data-dia-conteudo]').dataset.carregado)
                carregar(link, linha, link.href);
        } else {
            const linha = link.closest('.produto-dia-notas');
            const data = relatorio.querySelector('[aria-controls="' + linha.id + '"]');
            // Query-only links belong to the detail endpoint, not this page.
            carregar(data, linha, new URL(link.getAttribute('href'), data.href).href, true);
        }
    });
})();
