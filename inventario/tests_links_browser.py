from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .models import EventoInventario
from .tests import produto
from .tests_browser import CHROME


@skipUnless(Path(CHROME).is_file(), 'Chrome não disponível')
class LinksBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-links-browser', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Funcionário fictício')
        self.produtos = [produto(nome=f'Produto fictício {n}') for n in range(6)]
        self.inv = services.criar_inventario(self.gestor, 'Piloto fictício', 'manual', [p.pk for p in self.produtos])
        self.rodadas = list(self.inv.itens.values_list('rodadas__pk', flat=True))

    @contextmanager
    def navegador(self):
        client = Client()
        client.force_login(self.gestor)
        with TemporaryDirectory(prefix='inventario-links-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:detalhe', args=[self.inv.pk])})
                tab.wait('window.inventarioRevisaoPronta === true')
                yield tab
            finally:
                chrome.stop()

    def atribuir(self):
        return services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, self.rodadas)

    def test_cliques_reais_criam_e_renovam_link_visivel(self):
        with self.navegador() as tab:
            self.assertTrue(tab.evaluate("document.querySelector('#confirmar-atribuicao').disabled"))
            tab.evaluate(f"document.querySelector('[name=funcionario]').value='{self.funcionario.pk}'; document.querySelector('#selecionar-todos').click(); true")
            self.assertFalse(tab.evaluate("document.querySelector('#confirmar-atribuicao').disabled"))
            tab.evaluate("document.querySelector('#confirmar-atribuicao').click(); true")
            tab.wait("!!document.querySelector('#link-funcionario')")
            antigo = tab.evaluate("document.querySelector('#link-funcionario').value")
            self.assertEqual(tab.evaluate('document.activeElement.id'), 'compartilhar-acesso')
            self.assertTrue(tab.evaluate("document.querySelector('#compartilhar-acesso').getBoundingClientRect().top >= 0 && document.querySelector('#compartilhar-acesso').getBoundingClientRect().top < innerHeight"))
            self.assertTrue(tab.evaluate("document.querySelector('#selecionar-todos').disabled && document.querySelector('#confirmar-atribuicao').disabled"))
            # Mock clipboard: never replace the user's real clipboard contents.
            tab.evaluate("Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text=>{window.linkCopiado=text}}, configurable:true}); document.querySelector('#copiar-link').click(); true")
            tab.wait("document.querySelector('#feedback-link').textContent === 'Link copiado.'")
            self.assertEqual(tab.evaluate('window.linkCopiado'), antigo)
            tab.evaluate("const f=document.querySelector('[data-renovar-acesso]'); f.closest('details').open=true; f.querySelector('button').click(); true")
            tab.wait("document.querySelector('#compartilhar-acesso [role=status]')?.textContent.includes('Acesso renovado.')")
            self.assertNotEqual(tab.evaluate("document.querySelector('#link-funcionario').value"), antigo)
            self.assertEqual(tab.evaluate('document.activeElement.id'), 'compartilhar-acesso')
        self.assertEqual(self.inv.acessos.count(), 1)
        self.assertEqual(self.inv.acessos.get().atribuicoes.count(), 6)
        self.assertEqual(EventoInventario.objects.filter(acao='acesso_renovado').count(), 1)

    def test_processamento_bloqueia_repeticao_e_preserva_acao(self):
        self.atribuir()
        with self.navegador() as tab:
            # Observe serialization and cancel navigation, without another write.
            tab.evaluate("window.envios=0; const f=document.querySelector('[data-renovar-acesso]'); f.addEventListener('submit',e=>{if(!e.defaultPrevented){window.envios++; window.acaoEnviada=new FormData(f).get('acao');} e.preventDefault();}); f.requestSubmit(); f.requestSubmit(); true")
            self.assertEqual(tab.evaluate('window.envios'), 1)
            self.assertEqual(tab.evaluate('window.acaoEnviada'), 'renovar')
            self.assertTrue(tab.evaluate("document.querySelector('[data-renovar-acesso]').getAttribute('aria-busy') === 'true'"))
            self.assertTrue(tab.evaluate("document.querySelector('[data-renovar-acesso] button').disabled"))
            self.assertIn('Gerando novo link', tab.evaluate("document.querySelector('[data-renovar-acesso] button').textContent"))
            tab.evaluate("window.dispatchEvent(new Event('pageshow')); true")
            self.assertFalse(tab.evaluate("document.querySelector('[data-renovar-acesso] button').disabled"))
            self.assertTrue(tab.evaluate("document.querySelector('#confirmar-atribuicao').disabled"))
        self.assertFalse(EventoInventario.objects.filter(acao='acesso_renovado').exists())

    def test_erro_renovacao_recebe_foco(self):
        self.atribuir()
        self.funcionario.ativo = False
        self.funcionario.save(update_fields=['ativo'])
        with self.navegador() as tab:
            tab.evaluate("const f=document.querySelector('[data-renovar-acesso]'); f.closest('details').open=true; f.querySelector('button').click(); true")
            tab.wait("document.querySelector('#erro-acao')?.textContent.includes('Funcionário inativo.')")
            self.assertEqual(tab.evaluate('document.activeElement.id'), 'erro-acao')
            self.assertFalse(tab.evaluate("!!document.querySelector('#link-funcionario')"))
