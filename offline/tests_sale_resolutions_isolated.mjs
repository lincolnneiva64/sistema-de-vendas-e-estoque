// Run with node offline/tests_sale_resolutions_isolated.mjs. No network or storage.
import {readFile} from 'node:fs/promises';

export async function validateResolutionFlow(source) {
    const assert=(condition,message)=>{if(!condition)throw Error(message);};
    const copy=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
    const canonical=value=>JSON.stringify(value);
    const keys=['operation_id','aggregate_id','device_id','actor_id','environment_id','type','schema_version','sequence','created_at','payload'];
    const commandOf=op=>Object.fromEntries(keys.map(key=>[key,op[key]]));
    const hash=async value=>canonical(value); // Protocol hashing is covered by Django tests.
    const stripped=source.replace(/^import .*;$/gm,'').replace(/^export /gm,'')
        .replace(/const closures=\(\)=>import\([^;]+;/,'const closures=async()=>closureModule;');
    const factory=new Function('commandOf','hash','canonical','closureModule','fetch','navigator','document',
        'BroadcastChannel','crypto','AbortController','setTimeout','clearTimeout','URLSearchParams',
        stripped+'\nreturn {resolvePending,consultResolution,resolutionPending,resolutionIntentKey};');
    let passed=0;
    async function setup() {
        const scope={actor_id:'1',environment_id:'isolated',device_id:'device'};
        const op={operation_id:'original',aggregate_id:'original',device_id:'device',actor_id:'1',
            environment_id:'isolated',type:'criar_venda',schema_version:1,sequence:1,created_at:'2026-11-11T12:00:00Z',
            payload:{itens:[]},status:'conflito',attempts:1};
        op.payload_hash=await hash(commandOf(op));
        const maps={operations:new Map([[op.operation_id,copy(op)]]),metadata:new Map()};
        const repo={get:async(store,key)=>copy(maps[store].get(key)),all:async store=>copy([...maps[store].values()]),
            put:async(store,value)=>{if(repo.failMirror && value.receipt)throw Error('local failure');maps[store].set(value.key||value.operation_id,copy(value));}};
        const server=new Map();
        const scenario={posts:[],gets:[],busy:false,unavailable:false,lost:false,malformed:false,official:false};
        const closedLocally=(meta,row)=>meta.some(m=>m.key==='closed:'+row.operation_id && m.receipt?.operation_id===row.operation_id);
        const closureModule={closedLocally,closureKey:row=>'closed:'+row.operation_id,
            validClosure:(receipt,row)=>receipt?.operation_id===row.operation_id && receipt.hash===row.payload_hash && receipt.status==='encerrada_sem_venda',
            mirrorClosure:async(repo,scope,row,receipt)=>repo.put('metadata',{key:'closed:'+row.operation_id,receipt})};
        const fetch=async(url,options)=>{
            const id=url.split('/operations/')[1].split('/')[0];
            if(scenario.unavailable)return {ok:false,json:async()=>({erro:'indeterminate'})};
            if(options.method==='POST') {
                const body=JSON.parse(options.body);scenario.posts.push(copy(body));
                if(!server.has(id))server.set(id,{lookup:'encontrada',operation_id:id,hash:body.payload_hash,
                    closure:{operation_id:id,hash:body.payload_hash,status:'encerrada_sem_venda'}});
                if(scenario.lost)throw Error('lost response');
            }else scenario.gets.push(id);
            const row=maps.operations.get(id);
            let state=server.get(id)||{operation_id:id,hash:row.payload_hash,lookup:scenario.official?'oficial':'nao_encontrada',record_id:42};
            if(scenario.malformed)state={...state,closure:{status:'encerrada_sem_venda'}};
            return {ok:true,json:async()=>copy(state)};
        };
        class Channel{postMessage(){}close(){}}
        class Controller{abort(){}signal={}}
        class Params{constructor(data){this.data=data;}toString(){return '';}}
        const api=factory(commandOf,hash,canonical,closureModule,fetch,{locks:{request:async(name,options,work)=>work(scenario.busy?null:{})}},
            {cookie:'csrftoken=fake',dispatchEvent(){},addEventListener(){}},Channel,{randomUUID:()=>String(scenario.posts.length+1)},
            Controller,()=>1,()=>{},Params);
        return {scope,op,repo,api,maps,scenario,closedLocally,server};
    }
    async function test(name,work){await work(await setup());passed++;}
    await test('absence is not acknowledgement; reservation is idempotent',async({api,repo,scope,op,scenario,closedLocally})=>{
        assert((await api.consultResolution(repo,scope,op)).lookup==='nao_encontrada','absence');
        assert(!closedLocally(await repo.all('metadata'),op),'false closure');
        await api.resolvePending(repo,scope,op,'Reason',true);
        await api.resolvePending(repo,scope,op,'Other reason',true);
        assert(scenario.posts.length===1,'duplicate decision');
        assert(closedLocally(await repo.all('metadata'),op),'missing mirror');
    });
    await test('independent unknown is preserved',async({api,repo,scope,op,maps})=>{
        const other={...op,operation_id:'other',status:'resultado_desconhecido'};
        maps.operations.set('other',copy(other));
        await api.resolvePending(repo,scope,op,'Reason',true);
        assert(canonical(await repo.get('operations','other'))===canonical(other),'independent mutated');
        assert(canonical(await repo.get('operations',op.operation_id))===canonical(op),'original mutated');
    });
    await test('child before parent',async({api,repo,scope,op,maps,scenario})=>{
        const child={...op,operation_id:'child',aggregate_id:'child',status:'pendente',attempts:0,payload:{itens:[],revisao:{original_operation_id:op.operation_id}}};
        child.payload_hash=await hash(commandOf(child));maps.operations.set('child',copy(child));
        assert((await api.resolvePending(repo,scope,op,'Reason',true)).lookup==='substituta','dependency ignored');
        assert(scenario.posts.length===0,'parent closed too soon');
        await api.resolvePending(repo,scope,child,'Reason',true);
        await api.resolvePending(repo,scope,op,'Reason',true);
        assert(scenario.posts.length===2,'chain incomplete');
    });
    await test('unknown replacement is resolved by server reservation',async({api,repo,scope,op,maps,scenario,closedLocally})=>{
        const child={...op,operation_id:'unknown-child',aggregate_id:'unknown-child',status:'resultado_desconhecido',
            payload:{itens:[],revisao:{original_operation_id:op.operation_id}}};
        child.payload_hash=await hash(commandOf(child));maps.operations.set(child.operation_id,copy(child));
        assert((await api.resolvePending(repo,scope,op,'Reason',true)).lookup==='substituta','unknown dependency lost');
        await api.resolvePending(repo,scope,child,'Reason',true);
        assert(closedLocally(await repo.all('metadata'),child),'no administrative confirmation');
        assert(scenario.posts.length===1,'unexpected sends');
    });
    await test('lost response is recovered without another POST',async({api,repo,scope,op,scenario,closedLocally})=>{
        scenario.lost=true;
        try{await api.resolvePending(repo,scope,op,'Reason',true);}catch{}
        assert(!closedLocally(await repo.all('metadata'),op),'assumed success');
        await api.consultResolution(repo,scope,op);
        assert(closedLocally(await repo.all('metadata'),op),'not recovered');
        assert(scenario.posts.length===1,'commercial retry');
    });
    await test('unavailable query preserves operation',async({api,repo,scope,op,scenario,closedLocally})=>{
        scenario.unavailable=true;
        try{await api.resolvePending(repo,scope,op,'Reason',true);}catch{}
        assert(scenario.posts.length===0 && !closedLocally(await repo.all('metadata'),op),'unsafe decision');
    });
    await test('official sale prevents closure',async({api,repo,scope,op,scenario})=>{
        scenario.official=true;
        assert((await api.resolvePending(repo,scope,op,'Reason',true)).lookup==='oficial','official lost');
        assert(scenario.posts.length===0,'official closed');
    });
    await test('another tab holds lock',async({api,repo,scope,op,scenario})=>{
        scenario.busy=true;let rejected=false;
        try{await api.resolvePending(repo,scope,op,'Reason',true);}catch{rejected=true;}
        assert(rejected && scenario.posts.length===0,'lock bypassed');
    });
    await test('malformed acknowledgement is refused',async({api,repo,scope,op,scenario,closedLocally})=>{
        scenario.malformed=true;let rejected=false;
        try{await api.resolvePending(repo,scope,op,'Reason',true);}catch{rejected=true;}
        assert(rejected && !closedLocally(await repo.all('metadata'),op),'invalid ack accepted');
    });
    await test('local persistence failure can recover',async({api,repo,scope,op,closedLocally})=>{
        repo.failMirror=true;
        try{await api.resolvePending(repo,scope,op,'Reason',true);}catch{}
        assert(!closedLocally(await repo.all('metadata'),op),'local failure hidden');
        repo.failMirror=false;await api.consultResolution(repo,scope,op);
        assert(closedLocally(await repo.all('metadata'),op),'recovery failed');
    });
    await test('explicit confirmation is mandatory',async({api,repo,scope,op,scenario})=>{
        let rejected=false;try{await api.resolvePending(repo,scope,op,'Reason',false);}catch{rejected=true;}
        assert(rejected && scenario.posts.length===0,'confirmation bypassed');
    });
    return passed;
}

const source=await readFile(new URL('../static/offline/sales-resolutions.js',import.meta.url),'utf8');
console.log('Resolution flow tests passed:',await validateResolutionFlow(source));
