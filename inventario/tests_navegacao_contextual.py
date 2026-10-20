"""Navigation only, on synthetic records in the guarded disposable test database."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.apps import apps
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from estoque.models import MovimentacaoEstoqueManual, Produto
from offline.browser_support import Chrome
from .tests_browser import CHROME
from .tests_usabilidade import Dados


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class NavegacaoContextualBrowserTests(Dados, StaticLiveServerTestCase):
    def snapshot(self):
        return {
            **{model._meta.label: list(model.objects.order_by('pk').values())
               for model in apps.get_app_config('inventario').get_models()},
            'saldos': list(Produto.objects.order_by('pk').values_list('pk', 'quantidade')),
            'manuais': list(MovimentacaoEstoqueManual.objects.order_by('pk').values()),
        }

    def fluxo(self, largura):
        movimento = MovimentacaoEstoqueManual.objects.create(
            produto=self.produtos[0], tipo='conferencia', estoque_antes=10,
            estoque_depois=10, diferenca=0, motivo='Registro fictício para testar navegação.')
        antes = self.snapshot()
        lista = reverse('inventario:lista')
        vendas = reverse('estoque:vendas')
        detalhe = reverse('inventario:detalhe', args=[self.inv.pk])
        ajustes = reverse('inventario:ajustes', args=[self.inv.pk])
        historico = reverse('inventario:historico_produto', args=[self.inv.pk, self.rodadas[0]])
        manual = reverse('inventario:movimento_manual', args=[self.inv.pk, self.rodadas[0], movimento.pk])
        self.assertEqual(vendas, '/vendas/')
        self.assertEqual(lista, '/inventario/')
        voltar_vendas = {'texto':'Voltar para Vendas', 'href':vendas}
        inicio = {'texto':'Início do estoque', 'href':lista}
        voltar = {'texto':'Voltar à conferência', 'href':detalhe}
        client = Client()
        client.force_login(self.usuario)
        with TemporaryDirectory(prefix='inventario-navegacao-final-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=client.cookies['sessionid'].value, url=self.live_server_url))
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=844, deviceScaleFactor=1, mobile=largura<768))

                def abrir(path):
                    tab.call('Page.navigate', {'url':self.live_server_url+path})
                    tab.wait('location.pathname === '+json.dumps(path)+' && document.readyState === "complete" && window.inventarioFeedbackPronto === true')

                for path, expected in [(lista,[voltar_vendas]), (detalhe,[voltar_vendas,inicio]),
                        (ajustes,[voltar_vendas,inicio,voltar]), (historico,[voltar_vendas,inicio,{'texto':'Voltar a Produtos e diverg\u00eancias','href':detalhe+'#titulo-resumo'}]),
                        (manual,[voltar_vendas,inicio,voltar])]:
                    abrir(path)
                    self.assertEqual(tab.evaluate("[...document.querySelectorAll('.navegacao-contextual a')].map(a=>({texto:a.textContent,href:a.getAttribute('href')}))"), expected)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.navegacao-contextual a')].every(a=>a.getBoundingClientRect().height>=44)"))
                    self.assertFalse(tab.evaluate("!!document.querySelector('.voltar-painel,.extrato-pagina>a.voltar')"))
                    if path == ajustes:
                        self.assertFalse(tab.evaluate('!!document.querySelector("main>a")'))
                    for index, link in enumerate(expected):
                        abrir(path)
                        tab.evaluate(f'document.querySelectorAll(".navegacao-contextual a")[{index}].click();true')
                        tab.wait('location.pathname === '+json.dumps(link['href'].split('#')[0])+' && document.readyState === "complete"')

                # The individual counting access must not acquire administrative links or permissions.
                tab.call('Network.clearBrowserCookies')
                convite = reverse('inventario:convite', args=[self.token])
                tab.call('Page.navigate', {'url':self.live_server_url+convite})
                tab.wait('!!document.querySelector("form button")')
                tab.evaluate('document.querySelector("form button").click();true')
                tab.wait('window.inventarioContagemPronta === true')
                self.assertFalse(tab.evaluate('!!document.querySelector(".navegacao-contextual")'))
                self.assertTrue(tab.evaluate("[...document.querySelectorAll('a')].every(a=>a.getAttribute('href').startsWith('#'))"))
                self.assertEqual(tab.evaluate('(async()=>await Promise.all('+json.dumps([detalhe,ajustes])+'.map(async url=>(await fetch(url)).status)))()'), [403,403])
                self.assertEqual(antes, self.snapshot())
            finally:
                chrome.stop()

    def test_mobile(self):
        self.fluxo(390)

    def test_desktop(self):
        self.fluxo(1366)
