import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from estoque.models import ContaReceber, MovimentoFinanceiro, Produto, Venda
from .browser_support import Chrome
from .models import OperacaoSincronizacao
from .tests_commercial import commercial_fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class CommercialSnapshotBrowserTests(StaticLiveServerTestCase):
    def test_manual_update_read_offline_scope_failures_and_sales_layout(self):
        user, customer, product, _, *_ = commercial_fixtures()
        client = Client()
        client.force_login(user)
        before = {model.__name__: list(model.objects.order_by('pk').values())
                  for model in [Produto, Venda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]}
        with TemporaryDirectory(prefix='commercial-snapshot-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('!!document.querySelector(".offline-commercial button")')
                tab.evaluate("""window.core=await import('/offline/assets/2-8f/core.js');
                    window.commercial=await import('/offline/assets/2-8f/commercial.js');
                    window.repo=new core.Repository(await core.openDB());
                    window.scope={actor_id:document.getElementById('offline-global').dataset.actor,
                        environment_id:document.getElementById('offline-global').dataset.environment};
                    document.querySelector('.offline-disclosure').open=true;true""")
                self.assertIsNone(tab.evaluate('await commercial.carregarSnapshotComercial(repo,scope)'))
                tab.evaluate("""window.snapshotFetches=0;window.realFetch=fetch;
                    window.fetch=(url,options)=>{if(String(url).includes('/snapshot/comercial/')) snapshotFetches++;
                        return realFetch(url,options)};
                    document.querySelector('.offline-commercial button').click();
                    document.querySelector('.offline-commercial button').click();true""")
                tab.wait("document.querySelector('.offline-commercial [role=status]').textContent.includes('Catálogo salvo') && !document.querySelector('.offline-commercial button').disabled")
                self.assertEqual(tab.evaluate('snapshotFetches'), 1)
                tab.evaluate('window.saved=await commercial.carregarSnapshotComercial(repo,scope);true')
                saved = tab.evaluate('saved')
                self.assertEqual(saved['contagens'], {'clientes': 1, 'produtos': 2, 'operadores': 1})
                self.assertEqual(saved['clientes'][0]['id'], str(customer.pk))
                self.assertIn(str(product.pk), [p['id'] for p in saved['produtos']])
                self.assertEqual(tab.evaluate('repo.db.name'), 'vendas-offline-pilot')
                self.assertEqual(tab.evaluate('repo.db.version'), 1)
                self.assertEqual(tab.evaluate('[...repo.db.objectStoreNames].sort()'), ['history', 'metadata', 'operations', 'snapshots'])
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("history")).length'), 0)
                # Existing task snapshots are untouched by a commercial replacement.
                tab.evaluate('await repo.put("snapshots",{key:"pilot",tasks:[{id:"existing-task"}]});true')
                for width in [1280, 390]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width==390))
                    self.assertTrue(tab.evaluate('(()=>{let box=document.querySelector(".offline-commercial");let r=box.getBoundingClientRect();return r.width>0 && r.left>=0 && r.right<=innerWidth && box.scrollWidth<=box.clientWidth+1})()'))
                    self.assertTrue(tab.evaluate('!!document.getElementById("clienteBusca") && !!document.getElementById("produto") && !!document.getElementById("tipoVenda") && !!document.getElementById("operadorVenda")'))
                # Reject responses/failures without overwriting a valid catalog.
                failure_cases = [
                    "Promise.reject(new Error('sem rede'))",
                    "Promise.resolve(new Response('{}',{status:503,headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response('{}',{status:401,headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response('<html>login</html>',{status:200,headers:{'Content-Type':'text/html'}}))",
                    "Promise.resolve(new Response('not json',{status:200,headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response(JSON.stringify({...saved,schema_version:2}),{headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response(JSON.stringify({...saved,actor:{id:'999999',name:'outro'}}),{headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response(JSON.stringify({...saved,environment_id:'outro'}),{headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response(JSON.stringify({...saved,device_id:crypto.randomUUID()}),{headers:{'Content-Type':'application/json'}}))",
                    "Promise.resolve(new Response(JSON.stringify({...saved,contagens:{clientes:99,produtos:2,operadores:1}}),{headers:{'Content-Type':'application/json'}}))",
                ]
                for failure in failure_cases:
                    with self.subTest(failure=failure):
                        self.assertTrue(tab.evaluate("window.fetch=()=>" + failure + ";try{await commercial.atualizarSnapshotComercial(repo,scope);false}catch(e){true}"))
                        self.assertEqual(tab.evaluate('(await commercial.carregarSnapshotComercial(repo,scope)).snapshot_id'), saved['snapshot_id'])
                tab.evaluate('window.fetch=realFetch;true')
                # Allowlist strips unexpected sensitive fields before IDB.
                tab.evaluate("""window.extra=structuredClone(saved);extra.snapshot_id=crypto.randomUUID();
                    extra.clientes[0].limite_credito='secret';extra.financial='secret';
                    await commercial.salvarSnapshotComercial(repo,scope,extra);true""")
                self.assertFalse(tab.evaluate('JSON.stringify(await commercial.carregarSnapshotComercial(repo,scope)).includes("secret")'))
                # Abort a real IndexedDB write: keep the previously committed snapshot.
                tab.evaluate("""window.last=await commercial.carregarSnapshotComercial(repo,scope);
                    window.originalTransaction=repo.transaction;
                    repo.transaction=function(stores,write,work){return originalTransaction.call(this,stores,write,
                        (tx,done)=>{work(tx,done);if(write&&stores.includes('snapshots'))tx.abort()})};true""")
                self.assertTrue(tab.evaluate('try{await commercial.salvarSnapshotComercial(repo,scope,saved);false}catch(e){true}'))
                tab.evaluate('repo.transaction=originalTransaction;true')
                self.assertEqual(tab.evaluate('(await commercial.carregarSnapshotComercial(repo,scope)).snapshot_id'), tab.evaluate('last.snapshot_id'))
                # Concurrent older response cannot replace a newer commit.
                tab.evaluate("""window.old=structuredClone(last);old.snapshot_id=crypto.randomUUID();old.gerado_em='2000-01-01T00:00:00Z';
                    await commercial.salvarSnapshotComercial(repo,scope,old);true""")
                self.assertEqual(tab.evaluate('(await commercial.carregarSnapshotComercial(repo,scope)).snapshot_id'), tab.evaluate('last.snapshot_id'))
                self.assertIsNone(tab.evaluate('await commercial.carregarSnapshotComercial(repo,{...scope,actor_id:"999999"})'))
                self.assertIsNone(tab.evaluate('await commercial.carregarSnapshotComercial(repo,{...scope,environment_id:"outro"})'))
                self.assertEqual(tab.evaluate('(await repo.get("snapshots","pilot")).tasks[0].id'), 'existing-task')
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.all("history")).length'), 0)
                # The SW stores modules, never the commercial API response or sales page.
                tab.evaluate('await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                self.assertFalse(tab.evaluate("!!(await caches.match('/vendas/'))"))
                self.assertFalse(tab.evaluate("!!(await caches.match('/api/offline/snapshot/comercial/'))"))
                tab.call('Network.emulateNetworkConditions', dict(offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))
                self.assertEqual(tab.evaluate('(await commercial.obterClientesSnapshot(repo,scope)).length'), 1)
                self.assertEqual(tab.evaluate('(await commercial.obterProdutosSnapshot(repo,scope)).length'), 2)
                self.assertTrue(tab.evaluate('try{await commercial.atualizarSnapshotComercial(repo,scope);false}catch(e){true}'))
                self.assertEqual(tab.evaluate('(await commercial.carregarSnapshotComercial(repo,scope)).snapshot_id'), tab.evaluate('last.snapshot_id'))
                # Fresh document imports cached modules and reads IDB with the network off.
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                tab.wait('!!document.getElementById("offline-pilot")')
                self.assertEqual(tab.evaluate("""const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');
                    const {obterProdutosSnapshot}=await import('/offline/assets/2-8f/commercial.js');
                    (await obterProdutosSnapshot(new Repository(await openDB()),{actor_id:""" + repr(str(user.pk)) + """,environment_id:'offline-isolated-tests'})).length"""), 2)
            finally:
                chrome.stop()
        after = {model.__name__: list(model.objects.order_by('pk').values())
                 for model in [Produto, Venda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]}
        self.assertEqual(after, before)
