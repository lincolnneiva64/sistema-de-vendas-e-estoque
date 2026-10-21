"""Chronological presentation only, using fictitious local fixtures."""
import base64
from datetime import datetime, timedelta
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Compra, ItemCompra, ItemVenda, Venda
from offline.browser_support import Chrome
from . import services
from .tests import produto
from .tests_browser import CHROME
from .tests_historico import HistoricoFixture


class CronologiaFixture(HistoricoFixture):
    def setUp(self):
        super().setUp()
        self.ok = datetime(2026, 10, 20, 8, 11, tzinfo=ZoneInfo('America/Belem'))
        self.inicio = self.ok - timedelta(days=2)
        Compra.objects.filter(pk=self.compra.pk).update(
            estoque_entrada_realizada_em=self.inicio, data_compra=self.inicio.date())
        self.produto.nome = 'Cola 6/2L — demonstração fictícia'
        self.produto.save(update_fields=['nome'])
        self.item.cadastro = dict(self.item.cadastro, nome=self.produto.nome)
        self.item.save(update_fields=['cadastro'])
        self.rodada.ok_em = self.ok
        self.rodada.esperado = 15
        self.rodada.contado_base = 10
        self.rodada.save()
        self.anterior = self.registrar_venda(self.ok.replace(day=19, hour=10, minute=55))
        self.posterior = self.registrar_venda(self.ok.replace(hour=16, minute=19))
        self.outra = self.registrar_venda(self.ok.replace(hour=18, minute=30))

    def registrar_venda(self, instante):
        venda = Venda.objects.create(data_venda=instante.date())
        Venda.objects.filter(pk=venda.pk).update(criado_em=instante)
        return ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=1,
            unidade='PCT', preco_unitario=0, valor_total=0)


