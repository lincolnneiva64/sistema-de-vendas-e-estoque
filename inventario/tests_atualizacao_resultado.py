import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from estoque.models import MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from offline.browser_support import Chrome
from . import ajustes
from .models import AplicacaoAjuste, ConfirmacaoContagem, RodadaContagem
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AtualizacaoResultadoTests(AjustesFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])

    def previa(self):
        return self.client.get(self.url, {'atualizar': 'prever'}).context['previa_lote']

    def enviar(self, previa, **dados):
        return self.client.post(self.url, dict(acao='atualizar', previa=previa, confirmacao='sim', **dados))

    def test_lote_mesmo_resultado_uma_confirmacao_sem_reaplicacao(self):
        self.segunda()
        rodadas = list(RodadaContagem.objects.order_by('pk').values())
        oks = list(ConfirmacaoContagem.objects.order_by('pk').values())
        previa = self.previa()
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        resposta = self.enviar(previa)
        self.assertEqual(resposta.url, self.url + '#titulo-resumo')
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)
        self.enviar(previa)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 2)
        self.assertEqual(rodadas, list(RodadaContagem.objects.order_by('pk').values()))
        self.assertEqual(oks, list(ConfirmacaoContagem.objects.order_by('pk').values()))
        pagina = self.client.get(self.url)
        self.assertNotContains(pagina, 'id="revisar-atualizacao"')
        self.assertNotContains(pagina, 'id="confirmar-atualizacao-resultado"')
        self.assertContains(pagina, 'Saldo registrado: 8 CX')
        self.assertContains(pagina, 'Ver evolução do estoque')

    def test_bloqueio_exibido_e_excluido_da_previa(self):
        outra = self.segunda()
        RodadaContagem.objects.filter(pk=outra.pk).update(contado_base=17)
        pagina = self.client.get(self.url, {'atualizar': 'prever'})
        self.assertEqual(pagina.context['atualizacao']['resumo']['bloqueados'], 1)
        self.assertContains(pagina, 'OK sem confirmação original compatível')
        self.assertEqual(len(ajustes.ler_token(pagina.context['previa_lote'])['rodadas']), 1)
        self.enviar(pagina.context['previa_lote'])
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(self.fresca(outra.pk).item.produto.quantidade, 20)

    def test_previa_antiga_recusada_apos_movimento(self):
        previa = self.previa()
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=9,
            motivo='Movimento fictício posterior', usuario=self.usuario)
        pagina = self.enviar(previa)
        self.assertTrue(pagina.context['erro'])
        self.assertNotContains(pagina, 'id="confirmar-atualizacao-resultado"')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_saldo_aplicado_historico_independente_do_atual(self):
        self.enviar(self.previa())
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7,
            motivo='Movimento fictício posterior', usuario=self.usuario)
        pagina = self.client.get(self.url)
        self.assertContains(pagina, 'Saldo registrado: 8 CX')
        self.assertEqual(pagina.context['linhas'][0]['atualizacao']['atual'], 7)
        self.assertNotContains(pagina, 'id="revisar-atualizacao"')

    def test_confirmacao_token_flag_e_permissao_exigidos(self):
        previa = self.previa()
        self.client.post(self.url, dict(acao='atualizar', previa=previa))
        self.enviar('adulterado')
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=False):
            self.enviar(previa)
        consulta = get_user_model().objects.create_user('sem-aprovacao')
        self.client.force_login(consulta)
        self.enviar(previa)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_falha_no_lote_reverte_primeiro_ajuste(self):
        self.segunda()
        previa = self.previa()
        original = ajustes.conferir_ou_ajustar_estoque
        chamadas = []
        def aplicar(*args, **kwargs):
            chamadas.append(args)
            if len(chamadas) == 2:
                raise ValueError('Falha fictícia no segundo item')
            return original(*args, **kwargs)
        with patch.object(ajustes, 'conferir_ou_ajustar_estoque', side_effect=aplicar):
            self.enviar(previa)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(self.fresca().item.produto.quantidade, 10)

    def test_saldo_atual_mais_diferenca_documentada(self):
        from .tests_conciliacao_documental import ConciliacaoDocumentalTests
        ConciliacaoDocumentalTests.preparar_conferencia_um(self)
        ConciliacaoDocumentalTests.vender(self)
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.enviar(self.previa())
        registro = AplicacaoAjuste.objects.get()
        self.assertEqual((registro.antes, registro.diferenca, registro.depois), (4, -4, 0))


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ResultadoBrowserTests(AjustesFixture, StaticLiveServerTestCase):
    def demonstrar(self, largura):
        outra = self.segunda()
        RodadaContagem.objects.filter(pk=outra.pk).update(contado_base=17)
        url = self.live_server_url + reverse('inventario:detalhe', args=[self.inv.pk])
        pasta = Path('diagnostics/atualizacao-resultado')
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='resultado-atualizacao-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls': ['https://*']})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': largura < 800})
                tab.call('Page.navigate', {'url': url})
                tab.wait('!!document.getElementById("revisar-atualizacao")')
                def capturar(etapa):
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    rect = tab.evaluate('(()=>{const r=document.querySelector(".resumo-inventario").getBoundingClientRect();return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1}})()')
                    (pasta / f'{etapa}-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True, 'clip': rect})['data']))
                capturar('antes')
                tab.evaluate('document.getElementById("revisar-atualizacao").click();true')
                tab.wait('!!document.getElementById("confirmar-atualizacao-resultado")')
                self.assertFalse(AplicacaoAjuste.objects.exists())
                capturar('previa')
                tab.evaluate('document.querySelector("#confirmar-atualizacao-resultado button").click();true')
                tab.wait('!!document.querySelector(".saldo-aplicado")')
                self.assertFalse(tab.evaluate('!!document.getElementById("revisar-atualizacao")'))
                self.assertEqual(AplicacaoAjuste.objects.count(), 1)
                self.assertEqual(self.fresca().item.produto.quantidade, 8)
                capturar('depois')
            finally:
                chrome.stop()

    def test_desktop(self):
        self.demonstrar(1280)

    def test_mobile_390(self):
        self.demonstrar(390)

    def test_mobile_320(self):
        self.demonstrar(320)
