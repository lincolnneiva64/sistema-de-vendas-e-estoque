/* Catalog suggestions select an identity; Consultar still supports partial names. */
(() => {
    const form = document.querySelector('[data-produtos-sugestoes]');
    if (!form) return;
    const campo = form.elements.q;
    const identidade = form.elements.produto_id;
    const lista = document.getElementById('relatorios-produtos-sugestoes');
    const analisar = form.querySelector('[data-produto-analisar]');
    const status = form.querySelector('[data-produto-pesquisa-status]');
    let produtos = [], ativo = -1, timer, requisicao, versao = 0;

    // Position after layout settles: the native fragment may be displaced
    // while responsive cards and styles finish loading on a small screen.
    const enquadrarResultados = () => {
        if (location.hash !== '#resultados-produtos' || !form.isConnected) return;
        const resultados = document.getElementById('resultados-produtos');
        if (!resultados) return;
        resultados.focus({preventScroll:true});
        resultados.scrollIntoView({block:'start', behavior:'instant'});
    };
    if (document.readyState === 'complete') requestAnimationFrame(enquadrarResultados);
    else window.addEventListener('load', () => requestAnimationFrame(enquadrarResultados), {once:true});

    campo.autocomplete = 'off';
    campo.setAttribute('role', 'combobox');
    campo.setAttribute('aria-autocomplete', 'list');
    campo.setAttribute('aria-controls', lista.id);
    campo.setAttribute('aria-expanded', 'false');
    campo.setAttribute('aria-describedby', status.id);

    function fechar() {
        lista.hidden = true;
        ativo = -1;
        campo.setAttribute('aria-expanded', 'false');
        campo.removeAttribute('aria-activedescendant');
    }

    function atualizarLink() {
        if (!/^[1-9]\d*$/.test(identidade.value)) {
            analisar.hidden = true;
            analisar.removeAttribute('href');
            return;
        }
        const destino = new URL(form.dataset.produtoAnaliseBase + identidade.value + '/', document.baseURI);
        const parametros = new URLSearchParams(new FormData(form));
        const retorno = new URLSearchParams(parametros);
        retorno.set('pagina', form.dataset.rankingPagina || '1');
        parametros.set('ranking', retorno.toString());
        // Ranking pages and a previously viewed product's unit cannot choose
        // a unit or daily-detail page for this newly selected product.
        for (const nome of ['produto_id', 'pagina', 'unidade']) parametros.delete(nome);
        destino.search = parametros.toString();
        analisar.href = destino.href;
        analisar.hidden = false;
    }

    function cancelar() {
        clearTimeout(timer);
        requisicao?.abort();
        versao++;
    }

    function selecionar(p) {
        cancelar();
        identidade.value = String(p.id);
        campo.value = p.nome;
        fechar();
        atualizarLink();
        status.textContent = 'Produto selecionado: ' + p.nome + '. Abra a análise individual ou consulte somente este produto no ranking.';
        analisar.focus();
    }

    function destacar(indice) {
        ativo = indice;
        [...lista.children].forEach((opcao, i) => opcao.setAttribute('aria-selected', String(i === ativo)));
        const opcao = lista.children[ativo];
        campo.setAttribute('aria-activedescendant', opcao.id);
        opcao.scrollIntoView({block: 'nearest'});
    }

    function posicionarLista() {
        if (lista.hidden) return;
        const r = campo.getBoundingClientRect();
        const topo = window.visualViewport?.offsetTop || 0;
        const altura = window.visualViewport?.height || innerHeight;
        const acima = r.top - topo, abaixo = topo + altura - r.bottom;
        const subir = abaixo < 160 && acima > abaixo;
        lista.style.top = subir ? 'auto' : '100%';
        lista.style.bottom = subir ? '100%' : 'auto';
        lista.style.maxHeight = Math.max(80, Math.min(280, (subir ? acima : abaixo) - 12)) + 'px';
    }

    async function buscar() {
        const termo = campo.value.trim();
        if (termo.length < 2 || identidade.value) return;
        const atual = ++versao;
        requisicao = new AbortController();
        const url = new URL(form.dataset.produtosSugestoes, document.baseURI);
        url.searchParams.set('q', termo);
        try {
            const resposta = await fetch(url, {signal: requisicao.signal, credentials: 'same-origin',
                headers: {'Accept': 'application/json'}});
            if (!resposta.ok) throw new Error('suggestions');
            const dados = await resposta.json();
            if (atual !== versao || campo.value.trim() !== termo || !form.isConnected || identidade.value) return;
            produtos = dados.produtos;
            lista.replaceChildren();
            ativo = -1;
            produtos.forEach((p, i) => {
                const opcao = document.createElement('button');
                opcao.type = 'button';
                opcao.tabIndex = -1;
                opcao.id = 'relatorios-produto-opcao-' + i;
                opcao.dataset.produtoId = String(p.id);
                opcao.setAttribute('role', 'option');
                opcao.setAttribute('aria-selected', 'false');
                // IDs distinguish even two identical catalog names.
                opcao.title = p.nome + ' · código ' + p.id;
                const nome = document.createElement('span');
                nome.className = 'relatorios-sugestao-nome';
                nome.textContent = p.nome;
                const codigo = document.createElement('small');
                codigo.className = 'relatorios-sugestao-codigo';
                codigo.textContent = '#' + p.id;
                opcao.append(nome, codigo);
                opcao.addEventListener('pointerdown', e => e.preventDefault());
                opcao.addEventListener('click', () => selecionar(p));
                lista.append(opcao);
            });
            lista.hidden = !produtos.length;
            campo.setAttribute('aria-expanded', String(produtos.length > 0));
            posicionarLista();
            if (produtos.length) destacar(0);
            campo.removeAttribute('aria-activedescendant');
            status.textContent = produtos.length ?
                (dados.mais ? 'Mostrando 8 sugestões; digite mais para refinar. ' : '') +
                'Escolha um produto para analisar ou clique em Consultar para pesquisar o termo no ranking.' :
                'Nenhum produto cadastrado corresponde ao nome. Clique em Consultar para aplicar a pesquisa ao ranking.';
        } catch (erro) {
            if (erro.name === 'AbortError' || atual !== versao) return;
            fechar();
            status.textContent = 'Não foi possível carregar as sugestões. A pesquisa pelo botão Consultar continua disponível.';
        }
    }

    campo.addEventListener('input', () => {
        cancelar();
        identidade.value = '';
        produtos = [];
        fechar();
        atualizarLink();
        status.textContent = 'Pesquisa alterada. Clique em Consultar para atualizar o ranking ou escolha uma sugestão para analisar um produto.';
        if (campo.value.trim().length >= 2) timer = setTimeout(buscar, 180);
    });
    campo.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            cancelar(); fechar();
        } else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !lista.hidden && produtos.length) {
            e.preventDefault();
            destacar((ativo + (e.key === 'ArrowDown' ? 1 : ativo < 0 ? 0 : -1) + produtos.length) % produtos.length);
        } else if (e.key === 'Enter' && ativo >= 0 && !lista.hidden) {
            e.preventDefault(); selecionar(produtos[ativo]);
        } else if (e.key === 'Tab') {
            cancelar(); fechar();
        }
        // As in Vendas, Enter chooses the highlighted option; Consultar keeps
        // the partial-name search available without selecting a catalog ID.
    });
    campo.addEventListener('blur', () => { cancelar(); fechar(); });
    campo.addEventListener('focus', () => {
        if (!identidade.value && campo.value.trim().length >= 2) {
            cancelar(); timer = setTimeout(buscar, 180);
        }
    });
    form.addEventListener('change', atualizarLink);
    form.addEventListener('input', e => { if (e.target !== campo) atualizarLink(); });
    analisar.addEventListener('click', atualizarLink);
    window.addEventListener('resize', posicionarLista, {passive: true});
    window.visualViewport?.addEventListener('resize', posicionarLista, {passive: true});
    atualizarLink();
})();
