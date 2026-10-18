from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.test.utils import CaptureQueriesContext
from django.db import connection
from estoque.models import Compra, ItemCompra, Venda, ItemVenda, ItemVendaRemovido
from .models import MovimentoObservado
from . import services
from .tests import produto
from .tests_browser import CHROME
from offline.browser_support import Chrome


class SeletorCompra(HTMLParser):
    """Match the browser default when no option has an explicit selection."""
    def __init__(self, html):
        super().__init__()
        self.ativo = False
        self.opcoes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.ativo = attrs.get('name') == 'compra'
        if tag == 'option' and self.ativo:
            self.opcoes.append((attrs.get('value'), 'selected' in attrs))

    def handle_endtag(self, tag):
        if tag == 'select':
            self.ativo = False

    @property
    def valor(self):
        return next((valor for valor, selecionada in self.opcoes if selecionada), self.opcoes[0][0])


class HistoricoFixture:
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('hist-gestor', '', 'teste')
        self.produto = produto()
        self.inv = services.criar_inventario(self.gestor, 'Historico ficticio', 'manual', [self.produto.pk])
        self.item = self.inv.itens.get()
        self.rodada = self.item.rodadas.get()
        self.agora = timezone.now()
        self.inicio = self.agora - timedelta(days=4)
        self.compra = Compra.objects.create(data_compra=self.inicio.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.inicio)
        ItemCompra.objects.create(compra=self.compra, produto=self.produto, quantidade=2, unidade='CX', preco_unitario=0, valor_total=0)
        self.rodada.ok_em = self.agora
        self.rodada.esperado = 10
        self.rodada.contado_base = 8
        self.rodada.save()
        self.url = reverse('inventario:historico_produto', args=[self.inv.pk, self.rodada.pk])
        self.client.force_login(self.gestor)

    def venda(self, **kwargs):
        venda = Venda.objects.create(data_venda=self.inicio.date()+timedelta(days=1), operador='Operador teste', **kwargs)
        return ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=3, unidade='KG', preco_unitario=0, valor_total=0)

    def compras_anteriores(self):
        compras = []
        for numero in range(1, 32):
            data = self.inicio - timedelta(days=numero)
            compra = Compra.objects.create(data_compra=data.date(), estoque_entrada_realizada=True,
                                            estoque_entrada_realizada_em=data)
            ItemCompra.objects.create(compra=compra, produto=self.produto, quantidade=numero,
                                      unidade='CX', preco_unitario=0, valor_total=0)
            compras.append(compra)
        return compras


