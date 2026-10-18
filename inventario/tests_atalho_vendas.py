from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services, sessoes, tests_browser as navegador
from .models import Inventario
from .tests import produto


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html.split('class="atalhos-venda-lista"', 1)[1].split('</div>', 1)[0])

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


class PrepararAtalho:
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-atalho', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.produto = produto()
        self.inv = services.criar_inventario(self.gestor, 'Conferência fictícia', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, [self.rodada.pk])


class AtalhoVendasTests(PrepararAtalho, TestCase):
    def test_primeiro_atalho_para_anonimo_contador_e_gestor(self):
        esperado = None
        for contexto in ('anonimo', 'contagem', 'gestor'):
            with self.subTest(contexto=contexto):
                client = Client()
                if contexto == 'gestor':
                    client.force_login(self.gestor)
                if contexto == 'contagem':
                    client.post(reverse('inventario:convite', args=[self.token]))
                response = client.get('/vendas/')
                self.assertEqual(response.status_code, 200)
                links = Links(response.content.decode()).links
                self.assertEqual(links[0]['id'], 'btnConferenciaEstoqueVendas')
                self.assertEqual(links[0]['href'], reverse('inventario:lista'))
                self.assertEqual(links[1]['href'], reverse('estoque:pedidos'))
                if esperado is None:
                    esperado = links[1:]
                else:
                    self.assertEqual(links[1:], esperado)

    def test_anonimo_e_contador_entram_pelo_login_sem_expor_inventario(self):
        for contador in (False, True):
            client = Client()
            if contador:
                client.post(reverse('inventario:convite', args=[self.token]))
            response = client.get(reverse('inventario:lista'), follow=True)
            self.assertEqual(response.redirect_chain, [(reverse('inventario:login') + '?next=/inventario/', 302)])
            self.assertContains(response, 'Acesso do responsável')
            self.assertNotContains(response, self.inv.titulo)

    def test_permissoes_gerenciais_continuam_obrigatorias_com_contagem_ativa(self):
        for permissao in (None, 'gerenciar_inventario', 'revisar_inventario', 'aprovar_ajuste'):
            with self.subTest(permissao=permissao):
                user = get_user_model().objects.create_user('atalho-' + str(permissao))
                if permissao:
                    user.user_permissions.add(Permission.objects.get(content_type__app_label='inventario', codename=permissao))
                client = Client()
                client.force_login(user)
                client.post(reverse('inventario:convite', args=[self.token]))
                for url in (reverse('inventario:lista'), reverse('inventario:detalhe', args=[self.inv.pk]), reverse('inventario:relatorio', args=[self.inv.pk])):
                    self.assertEqual(client.get(url).status_code, 200 if permissao else 403)
                self.assertEqual(client.get(reverse('inventario:mobile')).status_code, 200)
                if permissao and permissao != 'gerenciar_inventario':
                    response = client.post(reverse('inventario:lista'), {'titulo': 'Não criar', 'criterio': 'manual', 'produtos': [self.produto.pk]})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(Inventario.objects.count(), 1)


@skipUnless(Path(navegador.CHROME).is_file(), 'Chrome não disponível')
class AtalhoBrowserTests(PrepararAtalho, StaticLiveServerTestCase):
    @contextmanager
    def chrome(self, autenticado=False):
        with TemporaryDirectory(prefix='inventario-atalho-chrome-') as profile:
            chrome = Chrome(navegador.CHROME, profile).start()
            try:
                vendas = chrome.tab()
                if autenticado:
                    client = Client()
                    client.force_login(self.gestor)
                    vendas.call('Network.enable')
                    vendas.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                vendas.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                vendas.wait("!!document.querySelector('#btnConferenciaEstoqueVendas')")
                self.assertTrue(vendas.evaluate("document.querySelector('.atalhos-venda-lista a').id === 'btnConferenciaEstoqueVendas'"))
                contagem = chrome.tab()
                contagem.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[self.token])})
                contagem.wait("!!document.querySelector('form button')")
                contagem.evaluate("document.querySelector('form button').click(); true")
                contagem.wait('window.inventarioContagemPronta === true')
                yield vendas, contagem
            finally:
                chrome.stop()

    def test_contagem_nao_bloqueia_login_gerencial_pelo_atalho(self):
        with self.chrome() as (vendas, contagem):
            vendas.evaluate("document.querySelector('#btnAtalhosVenda').click(); document.querySelector('#btnConferenciaEstoqueVendas').click(); true")
            vendas.wait("location.pathname === '/inventario/entrar/' && !!document.querySelector('[name=password]')")
            vendas.evaluate("document.querySelector('[name=username]').value='gestor-atalho'; document.querySelector('[name=password]').value='teste'; document.querySelector('form button').click(); true")
            vendas.wait("location.pathname === '/inventario/' && document.body.textContent.includes('Conferência fictícia')")
            self.assertEqual(contagem.evaluate("(async()=>{return (await fetch('/inventario/contagem/')).status})()"), 200)
            self.assertEqual(vendas.evaluate("(async()=>{return (await fetch('/vendas/')).status})()"), 200)
            self.assertEqual(vendas.evaluate("(async()=>{return (await fetch('/compras/')).status})()"), 200)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.versao, 0)

    def test_gestor_com_contagem_ativa_abre_atalho_sem_novo_login(self):
        with self.chrome(autenticado=True) as (vendas, contagem):
            vendas.call('Network.enable')
            antes = {c['name']: c['value'] for c in vendas.call('Network.getCookies', {'urls': [self.live_server_url + '/inventario/']})['cookies']}
            self.assertIn(sessoes.COOKIE, antes)
            vendas.evaluate("document.querySelector('#btnAtalhosVenda').click(); document.querySelector('#btnConferenciaEstoqueVendas').click(); true")
            vendas.wait("location.pathname === '/inventario/' && document.body.textContent.includes('Conferência fictícia')")
            depois = {c['name']: c['value'] for c in vendas.call('Network.getCookies', {'urls': [self.live_server_url + '/inventario/']})['cookies']}
            self.assertEqual(depois['sessionid'], antes['sessionid'])
            self.assertEqual(depois[sessoes.COOKIE], antes[sessoes.COOKIE])
            self.assertEqual(contagem.evaluate("(async()=>{return (await fetch('/inventario/contagem/')).status})()"), 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
