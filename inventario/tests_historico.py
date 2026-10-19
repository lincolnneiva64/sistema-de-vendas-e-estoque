from datetime import timedelta
import base64
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
        venda = Venda.objects.create(data_venda=timezone.localtime(self.inicio).date()+timedelta(days=1), operador='Operador teste', **kwargs)
        Venda.objects.filter(pk=venda.pk).update(criado_em=self.inicio+timedelta(days=1))
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
    def test_atalho_visivel_por_produto_preserva_investigacao(self):
        from .models import RodadaContagem
        outro = produto(nome='Outro produto fictício')
        inv = services.criar_inventario(self.gestor, 'Navegação fictícia', 'manual',
                                       [self.produto.pk, outro.pk])
        resposta = self.client.get(reverse('inventario:detalhe', args=[inv.pk]))
        self.assertContains(resposta, 'class="evolucao-estoque"', count=2)
        for rodada in RodadaContagem.objects.filter(item__inventario=inv):
            destino = reverse('inventario:historico_produto', args=[inv.pk, rodada.pk])
            self.assertContains(resposta, f'class="evolucao-estoque" href="{destino}"')
            self.assertEqual(self.client.get(destino).context['rodada'].pk, rodada.pk)
        self.assertContains(resposta, 'class="historico-comercial"', count=2)
        self.assertContains(resposta, 'data-detalhe=', count=2)

    def test_comparacao_apresenta_sinal_sem_mudar_valores_do_ok(self):
        for contado, sinal, classe in ((12, '+2', 'sobra'), (8, '-2', 'falta'), (10, '0', '')):
            with self.subTest(contado=contado):
                self.rodada.contado_base = contado
                self.rodada.save(update_fields=['contado_base'])
                resposta = self.client.get(self.url)
                self.assertEqual(resposta.context['diferenca'], contado-10)
                self.assertContains(resposta, f'class="comparacao {classe}"')
                self.assertContains(resposta, f'<dd>{sinal} <small>')
                self.assertContains(resposta, 'Valores registrados no OK de')

    def test_extrato_unico_cronologico_fontes_e_saldos_nao_presumidos(self):
        venda = self.venda()
        MovimentoObservado.objects.create(produto=self.produto, sequencia=1, antes=30, depois=130,
            registrado_em=self.inicio + timedelta(hours=1), origem='compra', referencia=f'Compra #{self.compra.pk}')
        removido = ItemVendaRemovido.objects.create(venda=venda.venda, produto=self.produto,
            produto_nome_snapshot='Teste', quantidade_snapshot=2, unidade_snapshot='UN',
            preco_unitario_snapshot=0, valor_total_snapshot=0, estoque_devolvido=True)
        ItemVendaRemovido.objects.filter(pk=removido.pk).update(criado_em=self.inicio + timedelta(days=2))
        resposta = self.client.get(self.url)
        linhas = list(resposta.context['extrato'])
        self.assertEqual([l['fonte'] for l in linhas], ['compra', 'digital', 'venda', 'remocao'])
        self.assertEqual(linhas[0]['entrada'], 2)
        self.assertEqual(linhas[2]['saida'], 3)
        self.assertTrue(all(l['saldo'] is None for l in linhas))
        self.assertIsNone(linhas[1]['entrada'])
        self.assertIsNone(linhas[3]['entrada'])
        self.assertEqual(linhas[0]['url'], reverse('estoque:compras_detalhe', args=[self.compra.pk]))
        self.assertEqual(linhas[2]['url'], reverse('estoque:venda_detalhe', args=[venda.venda_id]))
        self.assertContains(resposta, '<table>', count=1)
        self.assertContains(resposta, 'horários são de registro')
        self.assertEqual(resposta.context['diferenca'], -2)
        self.assertContains(resposta, 'class="comparacao falta"')

    def test_paginacao_unificada_sem_perdas_entre_fontes_e_filtros(self):
        self.venda()
        MovimentoObservado.objects.bulk_create([MovimentoObservado(produto=self.produto, sequencia=i,
            antes=10, depois=9, registrado_em=self.inicio + timedelta(minutes=i), origem='ajuste_manual')
            for i in range(1, 61)])
        primeira = self.client.get(self.url, {'compra': self.compra.pk, 'intervalo': 'entre_compras'})
        segunda = self.client.get(self.url + primeira.context['extrato'].proxima_url)
        ids = [r['detalhe_id'] for resposta in (primeira, segunda) for r in resposta.context['extrato']]
        self.assertEqual(len(ids), 62)
        self.assertEqual(len(set(ids)), 62)
        self.assertEqual(segunda.context['compra'].compra_id, self.compra.pk)
        self.assertEqual(segunda.context['intervalo'], 'entre_compras')
        self.assertNotContains(segunda, 'class="saldo-inicial"')
        self.assertEqual(segunda.context['extrato'].number, 2)

    def test_intervalo_entre_compras_exclui_proxima_e_data_em_portugues(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        data = datetime(2026, 10, 12, 23, 30, tzinfo=ZoneInfo('America/Sao_Paulo'))
        Compra.objects.filter(pk=self.compra.pk).update(estoque_entrada_realizada_em=data)
        self.rodada.ok_em = data + timedelta(days=4)
        self.rodada.save()
        proxima = Compra.objects.create(data_compra=data.date() + timedelta(days=2),
            estoque_entrada_realizada=True, estoque_entrada_realizada_em=data+timedelta(days=2))
        ItemCompra.objects.create(compra=proxima, produto=self.produto, quantidade=7, unidade='SC', preco_unitario=0, valor_total=0)
        resposta = self.client.get(self.url, {'compra': self.compra.pk, 'intervalo': 'entre_compras'})
        compras = [r for r in resposta.context['extrato'] if r['fonte'] == 'compra']
        self.assertEqual([r['documento'] for r in compras], [self.compra.pk])
        self.assertContains(resposta, 'Segunda-feira')
        self.assertContains(resposta, '12/10/2026')

    def test_compra_selecionada_fora_da_pagina_troca_e_retorno(self):
        antigas = self.compras_anteriores()

        def conferir(resposta, compra):
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta.context['compra'].compra_id, compra.pk)
            self.assertEqual(resposta.context['inicio'], compra.estoque_entrada_realizada_em)
            seletor = SeletorCompra(resposta.content.decode())
            self.assertEqual(seletor.valor, str(compra.pk))
            self.assertEqual(sum(valor == str(compra.pk) for valor, _ in seletor.opcoes), 1)
            self.assertContains(resposta, f'data-compra="{compra.pk}"')

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
        self.assertEqual(resposta.context['inicio'], self.agora-timedelta(days=30))
        self.assertEqual(resposta.context['vendas'].paginator.count, 0)
        self.assertContains(resposta, 'Nenhuma compra')

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
        self.assertContains(resposta, 'Reconstruído e corroborado não é snapshot do recebimento')
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
            resposta = self.client.get(self.url, {'extrato_pagina': 2})
        self.assertLessEqual(len(muitas), len(poucas)+1)
        self.assertEqual(len(resposta.context['extrato']), 50)
        self.assertContains(resposta, 'Página 2 de 3')


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponivel')
class HistoricoBrowserTests(HistoricoFixture, StaticLiveServerTestCase):
    def test_previa_visual_desktop_mobile_tabela_links_e_comparacao(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        data = datetime(2026, 10, 12, 9, 0, tzinfo=ZoneInfo('America/Sao_Paulo'))
        Compra.objects.filter(pk=self.compra.pk).update(estoque_entrada_realizada_em=data, data_compra=data.date())
        self.produto.nome = 'Água mineral · Caixa com 12 unidades'
        self.produto.save(update_fields=['nome'])
        self.item.cadastro = dict(self.item.cadastro, nome=self.produto.nome)
        self.item.save(update_fields=['cadastro'])
        self.rodada.ok_em = data + timedelta(days=4)
        self.rodada.esperado = 11
        self.rodada.contado_base = 10
        self.rodada.contado = 10
        # The panel also validates the OK chain; this synthetic OK needs its
        # sequence reference, normally populated by salvar_contagem().
        self.rodada.sequencia_ok = 0
        self.rodada.save()
        anterior = Venda.objects.create(data_venda=(data-timedelta(days=1)).date())
        Venda.objects.filter(pk=anterior.pk).update(criado_em=data-timedelta(days=1))
        ItemVenda.objects.create(venda=anterior, produto=self.produto, quantidade=4, unidade='CX',
            estoque_antes=19, estoque_movimentado=4, estoque_depois=15, estoque_unidade_snapshot='CX',
            preco_unitario=0, valor_total=0)
        saldo = 17
        for dia, quantidade in ((1, 3), (2, 1), (3, 2)):
            venda = Venda.objects.create(data_venda=data.date()+timedelta(days=dia), operador='Mariana Silva')
            Venda.objects.filter(pk=venda.pk).update(criado_em=data+timedelta(days=dia))
            ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=quantidade,
                unidade='CX', estoque_antes=saldo, estoque_movimentado=quantidade,
                estoque_depois=saldo-quantidade, estoque_unidade_snapshot='CX', preco_unitario=0, valor_total=0)
            saldo -= quantidade
        MovimentoObservado.objects.create(produto=self.produto, sequencia=1, antes=None, depois=None,
            registrado_em=data + timedelta(days=2, hours=3), origem='devolucao_venda', referencia='Estorno registrado')
        antiga = Venda.objects.create(data_venda=(data-timedelta(days=5)).date(),
            cancelada=True, estoque_devolvido_cancelamento=True,
            estoque_devolvido_cancelamento_em=data+timedelta(days=2, hours=1))
        Venda.objects.filter(pk=antiga.pk).update(criado_em=data-timedelta(days=5))
        ItemVenda.objects.create(venda=antiga, produto=self.produto, quantidade=1,
            unidade='CX', preco_unitario=0, valor_total=0)
        from estoque.models import MovimentacaoEstoqueManual
        manual = MovimentacaoEstoqueManual.objects.create(produto=self.produto, tipo='ajuste',
            estoque_antes=13, estoque_depois=12, diferenca=-1, motivo='Ajuste ficticio documentado')
        MovimentacaoEstoqueManual.objects.filter(pk=manual.pk).update(criado_em=data+timedelta(days=2,hours=4))
        MovimentoObservado.objects.create(produto=self.produto,sequencia=2,antes=13,depois=12,
            registrado_em=data+timedelta(days=2,hours=4),origem='ajuste_manual',referencia=f'Movimentação manual #{manual.pk}')
        with TemporaryDirectory(prefix='extrato-previa-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value,
                                               'url': self.live_server_url})
                for visual in ('a', 'b'):
                    for largura, altura in ((1366, 1000), (390, 844), (320, 800)):
                        tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': altura,
                            'deviceScaleFactor': 1, 'mobile': largura < 768})
                        painel = reverse('inventario:detalhe', args=[self.inv.pk])
                        self.assertEqual(self.client.get(painel).status_code, 200)
                        tab.call('Page.navigate', {'url': self.live_server_url + painel})
                        try:
                            tab.wait("!!document.querySelector('.evolucao-estoque') && window.inventarioRevisaoPronta === true")
                        except AssertionError:
                            self.fail(str(tab.evaluate("({url:location.href,html:document.body.innerText.slice(0,2000),scripts:[...document.scripts].map(s=>s.src)})")))
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('.detalhe-produto')].every(e=>!e.open)"))
                        self.assertTrue(tab.evaluate("!document.querySelector('.evolucao-estoque').closest('details')"))
                        self.assertTrue(tab.evaluate("document.querySelector('.evolucao-estoque').getBoundingClientRect().width > 0"))
                        self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        destino = Path('artifacts/extrato')
                        destino.mkdir(parents=True, exist_ok=True)
                        (destino / f'acesso-direto-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',
                            {'format': 'png', 'captureBeyondViewport': True})['data']))
                        tab.evaluate("document.querySelector('.evolucao-estoque').click(); true")
                        tab.wait("document.querySelectorAll('table tr[data-registro]').length === 7")
                        self.assertTrue(tab.evaluate("!!document.querySelector('.visual-b')"))
                        if visual == 'a':
                            tab.evaluate("document.querySelector('.alternativas-extrato a:not([aria-current])').click(); true")
                            tab.wait("!!document.querySelector('.visual-a')")
                        tab.wait("getComputedStyle(document.querySelector('.extrato-cartao')).borderRadius === '12px'")
                        tab.wait("document.querySelector('[name=data_inicial]').closest('label').hidden")
                        self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        self.assertEqual(tab.evaluate("document.querySelectorAll('table').length"), 1)
                        self.assertEqual(tab.evaluate("document.querySelectorAll('table thead th').length"), 5)
                        self.assertTrue(tab.evaluate("document.querySelector('[data-registro^=registro-cancelamento-] .saldo').textContent.includes('Não disponível')"))
                        self.assertTrue(tab.evaluate("!!document.querySelector('[data-registro^=registro-cancelamento-] a[href*=vendas]')"))
                        self.assertTrue(tab.evaluate("document.querySelector('table').textContent.toLowerCase().includes('segunda-feira')"))
                        self.assertTrue(tab.evaluate("document.querySelector('.movimento-compra td.saldo').textContent.includes('Reconstruído e corroborado')"))
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('.movimento-venda td.saldo')].every(e=>e.textContent.includes('Snapshot registrado'))"))
                        self.assertTrue(tab.evaluate("document.querySelector('.movimento-pendente td.saldo').textContent.includes('Não disponível')"))
                        self.assertTrue(tab.evaluate("!!document.querySelector('.comparacao.falta .diferenca-final')"))
                        self.assertTrue(tab.evaluate("document.querySelector('.extrato-cartao').lastElementChild.matches('[data-evento=ok-conferencia]')"))
                        self.assertTrue(tab.evaluate("!!document.querySelector('.movimento-manual a')"))
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('details')].every(e=>!e.open)"))
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('td.numero')].every(e=>e.scrollWidth<=e.clientWidth)"))
                        self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('td.saldo')).textAlign"), 'right')
                        if largura == 1366:
                            self.assertLess(tab.evaluate("document.querySelector('.movimento-venda').getBoundingClientRect().height"), 100 if visual == 'a' else 140)
                        tab.evaluate("document.querySelector('.movimento-venda details').open=true; true")
                        self.assertTrue(tab.evaluate("document.querySelector('.movimento-venda details').textContent.includes('Item de venda #')"))
                        self.assertTrue(tab.evaluate("document.querySelector('.movimento-venda details').getBoundingClientRect().height > 40"))
                        tab.evaluate("document.querySelector('.movimento-venda details').open=false; true")
                        cores = tab.evaluate("['.movimento-compra .entrada','.movimento-venda .saida','.diferenca-final'].map(s=>getComputedStyle(document.querySelector(s)).color)")
                        self.assertEqual(len(set(cores)), 3)
                        (destino / f'extrato-{visual}-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',
                            {'format': 'png', 'captureBeyondViewport': True})['data']))
                for largura, altura in ((1366,1000),(390,844),(320,800)):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':largura,'height':altura,'deviceScaleFactor':1,'mobile':largura<768})
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait("!!document.querySelector('[name=periodo]')")
                    tab.evaluate("document.querySelector('[name=periodo]').value='personalizado'; document.querySelector('[name=periodo]').dispatchEvent(new Event('change')); true")
                    self.assertTrue(tab.evaluate("!document.querySelector('[name=data_inicial]').closest('label').hidden && document.querySelector('[name=compra]').closest('label').hidden"))
                    tab.evaluate("document.querySelector('[name=data_inicial]').value='2026-10-12'; document.querySelector('[name=data_final]').value='2026-10-14'; document.querySelector('[name=data_final]').dispatchEvent(new Event('change')); true")
                    tab.evaluate("document.querySelector('form button').click(); true")
                    tab.wait("new URL(location.href).searchParams.get('periodo') === 'personalizado' && document.querySelector('[name=periodo]')?.value === 'personalizado'")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(tab.evaluate("document.querySelector('.comparacao').textContent.includes('16/10/2026')"))
                    (destino / f'periodos-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']))
                tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                tab.wait("!!document.querySelector('.extrato-cartao')")
                # Follow each document link in the same authenticated browser.
                for seletor, prefixo, titulo, conteudo in (('.movimento-compra a', '/compras/', 'Compra #', '.nota-compra-wrap'),
                                                          ('.movimento-venda a', '/vendas/', 'Venda #', '.pagina'),
                                                          ('.movimento-manual a', '/inventario/', 'Registro manual #', '.card')):
                    tab.evaluate(f"document.querySelector('{seletor}').click(); true")
                    tab.wait(f"document.title.startsWith('{titulo}') && !!document.querySelector('{conteudo}')")
                    tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                    tab.wait("!!document.querySelector('.extrato-cartao')")
            finally:
                chrome.stop()

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
                    tab.wait(f"document.querySelector('input[name=compras_pagina]')?.value === '{pagina}' && document.querySelector('.filtros-extrato')?.dataset.compra === '{compra.pk}'")
                    self.assertEqual(tab.evaluate("document.querySelector('select[name=compra]').value"), str(compra.pk))

                for largura in (1366, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 844,
                        'deviceScaleFactor': 1, 'mobile': largura < 768})
                    tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                    conferir(self.compra, 1)
                    tab.evaluate("document.querySelector('.filtros-extrato nav a[href*=compras_pagina]').click(); true")
                    conferir(self.compra, 2)
                    # Submit without changing the selection: the original bug silently changed it.
                    tab.evaluate("document.querySelector('form button').click(); true")
                    tab.wait("new URL(location.href).searchParams.get('compra') === '" + str(self.compra.pk) + "'")
                    conferir(self.compra, 2)
                    tab.evaluate(f"document.querySelector('select[name=compra]').value='{antigas[-1].pk}'; document.querySelector('select[name=compra]').dispatchEvent(new Event('change')); document.querySelector('form button').click(); true")
                    conferir(antigas[-1], 2)
                    tab.evaluate("document.querySelector('.filtros-extrato nav a[href*=compras_pagina]').click(); true")
                    conferir(antigas[-1], 1)
                    tab.evaluate(f"document.querySelector('select[name=compra]').value='{self.compra.pk}'; document.querySelector('select[name=compra]').dispatchEvent(new Event('change')); document.querySelector('form button').click(); true")
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
                    self.assertTrue(tab.evaluate("!!document.querySelector('table a[href*=vendas]')"))
                    tab.evaluate("document.querySelector('button').click(); true")
                    tab.wait("!!document.querySelector('select[name=compra]')")
            finally:
                chrome.stop()
