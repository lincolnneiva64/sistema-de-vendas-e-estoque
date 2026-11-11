// A preflight never creates a command, edits a draft, or authorizes a loss.
const unit = value => String(value || '').trim().toUpperCase();
const money = value => value === null ? 'Não disponível' : Number(value).toLocaleString('pt-BR', {style:'currency',currency:'BRL'});
function decimal(value) {
    if (typeof value !== 'string' || !/^\d+(?:\.\d{1,6})?$/.test(value)) throw Error('Referência incompleta');
    const [whole, fraction = ''] = value.split('.');
    return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6,'0'));
}
function halfEven(n, d) {
    const q = n / d, r = n % d;
    return q + (2n*r > d || 2n*r === d && q % 2n ? 1n : 0n);
}
export function inspectLocalCosts(items, snapshot) {
    const products = new Map((snapshot?.produtos || []).map(p => [p.id,p]));
    return items.map(item => {
        const p = products.get(String(item.produto_id));
        const row = {produto_id:String(item.produto_id), nome:p?.nome || item.produto_nome || 'Produto', unidade:unit(item.unidade),
            preco:String(item.preco_unitario), custo:null, estado:'indisponivel',
            editar_url:/^[1-9]\d*$/.test(String(item.produto_id)) ? `/produto/${item.produto_id}/editar/` : null};
        try {
            if (!p || p.ativo !== true || !row.unidade) return row;
            const base = unit(p.unidade_venda_1 || p.unidade_base);
            const secondary = p.vende_fracionado && row.unidade === unit(p.unidade_venda_2);
            if (row.unidade !== base && !secondary) return row;
            let cost = decimal(p.custo_referencia);
            if (secondary) {
                const fractional = decimal(p.custo_fracionado_referencia);
                if (fractional > 0n) cost = fractional;
                else {
                    const factor = decimal(p.fator_conversao);
                    if (factor <= 0n) return row;
                    // Same cent precision and half-even rounding as Decimal in Django.
                    cost = halfEven(cost * 100n, factor) * 10000n;
                }
            }
            if (cost <= 0n) return row;
            row.custo = (Number(cost) / 1000000).toFixed(2);
            row.estado = halfEven(decimal(row.preco),10000n) * 10000n < cost ? 'abaixo' : 'ok';
        } catch { /* Unknown data stays unknown, never an assumed zero cost. */ }
        return row;
    });
}
async function currentCosts(items) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    try {
        const csrf = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('csrftoken='));
        const response = await fetch('/api/vendas/online/custos/', {method:'POST', credentials:'same-origin', cache:'no-store',
            signal:controller.signal, headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(csrf?.slice(10) || '')},
            body:JSON.stringify({itens:items})});
        if (!response.ok) throw Error('Não foi possível consultar o cadastro. Confira sua conexão e seu acesso.');
        const data = await response.json();
        if (data.referencia !== 'atual' || !Array.isArray(data.itens) || data.itens.length !== items.length
            || data.itens.some((row,i) => row.produto_id !== String(items[i].produto_id) || row.unidade !== unit(items[i].unidade)
                || !['ok','abaixo','indisponivel'].includes(row.estado))) throw Error('Não foi possível conferir os dados do produto.');
        return {rows:data.itens, local:false};
    } catch (error) {
        if (error instanceof TypeError || error.name === 'AbortError') {
            const unavailable=Error('Não foi possível consultar o custo atual. Confira sua conexão; a venda continua em montagem.');
            unavailable.costNetworkFailure=true; throw unavailable;
        }
        if (error instanceof SyntaxError) throw Error('Não foi possível conferir os custos recebidos. Consulte novamente.');
        throw error;
    } finally { clearTimeout(timer); }
}
export async function checkCosts(items, {snapshot=null, offline=false} = {}) {
    if (!items.length) return {rows:[],local:false}; // Existing empty-sale validation.
    if (!offline || navigator.onLine) {
        try { return await currentCosts(items); }
        catch (error) {
            if (!offline || !error.costNetworkFailure)
                return {rows:[],local:false,error:error.message};
        }
    }
    if (!snapshot || !Number.isFinite(Date.parse(snapshot.gerado_em)))
        return {rows:[],local:true,error:'Não há custos de referência disponíveis. Conecte-se para conferir o cadastro antes de concluir.'};
    return {rows:inspectLocalCosts(items,snapshot),local:true,checkedAt:snapshot.gerado_em};
}
function showCosts(items, result) {
    const existing=document.getElementById('sale-cost-dialog');
    const previous=existing?.costOpener || document.activeElement;
    existing?.remove();
    const dialog = document.createElement('dialog');
    dialog.costOpener=previous;
    dialog.id = 'sale-cost-dialog'; dialog.className = 'venda-confirmacao-card venda-fechamento-card';
    dialog.style.cssText = 'width:min(560px,calc(100vw - 32px));box-sizing:border-box;max-height:85dvh;overflow:auto;overflow-wrap:anywhere;color:#0f172a;border:1px solid #dbe5f0;border-radius:16px;padding:18px';
    dialog.setAttribute('aria-labelledby','sale-cost-title');
    const title = document.createElement('h3'); title.id='sale-cost-title'; title.textContent='Confira o custo antes de concluir';
    const explanation = document.createElement('p');
    explanation.textContent = result.error || (result.local
        ? 'O preço está abaixo do custo de referência salvo neste dispositivo, ou faltam dados para conferir. Essa referência pode estar desatualizada e não confirma o cadastro atual.'
        : 'Há preço abaixo do custo cadastrado, ou faltam dados para conferir. Confira o cadastro do produto.');
    const list = document.createElement('div');
    for (const row of result.rows.filter(row=>row.estado !== 'ok')) {
        const group = document.createElement('section'), text = document.createElement('p');
        text.textContent = `${row.nome} · ${row.unidade} — ${result.local ? 'Custo de referência salvo' : 'Custo cadastrado'}: ${money(row.custo)} · Preço de venda informado: ${money(row.preco)}.`;
        group.append(text);
        if (row.editar_url && /^\/produto\/[1-9]\d*\/editar\/$/.test(row.editar_url)) {
            const edit = document.createElement('a'); edit.href=row.editar_url; edit.target='_blank'; edit.rel='noopener';
            edit.className='acao-venda-btn'; edit.textContent='Corrigir cadastro do produto';
            edit.style.cssText='min-height:44px;height:auto;padding:8px 13px;max-width:100%';
            edit.onclick=event=>{
                if (!navigator.onLine) {
                    event.preventDefault();
                    status.textContent='Conecte-se para abrir o cadastro do produto. Sua venda continua preservada nesta aba.';
                }
            };
            edit.setAttribute('aria-label',`Corrigir cadastro do produto ${row.nome} (abre em outra aba)`); group.append(edit);
        }
        list.append(group);
    }
    const note = document.createElement('p');
    note.textContent='Sua venda continua em montagem. Se o custo estiver correto, o preço precisa ser revisto; uma venda abaixo do custo exige tratamento específico. Nenhuma exceção será criada.';
    const status = document.createElement('p'); status.setAttribute('role','status');
    const actions = document.createElement('div'); actions.className='venda-confirmacao-acoes'; actions.style.cssText='flex-wrap:wrap';
    const refresh=document.createElement('button'), back=document.createElement('button');
    refresh.type=back.type='button'; refresh.textContent='Atualizar dados do produto'; back.textContent='Voltar à venda';
    refresh.style.minHeight=back.style.minHeight='44px';
    const close=()=>{dialog.close();dialog.remove();if(previous?.isConnected)previous.focus({preventScroll:true});};
    back.onclick=close;
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    refresh.onclick=async()=>{
        refresh.disabled=true; status.textContent='Conferindo o cadastro…';
        try {
            const updated=await currentCosts(items);
            if (updated.rows.some(row=>row.estado!=='ok')) { showCosts(items,updated); return; }
            // Refresh only displayed reference costs. Sale fields and draft identity stay untouched.
            for (const row of updated.rows) {
                const option=[...document.querySelectorAll('option[data-produto-id]')].find(o=>o.dataset.produtoId===row.produto_id);
                if (option) { option.dataset.custo=row.custo_base; option.dataset.custo2=row.custo_fracionado; }
            }
            close();
            window.salesDraftBridge?.costsUpdated?.();
        } catch(error) { status.textContent=error.message+' A venda continua em montagem.'; }
        finally { refresh.disabled=false; }
    };
    actions.append(refresh,back); dialog.append(title,explanation,list,note,status,actions);
    document.body.append(dialog);dialog.showModal();back.focus();
}
export async function ensureSaleCosts(items, options) {
    const result=await checkCosts(items,options);
    if (result.error || result.rows.some(row=>row.estado!=='ok')) {
        showCosts(items,result);
        const error=Error('Confira os custos dos produtos antes de concluir. Sua venda continua em montagem.');
        error.costPreflight=true;throw error;
    }
    return result;
}
