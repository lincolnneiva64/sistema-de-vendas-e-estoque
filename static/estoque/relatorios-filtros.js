/* Sincroniza campos locais; a consulta continua exclusiva do botão Consultar. */
(() => {
    const form = document.getElementById('relatorios-filtros-vendas');
    const dados = document.getElementById('relatorios-periodos-datas');
    if (!form || !dados) return;
    const periodos = JSON.parse(dados.textContent);
    const periodo = form.elements.periodo;
    const inicio = form.elements.data_inicial;
    const fim = form.elements.data_final;
    periodo.addEventListener('change', () => {
        const intervalo = periodos[periodo.value];
        if (!intervalo) return;
        inicio.value = intervalo.inicio;
        fim.value = intervalo.fim;
    });
    for (const campo of [inicio, fim]) {
        for (const evento of ['input', 'change']) {
            campo.addEventListener(evento, () => { periodo.value = 'personalizado'; });
        }
    }
})();

/* Barras nativas: no máximo 31 dias no DOM, sem fetch nem biblioteca gráfica. */
(() => {
    const fonte = document.getElementById('relatorios-barras-dados');
    const raiz = document.getElementById('relatorios-barras');
    if (!fonte || !raiz) return;
    const dados = JSON.parse(fonte.textContent);
    const produto = Boolean(dados.medida);
    let posicaoComparar = null;
    if (produto) {
        try {
            const chave = 'relatorios-comparar:' + location.pathname;
            posicaoComparar = JSON.parse(sessionStorage.getItem(chave));
            sessionStorage.removeItem(chave);
        } catch (_) { /* Navigation remains usable when browser storage is blocked. */ }
    }
    raiz.classList.toggle('grafico-barras-semanal', dados.modo === 'semanal');
    raiz.classList.toggle('grafico-barras-comparacao', dados.itens.some(p => p.comparacao));
    raiz.classList.toggle('grafico-barras-par-mensal', Boolean(dados.par_mensal));
    raiz.classList.toggle('grafico-barras-mensal', dados.modo === 'mensal');
    const lista = raiz.querySelector('ol');
    const plot = raiz.querySelector('.grafico-barras-plot');
    const janela = raiz.querySelector('.grafico-barras-janela');
    const anterior = raiz.querySelector('[data-barras-anterior]');
    const proximo = raiz.querySelector('[data-barras-proximo]');
    let deslocamento = 0;
    const tamanhoBloco = dados.modo === 'mensal' && dados.itens.some(p => p.indicadores) ? 6 : 31;
    // Números binários servem somente à geometria; textos mantêm os centavos
    // decimais enviados pelo servidor, sem recalcular faturamento ou média.
    const fantasmaDoPeriodo = periodo => {
        if (dados.modo !== 'mensal') return null;
        const indicadores = periodo.indicadores;
        if (indicadores?.previsao?.valor !== null && indicadores?.previsao?.valor !== undefined)
            return {valor: indicadores.previsao.valor, rotulo: 'Previsão de fechamento'};
        return indicadores?.equivalente_teorico || null;
    };
    // All pages share one zero-based scale, including every valid estimate.
    const estimativas = dados.itens.map(fantasmaDoPeriodo).filter(Boolean).map(p => Number(p.valor));
    const minimo = Math.min(0, Number(dados.minimo), ...estimativas), maximo = Math.max(0, Number(dados.maximo), ...estimativas);
    const amplitude = maximo - minimo || 1;
    let alturaBarras = 180;
    const y = valor => maximo === minimo ? alturaBarras + 28 : (maximo - Number(valor)) / amplitude * alturaBarras + 28;
    const moeda = valor => {
        if (dados.medida && dados.medida !== 'faturamento') {
            const [inteiro, fracao = ''] = String(valor ?? 0).split('.');
            const significativa = fracao.replace(/0+$/, '');
            return inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, '.') +
                (significativa ? ',' + significativa : '') + ' ' + (dados.unidade || '');
        }
        const [inteiro, centavos = '00'] = String(valor).split('.');
        return 'R$ ' + inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, '.') + ',' + centavos.padEnd(2, '0');
    };
    // Round only estimated display strings; source decimals and geometry stay intact.
    const estimativaFormatada = valor => {
        if (!produto || dados.medida === 'faturamento') return moeda(valor);
        const negativo = String(valor).startsWith('-');
        const [inteiro, fracao = ''] = String(valor).replace(/^-/, '').split('.');
        let centesimos = BigInt(inteiro) * 100n + BigInt(fracao.padEnd(2, '0').slice(0, 2));
        if ((fracao[2] || '0') >= '5') centesimos += 1n;
        return moeda((negativo && centesimos ? '-' : '') + (centesimos / 100n) + '.' + String(centesimos % 100n).padStart(2, '0'));
    };
    const dataCurta = iso => iso.slice(8, 10) + '/' + iso.slice(5, 7) + '/' + iso.slice(0, 4);
    const dinheiroSinal = valor => (String(valor).startsWith('-') ? '-' : Number(valor) > 0 ? '+' : '') + moeda(String(valor).replace(/^-/, ''));
    function indicadoresMensais(dadosMes, periodo) {
        const bloco = document.createElement('div');
        bloco.className = 'grafico-barra-indicadores';
        const linha = (rotulo, valor, classe = '') => {
            const p = document.createElement('p');
            p.className = classe;
            p.append(document.createTextNode(rotulo + ' '));
            const numero = document.createElement('strong');
            numero.textContent = valor;
            p.append(numero); bloco.append(p);
            return p;
        };
        if (dados.medida !== 'vendas') linha(dados.medida === 'quantidade' ? 'Média por venda:' : 'Ticket médio:', dadosMes.ticket_medio === null ? 'não calculável' : moeda(dadosMes.ticket_medio));
        linha('Média diária:', dadosMes.media_diaria === null ? 'indisponível' : moeda(dadosMes.media_diaria));
        const base = document.createElement('p');
        base.className = 'comparacao-nota';
        base.textContent = dadosMes.dias_media ? dadosMes.dias_media + (dadosMes.dias_encerrados ? ' dias encerrados' : ' dias comprovados') +
            ' · ' + dataCurta(dadosMes.media_inicio) + ' a ' + dataCurta(dadosMes.media_fim) : 'Sem base diária comprovada.';
        bloco.append(base);
        const situacao = document.createElement('p');
        situacao.className = 'indicadores-situacao';
        situacao.textContent = dadosMes.situacao;
        bloco.append(situacao);

        // Fechamento real de meses completos.
        if (!produto &&
            dados.modo === 'mensal' &&
            dados.medida !== 'vendas' &&
            dados.medida !== 'quantidade' &&
            dadosMes.situacao === 'Completo' &&
            !dadosMes.previsao &&
            !dadosMes.equivalente_teorico &&
            periodo.valor !== null &&
            periodo.valor !== undefined) {

            linha('Fechamento real:', moeda(periodo.valor),
                'indicadores-fechamento');

            const notaFechamento = document.createElement('p');
            notaFechamento.className = 'comparacao-nota';
            notaFechamento.textContent = 'Mês encerrado.';
            bloco.append(notaFechamento);
        }

        const metodo = document.createElement('details');
        metodo.className = 'indicadores-metodo';
        const tituloMetodo = document.createElement('summary');
        tituloMetodo.textContent = 'Como foi calculado';
        metodo.append(tituloMetodo);
        if (produto) {
            metodo.append(base, situacao);
            if (periodo.motivo) {
                const motivo = document.createElement('p');
                motivo.textContent = periodo.motivo;
                metodo.append(motivo);
            }
        }
        const prev = dadosMes.previsao;
        if (prev) {
            if (prev.valor === null) {
                const indisponivel = document.createElement('p');
                indisponivel.className = 'comparacao-nota';
                indisponivel.textContent = 'Previsão indisponível: ' + prev.motivo;
                bloco.append(indisponivel);
            } else {
                linha(produto ? 'Previsão de fechamento do mês · projeção:' : 'Previsão de fechamento:', estimativaFormatada(prev.valor), 'indicadores-previsao');
                if (produto) {
                    const prazo = document.createElement('p');
                    prazo.className = 'comparacao-nota';
                    const fimMes = new Date(Date.UTC(Number(dados.fim.slice(0, 4)), Number(dados.fim.slice(5, 7)), 0));
                    prazo.textContent = 'Estimado até ' + String(fimMes.getUTCDate()).padStart(2, '0') + '/' + dados.fim.slice(5, 7) + '/' + dados.fim.slice(0, 4);
                    bloco.append(prazo);
                }
                if (prev.diferenca !== null) {
                    const percentual = prev.variacao === null ? '% não calculável' :
                        (Number(prev.variacao) > 0 ? '+' : '') + String(prev.variacao).replace('.', ',') + '%';
                    const diferencaPrevista = produto ? (Number(prev.diferenca) > 0 ? '+' : '') + estimativaFormatada(prev.diferenca) : dinheiroSinal(prev.diferenca);
                    const delta = linha('Previsto vs mês anterior completo:', diferencaPrevista + ' (' + percentual + ')', 'indicadores-previsao');
                    delta.title = dataCurta(prev.anterior_inicio) + ' a ' + dataCurta(prev.anterior_fim) + ': ' + moeda(prev.anterior);
                    if (produto) {
                        const referencia = document.createElement('p');
                        referencia.className = 'comparacao-nota';
                        referencia.textContent = 'Mês anterior completo: ' + dataCurta(prev.anterior_inicio) + ' a ' + dataCurta(prev.anterior_fim);
                        bloco.append(referencia);
                    }
                } else {
                    const semReferencia = document.createElement('p');
                    semReferencia.className = 'comparacao-nota';
                    semReferencia.textContent = prev.motivo_comparacao;
                    bloco.append(semReferencia);
                }
                const detalhe = document.createElement('details');
                detalhe.className = 'indicadores-metodo';
                const resumo = document.createElement('summary');
                resumo.textContent = 'Como foi estimada';
                const texto = document.createElement('p');
                texto.textContent = prev.metodologia + ' ' + prev.dias_restantes + ' dias após hoje. Realizado: ' + moeda(prev.realizado) +
                    '. Hoje: ' + (prev.hoje === null ? 'sem registros até agora' : moeda(prev.hoje) + ' (parcial)') + '.' +
                    (prev.anterior !== null ? ' Mês anterior completo: ' + dataCurta(prev.anterior_inicio) + ' a ' + dataCurta(prev.anterior_fim) + ', ' + moeda(prev.anterior) + '.' : '');
                if (produto) metodo.append(texto);
                else { detalhe.append(resumo, texto); bloco.append(detalhe); }
            }
        }
        const teorico = dadosMes.equivalente_teorico;
        if (teorico) {
            const p = linha('Equivalente teórico:', estimativaFormatada(teorico.valor), 'indicadores-teorico');
            p.title = teorico.metodologia + ' ' + teorico.dias_base + ' dias comprovados; calendário de ' + teorico.dias_calendario + ' dias.';
            const nota = document.createElement('p');
            nota.className = 'comparacao-nota';
            nota.textContent = produto ? 'Estimativa teórica · não realizado.' : 'Média diária de ' + teorico.dias_base + ' dias × ' + teorico.dias_calendario + ' dias de calendário. Não realizado.';
            bloco.append(nota);
            if (produto) {
                const explicacao = document.createElement('p');
                explicacao.textContent = p.title;
                metodo.append(explicacao);
            }
        }
        if (produto) bloco.append(metodo);
        return bloco;
    }
    function ajustarAltura() {
        if (!raiz.isConnected) return;
        const figura = raiz.closest('.grafico-evolucao');
        const itens = [...lista.children];
        if (!figura || !itens.length) return;
        const alturaData = Math.max(...itens.map(i => i.querySelector('.grafico-barra-data').getBoundingClientRect().height));
        const alturaVariacao = Math.max(...itens.map(i => i.querySelector('.grafico-barra-variacao').getBoundingClientRect().height));
        const alturaIndicadores = Math.max(0, ...itens.map(i => i.querySelector('.grafico-barra-indicadores')?.getBoundingClientRect().height || 0));
        const antes = janela.getBoundingClientRect().top - figura.getBoundingClientRect().top;
        const navegacao = raiz.querySelector('.grafico-barras-controles').getBoundingClientRect().height;
        const alturaTela = window.visualViewport ? window.visualViewport.height : innerHeight;
        const disponivel = alturaTela - antes - navegacao - 36;
        // Only geometry changes: reserve readable dates/percentages first.
        const rodape = 28 + 32 + alturaData + 12 + alturaVariacao + 12 + (alturaIndicadores ? alturaIndicadores + 10 : 0);
        // Monthly bars must not collapse to make room for the financial notes.
        // Keep the chart readable and let the page scroll naturally below it.
        alturaBarras = dados.modo === 'mensal' ? Math.max(180, Math.min(260, alturaTela * .28)) :
            Math.max(40, Math.min(180, disponivel - rodape));
        const topoData = 28 + alturaBarras + 32;
        const topoVariacao = topoData + alturaData + 12;
        for (const [indice, item] of itens.entries()) {
            const valor = dados.itens[deslocamento + indice].valor;
            const barra = item.querySelector('.grafico-barra');
            barra.style.top = Math.min(y(valor), y(0)) + 'px';
            barra.style.height = Math.max(dados.modo === 'mensal' ? 0 : 1, Math.abs(y(valor) - y(0))) + 'px';
            const fantasma = item.querySelector('.grafico-barra-fantasma');
            if (fantasma) {
                const estimado = fantasmaDoPeriodo(dados.itens[deslocamento + indice]).valor;
                fantasma.style.top = Math.min(y(estimado), y(0)) + 'px';
                fantasma.style.height = Math.abs(y(estimado) - y(0)) + 'px';
                const legenda = item.querySelector('.grafico-barra-fantasma-valor');
                const caixa = item.getBoundingClientRect();
                const contorno = fantasma.getBoundingClientRect();
                const alturaLegenda = legenda.getBoundingClientRect().height;
                legenda.style.left = (contorno.right - caixa.left + 4) + 'px';
                legenda.style.top = Math.max(28, Math.min(28 + alturaBarras - alturaLegenda,
                    contorno.top - caixa.top + contorno.height / 2 - alturaLegenda / 2)) + 'px';
            }
            const rotulo = item.querySelector('.grafico-barra-valor');
            rotulo.style.top = (valor === null ? 6 : Number(valor) < 0 ? y(valor) + 4 : y(valor) - 22) + 'px';
            if (dados.modo === 'mensal') {
                const caixa = item.getBoundingClientRect();
                const solida = barra.getBoundingClientRect();
                rotulo.style.left = valor === null ? '50%' : (solida.left + solida.width / 2 - caixa.left) + 'px';
            }
            item.querySelector('.grafico-barra-data').style.top = topoData + 'px';
            item.querySelector('.grafico-barra-variacao').style.top = topoVariacao + 'px';
            const metricas = item.querySelector('.grafico-barra-indicadores');
            if (metricas) metricas.style.top = (topoVariacao + alturaVariacao + 10) + 'px';
            item.style.height = (topoVariacao + alturaVariacao + 12 + (alturaIndicadores ? alturaIndicadores + 10 : 0)) + 'px';
        }
        if (dados.media !== null) raiz.querySelector('.grafico-media-linha').style.top = y(dados.media) + 'px';
        const zero = raiz.querySelector('.grafico-zero-linha');
        if (zero) {
            zero.hidden = dados.modo !== 'mensal';
            zero.style.top = y(0) + 'px';
        }
    }
    function desenhar() {
        const fim = Math.min(deslocamento + tamanhoBloco, dados.itens.length);
        const fragmento = document.createDocumentFragment();
        for (let i = deslocamento; i < fim; i++) {
            const periodo = dados.itens[i], valor = periodo.valor;
            const parcial = periodo.parcial;
            const item = document.createElement('li');
            item.className = 'grafico-barra-dia';
            if (!dados.par_mensal) item.style.width = Math.max(periodo.indicadores ? 240 : 180, moeda(valor).length * 8 + 20) + 'px';
            item.dataset.data = periodo.inicio;
            if (!produto && dados.modo === 'semanal' && dados.semana_atual) {
                item.tabIndex = 0;
                item.setAttribute('role', 'button');
                item.title = 'Selecionar esta semana para comparar no gráfico diário';
            }
            const barra = document.createElement('span');
            barra.className = 'grafico-barra' + (parcial ? ' grafico-barra-parcial' : '');
            barra.style.top = Math.min(y(valor), y(0)) + 'px';
            barra.style.height = Math.max(1, Math.abs(y(valor) - y(0))) + 'px';
            barra.setAttribute('aria-hidden', 'true');
            const rotulo = document.createElement('strong');
            rotulo.className = 'grafico-barra-valor';
            rotulo.textContent = valor === null ? 'Sem histórico' : moeda(valor);
            barra.hidden = valor === null;
            rotulo.style.top = (Number(valor) < 0 ? y(valor) + 4 : y(valor) - 22) + 'px';
            const data = document.createElement('span');
            data.className = 'grafico-barra-data';
            data.textContent = periodo.indicadores ? periodo.inicio.slice(5, 7) + '/' + periodo.inicio.slice(0, 4) +
                ' · ' + periodo.inicio.slice(8, 10) + '/' + periodo.inicio.slice(5, 7) + '–' + periodo.fim.slice(8, 10) + '/' + periodo.fim.slice(5, 7) :
                periodo.rotulo + (parcial ? ' · ' + periodo.status : '');
            data.title = periodo.rotulo;
            // COMPARACAO ABAIXO DAS BARRAS V2
            if (dados.modo === 'mensal') {
                const dataCurta = iso =>
                    iso.slice(8, 10) + '/' +
                    iso.slice(5, 7) + '/' +
                    iso.slice(0, 4);
                const origem = dados.itens.find(p =>
                    p.comparacao?.anterior_inicio &&
                    p.comparacao?.anterior_fim &&
                    p.comparacao?.inicio &&
                    p.comparacao?.fim
                );
                const comp = origem?.comparacao;

                if (comp) {
                    const mesBarra = periodo.inicio.slice(0, 7);
                    const anterior = mesBarra === comp.anterior_inicio.slice(0, 7);
                    const atual = mesBarra === comp.inicio.slice(0, 7);

                    if (anterior || atual) {
                        const ini = anterior ? comp.anterior_inicio : comp.inicio;
                        const fim = anterior ? comp.anterior_fim : comp.fim;
                        const valor = anterior ? comp.anterior : comp.atual;

                        if (valor !== null && valor !== undefined) {
                            const detalhe = document.createElement('span');
                            detalhe.className = 'comparacao-abaixo-barra';

                            const datas = document.createElement('span');
                            datas.textContent = dataCurta(ini) + ' a ' + dataCurta(fim);

                            const numero = document.createElement('strong');
                            numero.textContent = moeda(valor);

                            detalhe.append(datas, numero);
                            data.append(detalhe);
                        }
                    }
                }
            }


            // Identifica semanas incompletas no inicio do filtro.
            if (!periodo.indicadores && parcial && periodo.rotulo.includes(' a ')) {
                const inicioSemana = Date.parse(periodo.inicio + 'T00:00:00Z');
                const fimSemana = Date.parse(periodo.fim + 'T00:00:00Z');
                const dias = Math.round((fimSemana - inicioSemana) / 86400000) + 1;
                if (dias >= 1 && dias < 7) {
                    data.textContent += ' · Semana parcial (' + dias + ' dias)';
                }
            }
            const variacao = document.createElement('span');
            variacao.className = 'grafico-barra-variacao';
            variacao.textContent = periodo.variacao === null ? 'Sem período comparável' :
                (Number(periodo.variacao) > 0 ? '+' : '') + String(periodo.variacao).replace('.', ',') + '% vs anterior';
            variacao.title = periodo.motivo || 'Comparação com o período anterior completo.';
            if ((dados.historico || dados.cobertura_mensal) && periodo.variacao === null) variacao.textContent = periodo.motivo;
            if (dados.cobertura_mensal && periodo.motivo.startsWith('Períodos não equivalentes')) {
                variacao.textContent = 'Períodos não equivalentes' +
                    (periodo.motivo.toLowerCase().includes('sem histórico') ? ' · sem histórico anterior' : '');
                variacao.setAttribute('aria-label', periodo.motivo);
            }
            if (periodo.referencia) variacao.textContent = 'Mês anterior · referência';
            // Mostra o faturamento real mesmo sem mes anterior comparavel.
            if (dados.cobertura_mensal && !produto && periodo.variacao === null &&
                    periodo.valor !== null && !periodo.referencia) {
                const realizado = document.createElement('span');
                realizado.className = 'comparacao-nota';
                realizado.textContent = 'Faturamento realizado: ' + moeda(periodo.valor);
                variacao.appendChild(realizado);
            }
            const tendencia = Number(periodo.comparacao ? periodo.diferenca : periodo.variacao);
            if (tendencia > 0) variacao.classList.add('crescimento');
            if (tendencia < 0) variacao.classList.add('queda');
            if (!periodo.comparacao && periodo.variacao !== null) variacao.classList.add('comparacao-valor');
            if (periodo.comparacao) {
                const comp = periodo.comparacao;
                const dataCurta = iso => iso.slice(8, 10) + '/' + iso.slice(5, 7) + '/' + iso.slice(0, 4);
                const sinal = v => String(v).startsWith('-') || Number(v) === 0 ? '' : '+';
                variacao.replaceChildren();
                const diferenca = String(periodo.diferenca);
                const textos = [(diferenca.startsWith('-') ? '-' : sinal(diferenca)) + moeda(diferenca.replace(/^-/, '')),
                    periodo.variacao === null ? '% não calculável' :
                        sinal(periodo.variacao) + String(periodo.variacao).replace('.', ',') + '%',
                    dados.par_mensal ? 'vs mês anterior' : comp.rotulo];
                if (!dados.par_mensal) textos.push('vs ' + dataCurta(comp.anterior_inicio) + ' a ' + dataCurta(comp.anterior_fim));
                if (produto) textos.splice(0, textos.length, 'Comparação do período realizado', textos[0] + ' · ' + textos[1],
                    dataCurta(comp.inicio) + ' a ' + dataCurta(comp.fim),
                    'vs ' + dataCurta(comp.anterior_inicio) + ' a ' + dataCurta(comp.anterior_fim));
                for (const [indice, texto] of textos.entries()) {
                    const linha = document.createElement('span');
                    linha.className = (produto ? indice === 1 : indice === 0 || indice === 1 && periodo.variacao !== null) ? 'comparacao-valor' : 'comparacao-nota';
                    linha.textContent = texto;
                    variacao.appendChild(linha);
                }
                variacao.title = 'Atual: ' + moeda(comp.atual) + '; anterior: ' + moeda(comp.anterior) +
                    '. ' + (periodo.motivo || 'Comparação entre as datas equivalentes indicadas.') +
                    (comp.inclui_hoje ? ' Hoje ainda está em andamento.' : '');
            }

            // EVOLUCAO_SEMANAL_DUPLO_PAINEL_20261123
            if (!produto && dados.modo === 'semanal' && dados.semana_atual &&
                periodo.inicio === dados.semana_atual.inicio) {
                const dia = dados.semana_atual.dias_transcorridos;
                data.textContent = 'Semana ' + periodo.inicio.slice(8, 10) + '/' +
                    periodo.inicio.slice(5, 7) + ' a ' +
                    dados.semana_atual.fim.slice(8, 10) + '/' +
                    dados.semana_atual.fim.slice(5, 7);
                const notaDia = document.createElement('span');
                notaDia.className = 'semana-dia-atual';
                notaDia.textContent = dia + ' de 7 dias · Em andamento';
                data.append(notaDia);
                variacao.replaceChildren();
                variacao.classList.remove('crescimento', 'queda');
                const nota = document.createElement('span');
                nota.className = 'comparacao-nota';
                nota.textContent = 'Faturamento acumulado até agora · Dia atual não encerrado';
                variacao.append(nota);
                variacao.title = 'Compare os dias equivalentes no painel de evolução diária.';
            }

            // COMPARACAO ESPELHADA V3
            // Preserva a base percentual usada na comparacao original.
            if (dados.modo === 'mensal' && dados.cobertura_mensal) {
                const origem = dados.itens.find(p =>
                    p.comparacao?.anterior_inicio &&
                    p.comparacao?.inicio &&
                    p.diferenca !== null &&
                    p.diferenca !== undefined
                );

                const comp = origem?.comparacao;

                if (comp &&
                    periodo.inicio.slice(0, 7) ===
                    comp.anterior_inicio.slice(0, 7) &&
                    comp.inicio.slice(0, 7) !==
                    comp.anterior_inicio.slice(0, 7)) {

                    const valorOriginal = String(origem.diferenca);
                    const negativo = valorOriginal.startsWith('-');
                    const absoluto = valorOriginal.replace(/^[+-]/, '');
                    const invertido = negativo ? '+' :
                        Number(absoluto) > 0 ? '-' : '';

                    const percentualOriginal = origem.variacao;
                    const percentual = percentualOriginal === null
                        ? '% nao calculavel'
                        : (Number(percentualOriginal) < 0 ? '+' :
                           Number(percentualOriginal) > 0 ? '-' : '') +
                          String(percentualOriginal)
                            .replace(/^[+-]/, '').replace('.', ',') + '%';

                    variacao.replaceChildren();

                    const valor = document.createElement('span');
                    valor.className = 'comparacao-valor';
                    valor.textContent = invertido + moeda(absoluto);

                    const taxa = document.createElement('span');
                    taxa.className = 'comparacao-valor';
                    taxa.textContent = percentual;

                    const referencia = document.createElement('span');
                    referencia.className = 'comparacao-nota';
                    referencia.textContent = 'Vendas parciais equivalentes';

                    variacao.append(valor, taxa, referencia);
                    variacao.classList.remove('crescimento', 'queda');

                    if (negativo) variacao.classList.add('crescimento');
                    else if (Number(absoluto) > 0)
                        variacao.classList.add('queda');

                    variacao.title =
                        'Comparacao parcial; percentual calculado sobre o periodo anterior.';
                }
            }

            item.append(barra, rotulo, data, variacao);
            const estimativa = fantasmaDoPeriodo(periodo);
            if (estimativa) {
                item.classList.add('tem-fantasma');
                const fantasma = document.createElement('span');
                fantasma.className = 'grafico-barra-fantasma';
                fantasma.setAttribute('role', 'img');
                fantasma.setAttribute('aria-label', estimativa.rotulo + ': ' + estimativaFormatada(estimativa.valor) + '. Valor estimado; não realizado.');
                fantasma.title = estimativa.rotulo + ': ' + estimativaFormatada(estimativa.valor);
                if (Number(estimativa.valor) === 0) fantasma.style.borderWidth = '0';
                const valorEstimado = document.createElement('strong');
                valorEstimado.className = 'grafico-barra-fantasma-valor';
                valorEstimado.textContent = estimativaFormatada(estimativa.valor);
                valorEstimado.setAttribute('aria-hidden', 'true');
                item.append(fantasma, valorEstimado);
            }
            if (periodo.indicadores) item.append(indicadoresMensais(periodo.indicadores, periodo));
            fragmento.appendChild(item);
        }
        lista.replaceChildren(fragmento);
        const media = raiz.querySelector('.grafico-media-linha');
        media.hidden = dados.media === null;
        if (dados.media !== null) media.style.top = y(dados.media) + 'px';
        plot.style.minWidth = dados.par_mensal ? '100%' : 'max-content';
        anterior.disabled = deslocamento === 0;
        proximo.disabled = fim === dados.itens.length;
        raiz.querySelector('[data-barras-intervalo]').textContent = fim ?
            (deslocamento + 1) + '–' + fim + ' de ' + dados.itens.length + ' períodos' : 'Sem período comparável';
        janela.scrollLeft = 0;
        ajustarAltura();
    }
    anterior.addEventListener('click', () => { deslocamento = Math.max(0, deslocamento - tamanhoBloco); desenhar(); });
    proximo.addEventListener('click', () => { deslocamento += tamanhoBloco; desenhar(); });
    raiz.addEventListener('toggle', ajustarAltura, true);
    raiz.hidden = false;
    desenhar();
    window.addEventListener('resize', ajustarAltura, {passive: true});
    if (window.visualViewport) window.visualViewport.addEventListener('resize', ajustarAltura, {passive: true});
    const controles = document.getElementById('evolucao-controles');
    if (controles) controles.addEventListener('change', ajustarAltura);
    const enquadrar = () => {
        if (!raiz.isConnected) return;
        ajustarAltura();
        if (posicaoComparar) {
            const controle = document.getElementById('evolucao-controles');
            const alvo = posicaoComparar.campo ? controle?.elements.namedItem(posicaoComparar.campo) : controle?.querySelector('button[type="submit"]');
            alvo?.focus({preventScroll: true});
            window.scrollTo({top: posicaoComparar.y, left: posicaoComparar.x, behavior: 'instant'});
            posicaoComparar = null;
            return;
        }
        if (location.hash === '#evolucao-vendas') {
            const figura = raiz.closest('.grafico-evolucao');
            if (dados.medida) figura.querySelector('h2')?.focus({preventScroll: true});
            window.scrollTo({top: Math.max(0, scrollY + figura.getBoundingClientRect().top - 12), behavior: 'instant'});
        }
    };
    // One initial positioning after drawing/loading; never scroll on resize.
    if (document.readyState === 'complete') requestAnimationFrame(enquadrar);
    else window.addEventListener('load', () => requestAnimationFrame(enquadrar), {once: true});
})();

