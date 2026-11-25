"""Testes direcionados do JavaScript real em memória, sem servidor ou banco."""
from pathlib import Path
import json
import re
import subprocess
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase
from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parents[1]


class SuspensionJavaScriptTests(SimpleTestCase):
    def evaluate(self, code):
        original = subprocess.Popen
        def launch(args, **kwargs):
            return original([args[0], '--no-sandbox', *args[1:]], **kwargs)
        with tempfile.TemporaryDirectory(prefix='teste-suspensao-js-') as profile, patch('subprocess.Popen', launch):
            chrome = Chrome('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', profile).start()
            try:
                return chrome.tab().evaluate('(async()=>{' + code + '})()')
            finally:
                chrome.stop()

    def test_envio_online_sem_fila_e_resposta_perdida(self):
        core = (ROOT / 'static/offline/core.js').read_text(encoding='utf-8').replace('export ', '')
        module = re.sub(r'^import .*?;\s*', '', (ROOT / 'static/offline/online-only.js').read_text(encoding='utf-8'), flags=re.M)
        fixture = r'''
const node=()=>({dataset:{},style:{},children:[],append(...x){this.children.push(...x)},prepend(...x){this.children.unshift(...x)},setAttribute(){},disabled:false});
const nodes={'offline-global':node(),'offline-status':node(),'layout-vendas':node(),'btnGravarVenda':node()};
nodes['offline-global'].dataset={environment:'test-env',actor:''};
const document={cookie:'csrftoken=test',getElementById:id=>nodes[id]||null,createElement:node,addEventListener(){}};
const store=new Map([['legacy-offline-sentinel','preserve']]);
let uuid=0;const crypto={randomUUID:()=>('00000000-0000-4000-8000-'+String(++uuid).padStart(12,'0')),subtle:{digest:async()=>new Uint8Array(32).buffer}};
const sessionStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
const sale={operador:'Teste',data_venda:'2026-11-25',tipo_pagamento:'A prazo',cliente_id:'41',itens:[{produto_id:'544',quantidade:'0.5',unidade:'CT',preco_unitario:'72.00'}]};
let network=true,loseResponse=false,posts=0,lastCommand=null;
const window={localSalesContext:{owner:'test-owner',actor_id:'local:test-owner',revision:''},salesOffline:{active:false},addEventListener(){},salesDraftBridge:{eligible:true,getData:()=>structuredClone(sale),error(){}},salesDraftResolve(){}};
const navigator={serviceWorker:{register:async()=>({})},get indexedDB(){throw Error('IndexedDB forbidden')}};
const indexedDB={open(){throw Error('IndexedDB forbidden')}};
const location={pathname:'/vendas/',search:'',assign(){}};
const setInterval=()=>0,ensureSaleCosts=async()=>{};
const reply=(data,status=200)=>({ok:status===200,status,json:async()=>data});
async function fetch(url,options={}){
 if(url==='/api/offline/health/')return reply({ok:network,environment:'test-env',protocol_version:1},network?200:503);
 if(url==='/api/offline/session/')return reply({authenticated:false,local_sales_owner:'test-owner',environment_id:'test-env'});
 if(options.method==='POST'){
  posts++;lastCommand=JSON.parse(options.body);
  if(options.headers['X-Online-Only']!=='1')throw Error('missing mode header');
  if(loseResponse)throw Error('lost response');
  return reply({sucesso:true,venda_id:123,receipt:{operation_id:lastCommand.operation_id,hash:lastCommand.payload_hash,status:'confirmada',record_id:123,completed_at:new Date().toISOString()}});
 }
 if(url.startsWith('/api/vendas/online/'))return reply({lookup:'encontrada',receipt:{operation_id:lastCommand.operation_id,hash:lastCommand.payload_hash,status:'confirmada',record_id:123,completed_at:new Date().toISOString()}});
 throw Error('unexpected request '+url);
}
'''
        tests = r'''
await probe();
network=false;let denied=false;try{await window.onlineOnlySales.submit(sale)}catch(_){denied=true}
if(!denied||posts!==0||window.onlineOnlySales.pending)throw Error('offline submission allowed');
network=true;await probe();if(!connected)throw Error('recovery waits for stability');
const result=await window.onlineOnlySales.submit(sale);
if(!result.sucesso||posts!==1||(lastCommand.payload.dados||lastCommand.payload).itens[0].quantidade!=='0.5')throw Error('online send failed');
window.onlineOnlySales.afterSuccess();
loseResponse=true;try{await window.onlineOnlySales.submit(sale)}catch(_){}
if(!window.onlineOnlySales.pending||posts!==2)throw Error('unknown response unprotected');
try{await window.onlineOnlySales.submit(sale)}catch(_){}
if(posts!==2)throw Error('automatic repeat');
await nodes['layout-vendas'].children[0].children[1].onclick();
if(posts!==2||window.onlineOnlySales.pending.receipt.status!=='confirmada')throw Error('lookup failed or resent');
if(store.get('legacy-offline-sentinel')!=='preserve')throw Error('legacy storage modified');
return true;
'''
        for local in (True, False):
            with self.subTest(local=local):
                current = fixture
                if not local:
                    current = current.replace("actor:''", "actor:'1'")
                    current = current.replace("localSalesContext:{owner:'test-owner',actor_id:'local:test-owner',revision:''}", 'localSalesContext:null')
                    current = current.replace("authenticated:false,local_sales_owner:'test-owner'", "authenticated:true,actor_id:'1',local_sales_owner:null")
                self.assertTrue(self.evaluate(current + core + module + tests))

    def test_worker_suspenso_nao_usa_nem_apaga_caches(self):
        worker = (ROOT / 'static/offline/service-worker.js').read_text(encoding='utf-8')
        fixture = r'''
const handlers={},self={OFFLINE_SUSPENDED:true,location:{origin:'http://localhost'},addEventListener:(name,fn)=>handlers[name]=fn,skipWaiting:async()=>{},clients:{claim:async()=>{}}};
const caches={open(){throw Error('cache opened')},keys(){throw Error('cache enumerated')},delete(){throw Error('cache deleted')},match(){throw Error('cached shell used')}};
const fetch=async()=>{throw Error('network unavailable')};
'''
        tests = r'''
let work;
handlers.install({waitUntil:p=>work=p});await work;
handlers.activate({waitUntil:p=>work=p});await work;
// Native network handling: no respondWith, fetch wrapper or fallback response.
work=null;
handlers.fetch({request:{url:'http://localhost/vendas/',method:'GET',mode:'navigate'},respondWith:p=>work=p});
if(work!==null)throw Error('navigation intercepted');
return true;
'''
        self.assertTrue(self.evaluate(fixture + worker + tests))

    def test_worker_antigo_reproduz_falha_e_atualizado_libera_links(self):
        antigo = subprocess.check_output(['git','show','5c1078b:static/offline/service-worker.js'], cwd=ROOT).decode('utf-8')
        novo = (ROOT / 'static/offline/service-worker.js').read_text(encoding='utf-8')
        fixture = r'''
const handlers={},self={OFFLINE_SUSPENDED:true,location:{origin:'http://localhost'},addEventListener:(name,fn)=>handlers[name]=fn};
const caches={open(){throw Error('cache touched')},match(){throw Error('cached shell used')},delete(){throw Error('cache deleted')}};
const fetch=async()=>{throw Error('temporary fetch failure')};
'''
        tests = r'''
let response;
handlers.fetch({request:{url:'http://localhost/vendas/',method:'GET',mode:'navigate'},respondWith:p=>response=p});
const old=await response;
if(old.status!==503||!(await old.text()).includes('Nenhuma grava'))throw Error('old regression not reproduced');
'''
        atualizado = r'''
for(const [path,method] of [['/vendas/?editar=1496','GET'],['/locacoes/checklist-operacional/','GET'],
 ['/locacoes/recolhimento-individual/assinatura/?retomar=1&token=abc','GET'],
 ['/locacoes/tarefas-operacionais/1/conferencia-recolhimento/?token=abc','GET'],
 ['/entregas/1/checklist/?token=abc','GET'],['/api/offline/health/','GET'],
 ['/api/vendas/online/','POST']]){
 response=null;
 handlers.fetch({request:{url:'http://localhost'+path,method,mode:method==='GET'?'navigate':'cors'},respondWith:p=>response=p});
 if(response!==null)throw Error('native request intercepted: '+path);
}
return true;
'''
        code = fixture + '\nnew Function("self","caches","fetch",' + json.dumps(antigo) + ')(self,caches,fetch);'
        code += tests + '\nnew Function("self","caches","fetch",' + json.dumps(novo) + ')(self,caches,fetch);' + atualizado
        self.assertTrue(self.evaluate(code))

    def test_timeout_do_servidor_nao_rotula_internet_como_offline(self):
        core = (ROOT / 'static/offline/core.js').read_text(encoding='utf-8').replace('export ', '')
        module = re.sub(r'^import .*?;\s*', '', (ROOT / 'static/offline/online-only.js').read_text(encoding='utf-8'), flags=re.M)
        fixture = r'''
const listeners={},timeouts=[],nodes={'offline-global':{dataset:{environment:'test-env'}},'offline-status':{}};
const document={getElementById:id=>nodes[id]||null,addEventListener(){}};
const window={addEventListener:(event,fn)=>listeners[event]=fn};
const navigator={onLine:true};const setInterval=()=>0;
const setTimeout=(fn,ms)=>{timeouts.push(ms);return 1},clearTimeout=()=>{};
let available=false;
const fetch=async()=>({ok:available,json:async()=>({ok:available,environment:'test-env',protocol_version:1})});
'''
        tests = r'''
await probe();
if(nodes['offline-status'].textContent==='Offline'||connected)throw Error('server failure misclassified as offline');
if(timeouts.some(ms=>ms!==15000))throw Error('health still uses short timeout');
navigator.onLine=false;listeners.offline();
if(nodes['offline-status'].textContent!=='Offline')throw Error('real offline not indicated');
navigator.onLine=true;available=true;await listeners.online();
if(nodes['offline-status'].textContent!=='Online'||!connected)throw Error('reconnection did not recover immediately');
return true;
'''
        self.assertTrue(self.evaluate(fixture + core + module + tests))
