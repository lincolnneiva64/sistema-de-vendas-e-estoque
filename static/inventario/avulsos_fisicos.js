'use strict';
document.querySelectorAll('form[data-avulsos-fisicos]').forEach(form => {
    const principal = form.elements.embalagens, avulsos = form.elements.avulsos;
    const output = form.querySelector('[data-total-fisico]');
    const aviso = document.createElement('small');aviso.setAttribute('role','status');output.after(aviso);
    const fator = /^(\d+)(?:\.0+)?$/.exec(form.dataset.fator || '');
    const atualizar = () => {
        const valido = fator && /^\d+$/.test(principal.value) && /^\d+$/.test(avulsos.value)
            && principal.validity.valid && avulsos.validity.valid;
        aviso.textContent = valido && ((BigInt(principal.value)*BigInt(fator[1])+BigInt(avulsos.value))*1000n)%BigInt(fator[1]) !== 0n
            ? 'Contagem exata preservada em UN. Ajuste automático indisponível: a equivalência em embalagens exige arredondamento.' : '';
        output.textContent = valido
            ? BigInt(principal.value).toLocaleString('pt-BR') + ' ' + (form.dataset.base || principal.dataset.unidade) + ' + ' + BigInt(avulsos.value).toLocaleString('pt-BR') + ' UN — Total físico: ' + (BigInt(principal.value) * BigInt(fator[1]) + BigInt(avulsos.value)).toLocaleString('pt-BR') + ' UN'
            : 'Total físico: —';
    };
    form.addEventListener('input', atualizar);
    principal.addEventListener('input', atualizar);
    new MutationObserver(atualizar).observe(form, {subtree:true, attributes:true, attributeFilter:['readonly']});
    atualizar();
});

document.querySelectorAll('form[data-relacao-fisica]').forEach(form => {
    let enviando = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (enviando || !form.reportValidity()) return;
        enviando = true;
        const botao=form.querySelector('button'), mensagem=form.querySelector('[data-mensagem-relacao]');
        botao.disabled=true;mensagem.textContent='Confirmando relação física…';
        try {
            const resposta=await fetch(form.action,{method:'POST',body:new FormData(form),credentials:'same-origin',headers:{Accept:'application/json'}});
            if (!resposta.headers.get('content-type')?.includes('application/json')) throw Error('Resposta incerta. Preserve a relação e tente novamente para verificar.');
            const dados=await resposta.json();if(!resposta.ok)throw Error(dados.erro || 'Não foi possível confirmar.');
            const contagem=form.closest('dialog')?.querySelector('form[data-edicao-admin]');
            if(contagem){
                const chave='inventario-contagem-apos-relacao:'+contagem.action+':'+contagem.elements.autorizacao.value.split(':')[0];
                try{sessionStorage.setItem(chave,JSON.stringify({embalagens:contagem.elements.embalagens.value,avulsos:contagem.elements.avulsos?.value || '0'}))}catch(e){}
            }
            location.reload();
        } catch(erro) {mensagem.textContent=erro.message;enviando=false;botao.disabled=false;}
    });
});

// One-time continuation after relation confirmation; never submits a count automatically.
document.querySelectorAll('form[data-edicao-admin]').forEach(form=>{
    const chave='inventario-contagem-apos-relacao:'+form.action+':'+form.elements.autorizacao.value.split(':')[0];
    try{
        const texto=sessionStorage.getItem(chave);if(!texto)return;
        sessionStorage.removeItem(chave);const dados=JSON.parse(texto);
        if(typeof dados.embalagens!=='string'||typeof dados.avulsos!=='string')return;
        form.elements.embalagens.value=dados.embalagens;
        if(form.elements.avulsos)form.elements.avulsos.value=dados.avulsos;
        setTimeout(()=>{
            const modal=form.closest('dialog');if(!modal.open)modal.showModal();
            // Keep the optional section collapsed after reload, including saved counts.
            form.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));
        },0);
    }catch(e){}
});