(() => {
    const form = document.getElementById('evolucao-controles');
    if (!form) return;
    if (form.dataset.produto) form.addEventListener('submit', () => {
        try {
            sessionStorage.setItem('relatorios-comparar:' + location.pathname, JSON.stringify({
                x: scrollX, y: scrollY, campo: form.contains(document.activeElement) ? document.activeElement.name : ''
            }));
        } catch (_) { /* Keep the standard GET submission available. */ }
    });
    const atualizar = () => {
        const unidadeProduto = form.querySelector('[data-produto-unidade]');
        if (unidadeProduto) unidadeProduto.hidden = form.elements.medida.value !== 'quantidade';
        form.querySelector('[data-evolucao-dia]').hidden = form.elements.modo.value !== 'diario';
        const alcance = form.elements.alcance?.value || 'principal';
        const mensal = form.elements.modo.value === 'mensal';
        const datas = form.querySelector('[data-historico-datas]');
        if (datas) datas.hidden = alcance !== 'personalizado';
        const comparacao = form.querySelector('[data-historico-comparacao]');
        if (comparacao) comparacao.hidden = !mensal || !form.dataset.produto && alcance === 'principal' && form.elements.comparacao?.value !== 'ano_anterior';
        form.querySelector('[data-evolucao-equivalente]').hidden = Boolean(form.dataset.produto) || !mensal || alcance !== 'principal' || form.elements.comparacao?.value === 'ano_anterior';
    };
    for (const nome of ['modo', 'alcance', 'comparacao', 'medida']) {
        if (form.elements[nome]) form.elements[nome].addEventListener('change', atualizar);
    }
    atualizar();
})();