class HistoricoTests(HistoricoFixture, TestCase):
    def test_compra_selecionada_fora_da_pagina_troca_e_retorno(self):
        antigas = self.compras_anteriores()

        def conferir(resposta, compra):
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta.context['compra'].compra_id, compra.pk)
            self.assertEqual(resposta.context['inicio'], compra.estoque_entrada_realizada_em)
            seletor = SeletorCompra(resposta.content.decode())
            self.assertEqual(seletor.valor, str(compra.pk))
            self.assertEqual(sum(valor == str(compra.pk) for valor, _ in seletor.opcoes), 1)
            self.assertContains(resposta, f'Entrada registrada · Compra #{compra.pk}')

        resposta = self.client.get(self.url, {'compra': self.compra.pk, 'intervalo': 'entre_compras'})
        conferir(resposta, self.compra)
        resposta = self.client.get(self.url + resposta.context['compras'].proxima_url)
        self.assertEqual(resposta.context['compras'].number, 2)
        self.assertNotIn(self.compra.pk, [opcao.compra_id for opcao in resposta.context['compras']])
        conferir(resposta, self.compra)
        resposta = self.client.get(self.url, {'compra': antigas[-1].pk, 'compras_pagina': 2, 'intervalo': 'entre_compras'})
        conferir(resposta, antigas[-1])
        resposta = self.client.get(self.url + resposta.context['compras'].anterior_url)
        self.assertEqual(resposta.context['compras'].number, 1)
        self.assertEqual(resposta.context['intervalo'], 'entre_compras')
        conferir(resposta, antigas[-1])
        resposta = self.client.get(self.url, {'compra': self.compra.pk, 'compras_pagina': 1})
        conferir(resposta, self.compra)

    def test_documentos_unidades_custo_ausente_e_nao_altera_dados(self):
        self.venda(cancelada=True, estoque_devolvido_cancelamento=True)
        self.item.custo_base = None
        self.item.save()
        antes = list(MovimentoObservado.objects.values())
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'KG')
        self.assertContains(resposta, 'Cancelada')
        self.assertContains(resposta, 'Não disponível')
        self.assertEqual(list(MovimentoObservado.objects.values()), antes)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_multiplas_compras_rascunho_futuro_e_intervalo_anterior(self):
        proxima = self.agora-timedelta(days=2)
        c = Compra.objects.create(data_compra=proxima.date(), estoque_entrada_realizada=True, estoque_entrada_realizada_em=proxima)
        ItemCompra.objects.create(compra=c, produto=self.produto, quantidade=1, unidade='FD', preco_unitario=0, valor_total=0)
        rascunho = Compra.objects.create(data_compra=self.agora.date())
        ItemCompra.objects.create(compra=rascunho, produto=self.produto, quantidade=999, unidade='SC', preco_unitario=0, valor_total=0)
        self.assertEqual(self.client.get(self.url).context['compra'].compra_id, c.pk)
        resposta = self.client.get(self.url, {'compra': self.compra.pk, 'intervalo': 'entre_compras'})
        self.assertEqual(resposta.context['fim'], proxima)
        self.assertEqual(resposta.context['compras'].paginator.count, 2)

    def test_permissoes_e_escopo_da_rodada(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        funcionario = get_user_model().objects.create_user('cego')
        self.client.force_login(funcionario)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.get(reverse('inventario:historico_produto', args=[9999, self.rodada.pk])).status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_historico_incompleto_sem_compra_datada(self):
        Compra.objects.filter(pk=self.compra.pk).update(estoque_entrada_realizada_em=None)
        resposta = self.client.get(self.url)
        self.assertIsNone(resposta.context['inicio'])
        self.assertEqual(resposta.context['vendas'].paginator.count, 0)
        self.assertContains(resposta, 'nenhuma compra')

    def test_movimentos_consumo_estorno_ajuste_desconhecido_sem_duplicar(self):
        for seq, origem in enumerate(('compra', 'venda', 'consumo_proprio', 'devolucao_venda', 'estorno_compra', 'ajuste_manual', 'nao_identificada'), 1):
            MovimentoObservado.objects.create(produto=self.produto, sequencia=seq, antes=10, depois=9,
                registrado_em=self.inicio+timedelta(hours=seq), origem=origem)
        self.venda(tipo_pagamento='consumo_proprio')
        ItemVendaRemovido.objects.create(venda=Venda.objects.first(), produto=self.produto,
            produto_nome_snapshot='Teste', quantidade_snapshot=2, unidade_snapshot='UN', preco_unitario_snapshot=0, valor_total_snapshot=0,
            estoque_devolvido=True, estoque_devolvido_em=self.agora)
        ItemVendaRemovido.objects.update(criado_em=self.inicio+timedelta(days=1))
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.context['movimentos'].paginator.count, 7)
        self.assertEqual(resposta.context['removidos'].paginator.count, 1)
        self.assertContains(resposta, 'Não somado novamente')

    def test_unidades_documentais_agrupadas_sem_conversao_atual(self):
        for unidade in ('CX', 'FD', 'SC', 'KG', 'UN'):
            ItemCompra.objects.create(compra=self.compra, produto=self.produto, quantidade=1,
                                      unidade=unidade, preco_unitario=0, valor_total=0)
        resposta = self.client.get(self.url)
        totais = {g['unidade']: g['total'] for g in resposta.context['itens_compra']}
        self.assertEqual(totais, {'CX': 3, 'FD': 1, 'SC': 1, 'KG': 1, 'UN': 1})
        self.assertContains(resposta, 'Saldo reconstruído: <strong>Não disponível</strong>')
        self.assertEqual(resposta.context['compras'].paginator.count, 1)

    def test_movimentos_fora_do_intervalo_e_saldos_nulos(self):
        for seq, data in enumerate((self.inicio-timedelta(seconds=1), self.inicio, self.agora), 1):
            MovimentoObservado.objects.create(produto=self.produto, sequencia=seq, antes=None,
                depois=None, registrado_em=data)
        resposta = self.client.get(self.url)
        self.assertEqual([m.sequencia for m in resposta.context['movimentos']], [2])
        self.assertContains(resposta, 'Saldo bruto: Não disponível → Não disponível.')

    def test_paginacao_e_quantidade_constante_de_consultas(self):
        self.venda()
        with CaptureQueriesContext(connection) as poucas:
            self.client.get(self.url)
        venda = Venda.objects.first()
        ItemVenda.objects.bulk_create([ItemVenda(venda=venda, produto=self.produto, quantidade=1, unidade='UN', preco_unitario=0, valor_total=0) for _ in range(100)])
        with CaptureQueriesContext(connection) as muitas:
            resposta = self.client.get(self.url, {'vendas_pagina': 2})
        self.assertLessEqual(len(muitas), len(poucas)+1)
        self.assertEqual(len(resposta.context['vendas']), 50)
        self.assertContains(resposta, 'Página 2 de 3')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponivel')
