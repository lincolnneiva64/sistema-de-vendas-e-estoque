// Stage 2.1: catalog only. No sale commands, queue entries, or stock mutations.
export const COMMERCIAL_SCHEMA = 1;
import {validReceipt} from '/offline/assets/2-8f/core.js';
export const COMMERCIAL_TYPE = 'comercial_vendas';
const clientFields = ['id', 'nome', 'ativo', 'apelido_nome_conhecido', 'telefone', 'prazo_padrao_dias'];
const productFields = ['id', 'nome', 'codigo', 'ativo', 'unidade_base', 'unidade_venda_1', 'unidade_venda_2',
    'vende_fracionado', 'fator_conversao', 'preco_venda', 'preco_venda_fracionado', 'custo_referencia',
    'custo_fracionado_referencia', 'estoque_referencia', 'estoque_conferido', 'preco_conferido'];
const revalidate = ['produto_ativo', 'cliente_ativo', 'preco', 'estoque', 'conversoes',
    'regras_comerciais', 'forma_pagamento', 'credito', 'operador', 'pedido_origem'];
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const decimal = value => typeof value === 'string' && /^-?\d+\.\d{2,3}$/.test(value);
const id = value => typeof value === 'string' && /^[1-9]\d*$/.test(value);
const pick = (value, fields) => Object.fromEntries(fields.map(field => [field, value[field]]));

function deviceOf(repo) {
    // Use the existing metadata/device identity without reserving an operation sequence.
    // Works with the previously cached core.js as well as the refreshed worker shell.
    return repo.transaction(['metadata'], true, (tx, done) => {
        const store = tx.objectStore('metadata');
        store.get('device').onsuccess = event => {
            const existing = event.target.result;
            const device = existing || {key: 'device', id: crypto.randomUUID(), sequence: 0};
            if (!existing) store.put(device);
            done(device);
        };
    });
}

export function chaveSnapshotComercial(scope) {
    if (!scope || !id(scope.actor_id) || typeof scope.environment_id !== 'string' || !scope.environment_id.trim())
        throw new Error('Informe usuário e ambiente autenticados para o catálogo.');
    return COMMERCIAL_TYPE + ':' + JSON.stringify([scope.environment_id, scope.actor_id]);
}

function validated(data, scope) {
    chaveSnapshotComercial(scope);
    if (!data || data.tipo !== COMMERCIAL_TYPE || data.schema_version !== COMMERCIAL_SCHEMA
        || !uuid(data.snapshot_id) || !uuid(data.device_id) || typeof data.gerado_em !== 'string'
        || !Number.isFinite(Date.parse(data.gerado_em)) || data.origem !== '/vendas/'
        || data.somente_referencia !== true || data.actor?.id !== scope.actor_id
        || typeof data.actor.name !== 'string' || data.environment_id !== scope.environment_id)
        throw new Error('Snapshot comercial inválido ou de outro usuário/ambiente.');
    for (const kind of ['clientes', 'produtos', 'operadores']) {
        if (!Array.isArray(data[kind]) || data.contagens?.[kind] !== data[kind].length
            || new Set(data[kind].map(item => item?.id)).size !== data[kind].length
            || data[kind].some(item => !item || !id(item.id) || typeof item.nome !== 'string'))
            throw new Error('Catálogo comercial incompleto.');
    }
    if (data.clientes.some(c => c.ativo !== true || !Number.isSafeInteger(c.prazo_padrao_dias) || c.prazo_padrao_dias < 0
        || typeof c.apelido_nome_conhecido !== 'string' || typeof c.telefone !== 'string')
        || data.produtos.some(p => p.ativo !== true
            || ['codigo', 'unidade_base', 'unidade_venda_1', 'unidade_venda_2'].some(field => typeof p[field] !== 'string')
            || ['vende_fracionado', 'estoque_conferido', 'preco_conferido'].some(field => typeof p[field] !== 'boolean')
            || ['fator_conversao', 'preco_venda', 'preco_venda_fracionado', 'custo_referencia',
                'custo_fracionado_referencia', 'estoque_referencia'].some(field => !decimal(p[field])))
        || !Array.isArray(data.formas_pagamento) || data.formas_pagamento.length !== 3
        || data.formas_pagamento.some((p, i) => !p || p.valor !== ['À vista', 'A prazo', 'consumo_proprio'][i] || typeof p.label !== 'string')
        || !Array.isArray(data.revalidar_no_servidor) || revalidate.some(rule => !data.revalidar_no_servidor.includes(rule)))
        throw new Error('Dados comerciais inválidos. O catálogo anterior foi preservado.');
    // Project again before writing: unknown/new server fields must not leak to IDB.
    return {
        ...pick(data, ['tipo', 'schema_version', 'snapshot_id', 'gerado_em', 'origem', 'environment_id', 'device_id']),
        actor: pick(data.actor, ['id', 'name']), somente_referencia: true, revalidar_no_servidor: [...revalidate],
        contagens: pick(data.contagens, ['clientes', 'produtos', 'operadores']),
        clientes: data.clientes.map(c => pick(c, clientFields)), produtos: data.produtos.map(p => pick(p, productFields)),
        operadores: data.operadores.map(o => pick(o, ['id', 'nome'])),
        formas_pagamento: data.formas_pagamento.map(p => pick(p, ['valor', 'label'])),
    };
}

