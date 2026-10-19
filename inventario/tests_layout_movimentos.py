"""Visual checks and captures using exclusively synthetic historical documents."""
import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.apps import apps
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone

from estoque.models import Compra, ItemCompra, Venda, ItemVenda, MovimentacaoEstoqueManual, Produto
from offline.browser_support import Chrome
from .models import MovimentoObservado
from .tests_browser import CHROME
from .tests_historico import HistoricoFixture


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class LayoutMovimentosBrowserTests(HistoricoFixture, StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        self.inicio = datetime(2026, 10, 14, 9, 7, 40, tzinfo=ZoneInfo('America/Sao_Paulo'))
        self.agora = self.inicio.replace(hour=12)
        Compra.objects.filter(pk=self.compra.pk).update(data_compra=self.inicio.date(), estoque_entrada_realizada_em=self.inicio)
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=4, unidade='UN')
        self.rodada.ok_em = self.agora
        self.rodada.save(update_fields=['ok_em'])
        self.venda_em(self.inicio - timedelta(days=1), 1, 0)
        self.vendas = [self.venda_em(self.inicio.replace(hour=9, minute=28, second=21), 4, 3),
                       self.venda_em(self.inicio.replace(hour=10, minute=21, second=0), 3, 2)]
        self.manuais = []
        for minuto, antes, depois in [(30,2,5), (40,5,4), (50,4,4)]:
            m = MovimentacaoEstoqueManual.objects.create(produto=self.produto, tipo='ajuste',
                estoque_antes=antes, estoque_depois=depois, diferenca=depois-antes,
                motivo='Ajuste fictício para revisão visual; unidade histórica não gravada.')
            MovimentacaoEstoqueManual.objects.filter(pk=m.pk).update(criado_em=self.inicio.replace(hour=10, minute=minuto))
            self.manuais.append(m)
        self.digitais = []
        for seq, origem, antes, depois in [(1,'venda',100,103), (2,'compra',103,101)]:
            self.digitais.append(MovimentoObservado.objects.create(produto=self.produto, sequencia=seq,
                antes=antes, depois=depois, origem=origem,
                registrado_em=self.inicio.replace(hour=11, minute=seq), referencia='Exemplo fictício sem unidade histórica'))

    def venda_em(self, instante, antes, depois):
        venda = Venda.objects.create(data_venda=timezone.localtime(instante).date())
        Venda.objects.filter(pk=venda.pk).update(criado_em=instante, atualizado_em=instante)
        return ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=1, unidade='UN',
            estoque_antes=antes, estoque_depois=depois, estoque_movimentado=1,
            estoque_unidade_snapshot='UN', preco_unitario=0, valor_total=0)

    def snapshot(self):
        models = list(apps.get_app_config('inventario').get_models()) + [Compra, ItemCompra, Venda, ItemVenda, MovimentacaoEstoqueManual, Produto]
        return {m._meta.label:list(m.objects.order_by('pk').values()) for m in models}

    def fluxo(self, largura):
        antes = self.snapshot()
        baseline = os.environ.get('EXTRATO_LAYOUT_CAPTURA_ANTES') == '1'
        fase = 'antes' if baseline else 'depois'
        destino = Path('artifacts/extrato-movimentos')
        destino.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='extrato-layout-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=1000, deviceScaleFactor=1, mobile=largura<768))
                for visual in ['a','b']:
                    url = self.live_server_url+self.url+'?visual='+visual
                    tab.call('Page.navigate', {'url':url})
                    tab.wait('document.readyState === "complete" && document.querySelectorAll("tr[data-registro]").length === 8')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(tab.evaluate('[...document.querySelectorAll(".tabela-extrato details")].every(d=>!d.open)'))
                    self.assertIn('0 UN', tab.evaluate('document.querySelector(".saldo-inicial .saldo").textContent'))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('tr[data-registro] time')].every((t,i,a)=>!i || t.dateTime >= a[i-1].dateTime)"))
                    dados = tab.evaluate("[...document.querySelectorAll('tr[data-registro]')].map(r=>({id:r.dataset.registro,data:r.querySelector('time').outerHTML,operacao:r.querySelector('.movimento').innerHTML,saldo:r.querySelector('.saldo').innerHTML}))")
                    arquivo = destino/f'{fase}-{visual}-{largura}'
                    if not baseline:
                        original = destino/f'antes-{visual}-{largura}.json'
                        if os.environ.get('EXTRATO_LAYOUT_COMPARAR_ANTES') == '1':
                            self.assertEqual(json.loads(original.read_text(encoding='utf-8')), dados)
                        self.validar_layout(tab, largura)
                    arquivo.with_suffix('.json').write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding='utf-8')
                    clip = tab.evaluate("(()=>{const r=document.querySelector('.extrato-cartao').getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1}})()")
                    arquivo.with_suffix('.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',
                        dict(format='png', captureBeyondViewport=True, clip=clip))['data']))
                    # Native details remain beneath, and inside, the operation column.
                    tab.evaluate('document.querySelector("tr[data-registro] .evidencia-saldo summary").click();true')
                    self.assertTrue(tab.evaluate('document.querySelector("tr[data-registro] .evidencia-saldo").open'))
                    self.assertIn('Antes: 0 UN', tab.evaluate('document.querySelector("tr[data-registro] .evidencia-saldo").textContent'))
                    # Click the existing purchase, sale and manual document links.
                    for fonte in ['compra','venda','manual']:
                        tab.call('Page.navigate', {'url':url})
                        tab.wait('document.readyState === "complete" && !!document.querySelector("tr[data-registro]")')
                        seletor = f'tr[data-registro^="registro-{fonte}-"] .movimento>a'
                        path = tab.evaluate('document.querySelector('+json.dumps(seletor)+').getAttribute("href")')
                        tab.evaluate('document.querySelector('+json.dumps(seletor)+').click();true')
                        tab.wait('document.readyState === "complete" && location.pathname === '+json.dumps(path))
                self.assertEqual(antes, self.snapshot())
            finally:
                chrome.stop()

    def validar_layout(self, tab, largura):
        self.assertTrue(tab.evaluate('Array.from(document.querySelectorAll(".tabela-extrato tbody tr")).every(r=>r.children.length===4)'))
        self.assertEqual(tab.evaluate('document.querySelector(".movimento-compra .fluxo").textContent.trim()'), 'Entrada +4 UN')
        self.assertTrue(tab.evaluate('[...document.querySelectorAll(".movimento-venda .fluxo")].slice(0,2).every(c=>c.textContent.trim()==="Saída −1 UN")'))
        for index, label in enumerate(['Entrada','Saída','Sem variação']):
            selector = f'tr[data-registro="registro-manual-{self.manuais[index].pk}"] .fluxo'
            self.assertTrue(tab.evaluate('document.querySelector('+json.dumps(selector)+').textContent.trim().startsWith('+json.dumps(label)+')'))
        # Raw records with misleading names still use the actual before/after direction.
        for movimento, label in zip(self.digitais, ['Entrada', 'Saída']):
            seletor = f'[data-registro=registro-digital-{movimento.pk}] .fluxo'
            self.assertTrue(tab.evaluate('document.querySelector('+json.dumps(seletor)+').textContent.trim().startsWith('+json.dumps(label)+')'))
        self.assertEqual(tab.evaluate('getComputedStyle(document.querySelector(".fluxo .entrada")).color'), 'rgb(34, 95, 192)')
        self.assertEqual(tab.evaluate('getComputedStyle(document.querySelector(".fluxo .saida")).color'), 'rgb(176, 45, 53)')
        self.assertTrue(tab.evaluate("[...document.querySelectorAll('tr[data-registro]')].every(r=>{const t=r.querySelector('.movimento>a,.movimento>strong').getBoundingClientRect();const d=r.querySelector('summary').getBoundingClientRect();return Math.abs(t.left-d.left)<2 && d.top>=t.bottom-1})"))
        if largura > 700:
            self.assertTrue(tab.evaluate("(()=>{const rows=[...document.querySelectorAll('tr[data-registro]')];const first=[...rows[0].children].map(c=>c.getBoundingClientRect().left);return rows.every(r=>[...r.children].every((c,i)=>Math.abs(c.getBoundingClientRect().left-first[i])<2)) && rows.every(r=>{const cells=[...r.children].map(c=>c.getBoundingClientRect());return cells.every(c=>Math.abs(c.top-cells[0].top)<2)})})()"))

    def test_desktop(self): self.fluxo(1366)
    def test_mobile(self): self.fluxo(390)