/* EVOLUCAO_SEMANAL_MULTIPLAS_SEMANAS_20261123 — gráfico SVG, somente leitura. */
(() => {
    const fonte = document.getElementById('relatorios-semana-diaria-dados');
    const destino = document.getElementById('semana-diaria-grafico');
    if (!fonte || !destino) return;
    const dados = JSON.parse(fonte.textContent);
    const semanas = dados.semanas || [];
    const seletor = document.getElementById('semana-selecionada');
    const quantidadeBotoes = [...document.querySelectorAll('[data-semana-quantidade]')];
    const visaoBotoes = [...document.querySelectorAll('[data-semana-visao]')];
    const painel = document.getElementById('semana-painel-diario');
    const graficoSemanal = document.querySelector('.grafico-barras-janela');
    const controlesSemanais = document.querySelector('.grafico-barras-controles');
    const legenda = document.getElementById('semana-legenda');
    const periodos = document.getElementById('semana-comparacao-periodos');
    const aviso = document.getElementById('semana-aviso-parcial');
    const lista = document.querySelector('.grafico-barras-lista');
    if (!seletor || !legenda || !periodos || !aviso || !painel || !semanas.length) return;
    const cores = ['#16734f', '#2459c5', '#c15b05', '#8b3fae'];
    const nomes = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'];
    const NS = 'http://www.w3.org/2000/svg';
    const dinheiro = n => Number(n).toLocaleString('pt-BR', {style:'currency', currency:'BRL'});
    const abreviar = iso => iso.slice(8,10) + '/' + iso.slice(5,7);
    const rotulo = s => abreviar(s.inicio) + ' a ' + abreviar(s.fim) + (s.parcial ? ' · em andamento' : '');
    let semanaEscolhida = dados.selecionada;
    let quantidade = 2;
    const criar = (tag, attrs, parent) => {
        const el = document.createElementNS(NS, tag);
        for (const [chave, valor] of Object.entries(attrs || {})) el.setAttribute(chave, String(valor));
        if (parent) parent.appendChild(el);
        return el;
    };
    for (const s of semanas.slice(1)) {
        const option = document.createElement('option');
        option.value = s.inicio;
        option.textContent = rotulo(s);
        seletor.appendChild(option);
    }
    if (![...seletor.options].some(o => o.value === semanaEscolhida)) {
        semanaEscolhida = seletor.options[seletor.options.length - 1]?.value;
    }
    seletor.value = semanaEscolhida;
    function selecionadas() {
        const pos = semanas.findIndex(s => s.inicio === semanaEscolhida);
        // A primeira série é referência; as outras 16 são selecionáveis.
        return semanas.slice(Math.max(0, pos - quantidade + 1), pos + 1).reverse();
    }
    function desenhar() {
        const series = selecionadas();
        quantidadeBotoes.forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.semanaQuantidade) === quantidade)));
        legenda.replaceChildren();
        for (let i=0;i<series.length;i++) {
            const item = document.createElement('span');
            item.className = 'semana-chave';
            item.style.setProperty('--cor-semana', cores[i]);
            item.textContent = rotulo(series[i]);
            legenda.appendChild(item);
        }
        periodos.textContent = series.length === 1 ? 'Semana selecionada: ' + rotulo(series[0]) :
            'Comparando ' + series.length + ' semanas: ' + series.map(rotulo).join(' • ');
        aviso.textContent = series.some(s => s.parcial) ?
            'A semana atual e o dia de hoje ainda estão em andamento. Não compare o acumulado parcial com o fechamento de semanas completas.' :
            'Semanas encerradas. Passe o mouse sobre os pontos para consultar os valores acumulados.';
        const largura=740, altura=315, margemEsq=88, margemDir=24, topo=25, rodape=50;
        const utilX=largura-margemEsq-margemDir, utilY=altura-topo-rodape;
        const valores = series.flatMap(s => s.pontos.map(p => p.valor)).filter(v => v !== null).map(Number);
        const max = Math.max(1, ...valores)*1.1;
        const x = i => margemEsq + utilX*i/6;
        const y = v => topo + utilY - Number(v)*utilY/max;
        const svg=criar('svg',{viewBox:`0 0 ${largura} ${altura}`,role:'img',
            'aria-label':'Faturamento acumulado de '+series.map(rotulo).join(' versus '),class:'semana-diaria-svg'});
        for (let t=0;t<=4;t++) {
            const v=max*t/4, yy=y(v);
            criar('line',{x1:margemEsq,x2:largura-margemDir,y1:yy,y2:yy,stroke:'#dbe4ea','stroke-dasharray':'3 4'},svg);
            const tx=criar('text',{x:margemEsq-10,y:yy+4,'text-anchor':'end',fill:'#526575','font-size':12},svg);
            tx.textContent=v>=1000?'R$ '+(v/1000).toLocaleString('pt-BR',{maximumFractionDigits:1})+' mil':'R$ '+Math.round(v);
        }
        for (let i=0;i<7;i++) {
            const tx=criar('text',{x:x(i),y:altura-18,'text-anchor':'middle',fill:'#354657','font-size':13},svg);
            tx.textContent=nomes[i];
        }
        series.forEach((serie,k) => {
            let grupo=[], grupos=[];
            serie.pontos.forEach((p,i) => {
                if (p.valor === null || p.valor === undefined) {
                    if (grupo.length) grupos.push(grupo);
                    grupo=[];
                } else grupo.push([i,Number(p.valor),p]);
            });
            if (grupo.length) grupos.push(grupo);
            for (const trecho of grupos) {
                if (trecho.length>1) criar('polyline', {points:trecho.map(([i,v])=>x(i)+','+y(v)).join(' '),
                    fill:'none',stroke:cores[k],'stroke-width':3,'stroke-linecap':'round','stroke-linejoin':'round'},svg);
                for (const [i,v,p] of trecho) {
                    const c=criar('circle',{cx:x(i),cy:y(v),r:5,fill:cores[k],stroke:'#fff','stroke-width':1.5},svg);
                    const tt=criar('title',{},c);
                    tt.textContent=rotulo(serie)+' — '+nomes[i]+': '+dinheiro(v)+(p.em_andamento?' · dia em andamento':'');
                }
            }
        });
        destino.replaceChildren(svg);
        destino.setAttribute('aria-label', series.map(s=>rotulo(s)+': '+s.pontos.map((p,i)=>
            nomes[i]+' '+(p.valor===null?'sem dado comprovado':dinheiro(p.valor))).join(', ')).join('. '));
    }
    function visao(tipo) {
        if (graficoSemanal) graficoSemanal.hidden=tipo==='diario';
        if (controlesSemanais) controlesSemanais.hidden=tipo==='diario';
        painel.hidden=tipo==='semanal';
        visaoBotoes.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.semanaVisao===tipo)));
    }
    function escolher(inicio) {
        if (!semanas.some(s=>s.inicio===inicio)) return;
        semanaEscolhida=inicio;
        seletor.value=inicio;
        desenhar();
    }
    quantidadeBotoes.forEach(b=>b.addEventListener('click',()=>{quantidade=Number(b.dataset.semanaQuantidade);desenhar();}));
    visaoBotoes.forEach(b=>b.addEventListener('click',()=>visao(b.dataset.semanaVisao)));
    seletor.addEventListener('change',()=>escolher(seletor.value));
    if (lista) {
        const acionar = e => {
            const li=e.target.closest('li[data-data]');
            if (li && lista.contains(li)) {escolher(li.dataset.data);visao('ambos');}
        };
        lista.addEventListener('click',acionar);
        lista.addEventListener('keydown',e=>{
            if (e.key==='Enter'||e.key===' ') {e.preventDefault();acionar(e);}
        });
    }
    desenhar();
    visao('ambos');
})();
