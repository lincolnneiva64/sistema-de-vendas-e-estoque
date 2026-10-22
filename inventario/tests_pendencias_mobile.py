from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .models import RodadaContagem
from .tests import produto
from .tests_browser import CHROME


class Preparar:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('pendencias', '', 'teste')
        produtos = [produto(nome=f'Produto {i:02d}') for i in range(30)]
        self.inv = services.criar_inventario(self.usuario, 'Pendencias', 'manual', [p.pk for p in produtos])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('item__cadastro__nome'))
        funcionario = Funcionario.objects.create(nome='Contador pendencias')
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inv.pk, funcionario.pk, [r.pk for r in self.rodadas])
        for rodada in self.rodadas[:-3]:
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, '2', '0', True)


class PendenciasBackendTests(Preparar, TestCase):
    def test_unidade_unica_nao_exige_avulsos(self):
        simples = produto(nome='Somente unidades', unidade_compra='UN', vende_fracionado=False)
        inv = services.criar_inventario(self.usuario, 'Unidade unica', 'manual', [simples.pk])
        rodada = inv.itens.get().rodadas.get()
        acesso, token = services.gerar_acesso(self.usuario, inv.pk, self.acesso.funcionario_id, [rodada.pk])
        self.client.post(reverse('inventario:convite', args=[token]))
        pagina = self.client.get(reverse('inventario:mobile'))
        self.assertNotContains(pagina, 'name="avulsos"')
        services.salvar_contagem(acesso.pk, rodada.pk, 0, '0', None, True)
        inv.refresh_from_db()
        services.finalizar_conferencia(acesso.pk, inv.versao)
        inv.refresh_from_db()
        self.assertIsNotNone(inv.finalizada_em)

    def test_campos_vazios_zero_e_preservacao(self):
        rodada = self.rodadas[-1]
        services.salvar_contagem(self.acesso.pk, rodada.pk, 0, '2', '')
        for principal, avulsos in [('2', ''), ('', '0'), ('', '')]:
            with self.subTest(principal=principal, avulsos=avulsos), self.assertRaises(ValueError):
                services.salvar_contagem(self.acesso.pk, rodada.pk, 1, principal, avulsos, True)
            rodada.refresh_from_db()
            self.assertEqual(rodada.embalagens, 2)
            self.assertIsNone(rodada.avulsos)
            self.assertIsNone(rodada.ok_em)
        services.salvar_contagem(self.acesso.pk, rodada.pk, 1, '2', '0', True)
        rodada.refresh_from_db()
        self.assertIsNotNone(rodada.ok_em)

    def test_trinta_produtos_tres_pendentes_envio_e_token(self):
        self.inv.refresh_from_db()
        antes = list(RodadaContagem.objects.values())
        with self.assertRaises(ValueError) as erro:
            services.finalizar_conferencia(self.acesso.pk, self.inv.versao)
        for i in range(27, 30):
            self.assertIn(f'Produto {i:02d}', str(erro.exception))
        self.assertNotIn('Produto 26', str(erro.exception))
        self.assertEqual(antes, list(RodadaContagem.objects.values()))
        self.client.post(reverse('inventario:convite', args=[self.token]))
        response = self.client.post(reverse('inventario:finalizar'), {'versao': self.inv.versao}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertIn('Produto 27', response.json()['erro'])
        rodada = self.rodadas[-1]
        response = self.client.post(reverse('inventario:contar', args=[rodada.pk]),
            {'acao': 'ok', 'versao': 0, 'embalagens': '2', 'avulsos': ''}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 409)
        for rodada in self.rodadas[-3:]:
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, '2', '3', True)
        self.inv.refresh_from_db()
        response = self.client.post(reverse('inventario:finalizar'), {'versao': self.inv.versao}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)

    def test_finalizacao_revalida_quantidades_mesmo_com_ok(self):
        for rodada in self.rodadas[-3:]:
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, '2', '0', True)
        RodadaContagem.objects.filter(pk=self.rodadas[-1].pk).update(avulsos=None)
        self.inv.refresh_from_db()
        with self.assertRaisesMessage(ValueError, 'Produto 29'):
            services.finalizar_conferencia(self.acesso.pk, self.inv.versao)
        self.acesso.refresh_from_db()
        self.assertIsNone(self.acesso.enviada_em)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class PendenciasBrowserTests(Preparar, StaticLiveServerTestCase):
    def test_mobile_320_e_390_lista_foco_rolagem_preservacao_e_envio(self):
        with TemporaryDirectory(prefix='pendencias-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[self.token])})
                tab.wait("!!document.querySelector('form button')")
                tab.evaluate("document.querySelector('form button').click();true")
                tab.wait('window.inventarioContagemPronta === true')
                self.assertTrue(tab.evaluate("document.querySelector('#proxima-pendencia').offsetParent === null"))
                for width in [320, 390]:
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                    tab.evaluate("document.querySelector('#finalizar-conferencia button').click();true")
                    tab.wait("document.querySelector('#aviso-pendencias').open")
                    self.assertEqual(tab.evaluate("[...document.querySelectorAll('#lista-pendentes li')].map(li=>li.textContent)"), ['Produto 27', 'Produto 28', 'Produto 29'])
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    tab.evaluate("document.querySelector('#ir-primeiro-pendente').click();true")
                    tab.wait("document.activeElement.name === 'embalagens'")
                    tab.wait("document.activeElement.getBoundingClientRect().top > 0 && document.activeElement.getBoundingClientRect().bottom < innerHeight")
                    self.assertTrue(tab.evaluate("document.activeElement.closest('article').classList.contains('pendencia-destacada')"))
                for index in range(27, 30):
                    tab.evaluate(f"window.formAtual=document.querySelectorAll('form.contagem')[{index}];true")
                    if index == 27:
                        tab.evaluate("formAtual.querySelector('[value=ok]').click();true")
                        self.assertIn('CX', tab.evaluate("formAtual.querySelector('.mensagem').textContent"))
                        self.assertIn('UN', tab.evaluate("formAtual.querySelector('.mensagem').textContent"))
                    tab.evaluate("formAtual.elements.embalagens.value='2';formAtual.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));formAtual.querySelector('[value=ok]').click();true")
                    self.assertEqual(tab.evaluate('document.activeElement.name'), 'avulsos')
                    self.assertEqual(tab.evaluate('formAtual.elements.embalagens.value'), '2')
                    if index == 27:
                        tab.evaluate("formAtual.querySelector('[value=salvar]').click();true")
                        tab.wait("formAtual.elements.embalagens.readOnly && !formAtual.querySelector('[value=ok]').disabled")
                        tab.evaluate("formAtual.querySelector('[value=ok]').click();true")
                        self.assertEqual(tab.evaluate('document.activeElement.name'), 'avulsos')
                        self.assertFalse(tab.evaluate('formAtual.elements.avulsos.readOnly'))
                        self.assertEqual(tab.evaluate('formAtual.elements.embalagens.value'), '2')
                    tab.evaluate("formAtual.elements.avulsos.value='0';formAtual.elements.avulsos.dispatchEvent(new Event('input',{bubbles:true}));formAtual.querySelector('[value=ok]').click();true")
                    tab.wait("formAtual.dataset.confirmado === 'true' && !formAtual.querySelector('[data-editar-contagem]').disabled")
                    if index < 29:
                        tab.evaluate("document.querySelector('#proxima-pendencia').click();true")
                        tab.wait("document.activeElement.name === 'embalagens'")
                self.assertTrue(tab.evaluate("document.querySelector('#proxima-pendencia').hidden"))
                self.assertTrue(tab.evaluate("document.querySelector('#proxima-pendencia').offsetParent === null"))
                self.assertEqual(tab.evaluate("[...document.querySelectorAll('form.contagem')].filter(f=>f.dataset.confirmado==='true').length"), 30)
                tab.evaluate("document.querySelector('#finalizar-conferencia button').click();true")
                tab.wait("document.querySelector('#estado-conferencia').textContent.includes('finalizada e enviada')")
            finally:
                chrome.stop()
