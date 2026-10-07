import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from estoque.models import Produto, Venda, ItemVenda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from .tests_commercial import commercial_fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class OfflineSalesBrowserTests(StaticLiveServerTestCase):
    reset_sequences = True
    def test_offline_assembly_safe_shell_and_no_mutations(self):
        user, customer, product, operator, *_ = commercial_fixtures()
        product.quantidade = 0
        product.save(update_fields=['quantidade'])
        client = Client()
        client.force_login(user)
        models = [Produto, Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]
        before = {m.__name__: list(m.objects.order_by('pk').values()) for m in models}
        shell = client.get('/offline/vendas-shell/').content.decode()
        for secret in [customer.nome, product.nome, operator.nome, user.username, 'segredo-financeiro']:
            self.assertFalse(secret in shell, secret)
        self.assertNotRegex(shell, r'<input[^>]*name="csrfmiddlewaretoken"')
        with TemporaryDirectory(prefix='offline-sales-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid', 'value':client.cookies['sessionid'].value, 'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
                tab.wait('!!document.querySelector(".offline-commercial button") && !!salesOffline.activate')
                self.assertFalse(tab.evaluate('salesOffline.active'))
                self.assertTrue(tab.evaluate('!!document.querySelector("#produto option[data-produto-id]")'))
                tab.evaluate('document.querySelector(".offline-commercial button").click();true')
                tab.wait("document.querySelector('.offline-commercial [role=status]').textContent.includes('Catálogo salvo')")
                tab.wait('!document.querySelector(".offline-commercial button").disabled')
                tab.evaluate('window.originalFetch=fetch;window.fetch=(url,options)=>String(url).includes("/clientes/autocomplete/") ? Promise.reject(new TypeError("Failed to fetch")) : originalFetch(url,options);clienteBusca.value="termo-sem-cache";clienteBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('salesOffline.active && salesOffline.available')
                self.assertTrue(tab.evaluate('navigator.onLine'))
                tab.evaluate('window.fetch=originalFetch;true')
                tab.wait('!!navigator.serviceWorker.controller')
                worker = chrome.worker()
                worker.call('Network.enable')
                worker.call('Network.emulateNetworkConditions', dict(offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))
                tab.call('Network.emulateNetworkConditions', dict(offline=True, latency=0, downloadThroughput=0, uploadThroughput=0))
                tab.wait('salesOffline.active && document.getElementById("btnGravarVenda").textContent==="Salvar venda offline"')
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('window.salesOffline?.shell && !!window.salesOffline?.clients && !!document.querySelector("#produto option[data-produto-id]")')
                self.assertTrue(tab.evaluate('document.getElementById("sales-offline-notice").textContent.includes("Modo offline")'))
                tab.evaluate('clienteBusca.value="Mercadinho";clienteBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('!!document.querySelector(".cliente-sugestao-item")')
                tab.evaluate('document.querySelector(".cliente-sugestao-item").click();true')
                self.assertEqual(tab.evaluate('clienteId.value'), str(customer.pk))
                self.assertEqual(tab.evaluate('tipoVenda.value'), 'A prazo')
                tab.evaluate('operadorVenda.value="Operador Comercial";operadorVenda.dispatchEvent(new Event("change",{bubbles:true}));produtoBusca.value="P-01";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('!!document.querySelector(".produto-sugestao-item")')
                tab.evaluate('document.querySelector(".produto-sugestao-item").click();true')
                self.assertEqual(tab.evaluate('unidade.value'), 'CX')
                self.assertEqual(tab.evaluate('Number(preco.value)'), 24)
                self.assertEqual(tab.evaluate('document.querySelector("#produto option:checked").dataset.fator'), '12.00')
                self.assertIn('referência', tab.evaluate('estoquePreview.textContent'))
                tab.evaluate('unidade.value="UN";unidade.dispatchEvent(new Event("change",{bubbles:true}));true')
                self.assertEqual(tab.evaluate('Number(preco.value)'), 3)
                tab.evaluate('unidade.value="CX";unidade.dispatchEvent(new Event("change",{bubbles:true}));true')
                tab.evaluate('quantidade.value="10";document.getElementById("btnAdicionarItemVenda").click();true')
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===1')
                self.assertIn('240', tab.evaluate('totalGeral.textContent'))
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();quantidade.value="5";document.getElementById("btnAdicionarItemVenda").click();true')
                tab.wait('totalGeral.textContent.includes("120")')
                for width in [1280, 390]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=850,deviceScaleFactor=1,mobile=width==390))
                    self.assertTrue(tab.evaluate('(()=>{const r=document.getElementById("sales-offline-notice").getBoundingClientRect();return r.width>0 && r.right<=innerWidth+1})()'))
                self.assertEqual(tab.evaluate('tipoVenda.options.length'), 4)
                self.assertTrue(tab.evaluate('!btnGravarVenda.disabled && btnGravarVenda.textContent==="Salvar venda offline"'))
                tab.evaluate('document.querySelector("#tabelaProdutos tr").click();document.dispatchEvent(new KeyboardEvent("keydown",{key:"Delete",bubbles:true}));true')
                tab.wait('!!document.getElementById("btn-confirmar-excluir")')
                tab.evaluate('document.getElementById("btn-confirmar-excluir").click();true')
                tab.wait('document.querySelectorAll("#tabelaProdutos tr").length===0')
                tab.evaluate("window.repo=new (await import('/static/offline/core.js')).Repository(await (await import('/static/offline/core.js')).openDB());true")
                self.assertEqual(tab.evaluate('(await repo.all("operations")).length'), 0)
                self.assertEqual(tab.evaluate('(await repo.get("metadata","device")).sequence'), 0)
                tab.evaluate('window.oldCatalog=(await repo.all("snapshots")).find(s=>s.tipo==="comercial_vendas");oldCatalog.prepared_at="2000-01-01T12:00:00Z";await repo.put("snapshots",oldCatalog);true')
                tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
                tab.wait('!!document.getElementById("sales-offline-notice") && document.getElementById("sales-offline-notice").textContent.includes("2000")')
                self.assertTrue(tab.evaluate('salesOffline.available'))
                tab.evaluate("window.repo=new (await import('/static/offline/core.js')).Repository(await (await import('/static/offline/core.js')).openDB());true")
                # A fresh shell must never choose another actor/environment's catalog.
                for replacement in ["{key:'sales-identity',actor_id:'99999',environment_id:'offline-isolated-tests'}", "{key:'sales-identity',actor_id:'" + str(user.pk) + "',environment_id:'other'}"]:
                    tab.evaluate('await repo.put("metadata",' + replacement + ');true')
                    tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
                    tab.wait('!!document.getElementById("sales-offline-notice") && document.getElementById("sales-offline-notice").textContent.includes("não preparados")')
                    self.assertEqual(tab.evaluate('produto.options.length'), 1)
                    tab.evaluate("window.repo=new (await import('/static/offline/core.js')).Repository(await (await import('/static/offline/core.js')).openDB());true")
                tab.evaluate("await repo.put('metadata',{key:'sales-identity',actor_id:'" + str(user.pk) + "',environment_id:'offline-isolated-tests'}); await repo.transaction(['snapshots'],true,(tx,done)=>{tx.objectStore('snapshots').clear();done(true)});true")
                tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
                tab.wait('!!document.getElementById("sales-offline-notice") && document.getElementById("sales-offline-notice").textContent.includes("não preparados")')
                self.assertEqual(tab.evaluate('produto.options.length'), 1)
            finally:
                chrome.stop()
        self.assertEqual({m.__name__: list(m.objects.order_by('pk').values()) for m in models}, before)
