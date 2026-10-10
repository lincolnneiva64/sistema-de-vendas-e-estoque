import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {webcrypto} from 'node:crypto';

globalThis.crypto ||= webcrypto;
const localValues = new Map();
globalThis.localStorage = {
    getItem: key => localValues.get(key) ?? null,
    setItem: (key, value) => localValues.set(key, String(value)),
    removeItem: key => localValues.delete(key),
};

const coreSource = `
export const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
    : value && typeof value === 'object' ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
    : JSON.stringify(value);
export const commandOf = operation => Object.fromEntries(['operation_id','device_id','actor_id','environment_id','type',
    'schema_version','aggregate_id','payload','created_at','sequence'].map(key => [key,operation[key]]));
export async function hash(value) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(value)));
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2,'0')).join('');
}
export function reserveSequence(store, device) {
    const next = {...device,sequence:device.sequence + 1}; store.put(next); return next;
}
export const saleOperationState = () => ({});
`;
const revisionsSource = `export const prepareRevisionResolution = async () => ({}); export const resolutionFromSnapshot = () => null; export const resolutionObservationKey = () => '';`;
const stockSource = `export const readStock = async () => null; export const validateStock = () => {}; export const stockSignature = () => ''; export const stockBaselineKey = () => '';`;
const commercialSource = `export const chaveSnapshotComercial = () => '';`;
const asDataUrl = source => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
let source = await readFile(new URL('../static/offline/sales-drafts.js', import.meta.url), 'utf8');
for (const [path, stub] of [
    ['/offline/assets/2-8f/core.js',coreSource],
    ['/offline/assets/2-8g-close/sales-revisions.js',revisionsSource],
    ['/offline/assets/2-8g-close/sales-stock.js',stockSource],
    ['/offline/assets/2-8f-fix/commercial.js',commercialSource],
]) source = source.replaceAll(`'${path}'`, `'${asDataUrl(stub)}'`);
const drafts = await import(asDataUrl(source));

const clone = value => value === undefined ? undefined : structuredClone(value);
class MemoryRepository {
    constructor() {
        this.stores = new Map(['metadata','operations','snapshots'].map(name => [name,new Map()]));
        this.tail = Promise.resolve();
    }
    transaction(names, write, work) {
        const run = this.tail.then(() => new Promise((resolve, reject) => {
            const originals = new Map(names.map(name => [name,new Map([...this.stores.get(name)].map(([key,value]) => [key,clone(value)]))]));
            let result, pending = 0, completed = false, aborted = false;
            const tx = {
                error:null,
                abort() {
                    if (aborted) return;
                    aborted = true;
                    for (const [name,store] of originals) thisRepo.stores.set(name,store);
                    queueMicrotask(() => reject(tx.error || new Error('Transaction aborted.')));
                },
                objectStore(name) {
                    const values = thisRepo.stores.get(name);
                    const keyOf = value => name === 'operations' ? value.operation_id : value.key;
                    const request = action => {
                        const req = {};
                        pending++;
                        setTimeout(() => {
                            if (!aborted) {
                                try {
                                    req.result = action(values);
                                    req.onsuccess?.({target:req});
                                } catch (error) {
                                    req.error = error; tx.error = error; tx.abort();
                                    req.onerror?.({target:req});
                                }
                            }
                            pending--;
                            setTimeout(() => {
                                if (!aborted && !completed && pending === 0) {
                                    completed = true;
                                    tx.oncomplete?.();
                                    resolve(result);
                                }
                            },0);
                        },0);
                        return req;
                    };
                    return {
                        get: key => request(store => clone(store.get(key))),
                        getAll: () => request(store => [...store.values()].map(clone)),
                        put: value => request(store => { store.set(keyOf(value),clone(value)); return keyOf(value); }),
                        add: value => request(store => {
                            const key = keyOf(value);
                            if (store.has(key)) throw new Error('Duplicate key.');
                            store.set(key,clone(value)); return key;
                        }),
                        delete: key => request(store => store.delete(key)),
                    };
                },
            };
            const thisRepo = this;
            tx.oncomplete = null;
            tx.onabort = null;
            tx.onerror = null;
            tx.oncomplete = () => resolve(result);
            try { work(tx, value => { result = value; }); } catch (error) { tx.error = error; tx.abort(); }
        }));
        this.tail = run.catch(() => {});
        return run;
    }
    get(store,key) { return this.transaction([store],false,(tx,done) => { tx.objectStore(store).get(key).onsuccess = event => done(event.target.result); }); }
    all(store) { return this.transaction([store],false,(tx,done) => { tx.objectStore(store).getAll().onsuccess = event => done(event.target.result); }); }
    put(store,value) { return this.transaction([store],true,tx => tx.objectStore(store).put(value)); }
}

const identity = {actor_id:'17',environment_id:'isolated-sales-test'};
const deviceId = '11111111-1111-4111-8111-111111111111';
const tabA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const tabB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const item = (n, name = `Produto ${n}`) => ({produto_id:String(n),produto_nome:name,quantidade:'1',
    unidade:'UN',preco_unitario:'2.00',fator:'1',unidade1:'UN',unidade2:'',fracionado:false});
