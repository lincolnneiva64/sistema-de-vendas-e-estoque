'use strict';
document.querySelectorAll('form[data-edicao-admin]').forEach(form => {
    form.noValidate = true;
    const modal = form.closest('dialog');
    const abrir = document.querySelector('[data-abrir-edicao-admin="' + modal.id + '"]');
    abrir.addEventListener('click', () => {
        modal.showModal();
        form.elements.embalagens.focus();
    });
    modal.addEventListener('cancel', event => { if (enviando) event.preventDefault(); });
    const principal = form.elements.embalagens;
    const avulsos = form.elements.avulsos;
    const mensagem = form.querySelector('[data-mensagem-admin]');
    const botao = form.querySelector('button[type=submit]');
    let enviando = false;
    const atualizarTotal = () => {
        const valido = principal.value !== '' && principal.validity.valid
            && (!avulsos || (avulsos.value !== '' && avulsos.validity.valid));
        // Work in thousandths and round ties to even, like Decimal.quantize on the server.
        const milesimos = valor => BigInt(Number(valor).toFixed(3).replace('.', ''));
        let total = NaN;
        if (valido) {
            const fator = avulsos ? milesimos(form.dataset.fator) : 1000n;
            if (fator > 0n) {
                const numerador = milesimos(principal.value) * fator + (avulsos ? milesimos(avulsos.value) * 1000n : 0n);
                let arredondado = numerador / fator;
                const resto = numerador % fator;
                if (resto * 2n > fator || (resto * 2n === fator && arredondado % 2n !== 0n)) arredondado++;
                total = Number(arredondado) / 1000;
            }
        }
        form.querySelector('[data-total-admin]').textContent = Number.isFinite(total)
            ? total.toLocaleString('pt-BR', {maximumFractionDigits: 3}) + ' ' + form.dataset.base : '—';
    };
    form.addEventListener('input', atualizarTotal);
    principal.addEventListener('input', () => {
        principal.removeAttribute('aria-invalid');
        if (mensagem.dataset.erroPrincipal) { mensagem.textContent=''; delete mensagem.dataset.erroPrincipal; }
        atualizarTotal();
    });
    atualizarTotal();
    form.querySelector('[data-fechar-edicao-admin]').addEventListener('click', () => {
        if (!enviando) modal.close();
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (enviando) return;
        if (principal.value.trim() === '' || !principal.validity.valid) {
            principal.setAttribute('aria-invalid','true');
            mensagem.dataset.erroPrincipal='true';
            mensagem.textContent=principal.value.trim()==='' ? 'Informe a quantidade em '+form.dataset.base+'. Use 0 quando não houver pacotes completos.' : 'Confira a quantidade em '+form.dataset.base+'.';
            principal.focus();return;
        }
        if (form.hasAttribute('data-relacao-pendente') && avulsos && avulsos.value !== '' && Number(avulsos.value)>0) {
            mensagem.textContent='Confirme primeiro a relação física da embalagem em Unidades avulsas.';
            modal.querySelector('.unidades-avulsas').open=true;return;
        }
        if (!form.reportValidity()) return;
        enviando = true;
        botao.disabled = true;
        form.setAttribute('aria-busy', 'true');
        mensagem.textContent = 'Confirmando contagem…';
        try {
            const resposta = await fetch(form.action, {method: 'POST', body: new FormData(form),
                credentials: 'same-origin', headers: {'Accept': 'application/json'}});
            if (!resposta.headers.get('content-type')?.includes('application/json')) {
                throw new Error('Não foi possível confirmar. Preserve os valores e tente novamente para verificar o salvamento.');
            }
            const dados = await resposta.json();
            if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível confirmar a contagem.');
            modal.close();
            const destino = new URL(dados.destino, window.location.origin);
            if (destino.pathname === window.location.pathname && destino.search === window.location.search) {
                window.location.hash = destino.hash;
                window.location.reload();
            } else {
                window.location.assign(destino.href);
            }
        } catch (erro) {
            mensagem.textContent = erro.message || 'Falha de conexão. Tente novamente com os mesmos valores para verificar o salvamento.';
            enviando = false;
            botao.disabled = false;
            form.removeAttribute('aria-busy');
        }
    });
});
window.inventarioContagemAdminPronta = true;
