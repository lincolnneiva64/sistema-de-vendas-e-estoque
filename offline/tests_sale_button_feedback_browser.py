"""Button feedback uses real receipts, private Chrome profiles and fictional sales."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from estoque.models import Venda, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .browser_support import Chrome
from . import tests_online_sales_browser as online
from . import tests_sale_sync_browser as sync
from . import tests_local_sales_browser as local


@skipUnless(Path(online.CHROME).is_file(), 'Chrome unavailable')
class SaleButtonFeedbackTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = online.OnlineSaleBrowserTests.setUp
    open_sales = online.OnlineSaleBrowserTests.open_sales
    select_customer = online.OnlineSaleBrowserTests.select_customer
    add_product = online.OnlineSaleBrowserTests.add_product
    repository = online.OnlineSaleBrowserTests.repository
    reload = online.OnlineSaleBrowserTests.reload

    def browser(self):
        profile = TemporaryDirectory(prefix='sale-feedback-fictional-', ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        browser = Chrome(online.CHROME, profile.name).start()
        self.addCleanup(browser.stop)
        return browser

    def observe(self, t):
        t.evaluate("window.buttonLabels=[btnGravarVenda.textContent];new MutationObserver(()=>buttonLabels.push(btnGravarVenda.textContent)).observe(btnGravarVenda,{childList:true});window.salePosts=0;window.saleFetch=fetch;true")

    def label(self, t, text):
        t.wait('document.getElementById("btnGravarVenda").textContent==='+json.dumps(text))

    def repeat(self, t):
        self.assertTrue(t.evaluate('btnGravarVenda.disabled'))
        t.evaluate("btnGravarVenda.click();btnGravarVenda.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));true")
        self.assertEqual(t.evaluate('salePosts'), 1)

    def test_cash_modal_slow_response_and_repeated_click(self):
        t = online.prepare(self, self.browser(), payment='À vista')
        self.observe(t)
        t.evaluate("fetch=async(u,o)=>{const r=await saleFetch(u,o);if(String(u)==='/api/vendas/online/'&&o?.method==='POST'){salePosts++;await new Promise(resolve=>window.releaseSale=resolve)}return r};btnGravarVenda.click();true")
        t.wait("document.getElementById('vendaFechamentoOverlay').getAttribute('aria-hidden')==='false'")
        self.assertEqual(t.evaluate('btnGravarVenda.textContent'), 'Gravar Venda')
        self.assertEqual(Venda.objects.count(), 0)

        t.evaluate("vendaOrigemCaixa.value='10,00';vendaOrigemBanco.value='14,00';btnConfirmarFechamentoVenda.click();true")
        t.wait('!!window.releaseSale')
        self.label(t, 'Aguardando confirmação...')
        self.assertIn('Gravando venda...', t.evaluate('buttonLabels'))
        self.assertNotIn('Venda offline pendente', t.evaluate('buttonLabels'))
        self.assertNotIn('Venda gravada!', t.evaluate('buttonLabels'))
        self.repeat(t)
        self.assertEqual(Venda.objects.count(), 1)
        t.evaluate('releaseSale();true')
        self.label(t, 'Venda gravada!')
        self.assertEqual(OperacaoSincronizacao.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 2)
        self.repeat(t)

    def test_credit_fast_response_and_new_assembly(self):
        t = online.prepare(self, self.browser())
        self.observe(t)
        t.evaluate("fetch=(u,o)=>{if(String(u)==='/api/vendas/online/'&&o?.method==='POST')salePosts++;return saleFetch(u,o)};btnGravarVenda.click();true")
        self.label(t, 'Venda gravada!')
        self.assertEqual(Venda.objects.count(), 1)
        self.assertIn('Gravando venda...', t.evaluate('buttonLabels'))
        self.assertNotIn('Venda offline pendente', t.evaluate('buttonLabels'))
        self.select_customer(t, 'Cliente Comercial')
        self.add_product(t, 'Segundo Produto', '1')
        self.label(t, 'Gravar Venda')
        self.assertFalse(t.evaluate('btnGravarVenda.disabled'))

    def test_lost_response_reload_and_lookup_do_not_resubmit(self):
        t = online.prepare(self, self.browser())
        self.observe(t)
        t.evaluate("fetch=async(u,o)=>{const r=await saleFetch(u,o);if(String(u)==='/api/vendas/online/'&&o?.method==='POST'){salePosts++;throw TypeError('Resposta ficticia perdida depois do commit')}return r};btnGravarVenda.click();true")
        self.label(t, 'Verificar gravação')
        self.repeat(t)
        operation = t.evaluate('(await repo.all("operations"))[0]')
        self.assertEqual(operation['status'], 'resultado_desconhecido')
        self.assertEqual(Venda.objects.count(), 1)
        # Hold only receipt lookups on reload; real queue/UUID remain intact.
        script = "const lookupFetch=fetch;window.lookupWait=[];window.recoveryPosts=0;fetch=async(u,o)=>{if(String(u).startsWith('/api/vendas/online/')&&o?.method!=='POST')await new Promise(resolve=>lookupWait.push(resolve));if(String(u)==='/api/vendas/online/'&&o?.method==='POST')recoveryPosts++;return lookupFetch(u,o)};"
        t.call('Page.addScriptToEvaluateOnNewDocument', dict(source=script))
        self.reload(t)
        self.repository(t)
        self.label(t, 'Verificar gravação')
        self.assertTrue(t.evaluate('btnGravarVenda.disabled'))
        self.assertEqual(t.evaluate('(await repo.all("operations"))[0].operation_id'), operation['operation_id'])
        t.wait('lookupWait.length>0')
        t.evaluate('lookupWait.forEach(resolve=>resolve());true')
        self.label(t, 'Venda gravada!')
        self.assertEqual(t.evaluate('recoveryPosts'), 0)
        self.assertEqual(Venda.objects.count(), 1)

    def test_connection_failure_not_found_remains_protected(self):
        t = online.prepare(self, self.browser())
        self.observe(t)
        t.evaluate("fetch=(u,o)=>{if(String(u)==='/api/vendas/online/'&&o?.method==='POST'){salePosts++;return Promise.reject(TypeError('Conexao ficticia interrompida'))}return saleFetch(u,o)};btnGravarVenda.click();true")
        self.label(t, 'Verificar gravação')
        self.repeat(t)
        self.assertEqual(Venda.objects.count(), 0)
        t.evaluate('fetch=saleFetch;document.getElementById("sales-online-recover").click();true')
        t.wait("(await repo.all('operations'))[0].last_lookup?.outcome==='not_found'")
        self.label(t, 'Verificar gravação')
        self.repeat(t)

    def test_real_offline_persisted_and_restored(self):
        # Existing helper obtains a real fictional snapshot, confirms failed
        # health probes and persists via the existing offline finalizer.
        self.open_sales = sync.SaleSyncBrowserTests.open_sales.__get__(self)
        t = sync.SaleSyncBrowserTests.prepare(self, self.browser(), quantity='1')
        self.label(t, 'Venda offline pendente')
        operation = t.evaluate('(await repo.all("operations"))[0]')
        self.assertEqual((operation['transport'], operation['status'], operation['attempts']), ('offline', 'pendente', 0))
        self.assertEqual(Venda.objects.count(), 0)
        self.assertTrue(t.evaluate('btnGravarVenda.disabled'))
        self.reload(t)
        self.repository(t)
        self.label(t, 'Venda offline pendente')
        self.assertEqual(t.evaluate('(await repo.all("operations"))[0].operation_id'), operation['operation_id'])
        self.assertEqual(Venda.objects.count(), 0)


@skipUnless(Path(online.CHROME).is_file(), 'Chrome unavailable')
@override_settings(LOCAL_SALES_LOOPBACK=True)
class LocalSaleButtonFeedbackTests(StaticLiveServerTestCase):
    reset_sequences = True
    setUp = local.LocalSaleBrowserTests.setUp
    open = local.LocalSaleBrowserTests.open
    prepare = local.LocalSaleBrowserTests.prepare
    select_customer = local.LocalSaleBrowserTests.select_customer
    add_product = local.LocalSaleBrowserTests.add_product
    browser = SaleButtonFeedbackTests.browser
    label = SaleButtonFeedbackTests.label

    def test_local_adapter_loss_and_official_lookup(self):
        t = self.open(self.browser())
        self.prepare(t)
        t.evaluate("window.saleFetch=fetch;window.salePosts=0;fetch=async(u,o)=>{const r=await saleFetch(u,o);if(String(u)==='/api/vendas/online/'&&o?.method==='POST'){salePosts++;await new Promise(resolve=>window.loseLocal=()=>resolve());throw TypeError('Resposta ficticia perdida')}return r};btnGravarVenda.click();true")
        t.wait('!!window.loseLocal')
        self.label(t, 'Aguardando confirmação...')
        t.evaluate('btnGravarVenda.click();true')
        self.assertEqual(t.evaluate('salePosts'), 1)
        t.evaluate('loseLocal();true')
        self.label(t, 'Verificar gravação')
        self.assertTrue(t.evaluate('btnGravarVenda.disabled'))
        t.evaluate('fetch=saleFetch;document.getElementById("local-sale-query").click();true')
        self.label(t, 'Venda gravada!')
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(t.evaluate('salePosts'), 1)
