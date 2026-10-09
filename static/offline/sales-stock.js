import {canonical, commandOf, hash, validReceipt} from '/offline/assets/2-8f/core.js';
import {chaveSnapshotComercial} from '/offline/assets/2-8f-fix/commercial.js';
import {closedLocally} from '/offline/assets/2-8g-close/sales-closures.js';

// Decimal arithmetic at the server's 0.001 precision, including half-even rounding.
function ratio(value) {
    const text = String(value ?? '').replace(',', '.');
    if (!/^-?\d+(?:\.\d{1,6})?$/.test(text)) throw new Error('Quantidade ou fator local inválido.');
    const [whole, fraction = ''] = text.split('.');
    const sign = whole.startsWith('-') ? -1n : 1n;
    return [BigInt(whole.replace('-', '') + fraction) * sign, 10n ** BigInt(fraction.length)];
}
function rounded(n, d) {
    const sign = n < 0n ? -1n : 1n; n = n < 0n ? -n : n;
    const q = n / d, r = n % d;
    return sign * (q + (2n*r > d || 2n*r === d && q % 2n ? 1n : 0n));
}
const milli = value => { const [n,d] = ratio(value); return rounded(n*1000n,d); };
const normalized = value => String(value || '').trim().toUpperCase();
const packages = new Set(['PCT','PACOTE','FARDO','FD','CX','CAIXA']);
const wholeAtPrecision = (quantity,n,d) => rounded(quantity*n,d) % 1000n === 0n;
function baseQuantity(product,item) {
    const quantity = milli(item.quantidade), unit = normalized(item.unidade);
    const base = normalized(product.unidade_venda_1 || product.unidade_base || product.unidade1);
    const secondary = normalized(product.unidade_venda_2 || product.unidade2);
    const fractional = product.vende_fracionado ?? product.fracionado;
    const [fn,fd] = ratio(product.fator_conversao ?? product.fator ?? '0');
    if (quantity <= 0n || !unit || unit !== base && !(fractional && unit === secondary))
        throw new Error('Unidade ou quantidade incompatível com ' + (product.nome || item.produto_nome || item.produto_id) + '.');
    if (fractional && unit === secondary && fn <= 0n) throw new Error('Fator de conversão local inválido.');
    const allowedFraction = ['KG','MIL'].includes(unit)
        || unit === 'DZ' && wholeAtPrecision(quantity,12n,1n)
        || fractional && fn > 0n && (unit === secondary || unit === base && packages.has(unit) && wholeAtPrecision(quantity,fn,fd))
        || packages.has(unit) && wholeAtPrecision(quantity,2n,1n);
    if (quantity % 1000n && !allowedFraction) throw new Error('Quantidade fracionada não permitida em ' + unit + '.');
    const converted = unit === base ? quantity : rounded(quantity*fd,fn);
    if (!fractional && converted % 1000n && !(['KG','MIL'].includes(base)
        || base === 'DZ' && wholeAtPrecision(converted,12n,1n)
        || packages.has(base) && wholeAtPrecision(converted,2n,1n)))
        throw new Error('Quantidade fracionada não permitida na unidade base.');
    return converted;
}
export const stockBaselineKey = snapshotId => 'sales-stock-baseline:' + snapshotId;
export function stockSignature(state) { return canonical({snapshot:state.snapshot,baseline:state.baseline,operations:state.operations,closures:state.closures || []}); }
export async function readStock(repo,scope) {
    const key = chaveSnapshotComercial(scope);
    const state = await repo.transaction(['snapshots','metadata','operations'],false,(tx,done) => {
        tx.objectStore('snapshots').get(key).onsuccess = event => {
            const snapshot = event.target.result || null;
            tx.objectStore('operations').getAll().onsuccess = result => {
                const operations = result.target.result;
                tx.objectStore('metadata').getAll().onsuccess = result => {
                    const meta=result.target.result;
                    done({snapshot,baseline:snapshot ? meta.find(row=>row.key===stockBaselineKey(snapshot.snapshot_id)) || null : null,
                        operations,closures:meta.filter(row=>row.key.startsWith('sales-closure:') && row.receipt)});
                };
            };
        };
    });
    if (state.snapshot && (state.snapshot.actor?.id !== scope.actor_id || state.snapshot.environment_id !== scope.environment_id
        || state.snapshot.device_id !== scope.device_id || !Array.isArray(state.snapshot.produtos)))
        throw new Error('Identidade do estoque local incompatível.');
    for (const op of state.operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id && op.environment_id === scope.environment_id))
        if (await hash(commandOf(op)) !== op.payload_hash) throw new Error('Comando da fila divergente. Estoque local não pode ser calculado com segurança.');
    return state;
}
export function validateStock(state,scope,items,fallback = []) {
    if (!state) throw new Error('Aguarde a leitura do estoque local antes de adicionar.');
    const products = new Map((state.snapshot?.produtos || []).map(p => [p.id,p]));
    const known = new Map(fallback.map(p => [p.produto_id,p]));
    const sales = state.operations.filter(op => op.type === 'criar_venda' && op.actor_id === scope.actor_id && op.environment_id === scope.environment_id);
    const replaced = new Set();
    for (const op of sales) {
        if (closedLocally(state.closures,op)) continue;
        const link = op.payload?.revisao;
        const parent = link && sales.find(p => p.operation_id === link.original_operation_id && p.payload_hash === link.original_hash);
        if (parent && link.relacao === 'revisao_de_conflito') replaced.add(parent.operation_id);
    }
    const hasBaseline = !!state.baseline && !!state.snapshot?.snapshot_id
        && state.baseline.snapshot_id === state.snapshot.snapshot_id && Array.isArray(state.baseline.incorporated);
    const incorporated = hasBaseline ? new Set(state.baseline.incorporated) : new Set();
    const used = new Map();
    function consume(list,required) {
        for (const item of list) {
            const p = products.get(item.produto_id) || known.get(item.produto_id);
            if (!p) { if (required && state.snapshot) throw new Error('Produto fora do catálogo local. Revise antes de concluir.'); continue; }
            const quantity = baseQuantity(p,item);
            used.set(item.produto_id,(used.get(item.produto_id) || 0n) + quantity);
        }
    }
    for (const op of sales) {
        if (replaced.has(op.operation_id)) continue;
        if (closedLocally(state.closures,op)) continue;
        if (incorporated.has(op.operation_id) && op.status === 'confirmada' && validReceipt(op.server_result,op)) continue;
        if (state.snapshot && !hasBaseline && op.status === 'confirmada'
            && op.reference_snapshot?.snapshot_id !== state.snapshot.snapshot_id)
            throw new Error('Catálogo antigo sem evidência do saldo após vendas confirmadas. Prepare os dados manualmente online antes de concluir outra venda offline. Operações preservadas.');
        consume(op.payload?.itens || [],false);
    }
    consume(items,true);
    for (const [id,quantity] of used) {
        const p = products.get(id);
        if (p && items.some(i => i.produto_id === id) && quantity > milli(p.estoque_referencia))
            throw new Error('Estoque local insuficiente para ' + p.nome + '. O saldo de referência considera as vendas já salvas neste dispositivo. Revise quantidade e unidade.');
    }
    return {hasStock:!!state.snapshot};
}