class CronologiaTests(CronologiaFixture, TestCase):
    def resposta(self, **params):
        resposta = self.client.get(self.url, params)
        self.assertEqual(resposta.status_code, 200)
        return resposta

    def test_ok_entre_vendas_e_get_somente_leitura(self):
        with CaptureQueriesContext(connection) as consultas:
            r = self.resposta()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT', '(SELECT')) for q in consultas))
        eventos = list(r.context['extrato'])
        self.assertEqual([e['instante'] for e in eventos], sorted(e['instante'] for e in eventos))
        self.assertEqual([e['fonte'] for e in eventos], ['compra', 'venda', 'conferencia', 'venda', 'venda'])
        self.assertContains(r, 'Conferência física — OK', count=1)
        self.assertNotContains(r, 'ÚLTIMA ETAPA')
        self.assertEqual(r.context['esperado'], 15)
        self.assertEqual(r.context['fisica'], 10)
        self.assertEqual(r.context['diferenca'], -5)
        self.assertContains(r, 'não representam o saldo atual')

    def test_intervalo_anterior_mantem_ok_identificado(self):
        r = self.resposta(periodo='personalizado', data_inicial='2026-10-19', data_final='2026-10-19')
        self.assertEqual([e['fonte'] for e in r.context['extrato']], ['venda', 'conferencia'])
        self.assertContains(r, 'Referência posterior ao período investigado')
        self.assertEqual(r.context['referencia'], self.ok)

    def test_intervalo_posterior_mantem_ok_identificado(self):
        r = self.resposta(periodo='personalizado', data_inicial='2026-10-21', data_final='2026-10-21')
        self.assertContains(r, 'Referência anterior ao período investigado')
        self.assertEqual([e['fonte'] for e in r.context['extrato']], ['conferencia'])

    def test_limite_exclusivo_no_instante_do_ok(self):
        # The next purchase ends a purchase-specific period exactly at the OK.
        compra = Compra.objects.create(data_compra=self.ok.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.ok)
        ItemCompra.objects.create(compra=compra, produto=self.produto, quantidade=1,
            unidade='PCT', preco_unitario=0, valor_total=0)
        r = self.resposta(periodo='compra', compra=self.compra.pk, intervalo='entre_compras')
        self.assertEqual(r.context['fim'], self.ok)
        self.assertContains(r, 'Referência posterior ao período investigado')

    def test_venda_ordenada_por_registro_nao_data_comercial(self):
        Venda.objects.filter(pk=self.posterior.venda_id).update(data_venda=self.inicio.date()-timedelta(days=10))
        r = self.resposta()
        self.assertTrue(any(e['fonte']=='venda' and e['pk']==self.posterior.pk for e in r.context['extrato']))

    def test_compras_e_cancelamentos_posteriores_ordenados(self):
        instante = self.ok + timedelta(hours=1)
        compra = Compra.objects.create(data_compra=instante.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=instante)
        ItemCompra.objects.create(compra=compra, produto=self.produto, quantidade=1,
            unidade='PCT', preco_unitario=0, valor_total=0)
        Venda.objects.filter(pk=self.anterior.venda_id).update(cancelada=True,
            estoque_devolvido_cancelamento=True, estoque_devolvido_cancelamento_em=instante+timedelta(hours=1))
        eventos = list(self.resposta().context['extrato'])
        self.assertEqual([e['fonte'] for e in eventos],
            ['compra', 'venda', 'conferencia', 'compra', 'cancelamento', 'venda', 'venda'])

    def test_paginacao_inclui_ok_uma_vez_no_meio(self):
        for i in range(55):
            self.registrar_venda(self.ok+timedelta(minutes=i+1))
        primeiro = self.resposta()
        segundo = self.resposta(extrato_pagina=2)
        self.assertContains(primeiro, 'data-evento="ok-conferencia"', count=1)
        self.assertNotContains(segundo, 'data-evento="ok-conferencia"')
        self.assertEqual(len(primeiro.context['extrato']), 50)
        self.assertEqual(primeiro.context['extrato'].paginator.count, 60)

    def test_empate_deterministico_e_sem_ok_inventado(self):
        self.registrar_venda(self.ok)
        a = [(e['instante'], e['fonte'], e['pk']) for e in self.resposta().context['extrato']]
        b = [(e['instante'], e['fonte'], e['pk']) for e in self.resposta().context['extrato']]
        self.assertEqual(a, b)
        self.rodada.ok_em = None
        self.rodada.save()
        r = self.resposta()
        self.assertFalse(any(e['fonte']=='conferencia' for e in r.context['extrato']))
        self.assertContains(r, 'Sem OK confirmado')

    def test_tabela_a_preserva_intervalo_e_comparacao_terminal(self):
        r = self.resposta(visual='a')
        self.assertEqual(r.context['fim'], self.ok)
        self.assertFalse(any(e['fonte']=='conferencia' for e in r.context['extrato']))
        self.assertContains(r, 'ÚLTIMA ETAPA · REFERÊNCIA DA CONFERÊNCIA')
        self.assertEqual([e['fonte'] for e in r.context['extrato']], ['compra', 'venda', 'venda', 'venda'])
        # A preserves its existing boundary-day business-date candidates.
        self.assertContains(r, 'Referência da conferência')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class CronologiaBrowserTests(CronologiaFixture, StaticLiveServerTestCase):
    def conferir(self, largura):
        with TemporaryDirectory(prefix='cronologia-chrome-') as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value,
                    'url':self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', {'width':largura, 'height':900,
                    'deviceScaleFactor':1, 'mobile':largura<768})
                for nome, minuto in (('cola-pacote',11), ('cola-lata',14)):
                    # Update fictitious test fixtures only, then render the real page.
                    self.item.cadastro = dict(self.item.cadastro, nome=('Cola 6/2L' if minuto == 11 else 'Cola lata 12/350ml') + ' — simulação fictícia')
                    self.item.save(update_fields=['cadastro'])
                    self.rodada.ok_em = self.ok.replace(minute=minuto)
                    self.rodada.save()
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait("!!document.querySelector('.evento-conferencia')")
                    tab.wait("getComputedStyle(document.querySelector('.evento-conferencia')).display === 'block'")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    fontes = tab.evaluate("[...document.querySelectorAll('[data-registro]')].map(e=>e.dataset.registro.split('-')[1])")
                    self.assertEqual(fontes, ['compra','venda','conferencia','venda','venda'])
                    self.assertEqual(tab.evaluate("document.querySelectorAll('[data-evento=ok-conferencia]').length"),1)
                    self.assertIn('Conferência física — OK', tab.evaluate("document.querySelector('.evento-conferencia').innerText"))
                    pasta = os.environ.get('INVENTARIO_VISUAL_ARTIFACTS')
                    if pasta:
                        destino = Path(pasta)
                        destino.mkdir(parents=True, exist_ok=True)
                        dimensoes = tab.call('Page.getLayoutMetrics')['cssContentSize']
                        dados = tab.call('Page.captureScreenshot', {'format':'png', 'captureBeyondViewport':True,
                            'clip':dict(x=0,y=0,width=dimensoes['width'],height=dimensoes['height'],scale=1)})
                        (destino/f'{nome}-{largura}.png').write_bytes(base64.b64decode(dados['data']))
            finally:
                chrome.stop()

    def test_desktop_1440(self):
        self.conferir(1440)

    def test_mobile_390(self):
        self.conferir(390)

    def test_mobile_320(self):
        self.conferir(320)
