import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .models import Compra, ContaPagar, Fornecedor


class FiltrosContasPagarTests(TestCase):
    def test_navegacao_real_fornecedor_por_enter_e_clique(self):
        garcia = Fornecedor.objects.create(nome='Garcia Distribuidora')
        amazonia = Fornecedor.objects.create(nome='Comercial Amazonia')
        for fornecedor in [garcia, amazonia]:
            for status in [ContaPagar.STATUS_ABERTA, ContaPagar.STATUS_PARCIAL, ContaPagar.STATUS_PAGA]:
                ContaPagar.objects.create(fornecedor=fornecedor, status=status,
                    data_emissao=date(2026, 9, 1), data_vencimento=date(2026, 9, 10),
                    valor_original=100, valor_em_aberto=0 if status == ContaPagar.STATUS_PAGA else 50)
        def chave(params):
            return tuple(sorted((k, v) for k, v in params.items() if v))
        paginas = {}
        for fornecedor in ['', str(garcia.pk), str(amazonia.pk), str(self.coca.pk)]:
            for inicio in ['', '2026-09-10']:
                params = dict(fornecedor=fornecedor, compra='', data_inicio=inicio, data_fim='', data_por='compra')
                paginas[chave(params)] = self.client.get(self.url, params, secure=True).content
        paginas[()] = self.client.get(self.url, secure=True).content
        requisicoes = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                parsed = urlsplit(handler.path)
                if parsed.path != self.url:
                    handler.send_error(404)
                    return
                params = dict(parse_qsl(parsed.query))
                requisicoes.append(params)
                body = paginas[chave(params)]
                marker = ('<script>window.testParams=' + json.dumps(params) + ';</script>').encode()
                body = body.replace(b'</body>', marker + b'</body>')
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/html; charset=utf-8')
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory(prefix='fornecedor-real-') as profile:
                chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
                try:
                    tab = chrome.tab()
                    url = f'http://127.0.0.1:{server.server_port}{self.url}'
                    for width in [1280, 390]:
                        tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width == 390))
                        tab.call('Page.navigate', {'url': url})
                        tab.wait('document.getElementById("cpFornecedorBusca")?.hidden === false && document.getElementById("cpFornecedorSelect").value === ""')
                        self.assertIsNone(tab.evaluate('document.getElementById("cpFiltro_situacao")'))
                        def esperar(count, fornecedor, inicio=''):
                            tab.wait('window.testParams && (window.testParams.fornecedor || "") === ' + json.dumps(fornecedor) + ' && (window.testParams.data_inicio || "") === ' + json.dumps(inicio))
                            tab.wait('document.getElementById("cpFornecedorBusca")?.hidden === false && document.getElementById("cpFornecedorSelect").value === ' + json.dumps(fornecedor) + ' && document.getElementById("cpFiltro_data_inicio").value === ' + json.dumps(inicio))
                            tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,200));return true})()')
                            self.assertEqual(len(requisicoes), count + 1)
                            self.assertEqual(requisicoes[-1].get('fornecedor', ''), fornecedor)
                            self.assertNotIn('situacao', requisicoes[-1])
                            self.assertNotIn('status', requisicoes[-1])
                            self.assertNotIn('q', requisicoes[-1])
                            if fornecedor:
                                nome = Fornecedor.objects.get(pk=fornecedor).nome
                                self.assertEqual(tab.evaluate('[...document.querySelectorAll(".cp-provider-name")].map(e=>e.textContent.trim())'), [nome] * tab.evaluate('document.querySelectorAll(".cp-provider-name").length'))
                                self.assertGreater(tab.evaluate('document.querySelectorAll(".cp-provider-name").length'), 0)
                                self.assertEqual(tab.evaluate('document.getElementById("cpFornecedorBusca").value'), nome)
                        for termo, fornecedor, clique in [('gar', garcia, False), ('gar', garcia, True), ('amazon', amazonia, True), ('coc', self.coca, False)]:
                            if clique and fornecedor == garcia:
                                tab.call('Page.navigate', {'url': url})
                                tab.wait('document.getElementById("cpFornecedorBusca")?.hidden === false && document.getElementById("cpFornecedorSelect").value === ""')
                            count = len(requisicoes)
                            tab.evaluate('let e=document.getElementById("cpFornecedorBusca");e.focus();e.value=' + json.dumps(termo) + ';e.dispatchEvent(new Event("input",{bubbles:true}))')
                            self.assertEqual(tab.evaluate('document.querySelector(".cp-supplier-option").textContent'), fornecedor.nome)
                            # Observe the order without cancelling the real GET navigation.
                            tab.evaluate('document.getElementById("contasPagarFiltros").addEventListener("submit",()=>sessionStorage.setItem("focoSubmit",document.activeElement.id),{once:true})')
                            if clique:
                                tab.evaluate('document.querySelector(".cp-supplier-option").scrollIntoView({block:"center",behavior:"instant"})')
                                tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,200));return true})()')
                                pos = tab.evaluate('(()=>{let r=document.querySelector(".cp-supplier-option").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()')
                                self.assertEqual(tab.evaluate('document.elementFromPoint(' + str(pos['x']) + ',' + str(pos['y']) + ').className'), 'cp-supplier-option ativa')
                                tab.call('Input.dispatchMouseEvent', dict(type='mousePressed', button='left', clickCount=1, **pos))
                                tab.call('Input.dispatchMouseEvent', dict(type='mouseReleased', button='left', clickCount=1, **pos))
                            else:
                                tab.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})
                                tab.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})
                            try:
                                esperar(count, str(fornecedor.pk))
                            except AssertionError as error:
                                self.fail(f'{width=} {termo=} {clique=} {requisicoes=} DOM=' + str(tab.evaluate('({url:location.href,params:window.testParams,id:document.getElementById("cpFornecedorSelect").value,texto:document.getElementById("cpFornecedorBusca").value,foco:document.activeElement.id})')) + str(error))
                            self.assertEqual(tab.evaluate('sessionStorage.getItem("focoSubmit")'), 'cpFornecedorBusca')
                            self.assertEqual(tab.evaluate('document.activeElement.id'), 'cpFiltro_compra')
                        count = len(requisicoes)
                        tab.evaluate('let e=document.getElementById("cpFiltro_data_inicio");e.value="2026-09-10";e.dispatchEvent(new Event("change",{bubbles:true}))')
                        esperar(count, str(self.coca.pk), '2026-09-10')
                        count = len(requisicoes)
                        tab.evaluate('let e=document.getElementById("cpFornecedorBusca");e.value="";e.dispatchEvent(new Event("input",{bubbles:true}))')
                        esperar(count, '', '2026-09-10')
                        count = len(requisicoes)
                        tab.evaluate('document.querySelector(".cp-filter-actions a").click()')
                        esperar(count, '')
                        self.assertEqual(tab.evaluate('document.getElementById("cpFiltro_data_fim").value'), '')
                        # Unmatched text must not submit or retain a pretend selection.
                        count = len(requisicoes)
                        tab.evaluate('let e=document.getElementById("cpFornecedorBusca");e.focus();e.value="sem fornecedor correspondente";e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}))')
                        self.assertEqual(tab.evaluate('document.getElementById("cpFornecedorBusca").value'), '')
                        self.assertEqual(len(requisicoes), count)
                finally:
                    chrome.stop()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

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
            compra = Compra.objects.create(fornecedor=fornecedor, data_compra=date(2026, 9, dia))
            self.contas.append(ContaPagar.objects.create(compra=compra,
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
                self.assertNotContains(response, '<strong>Situação:</strong>')
                self.assertNotContains(response, 'name="situacao"')
                self.assertNotContains(response, 'name="status"')
                if not params:
                    self.assertNotContains(response, 'class="cp-results cp-results-filtered"')
                    self.assertEqual(response.context['situacao'], 'todas')
                else:
                    self.assertContains(response, 'class="cp-results cp-results-filtered"', count=1)
                if not indices:
                    self.assertContains(response, 'Nenhuma conta corresponde aos filtros aplicados.')

    def test_browser_autocomplete_enter_autoaplicacao_e_responsividade(self):
        antiga = self.client.get(self.url, {'q': 'Outro', 'situacao': ContaPagar.STATUS_PARCIAL}, secure=True)
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
                        self.assertEqual(tab.evaluate('document.getElementById("cpFiltro_data_por").value'), 'compra')
                        tab.evaluate('let d=document.getElementById("cpFiltro_data_inicio");d.value="0002-09-28";d.dispatchEvent(new Event("change",{bubbles:true}))')
                        tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,100));return true})()')
                        self.assertEqual(tab.evaluate('window.submits.length'), 0)
                        self.assertFalse(tab.evaluate('document.getElementById("contasPagarFiltros").checkValidity()'))
                        tab.evaluate('document.getElementById("cpFiltro_data_inicio").value=""')
                        tab.evaluate('window.change=(id,value)=>{let e=document.getElementById(id);e.value=value;e.dispatchEvent(new Event("change",{bubbles:true}))};window.enter=id=>{let e=document.getElementById(id);e.focus();e.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));e.dispatchEvent(new KeyboardEvent("keyup",{key:"Enter",bubbles:true,cancelable:true}))}')
                        tab.evaluate('let e=document.getElementById("cpFornecedorBusca");e.value="coc";e.dispatchEvent(new Event("input",{bubbles:true}));window.enter(e.id)')
                        tab.wait('window.submits.length === 1')
                        self.assertEqual(tab.evaluate('document.activeElement.id'), 'cpFiltro_compra')
                        self.assertNotIn('situacao', tab.evaluate('window.submits[0]'))
                        self.assertEqual(tab.evaluate('window.submits[0].fornecedor'), str(self.coca.pk))
                        enviados = tab.evaluate('window.submits[0]')
                        self.assertNotIn('q', enviados)
                        nova = self.client.get(self.url, enviados, secure=True)
                        self.assertEqual({c.pk for c in nova.context['contas']}, {c.pk for c in self.contas[:3]})
                        self.assertNotContains(nova, '<strong>Busca:</strong>')
                        for origem, destino in [('cpFiltro_compra', 'cpFiltro_data_por'), ('cpFiltro_data_por', 'cpFiltro_data_inicio'), ('cpFiltro_data_inicio', 'cpFiltro_data_fim'), ('cpFiltro_data_fim', 'cpFiltro_data_fim')]:
                            tab.evaluate('window.enter(' + json.dumps(origem) + ')')
                            self.assertEqual(tab.evaluate('document.activeElement.id'), destino)
                        self.assertEqual(tab.evaluate('window.submits.length'), 1)
                        self.assertEqual(tab.evaluate('document.activeElement.id'), 'cpFiltro_data_fim')
                        # Simulate pageshow after each completed navigation; preserve the fields.
                        for field, value in [('cpFiltro_compra', '28745'), ('cpFiltro_data_inicio', '2026-09-01'), ('cpFiltro_data_fim', '2026-09-30'), ('cpFiltro_data_por', 'vencimento')]:
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
                        self.assertEqual(tab.evaluate('window.submits.at(-1).data_por'), 'vencimento')
                        self.assertTrue(tab.evaluate('window.submits.every(dados => !("q" in dados) && !("situacao" in dados) && !("status" in dados))'))
                        self.assertEqual(tab.evaluate('document.querySelector(".cp-filter-actions a").getAttribute("href")'), self.url)
            finally:
                chrome.stop()