export async function salvarSnapshotComercial(repo, scope, data, incorporated = []) {
    const safe = validated(data, scope);
    const device = await deviceOf(repo);
    if (safe.device_id !== device.id) throw new Error('Snapshot de outro dispositivo.');
    const next = {...safe, key: chaveSnapshotComercial(scope), prepared_at: new Date().toISOString()};
    // One commit replaces the complete catalog; abort/quota errors leave the old one intact.
    return repo.transaction(['snapshots','metadata'], true, (tx, done) => {
        const store = tx.objectStore('snapshots');
        store.get(next.key).onsuccess = event => {
            const previous = event.target.result;
            let previousValid = false;
            try { previousValid = previous?.device_id === device.id && !!validated(previous, scope); }
            catch (_) { /* An incompatible local catalog can be replaced online. */ }
            // A slower response from another tab must not replace a newer snapshot.
            if (previousValid && Date.parse(previous.gerado_em) > Date.parse(next.gerado_em)) {
                done(previous); return;
            }
            store.put(next);
            tx.objectStore('metadata').put({key:'sales-stock-baseline:' + next.snapshot_id,
                snapshot_id:next.snapshot_id,incorporated:[...incorporated]});
            done(next);
        };
    });
}

export async function carregarSnapshotComercial(repo, scope) {
    const record = await repo.get('snapshots', chaveSnapshotComercial(scope));
    if (!record) return null;
    const safe = validated(record, scope);
    const device = await repo.get('metadata', 'device');
    if (!device || device.id !== safe.device_id) return null;
    return {...safe, key: record.key, prepared_at: record.prepared_at};
}

export async function obterProdutosSnapshot(repo, scope) {
    return (await carregarSnapshotComercial(repo, scope))?.produtos || [];
}
export async function obterClientesSnapshot(repo, scope) {
    return (await carregarSnapshotComercial(repo, scope))?.clientes || [];
}

export async function atualizarSnapshotComercial(repo, scope) {
    chaveSnapshotComercial(scope);
    if (!navigator.onLine) throw new Error('Conecte-se para atualizar. Dados locais preservados.');
    const device = await deviceOf(repo);
    // Only receipts known BEFORE the stock query are guaranteed incorporated.
    const incorporated = (await repo.all('operations')).filter(op => op.type === 'criar_venda'
        && op.actor_id === scope.actor_id && op.environment_id === scope.environment_id
        && op.status === 'confirmada' && validReceipt(op.server_result,op)).map(op => op.operation_id);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const response = await fetch('/api/offline/snapshot/comercial/?device_id=' + encodeURIComponent(device.id),
            {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
        if (!response.ok || response.redirected || !response.headers.get('Content-Type')?.includes('application/json'))
            throw new Error('Não foi possível atualizar o catálogo. Confira a conexão e a permissão; dados locais preservados.');
        return await salvarSnapshotComercial(repo, scope, await response.json(),incorporated);
    } finally { clearTimeout(timer); }
}
