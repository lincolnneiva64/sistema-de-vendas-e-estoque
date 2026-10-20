"""Finalized conferences are read-only until deliberate administrative access."""
import base64
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Produto
from offline.browser_support import Chrome
from . import acesso_ajustes, ajustes, services
from .models import AplicacaoAjuste, Inventario, RodadaContagem
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME
from .tests_legibilidade_conferencia import DadosLegibilidade


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class HistoricoSeguroTests(AjustesFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.detalhe = reverse('inventario:detalhe', args=[self.inv.pk])
        self.painel = reverse('inventario:ajustes', args=[self.inv.pk])

    def finalizar(self, inventario=None):
        inventario = inventario or self.inv
        for acesso in inventario.acessos.all():
            inventario.refresh_from_db()
            services.finalizar_conferencia(acesso.pk, inventario.versao)
        inventario.refresh_from_db()
        self.assertIsNotNone(inventario.finalizada_em)

    def snapshot(self):
        return {**{m._meta.label: list(m.objects.order_by('pk').values())
            for m in apps.get_app_config('inventario').get_models()},
            'estoque': list(Produto.objects.order_by('pk').values())}

    def entrar(self):
        resposta = self.client.post(self.detalhe, {'acao': 'revisar_ajustes_historicos'})
        self.assertEqual(resposta.status_code, 302)
        return resposta.url

    def test_historico_somente_leitura_sem_previa_documental(self):
        services.revisar(self.usuario, self.rodada.pk, 'Revisao ficticia registrada')
        self.finalizar()
        antes = self.snapshot()
        with patch.object(ajustes, 'preparar_resultado', side_effect=AssertionError('Heavy history')), \
                patch('inventario.conciliacao_documental.conciliar', side_effect=AssertionError('Documentary history')), \
                CaptureQueriesContext(connection) as queries:
            pagina = self.client.get(self.detalhe)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertContains(pagina, 'Diferença registrada — aplicação não comprovada nesta conferência')
        self.assertContains(pagina, 'Revisão por')
        self.assertContains(pagina, 'Revisao ficticia registrada')
        self.assertContains(pagina, 'id="acoes-administrativas"')
        self.assertNotContains(pagina, 'id="revisar-atualizacao"')
        self.assertNotContains(pagina, 'Requer prévia')
        self.assertNotContains(pagina, 'Apto para atualizar')
        self.assertNotContains(pagina, 'id="confirmar-atualizacao-resultado"')
        self.assertEqual(antes, self.snapshot())

    def test_urls_parametros_e_posts_diretos_nao_contornam_acesso(self):
        avaliacao = self.avaliar()
        token = ajustes.previa_atualizacao(self.inv.pk, [avaliacao])
        self.finalizar()
        antes = self.snapshot()
        with patch.object(ajustes, 'preparar_resultado', side_effect=AssertionError('Bypass')), \
                patch.object(ajustes, 'atualizar', side_effect=AssertionError('Bypass')):
            self.assertEqual(self.client.get(self.detalhe, {'atualizar': 'prever'}).status_code, 403)
            self.assertEqual(self.client.post(self.detalhe, dict(acao='atualizar', previa=token,
                confirmacao='sim')).status_code, 403)
            for params in ({}, {'confirmar': 'todos'}, {'preparar': '1'},
                           {'revisao_historica': 'sim'}):
                self.assertEqual(self.client.get(self.painel, params).status_code, 403)
            for acao in ('atualizar', 'aprovar', 'rejeitar', 'aplicar'):
                self.assertEqual(self.client.post(self.painel, dict(acao=acao, previa=token,
                    confirmacao='sim')).status_code, 403)
        self.assertEqual(antes, self.snapshot())

    def test_entrada_explicita_previa_aplicacao_unica_sem_reabrir(self):
        self.finalizar()
        original = self.inv.finalizada_em
        rodadas = list(RodadaContagem.objects.values())
        antes = self.snapshot()
        url = self.entrar()
        self.assertEqual(antes, self.snapshot())
        acesso = parse_qs(urlsplit(url).query)['revisao_historica'][0]
        with CaptureQueriesContext(connection) as queries:
            pagina = self.client.get(url)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(antes, self.snapshot())
        self.assertEqual(pagina.context['resumo']['aptos'], 1)
        confirmar = self.client.get(url + '&confirmar=todos')
        dados = dict(acao='atualizar', previa=confirmar.context['token_aplicacao'],
            confirmacao='sim', revisao_historica=acesso)
        resposta = self.client.post(self.painel, dados)
        self.assertEqual(resposta.url, self.detalhe + '#titulo-resumo')
        self.assertEqual(self.client.post(self.painel, dados).status_code, 302)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(self.fresca().item.produto.quantidade, 8)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.finalizada_em, original)
        self.assertEqual(rodadas, list(RodadaContagem.objects.values()))
        historico = self.client.get(self.detalhe)
        self.assertContains(historico, 'Saldo registrado: 8 CX')
        self.assertNotContains(historico, 'id="acessar-revisao-historica"')

    def test_acesso_expirado_adulterado_outro_usuario_conferencia_e_ciclo(self):
        self.finalizar()
        url = self.entrar()
        token = parse_qs(urlsplit(url).query)['revisao_historica'][0]
        self.assertEqual(self.client.get(self.painel, {'revisao_historica': token+'x'}).status_code, 403)
        futuro = time.time() + 1201
        with patch('django.core.signing.time.time', return_value=futuro):
            self.assertEqual(self.client.get(url).status_code, 403)
        outro = get_user_model().objects.create_superuser('outro-administrador', '', 'teste')
        self.client.force_login(outro)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.usuario)
        outra = services.criar_inventario(self.usuario, criterio='manual', selecionados=[self.produto.pk])
        self.confirmar(outra, 8)
        self.finalizar(outra)
        self.assertEqual(self.client.get(reverse('inventario:ajustes', args=[outra.pk]),
            {'revisao_historica': token}).status_code, 403)
        Inventario.objects.filter(pk=self.inv.pk).update(ciclo=self.inv.ciclo+1)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_permissao_e_csrf_obrigatorios_para_entrada(self):
        self.finalizar()
        token = acesso_ajustes.iniciar(self.usuario, self.inv)
        consulta = get_user_model().objects.create_user('consulta-historico')
        consulta.user_permissions.add(Permission.objects.get(
            content_type__app_label='inventario', codename='revisar_inventario'))
        self.client.force_login(consulta)
        pagina = self.client.get(self.detalhe)
        self.assertNotContains(pagina, 'id="acessar-revisao-historica"')
        self.assertEqual(self.client.post(self.detalhe,
            {'acao': 'revisar_ajustes_historicos'}).status_code, 200)
        self.assertEqual(self.client.get(self.painel, {'revisao_historica': token}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(self.detalhe, {'acao': 'revisar_ajustes_historicos'}).status_code, 403)

    def test_bloqueio_documental_posterior_permanece_explicito(self):
        from estoque.models import EventoVenda
        from .tests_conciliacao_documental import ConciliacaoDocumentalTests
        venda, _, _ = ConciliacaoDocumentalTests.vender(self, 1)
        EventoVenda.objects.create(venda=venda, tipo_evento='edicao_desconhecida',
            descricao='Sem evidencia documental ficticia')
        self.finalizar()
        antes = self.snapshot()
        pagina = self.client.get(self.entrar() + '&confirmar=todos')
        self.assertEqual(pagina.context['resumo']['bloqueados'], 1)
        self.assertFalse(pagina.context['token_aplicacao'])
        self.assertNotContains(pagina, 'id="confirmar-aplicacao"')
        self.assertEqual(antes, self.snapshot())

    def test_acesso_antigo_nao_contorna_finalizacao_entre_leitura_e_post(self):
        original = acesso_ajustes.exigir
        chamadas = []
        def finalizar_apos_leitura(request, inventario):
            chamadas.append(inventario.finalizada_em)
            if len(chamadas) == 1:
                self.finalizar()
                return ''
            return original(request, inventario)
        token = ajustes.previa_atualizacao(self.inv.pk, [self.avaliar()])
        with patch.object(acesso_ajustes, 'exigir', side_effect=finalizar_apos_leitura):
            resposta = self.client.post(self.painel, dict(acao='atualizar', previa=token, confirmacao='sim'))
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(len(chamadas), 2)
        self.assertFalse(AplicacaoAjuste.objects.exists())


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class HistoricoSeguroBrowserTests(DadosLegibilidade, StaticLiveServerTestCase):
    def demonstrar(self, largura):
        self.concluir(self.distribuir())
        pasta = Path('diagnostics/historico-seguro')
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='historico-seguro-chrome-') as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls': ['https://*']})
                tab.call('Network.setCookie', {'name': 'sessionid',
                    'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=900,
                    deviceScaleFactor=1, mobile=largura < 800))
                tab.call('Page.navigate', {'url': self.live_server_url+self.url})
                tab.wait('!!document.getElementById("acoes-administrativas")')
                # The DOM can be parsed while the external stylesheet is still loading.
                tab.wait("""!!document.querySelector('link[href*="inventario/revisao.css"]').sheet""")
                self.assertFalse(tab.evaluate('document.getElementById("acoes-administrativas").open'))
                self.assertGreaterEqual(tab.evaluate('parseFloat(getComputedStyle(document.querySelector(".aplicacao-nao-comprovada")).fontSize)'), 16)
                self.assertGreaterEqual(tab.evaluate('parseInt(getComputedStyle(document.querySelector(".aplicacao-nao-comprovada")).fontWeight)'), 500)
                self.assertFalse(tab.evaluate('!!document.getElementById("revisar-atualizacao")'))
                self.assertFalse(tab.evaluate('document.querySelector(".resumo-inventario").textContent.includes("Requer prévia")'))
                def capturar(nome):
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    rect = tab.call('Page.getLayoutMetrics')['cssContentSize']
                    rect['scale'] = 1
                    (pasta/f'{nome}-{largura}.png').write_bytes(base64.b64decode(
                        tab.call('Page.captureScreenshot', dict(format='png',
                            captureBeyondViewport=True, clip=rect))['data']))
                capturar('01-historico')
                tab.evaluate('document.getElementById("acoes-administrativas").open=true;true')
                capturar('02-acoes-administrativas')
                tab.evaluate('document.getElementById("acessar-revisao-historica").requestSubmit();true')
                tab.wait('!!document.querySelector(".lista-ajustes")')
                tab.evaluate("""document.querySelector('a[href*="confirmar=todos"]').click();true""")
                tab.wait('!!document.getElementById("confirmar-aplicacao")')
                capturar('03-previa-autorizada')
                tab.evaluate('document.querySelector("#confirmar-aplicacao button").click();true')
                tab.wait('document.querySelectorAll(".saldo-aplicado").length===2')
                self.assertFalse(tab.evaluate('!!document.getElementById("revisar-atualizacao")'))
                self.assertEqual(AplicacaoAjuste.objects.count(), 2)
                capturar('04-retorno-historico')
            finally:
                chrome.stop()

    def test_desktop_1440(self):
        self.demonstrar(1440)

    def test_mobile_390(self):
        self.demonstrar(390)

    def test_mobile_320(self):
        self.demonstrar(320)