class HistoricoBrowserTests(HistoricoFixture, StaticLiveServerTestCase):
    def test_paginacao_compra_seletor_corresponde_ao_historico(self):
        antigas = self.compras_anteriores()
        with TemporaryDirectory(prefix='historico-paginacao-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value,
                                               'url': self.live_server_url})

                def conferir(compra, pagina):
                    tab.wait(f"document.querySelector('input[name=compras_pagina]')?.value === '{pagina}' && document.querySelector('section.evento-compra h2')?.textContent.endsWith('#{compra.pk}')")
                    self.assertEqual(tab.evaluate("document.querySelector('select[name=compra]').value"), str(compra.pk))

                for largura in (1366, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 844,
                        'deviceScaleFactor': 1, 'mobile': largura < 768})
                    tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                    conferir(self.compra, 1)
                    tab.evaluate("document.querySelector('nav a[href*=compras_pagina]').click(); true")
                    conferir(self.compra, 2)
                    # Submit without changing the selection: the original bug silently changed it.
                    tab.evaluate("document.querySelector('form button').click(); true")
                    tab.wait("new URL(location.href).searchParams.get('compra') === '" + str(self.compra.pk) + "'")
                    conferir(self.compra, 2)
                    tab.evaluate(f"document.querySelector('select[name=compra]').value='{antigas[-1].pk}'; document.querySelector('form button').click(); true")
                    conferir(antigas[-1], 2)
                    tab.evaluate("document.querySelector('nav a[href*=compras_pagina]').click(); true")
                    conferir(antigas[-1], 1)
                    tab.evaluate(f"document.querySelector('select[name=compra]').value='{self.compra.pk}'; document.querySelector('form button').click(); true")
                    conferir(self.compra, 1)
            finally:
                chrome.stop()

    def test_desktop_mobile_recolhido_e_consulta(self):
        self.venda()
        with TemporaryDirectory(prefix='historico-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<768})
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait("!!document.querySelector('select[name=compra]')")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('details')].every(e=>!e.open)"))
                    tab.evaluate("document.querySelector('details').open=true; true")
                    self.assertTrue(tab.evaluate("!!document.querySelector('details a[href*=vendas]')"))
                    tab.evaluate("document.querySelector('button').click(); true")
                    tab.wait("!!document.querySelector('select[name=compra]')")
            finally:
                chrome.stop()
