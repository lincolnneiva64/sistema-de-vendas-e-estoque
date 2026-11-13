"""Visual fractional cost calculation; only disposable PostgreSQL fixtures."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from estoque import tests_apresentacao_vinculada as apresentacao
from estoque.tests_frisco_vinculo import CHROME
from estoque.models import AlteracaoPrecoVinculado, MembroGrupoProduto
from offline.browser_support import Chrome


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class CustoFracionadoBrowserTests(apresentacao.ApresentacaoFixture, StaticLiveServerTestCase):
    abrir = apresentacao.ApresentacaoBrowserTests.abrir
    tocar = apresentacao.ApresentacaoBrowserTests.tocar

    def setUp(self):
        super().setUp()
        # Enable the actual persisted CX/18 UN state through the published
        # presentation command. Stored fractional costs must remain zero.
        dados = self.dados()
        previa = self.client.post(self.grupo_url, dados).json()
        resposta = self.confirmar(dados, previa)
        self.assertEqual(resposta.status_code, 200, resposta.content)

    def custo(self, t):
        return Decimal(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value").replace(',', '.'))

    def editar(self, t, seletor, valor):
        import json
        t.evaluate('(()=>{const e=document.querySelector('+json.dumps(seletor)+');e.value='+json.dumps(valor)+';e.dispatchEvent(new Event("input",{bubbles:true}));return true})()')

    def foto(self, t, largura):
        pasta = Path('amostras/custo-fracionado'); pasta.mkdir(parents=True, exist_ok=True)
        t.evaluate("document.querySelector('#id_preco_compra_fracionado').scrollIntoView({block:'center',behavior:'instant'});true")
        (pasta/(str(largura)+'-custo-calculado.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot', {'format':'png'})['data']))

    def fluxo(self, largura):
        antes = self.estado()
        eventos = list(AlteracaoPrecoVinculado.objects.order_by('pk').values())
        vinculos = list(MembroGrupoProduto.objects.order_by('pk').values())
        with TemporaryDirectory(prefix='custo-fracionado-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(CHROME, perfil).start()
            try:
                t = browser.tab()
                t.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
                if largura < 820:
                    t.call('Emulation.setTouchEmulationEnabled', dict(enabled=True,maxTouchPoints=1))
                t.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                self.abrir(t, self.produtos[0])
                self.assertEqual(self.custo(t), Decimal('.65'))
                self.foto(t, largura)
                self.assertEqual(self.estado(), antes)
                vendas = t.evaluate("['id_preco_vista','id_preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'].map(id=>document.getElementById(id).value)")
                self.editar(t, '#id_preco_compra', '18,00')
                self.assertEqual(self.custo(t), Decimal('1.00'))
                self.editar(t, '#fator_visual', '24')
                self.assertEqual(self.custo(t), Decimal('.75'))
                self.assertEqual(t.evaluate("['id_preco_vista','id_preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'].map(id=>document.getElementById(id).value)"), vendas)
                self.editar(t, '#fator_visual', '')
                self.assertEqual(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value"), '')
                self.assertEqual(t.evaluate("document.querySelector('#id_fator_conversao').value"), '')
                self.editar(t, '#fator_visual', '0')
                self.assertEqual(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value"), '')
                self.editar(t, '#id_preco_compra', '11,70')
                self.editar(t, '#fator_visual', '18')
                self.assertEqual(self.custo(t), Decimal('.65'))
                self.assertEqual(self.estado(), antes)
                # A blocked factor change still shows the coordinated preview,
                # then cancel/reopen confirms that neither cost nor stock saved.
                self.editar(t, '#fator_visual', '24')
                self.assertEqual(self.custo(t), Decimal('.49'))
                self.tocar(t, '.btn-salvar')
                t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                self.assertTrue(t.evaluate("document.querySelector('[data-confirmar-apresentacao]').disabled"))
                self.assertIn('saldo ou o mínimo', t.evaluate("document.querySelector('#confirmarApresentacao').textContent"))
                self.tocar(t, '[data-cancelar-apresentacao]')
                self.abrir(t, self.produtos[0])
                self.assertEqual(self.custo(t), Decimal('.65'))
                self.assertEqual(self.estado(), antes)
                self.assertEqual(list(AlteracaoPrecoVinculado.objects.order_by('pk').values()), eventos)
                self.assertEqual(list(MembroGrupoProduto.objects.order_by('pk').values()), vinculos)
            finally:
                browser.stop()

    def test_desktop_carregar_compra_fator_sem_gravacao_colateral(self):
        self.fluxo(1280)

    def test_mobile_carregar_compra_fator_sem_gravacao_colateral(self):
        self.fluxo(390)
