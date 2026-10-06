import json
import re
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .models import ContaPagar, Fornecedor


class FiltrosContasPagarTests(TestCase):
    def setUp(self):
        self.url = reverse('estoque:contas_pagar')
        self.coca = Fornecedor.objects.create(nome='Coca Cola')
        self.outro = Fornecedor.objects.create(nome='Outro fornecedor')
        self.contas = []
        for fornecedor, dia, status, documento in [
            (self.coca, 10, ContaPagar.STATUS_ABERTA, '28745'),
            (self.coca, 15, ContaPagar.STATUS_PARCIAL, '28746'),
            (self.coca, 20, ContaPagar.STATUS_PAGA, '28747'),
            (self.outro, 10, ContaPagar.STATUS_PARCIAL, '28745-B'),
        ]:
            self.contas.append(ContaPagar.objects.create(
                fornecedor=fornecedor, data_emissao=date(2026, 9, 1),
                data_vencimento=date(2026, 9, dia), status=status,
                documento_legado=documento, valor_original=100,
                valor_em_aberto=0 if status == ContaPagar.STATUS_PAGA else 50,
            ))

    def test_combinacoes_remocao_reload_e_urls_antigas(self):
        fornecedor = {'fornecedor': str(self.coca.pk)}
        dia = {'data_inicio': '2026-09-10', 'data_fim': '2026-09-10'}
        periodo = {'data_inicio': '2026-09-01', 'data_fim': '2026-09-30'}
        parcial = {'situacao': ContaPagar.STATUS_PARCIAL}
        casos = [
            ({}, [0, 1, 2, 3]), (fornecedor, [0, 1, 2]),
            ({'data_inicio': '2026-09-15'}, [1, 2]),
            ({'data_fim': '2026-09-10'}, [0, 3]),
            (dia, [0, 3]), (periodo, [0, 1, 2, 3]),
            (parcial, [1, 3]), ({'compra': '28745'}, [0, 3]),
            (fornecedor | dia, [0]), (fornecedor | periodo, [0, 1, 2]),
            (fornecedor | parcial, [1]), (fornecedor | periodo | parcial, [1]),
            (periodo | parcial, [1, 3]),
            (fornecedor | periodo | {'situacao': 'todas'}, [0, 1, 2]),
            (fornecedor | periodo | {'compra': '28745', 'situacao': ContaPagar.STATUS_ABERTA}, [0]),
            ({'status': ContaPagar.STATUS_PARCIAL}, [1, 3]),
            ({'q': 'Coca'}, [0, 1, 2]), ({'compra': 'inexistente'}, []),
        ]
        for params, indices in casos:
            with self.subTest(params=params):
                response = self.client.get(self.url, params, secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual({c.pk for c in response.context['contas']},
                                 {self.contas[i].pk for i in indices})
                reload = self.client.get(self.url, params, secure=True)
                self.assertEqual([c.pk for c in response.context['contas']],
                                 [c.pk for c in reload.context['contas']])
                self.assertNotContains(response, 'Busca livre')
                if response.context['situacao'] == 'todas':
                    self.assertNotContains(response, '<strong>Situação:</strong>')
                if not params:
                    self.assertNotContains(response, 'class="cp-results cp-results-filtered"')
                    self.assertEqual(response.context['situacao'], 'todas')
                else:
                    self.assertContains(response, 'class="cp-results cp-results-filtered"', count=1)
                if not indices:
                    self.assertContains(response, 'Nenhuma conta corresponde aos filtros aplicados.')

    def test_browser_autocomplete_enter_autoaplicacao_e_responsividade(self):
        antiga = self.client.get(self.url, {'q': 'Outro'}, secure=True)
        self.assertEqual([c.pk for c in antiga.context['contas']], [self.contas[3].pk])
        self.assertContains(antiga, '<strong>Busca:</strong> Outro')
        self.assertNotContains(antiga, 'name="q"')
        html = antiga.content.decode()
        script = next(s for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                      if 'const valorPago = ' in s)
        with TemporaryDirectory(prefix='filtros-contas-') as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                for width in [1280, 390]:
                    with self.subTest(width=width):
                        tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 850, 'deviceScaleFactor': 1, 'mobile': width == 390})
                        for nome, conteudo in [('htmlTeste', html), ('scriptTeste', script)]:
                            tab.evaluate(f'window.{nome} = ""')
                            for inicio in range(0, len(conteudo), 8000):
                                tab.evaluate(f'window.{nome} += ' + json.dumps(conteudo[inicio:inicio + 8000]))
                        tab.evaluate('document.body.innerHTML = window.htmlTeste;true')
                        self.assertEqual(tab.evaluate('getComputedStyle(document.querySelector(".cp-desktop")).display !== "none"'), width == 1280)
                        self.assertEqual(tab.evaluate('getComputedStyle(document.querySelector(".cp-mobile")).display !== "none"'), width == 390)
                        tab.evaluate('window.submits=[];document.getElementById("contasPagarFiltros").addEventListener("submit",e=>{e.preventDefault();window.submits.push(Object.fromEntries(new FormData(e.target)))})')
                        tab.evaluate('eval(window.scriptTeste)')
                        tab.evaluate('window.change=(id,value)=>{let e=document.getElementById(id);e.value=value;e.dispatchEvent(new Event("change",{bubbles:true}))};window.enter=id=>{let e=document.getElementById(id);e.focus();e.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));e.dispatchEvent(new KeyboardEvent("keyup",{key:"Enter",bubbles:true,cancelable:true}))}')
                        tab.evaluate('let e=document.getElementById("cpFornecedorBusca");e.value="coc";e.dispatchEvent(new Event("input",{bubbles:true}));window.enter(e.id)')
                        tab.wait('window.submits.length === 1')
                        self.assertEqual(tab.evaluate('document.activeElement.id'), 'cpFiltro_compra')
                        self.assertEqual(tab.evaluate('window.submits[0].situacao'), 'todas')
                        self.assertEqual(tab.evaluate('window.submits[0].fornecedor'), str(self.coca.pk))
                        enviados = tab.evaluate('window.submits[0]')
                        self.assertNotIn('q', enviados)
                        nova = self.client.get(self.url, enviados, secure=True)
                        self.assertEqual({c.pk for c in nova.context['contas']}, {c.pk for c in self.contas[:3]})
                        self.assertNotContains(nova, '<strong>Busca:</strong>')
                        for origem, destino in [('cpFiltro_compra', 'cpFiltro_data_inicio'), ('cpFiltro_data_inicio', 'cpFiltro_data_fim'), ('cpFiltro_data_fim', 'cpFiltro_situacao'), ('cpFiltro_situacao', 'cpFiltro_situacao')]:
                            tab.evaluate('window.enter(' + json.dumps(origem) + ')')
                            self.assertEqual(tab.evaluate('document.activeElement.id'), destino)
                        self.assertEqual(tab.evaluate('window.submits.length'), 1)
                        self.assertEqual(tab.evaluate('document.activeElement.id'), 'cpFiltro_situacao')
                        # Simulate pageshow after each completed navigation; preserve the fields.
                        for field, value in [('cpFiltro_compra', '28745'), ('cpFiltro_data_inicio', '2026-09-01'), ('cpFiltro_data_fim', '2026-09-30'), ('cpFiltro_situacao', ContaPagar.STATUS_PARCIAL), ('cpFiltro_situacao', 'todas')]:
                            count = tab.evaluate('window.submits.length')
                            tab.evaluate('window.dispatchEvent(new Event("pageshow"));')
                            if field == 'cpFiltro_compra':
                                tab.evaluate('document.getElementById("cpFiltro_compra").value="28745";window.enter("cpFiltro_compra")')
                            else:
                                tab.evaluate('window.change(' + json.dumps(field) + ',' + json.dumps(value) + ')')
                            tab.wait(f'window.submits.length === {count + 1}')
                            self.assertEqual(tab.evaluate('window.submits.at(-1).fornecedor'), str(self.coca.pk))
                        tab.evaluate('window.dispatchEvent(new Event("pageshow"));let e=document.getElementById("cpFornecedorBusca");e.value="";e.dispatchEvent(new Event("input",{bubbles:true}))')
                        tab.wait('window.submits.at(-1).fornecedor === ""')
                        self.assertEqual(tab.evaluate('window.submits.at(-1).data_inicio'), '2026-09-01')
                        self.assertEqual(tab.evaluate('window.submits.at(-1).compra'), '28745')
                        self.assertTrue(tab.evaluate('window.submits.every(dados => !("q" in dados))'))
                        self.assertEqual(tab.evaluate('document.querySelector(".cp-filter-actions a").getAttribute("href")'), self.url)
            finally:
                chrome.stop()
