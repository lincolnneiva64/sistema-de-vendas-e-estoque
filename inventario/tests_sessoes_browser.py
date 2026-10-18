from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings
from django.urls import reverse

from estoque.models import Venda
from offline.browser_support import Chrome
from offline.tests_commercial import commercial_fixtures
from offline import tests_sales_drafts_browser as vendas_browser
from . import services, sessoes


@skipUnless(Path(vendas_browser.CHROME).is_file(), 'Chrome não disponível')
@override_settings(LOCAL_SALES_LOOPBACK=True)
class SessoesBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.comercial, self.cliente, self.produto, self.funcionario, *_ = commercial_fixtures()
        self.gestor = get_user_model().objects.create_superuser('gestor-isolado', '', 'teste')
        self.inv = services.criar_inventario(self.gestor, 'Sessões fictícias', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, [self.rodada.pk])
        self.client = Client()
        self.client.force_login(self.comercial)

    def abrir_vendas(self, chrome):
        tab = chrome.tab()
        tab.call('Network.enable')
        tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
        tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
        tab.wait('window.localSales && window.salesDraftUI?.ready === true')
        return tab

    def cookies_comerciais(self, tab):
        cookies = tab.call('Network.getCookies', {'urls': [self.live_server_url + '/vendas/']})['cookies']
        return {c['name']: c['value'] for c in cookies if c['name'] in {'sessionid', 'csrftoken', 'local_sales_owner'}}

    def test_duas_abas_preservam_login_cookie_comando_pendente_e_armazenamento(self):
        with TemporaryDirectory(prefix='inventario-sessoes-chrome-') as profile:
            chrome = Chrome(vendas_browser.CHROME, profile).start()
            try:
                vendas = self.abrir_vendas(chrome)
                vendas_browser.SalesDraftBrowserTests.select_customer(self, vendas, 'Cliente Comercial')
                vendas_browser.SalesDraftBrowserTests.add_product(self, vendas, 'Produto Fracionado', '1')
                vendas.evaluate("tipoVenda.value='A prazo'; tipoVenda.dispatchEvent(new Event('change',{bubbles:true})); await salesDraftUI.flush(); true")
                vendas.evaluate("window.realFetch=fetch; window.fetch=async(url,options)=>{if(new URL(String(url),location.href).pathname==='/api/vendas/online/' && options?.method==='POST') throw new TypeError('Envio suspenso no teste');return realFetch(url,options);}; btnGravarVenda.click(); true")
                vendas.wait('!!localSales.record.command && !document.getElementById("local-sale-query").hidden')
                comando = vendas.evaluate('JSON.stringify(localSales.record.command)')
                chave = vendas.evaluate('localSales.key')
                vendas.evaluate("window.core=await import('/offline/assets/2-8f/core.js'); window.repo=new core.Repository(await core.openDB()); await repo.put('metadata',{key:'rascunho_venda:preservar-sessao',payload:'intacto'}); localStorage.setItem('venda-pendente-preservar','intacto'); sessionStorage.setItem('aba-comercial-preservar','intacto'); true")
                cookies = self.cookies_comerciais(vendas)
                contagem = chrome.tab()
                contagem.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[self.token])})
                contagem.wait("!!document.querySelector('form button')")
                contagem.evaluate("document.querySelector('form button').click(); true")
                contagem.wait('window.inventarioContagemPronta === true')
                self.assertEqual(self.cookies_comerciais(vendas), cookies)
                self.assertTrue(vendas.evaluate("(async()=>{return (await (await fetch('/api/offline/session/')).json()).authenticated})()"))
                estados = vendas.evaluate("(async()=>{return await Promise.all(['/vendas/','/compras/','/','/locacoes/'].map(async url=>(await fetch(url)).status))})()")
                self.assertEqual(estados, [200, 200, 200, 200])
                # The inventory cookie is absent from actual browser requests to Sales.
                nomes = {c['name'] for c in vendas.call('Network.getCookies', {'urls': [self.live_server_url + '/vendas/']})['cookies']}
                self.assertNotIn(sessoes.COOKIE, nomes)
                contagem.evaluate("document.querySelector('[name=embalagens]').value='1'; document.querySelector('[name=avulsos]').value='0'; document.querySelector('[value=salvar]').click(); true")
                contagem.wait("document.querySelector('.mensagem').textContent.includes('Contagem guardada')")
                self.assertEqual(vendas.evaluate('localSales.key'), chave)
                self.assertEqual(vendas.evaluate("JSON.stringify((await repo.get('metadata',localSales.key)).command)"), comando)
                self.assertEqual(vendas.evaluate("(await repo.get('metadata','rascunho_venda:preservar-sessao')).payload"), 'intacto')
                self.assertEqual(vendas.evaluate("localStorage.getItem('venda-pendente-preservar')"), 'intacto')
                self.assertEqual(vendas.evaluate("sessionStorage.getItem('aba-comercial-preservar')"), 'intacto')
                self.assertEqual(contagem.evaluate("(async()=>{return (await fetch('/inventario/')).status})()"), 403)
                services.revogar_acesso(self.gestor, self.acesso.pk)
                self.assertEqual(contagem.evaluate("(async()=>{return (await fetch('/inventario/contagem/')).status})()"), 403)
                self.assertEqual(vendas.evaluate("(async()=>{return (await fetch('/vendas/')).status})()"), 200)
                self.assertEqual(self.cookies_comerciais(vendas), cookies)
                self.assertEqual(Venda.objects.count(), 0)
                self.produto.refresh_from_db()
                self.assertEqual(str(self.produto.quantidade), '2.500')
            finally:
                chrome.stop()

    def test_chrome_migra_sessao_legada_sem_logout_ou_limpeza_comercial(self):
        sessao = self.client.session
        sessao['inventario_acesso'] = self.acesso.pk
        sessao['inventario_token_hash'] = self.acesso.token_hash
        sessao['rascunho_comercial'] = {'preservar': True}
        sessao.save()
        chave = sessao.session_key
        with TemporaryDirectory(prefix='inventario-sessao-legada-') as profile:
            chrome = Chrome(vendas_browser.CHROME, profile).start()
            try:
                vendas = self.abrir_vendas(chrome)
                self.assertEqual(self.cookies_comerciais(vendas)['sessionid'], chave)
                self.assertTrue(vendas.evaluate("(async()=>{return (await (await fetch('/api/offline/session/')).json()).authenticated})()"))
                contagem = chrome.tab()
                contagem.call('Page.navigate', {'url': self.live_server_url + '/inventario/contagem/'})
                contagem.wait('window.inventarioContagemPronta === true')
                self.assertEqual(contagem.evaluate("document.querySelector('[name=embalagens]').value"), '')
                self.assertEqual(self.client.session['rascunho_comercial'], {'preservar': True})
                self.assertEqual(int(self.client.session['_auth_user_id']), self.comercial.pk)
                self.assertNotIn('inventario_acesso', self.client.session)
                self.rodada.refresh_from_db()
                self.assertEqual(self.rodada.versao, 0)
            finally:
                chrome.stop()
