"""Testes direcionados do JavaScript real em memória, sem servidor ou banco."""
from pathlib import Path
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
handlers.fetch({request:{url:'http://localhost/vendas/',method:'GET',mode:'navigate'},respondWith:p=>work=p});
const response=await work;if(response.status!==503)throw Error('offline form returned');
return true;
'''
        self.assertTrue(self.evaluate(fixture + worker + tests))
