"""Use inventario.validar_confiabilidade_postgresql; fictitious data only."""
import json
import base64
import os
import subprocess
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from inventario.tests import produto as criar_produto
from estoque.models import ItemVenda, Venda, Produto
from estoque.services.relatorios import resumo_vendas
from estoque.services.relatorios_produtos import ranking_produtos, historico_produto
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from offline.browser_support import Chrome

HOJE = date(2026, 11, 20)
ROOT = Path(__file__).resolve().parents[1]


class ProdutosRelatoriosTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('produtos-admin', is_staff=True, is_superuser=True)
        cls.p = criar_produto(nome='Produto em pacotes', quantidade=100)

    def nota(self, *, produto=None, unidade='PCT', quantidade='2', valor='10', dia=HOJE,
             pagamento='À vista', cancelada=False, total=None, venda=None):
        v = venda or Venda.objects.create(data_venda=dia, total=D(valor if total is None else total),
                                         tipo_pagamento=pagamento, cancelada=cancelada)
        return ItemVenda.objects.create(venda=v, produto=produto or self.p, unidade=unidade,
            quantidade=D(quantidade), preco_unitario=D('999.99'), valor_total=D(valor))

    def ranking(self, **kwargs):
        return ranking_produtos(HOJE, HOJE, **kwargs)

    def historico(self, inicio=HOJE.replace(day=1), **kwargs):
        return historico_produto(self.p.pk, inicio, HOJE, hoje=HOJE, **kwargs)

    def test_fontes_cancelamento_consumo_aliases_e_valor_registrado(self):
        self.nota(valor='12.34')
        for pagamento in ('consumo_proprio', 'CONSUMO PRÓPRIO', ' Consumo próprio '):
            self.nota(valor='100', pagamento=pagamento)
        self.nota(valor='200', cancelada=True)
        self.assertEqual(self.ranking()['total_itens'], D('12.34'))
        linha = self.ranking()['linhas'][0]
        self.assertEqual(linha['preco_medio'], D('6.17'))
        self.assertEqual(linha['vendas'], 1)

    def test_unidades_separadas_sem_fator_do_cadastro(self):
        self.nota(unidade=' pct ', quantidade='2', valor='20')
        self.nota(unidade='PCT', quantidade='3', valor='60')
        self.nota(unidade='UN', quantidade='7', valor='35')
        linhas = {l['unidade_comercial']: l for l in self.ranking()['linhas']}
        self.assertEqual(linhas['PCT']['quantidade'], 5)
        self.assertEqual(linhas['PCT']['preco_medio'], 16)
        self.assertEqual(linhas['UN']['quantidade'], 7)
        self.assertEqual(self.ranking()['total_itens'], 115)
        self.assertEqual(self.historico(medida='quantidade', unidade='UN')['evolucao'][0]['faturamento'], 7)

    def test_unidade_ausente_nao_certifica_quantidade_nem_preco(self):
        self.nota(unidade='', valor='40')
        linha = self.ranking()['linhas'][0]
        self.assertIsNone(linha['quantidade'])
        self.assertIsNone(linha['preco_medio'])
        self.assertEqual(linha['faturamento'], 40)
        with self.assertRaises(ValueError):
            self.historico(medida='quantidade')

    def test_vendas_distintas_com_itens_repetidos(self):
        i = self.nota(total='25')
        self.nota(venda=i.venda, valor='15')
        self.assertEqual(self.ranking()['linhas'][0]['vendas'], 1)
        dados = self.historico(medida='vendas')
        self.assertEqual(dados['quantidade'], 1)
        self.assertEqual(dados['evolucao'][0]['faturamento'], 1)

    def test_edicao_e_remocao_usam_apenas_estado_atual_comercial(self):
        i = self.nota(valor='10')
        ItemVenda.objects.filter(pk=i.pk).update(quantidade=D('5'), valor_total=D('45'))
        Venda.objects.filter(pk=i.venda_id).update(total=D('45'))
        outro = self.nota(venda=i.venda, valor='200')
        outro.delete()
        self.assertEqual(self.ranking()['total_itens'], 45)
        self.assertEqual(self.ranking()['linhas'][0]['quantidade'], 5)
        with CaptureQueriesContext(connection) as sql:
            self.ranking()
        self.assertFalse(any('movimentoobservado' in q['sql'].lower() for q in sql))

    def test_reconciliacao_nao_rateia_diferenca_e_inclui_sem_produto_e_sem_itens(self):
        self.nota(valor='9.99', total='10')
        outro = self.nota(valor='4.50')
        ItemVenda.objects.filter(pk=outro.pk).update(produto=None)
        Venda.objects.create(data_venda=HOJE, total=D('7'), tipo_pagamento='Pix')
        r = self.ranking(pesquisa='Produto')
        self.assertEqual((r['identificados'], r['nao_identificados'], r['total_itens']), (D('9.99'), D('4.50'), D('14.49')))
        self.assertEqual(r['diferenca'], D('7.01'))
        self.assertEqual(r['total_itens'] + r['diferenca'], resumo_vendas(HOJE, HOJE)['faturamento'])
        self.assertEqual(len(r['linhas']), 1)
        self.assertEqual(r['linhas'][0]['participacao'], D('46.47'))

    def test_pesquisa_ordem_e_paginacao_no_banco(self):
        for n in range(32):
            p = criar_produto(nome=f'Ranking produto {n:02}', quantidade=0)
            self.nota(produto=p, valor=str(n+1), quantidade=str(50-n))
        with CaptureQueriesContext(connection) as sql:
            r = self.ranking(pesquisa='Ranking', pagina=2)
        self.assertEqual(len(r['linhas']), 7)
        self.assertEqual(r['linhas'].paginator.count, 32)
        self.assertLessEqual(len(sql), 4)
        self.assertTrue(any('LIMIT' in q['sql'] and 'OFFSET' in q['sql'] for q in sql))
        self.assertEqual(self.ranking(ordem='quantidade')['linhas'][0]['quantidade'], 50)

    def test_quantidades_milesimais_e_diferencas_sem_arredondar_centavos(self):
        self.nota(dia=HOJE-timedelta(days=2), quantidade='.001')
        self.nota(dia=HOJE-timedelta(days=1), quantidade='.004')
        r = self.historico(medida='quantidade', unidade='PCT', modo='diario')['analise_evolucao']
        item = next(i for i in r['itens'] if i['inicio'] == '2026-11-19')
        self.assertEqual(item['valor'], D('.004'))
        self.assertEqual(item['diferenca'], D('.003'))
        self.assertEqual(item['variacao'], D('300.00'))

    def preencher_mes(self, ano, mes, dias, quantidade='2', valor='10'):
        for d in range(1, dias+1):
            self.nota(dia=date(ano, mes, d), quantidade=quantidade, valor=valor)

    def test_previsao_exige_cobertura_do_proprio_produto(self):
        self.nota(dia=date(2026, 11, 1))
        self.nota(dia=HOJE)
        r = self.historico(modo='mensal')['analise_evolucao']['itens'][0]
        self.assertEqual(r['valor'], 20)
        self.assertIsNone(r['indicadores']['previsao']['valor'])
        self.assertIsNone(r['comparacao'])

    def test_previsao_e_equivalente_teorico_reutilizam_motor_para_tres_medidas(self):
        self.preencher_mes(2026, 11, 20)
        self.preencher_mes(2026, 10, 31)
        for medida, unidade, valor in [('faturamento', '', D('300')), ('quantidade', 'PCT', D('60')), ('vendas', '', D('30'))]:
            r = self.historico(modo='mensal', medida=medida, unidade=unidade)
            i = r['analise_evolucao']['itens'][0]
            self.assertEqual(i['indicadores']['previsao']['valor'], valor)
            self.assertEqual(i['comparacao']['anterior_fim'], '2026-10-20')
        self.assertEqual(self.historico()['faturamento'], 200)

    def test_semana_parcial_compara_mesmos_dias_da_semana_anterior(self):
        for d in range(9, 21):
            self.nota(dia=date(2026, 11, d), valor=str(d))
        r = self.historico(modo='semanal')['analise_evolucao']['itens'][-1]
        self.assertEqual((r['inicio'], r['fim']), ('2026-11-16', '2026-11-20'))
        self.assertEqual((r['comparacao']['anterior_inicio'], r['comparacao']['anterior_fim']), ('2026-11-09', '2026-11-13'))
        self.assertEqual(r['diferenca'], 35)

    def test_mensal_ano_anterior_e_teorico_com_cobertura_sem_inventar_dias(self):
        self.preencher_mes(2025, 11, 20, valor='5')
        self.preencher_mes(2026, 11, 20, valor='10')
        i = self.historico(modo='mensal', comparacao='ano_anterior')['analise_evolucao']['itens'][0]
        self.assertEqual(i['comparacao']['anterior_inicio'], '2025-11-01')
        self.assertEqual(i['comparacao']['anterior_fim'], '2025-11-20')
        self.assertEqual(i['diferenca'], 100)
        self.assertEqual(i['variacao'], 100)
        for dia in range(7, 31):
            self.nota(dia=date(2024, 9, dia), valor='3')
        r = historico_produto(self.p.pk, date(2024,9,1), date(2024,9,30), hoje=HOJE, modo='mensal')
        i = r['analise_evolucao']['itens'][0]
        self.assertEqual(i['valor'], 72)
        self.assertEqual(i['indicadores']['equivalente_teorico']['valor'], 90)

    def test_diario_todos_e_dia_especifico_usam_referencia_correta(self):
        for dia in range(1, 21):
            self.nota(dia=date(2026,11,dia), valor=str(dia))
        todos = self.historico(modo='diario', dia_semana=-1)['analise_evolucao']['itens']
        segunda = self.historico(modo='diario', dia_semana=0)['analise_evolucao']['itens']
        self.assertEqual(todos[-2]['comparacao']['anterior_inicio'], '2026-11-18')
        self.assertTrue(all(date.fromisoformat(i['inicio']).weekday() == 0 for i in segunda))
        self.assertEqual(segunda[-1]['comparacao']['anterior_inicio'], '2026-11-09')

    def test_media_numero_vendas_preserva_fracao_sem_mudar_contagem_real(self):
        for dia in range(1, 21):
            self.nota(dia=date(2026,11,dia))
        self.nota(dia=date(2026,11,1))
        item = self.historico(modo='mensal', medida='vendas')['analise_evolucao']['itens'][0]
        self.assertEqual(item['valor'], 21)
        self.assertEqual(item['indicadores']['media_diaria'], D('1.05'))
        self.assertEqual(item['indicadores']['previsao']['valor'], D('31.53'))

    def test_http_seguranca_filtros_unidade_ancora_e_alcances(self):
        url = reverse('estoque:relatorios_produto', args=[self.p.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        comum = get_user_model().objects.create_user('produtos-comum')
        self.client.force_login(comum)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        self.nota(unidade='PCT')
        self.nota(unidade='UN')
        parametros = dict(periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20', modo='mensal')
        for alcance in ('principal', '3', '6', '12', '24', 'personalizado'):
            r = self.client.get(url, {**parametros, 'alcance': alcance, 'historico_inicio': '2026-09-01', 'historico_fim': '2026-11-20'})
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, url + '#evolucao-vendas')
            self.assertEqual(r.context['resumo']['analise_evolucao']['modo'], 'mensal')
        r = self.client.get(url, {**parametros, 'medida': 'quantidade'})
        self.assertContains(r, 'Escolha a unidade para comparar')
        self.assertNotIn('resumo', r.context)
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get('/estoque' + url).status_code, 200)

    def test_somente_leitura_e_menu(self):
        self.nota()
        antes = list(ItemVenda.objects.values())
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as sql:
            r = self.client.get(reverse('estoque:relatorios_produto', args=[self.p.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT', '(SELECT')) for q in sql))
        self.assertEqual(antes, list(ItemVenda.objects.values()))
        self.assertContains(self.client.get(reverse('estoque:relatorios_inicio')), reverse('estoque:relatorios_produtos'))

    def test_layout_ranking_e_graficos_sem_servidor_adicional(self):
        self.preencher_mes(2026, 11, 20)
        # Commercial days without this product still prove coverage. Exercise
        # both historical equivalence and the current-month ghost together.
        d = date(2026, 9, 7)
        while d < date(2026, 11, 1):
            Venda.objects.create(data_venda=d, total=100, tipo_pagamento='pix')
            d += timedelta(days=1)
        self.nota(dia=date(2026, 9, 10), quantidade='27')
        self.nota(dia=date(2026, 10, 10), quantidade='65.5')
        self.client.force_login(self.admin)
        browser_path = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        self.assertTrue(browser_path.is_file())
        css = (ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        base_css = (ROOT/'static/estoque/home.css').read_text(encoding='utf-8')
        bootstrap = ROOT/'artifacts/relatorios-produtos-20261120/bootstrap.min.css'
        base_css = (bootstrap.read_text(encoding='utf-8') if bootstrap.exists() else '') + base_css
        js = (ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        pages = [('ranking', self.client.get(reverse('estoque:relatorios_produtos')).content.decode())]
        url = reverse('estoque:relatorios_produto', args=[self.p.pk])
        for medida in ('faturamento', 'quantidade', 'vendas'):
            pages.append((medida, self.client.get(url, dict(periodo='personalizado', data_inicial='2026-11-01',
                data_final='2026-11-20', modo='mensal', alcance='3', medida=medida, unidade='PCT')).content.decode()))
        with TemporaryDirectory(prefix='produtos-layout-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(str(browser_path), perfil)
            original = subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen', side_effect=lambda cmd, **kw: original([cmd[0], '--no-sandbox', *cmd[1:]], **kw)):
                browser.start()
            try:
                tab = browser.tab()
                for largura in (1366, 768, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=740, deviceScaleFactor=1, mobile=largura<768))
                    for nome, html in pages:
                        tab.evaluate('document.open();document.write('+json.dumps(html)+');document.close();')
                        tab.evaluate("document.querySelectorAll('link,script[src]').forEach(e=>e.remove());")
                        tab.evaluate("document.head.insertAdjacentHTML('beforeend','<style id=estilos-teste></style>')")
                        estilos = base_css + '*{box-sizing:border-box}body{margin:0;font:16px system-ui}' + css
                        for inicio_css in range(0, len(estilos), 8000):
                            tab.evaluate('document.getElementById("estilos-teste").textContent+='+json.dumps(estilos[inicio_css:inicio_css+8000]))
                        if nome != 'ranking':
                            tab.evaluate("location.hash='';location.hash='evolucao-vendas';")
                        tab.evaluate(js)
                        self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'), (largura, nome))
                        if nome != 'ranking':
                            cards = tab.evaluate("""[...document.querySelectorAll('.produto-indicadores > div')].map(e=>{
                                const r=e.getBoundingClientRect(),v=e.querySelector('dd').getBoundingClientRect();
                                return {top:r.top,left:r.left,right:r.right,height:r.height,valorTop:v.top};
                            })""")
                            self.assertEqual(len(cards), 3)
                            if largura >= 768:
                                self.assertLess(max(c['top'] for c in cards)-min(c['top'] for c in cards), 1)
                                self.assertLess(max(c['height'] for c in cards)-min(c['height'] for c in cards), 1)
                                self.assertLess(max(c['valorTop'] for c in cards)-min(c['valorTop'] for c in cards), 1)
                                self.assertTrue(all(a['right'] <= b['left'] for a,b in zip(cards,cards[1:])))
                            else:
                                self.assertTrue(all(a['top'] < b['top'] for a,b in zip(cards,cards[1:])))
                            self.assertGreater(tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"), 0)
                            geometria = tab.evaluate("""[...document.querySelectorAll('.grafico-barra-dia')].map(e=>{
                                const b=e.querySelector('.grafico-barra'),r=b.getBoundingClientRect();
                                return {data:e.dataset.data,altura:r.height,largura:r.width,cor:getComputedStyle(b).backgroundColor};
                            })""")
                            self.assertTrue(all(b['altura'] > 0 and b['largura'] > 0 for b in geometria), geometria)
                            texto = tab.evaluate("document.querySelector('.grafico-barra-valor').textContent")
                            self.assertEqual('R$' in texto, nome == 'faturamento')
                            self.assertEqual(tab.evaluate("document.querySelector('#evolucao-controles').getAttribute('action').endsWith('#evolucao-vendas')"), True)
                            tab.wait("document.activeElement.id==='titulo-evolucao-vendas'")
                            self.assertGreaterEqual(tab.evaluate("document.getElementById('titulo-evolucao-vendas').getBoundingClientRect().top"), 0)
                            tab.evaluate("document.querySelector('.indicadores-metodo summary')?.click()")
                            self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                            if nome == 'quantidade':
                                unidades = tab.evaluate("document.querySelectorAll('.grafico-barra-fantasma-valor').length")
                                self.assertEqual(unidades, 2)
                                self.assertIn('PCT', tab.evaluate("document.querySelector('.grafico-barra-fantasma-valor').textContent"))
                        if os.environ.get('PRODUTOS_VISUAL_ARTIFACTS') and largura in (1366, 390):
                            pasta = Path(os.environ['PRODUTOS_VISUAL_ARTIFACTS']); pasta.mkdir(parents=True, exist_ok=True)
                            (pasta/f'{nome}-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']))
                tab.call('Page.close'); tab.socket.close()
            finally:
                browser.stop()


class DesempenhoProdutosTests(PostgreSQLIsoladoTests, TestCase):
    def test_600_produtos_60000_itens_agregados_pagina_e_consultas_limitadas(self):
        produtos = [criar_produto(nome=f'Produto volume {i}', quantidade=0) for i in range(600)]
        vendas = Venda.objects.bulk_create([Venda(data_venda=HOJE-timedelta(days=i%730), total=100, tipo_pagamento='Pix') for i in range(6000)])
        for n in range(0, 60000, 2000):
            ItemVenda.objects.bulk_create([ItemVenda(venda=vendas[i%6000], produto=produtos[i%600], quantidade=1,
                unidade='UN', preco_unitario=10, valor_total=10) for i in range(n, n+2000)], batch_size=2000)
        inicio = HOJE-timedelta(days=729)
        with CaptureQueriesContext(connection) as sql:
            t = perf_counter(); r = ranking_produtos(inicio, HOJE); elapsed = perf_counter()-t
        self.assertEqual(r['linhas'].paginator.count, 600)
        self.assertEqual(len(r['linhas']), 25)
        self.assertEqual(r['diferenca'], 0)
        self.assertLessEqual(len(sql), 4)
        self.assertLess(elapsed, 2)
        with CaptureQueriesContext(connection) as sql:
            t = perf_counter(); h = historico_produto(produtos[0].pk, inicio, HOJE, hoje=HOJE, modo='mensal'); tempo = perf_counter()-t
        self.assertLessEqual(len(sql), 9)  # One additional bulk commercial-coverage aggregate, never per bar.
        self.assertLessEqual(len(h['evolucao']), 31)
        self.assertLessEqual(len(h['documentos']), 10)
        self.assertLess(tempo, 2)
        print('PRODUTOS_BENCHMARK ' + json.dumps(dict(produtos=600,itens=60000,ranking_ms=round(elapsed*1000,2),historico_ms=round(tempo*1000,2),consultas_historico=len(sql))))