const assembly = (items = [item(101)]) => ({cliente:null,operador:'Operador',tipo_venda:'À vista',
    data_venda:'2026-10-10',data_vencimento:'',lancamento:null,itens:items});
async function setup() {
    const repo = new MemoryRepository();
    await repo.put('metadata',{key:'sales-identity',...identity});
    await repo.put('metadata',{key:'device',id:deviceId,sequence:0});
    return {repo,scopeA:await drafts.draftScope(repo,identity,tabA),scopeB:await drafts.draftScope(repo,identity,tabB)};
}

test('different tabs save separately; finalizing B preserves A and retry does not duplicate', async () => {
    const {repo,scopeA,scopeB} = await setup();
    const savedA = await drafts.saveDraft(repo,scopeA,assembly([item(101),item(102),item(103),item(104)]),0);
    const savedB = await drafts.saveDraft(repo,scopeB,assembly([item(201)]),0);
    assert.notEqual(drafts.draftKey(scopeA),drafts.draftKey(scopeB));
    const first = await drafts.finalizeDraftOffline(repo,scopeB,{revision:savedB.revision,draft_id:savedB.draft.draft_id,transport:'online'});
    const retry = await drafts.finalizeDraftOffline(repo,scopeB,{revision:savedB.revision,draft_id:savedB.draft.draft_id,transport:'online'});
    assert.equal(first.finalization.draft_id,savedB.draft.draft_id);
    assert.equal(retry.alreadyFinalized,true);
    assert.equal((await drafts.loadDraft(repo,scopeA)).draft.itens.length,4);
    assert.equal((await drafts.loadDraft(repo,scopeA)).draft.draft_id,savedA.draft.draft_id);
    assert.equal((await repo.all('operations')).length,1);
});

test('same draft rejects stale concurrent revisions', async () => {
    const {repo,scopeA} = await setup();
    const saved = await drafts.saveDraft(repo,scopeA,assembly(),0);
    const results = await Promise.allSettled([
        drafts.saveDraft(repo,scopeA,{...assembly(),operador:'A'},saved.revision),
        drafts.saveDraft(repo,scopeA,{...assembly(),operador:'B'},saved.revision),
    ]);
    assert.equal(results.filter(result => result.status === 'fulfilled').length,1);
    assert.equal(results.filter(result => result.status === 'rejected').length,1);
});

test('legacy draft remains intact and can be claimed only once', async () => {
    const {repo,scopeA,scopeB} = await setup();
    const legacyScope = await drafts.draftScope(repo,identity);
    const original = await drafts.saveDraft(repo,legacyScope,assembly([item(301),item(302),item(303),item(304)]),0);
    const candidate = (await drafts.listRecoverableDrafts(repo,scopeA)).find(entry => entry.key === drafts.legacyDraftKey(scopeA));
    assert.ok(candidate && !candidate.blocked);
    const recovered = await drafts.recoverDraft(repo,scopeA,candidate);
    assert.equal(recovered.source_preserved,true);
    assert.equal((await repo.get('metadata',candidate.key)).draft_id,original.draft.draft_id);
    assert.equal((await drafts.loadDraft(repo,scopeA)).draft.itens.length,4);
    assert.equal((await drafts.listRecoverableDrafts(repo,scopeB)).some(entry => entry.key === candidate.key),false);
    await assert.rejects(() => drafts.recoverDraft(repo,scopeB,candidate),/mudou desde a listagem|recuperado em outra aba/);
});

test('pending blank-search editor survives persistence and blocks finalization specifically', async () => {
    const {repo,scopeA} = await setup();
    const pending = {...assembly([item(401),item(402),item(403),item(404)]),
        lancamento:{produto_id:'499',produto_nome:'Produto pendente',quantidade:'',unidade:'UN',preco:'2.00',
            fator:'1',unidade1:'UN',unidade2:'',fracionado:false,indice_edicao:null}};
    const saved = await drafts.saveDraft(repo,scopeA,pending,0);
    assert.equal((await drafts.loadDraft(repo,scopeA)).draft.lancamento.produto_nome,'Produto pendente');
    await assert.rejects(
        () => drafts.finalizeDraftOffline(repo,scopeA,{revision:saved.revision,draft_id:saved.draft.draft_id,transport:'online'}),
        /Informe a quantidade da linha pendente: Produto pendente/,
    );
    assert.equal((await repo.all('operations')).length,0);
});

test('edited line data and existing items survive projection', async () => {
    const edited = {...assembly([item(501),item(502),item(503),item(504)]),
        lancamento:{produto_id:'501',produto_nome:'Produto 501',quantidade:'3',unidade:'UN',preco:'4.50',
            fator:'1',unidade1:'UN',unidade2:'',fracionado:false,indice_edicao:0}};
    const projected = drafts.projectAssembly(edited);
    assert.equal(projected.itens.length,4);
    assert.equal(projected.lancamento.quantidade,'3');
    assert.equal(projected.lancamento.preco,'4.50');
    assert.equal(projected.lancamento.indice_edicao,0);
});
