import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from estoque.models import Fornecedor, ProdutoFornecedor, Funcionario, Produto
from offline.browser_support import Chrome
from . import services
from .models import AplicacaoAjuste, RodadaContagem
from .tests import produto
from .tests_browser import CHROME
from .tests_links_atribuicao import Elementos


class DadosLegibilidade:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('gestor-visual', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Funcionário fictício')
        fornecedor = Fornecedor.objects.create(nome='Fornecedor fictício — revisão visual')
        self.produtos = [produto(nome=nome, quantidade=saldo, unidade_compra='UN',
            unidade_venda_1='UN', vende_fracionado=False) for nome, saldo in
            [('Arroz', 10), ('Feijão', 20), ('Açúcar', 12), ('Café', 5), ('Farinha', 5)]]
        for p in self.produtos:
            ProdutoFornecedor.objects.create(produto=p, fornecedor=fornecedor)
        self.inv = services.criar_inventario(self.user, criterio='fornecedor', selecionados=fornecedor.pk)
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk'))
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.client.force_login(self.user)

    def distribuir(self):
        self.inv.refresh_from_db()
        return services.distribuir_produtos(self.user, self.inv.pk, self.funcionario.pk,
            [r.pk for r in self.rodadas[:3]], 24, self.inv.versao)[0]

    def concluir(self, acesso):
        for rodada, quantidade in zip(self.rodadas, ['8', '22', '12']):
            services.salvar_contagem(acesso.pk, rodada.pk, rodada.versao, quantidade, '', True)
        services.sinalizar_fisico(self.rodadas[2].pk, 'Dúvida física fictícia aguardando evidência.',
            str(uuid4()), usuario=self.user)
        self.inv.refresh_from_db()
        services.finalizar_conferencia(acesso.pk, self.inv.versao)

    def snapshot(self):
        return {**{m._meta.label: list(m.objects.order_by('pk').values())
                   for m in apps.get_app_config('inventario').get_models()},
                'saldos': list(Produto.objects.order_by('pk').values_list('pk', 'quantidade'))}


class LegibilidadeTests(DadosLegibilidade, TestCase):
    def test_distribuicao_prioritaria_antes_e_recolhida_depois_sem_campos_inuteis(self):
        resposta = self.client.get(self.url)
        html = resposta.content.decode()
        self.assertLess(html.index('id="distribuicao"'), html.index('id="titulo-resumo"'))
        self.assertIn('open', Elementos(html).ids['distribuicao'])
        self.distribuir()
        resposta = self.client.get(self.url)
        html = resposta.content.decode()
        self.assertGreater(html.index('id="distribuicao"'), html.index('id="titulo-resumo"'))
        self.assertNotIn('open', Elementos(html).ids['distribuicao'])
        self.assertNotContains(resposta, 'id="form-atribuicao"')
        self.assertContains(resposta, 'Todos os produtos já estão atribuídos')

    def test_consulta_e_historico_nao_alteram_dados(self):
        acesso = self.distribuir()
        self.concluir(acesso)
        antes = self.snapshot()
        resposta = self.client.get(self.url)
        self.assertNotContains(resposta, 'id="form-atribuicao"')
        self.assertContains(resposta, 'Movimentação física sinalizada sem resolução')
        historico = self.client.get(reverse('inventario:historico_produto', args=[self.inv.pk, self.rodadas[0].pk]))
        self.assertContains(historico, self.url + '#titulo-resumo')
        self.assertEqual(antes, self.snapshot())


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class LegibilidadeBrowserTests(DadosLegibilidade, StaticLiveServerTestCase):
    def demonstrar(self, largura):
        pasta = Path('diagnostics/etapa3-nativas')
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='etapa3-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls': ['https://*']})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 900, 'deviceScaleFactor': 1, 'mobile': largura < 800})
                def abrir():
                    tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                    tab.wait('window.inventarioRevisaoPronta===true')
                def capturar(etapa, foco='.resumo-inventario'):
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    tab.evaluate('window.scrollTo(0,0);true')
                    metrics = tab.call('Page.getLayoutMetrics')
                    tamanho = metrics.get('cssContentSize', metrics['contentSize'])
                    dados = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True,
                        'clip': {'x': 0, 'y': 0, 'width': largura, 'height': tamanho['height'], 'scale': 1}})
                    (pasta / f'{etapa}-{largura}-pagina-inteira.png').write_bytes(base64.b64decode(dados['data']))
                    tab.evaluate('document.querySelector('+json.dumps(foco)+').scrollIntoView({block:"start",behavior:"instant"});true')
                    dados = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
                    (pasta / f'{etapa}-{largura}-viewport.png').write_bytes(base64.b64decode(dados['data']))
                abrir()
                self.assertTrue(tab.evaluate('document.getElementById("distribuicao").open'))
                capturar('01-distribuir', '#distribuicao')
                tab.evaluate('document.querySelector("[name=funcionario]").value="'+str(self.funcionario.pk)+'";document.querySelectorAll("[name=rodadas]").forEach(c=>c.checked='+json.dumps([r.pk for r in self.rodadas[:3]])+'.includes(Number(c.value)));document.getElementById("form-atribuicao").dispatchEvent(new Event("change",{bubbles:true}));document.getElementById("confirmar-atribuicao").click();true')
                tab.wait('!!document.getElementById("link-funcionario")')
                self.assertFalse(tab.evaluate('document.getElementById("distribuicao").open'))
                self.assertFalse(tab.evaluate('!!document.getElementById("form-atribuicao")'))
                capturar('02-distribuida')
                self.concluir(self.inv.acessos.get())
                abrir()
                self.assertFalse(tab.evaluate('!!document.getElementById("form-atribuicao")'))
                self.assertFalse(tab.evaluate('document.getElementById("distribuicao").open'))
                self.assertTrue(tab.evaluate('[...document.querySelectorAll(".detalhe-produto")].every(d=>!d.open)'))
                self.assertGreaterEqual(tab.evaluate('parseFloat(getComputedStyle(document.querySelector(".numero-painel>span")).fontSize)'), 17)
                self.assertEqual(tab.evaluate('document.querySelectorAll(".resumo-linha").length'), 3)
                capturar('03-concluida')
                tab.evaluate('document.querySelector(".evolucao-estoque").click();true')
                tab.wait('!!document.getElementById("voltar-resultados")')
                capturar('04-evolucao', '#voltar-resultados')
                tab.evaluate('document.getElementById("voltar-resultados").click();true')
                tab.wait('location.hash==="#titulo-resumo" && window.inventarioRevisaoPronta===true')
                self.assertTrue(tab.evaluate('document.getElementById("titulo-resumo").getBoundingClientRect().top>=0 && document.getElementById("titulo-resumo").getBoundingClientRect().top<innerHeight'))
                tab.evaluate('document.querySelector("a[data-detalhe]").click();true')
                tab.wait('document.querySelector(".detalhe-produto").open')
                capturar('05-investigacao', '.detalhe-produto')
                tab.evaluate('document.querySelector(".detalhe-produto").open=false;document.getElementById("revisar-atualizacao").click();true')
                tab.wait('!!document.getElementById("confirmar-atualizacao-resultado")')
                capturar('06-previa', '#atualizacao-resultado')
                tab.evaluate('document.querySelector("#confirmar-atualizacao-resultado button").click();true')
                tab.wait('document.querySelectorAll(".saldo-aplicado").length===2')
                self.assertFalse(tab.evaluate('!!document.getElementById("revisar-atualizacao")'))
                self.assertTrue(tab.evaluate('document.querySelector(".estado-bloqueado").textContent.includes("Bloqueado")'))
                self.assertEqual(AplicacaoAjuste.objects.count(), 2)
                self.assertEqual(list(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[:3]]).order_by('pk').values_list('quantidade', flat=True)), [8, 22, 12])
                capturar('07-aplicada')
            finally:
                chrome.stop()

    def test_desktop_1440(self):
        self.demonstrar(1440)

    def test_mobile_390(self):
        self.demonstrar(390)

    def test_mobile_320(self):
        self.demonstrar(320)
