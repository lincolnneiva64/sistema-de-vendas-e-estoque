"""Presentation and navigation regressions; no operational writes."""
from copy import deepcopy
from datetime import date
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from .analise_evolucao import analisar, variacao
from .graficos_relatorios import barras_evolucao
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests
from offline.browser_support import Chrome


HOJE = date(2026, 11, 20)
ROOT = Path(__file__).resolve().parents[1]
BROWSER = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


class BarrasEquivalentesTests(SimpleTestCase):
    def test_duas_barras_usam_totais_existentes_e_mesma_escala_sem_alterar_analise(self):
        for anterior, atual in ((D('46869.81'), D('43206.21')), (D('100'), D('125')), (D(0), D('50'))):
            analise = analisar([], date(2026, 11, 1), HOJE, HOJE, 'mensal', 0)
            analise['equivalente'] = dict(atual=atual, anterior=anterior,
                atual_inicio=date(2026, 11, 1), atual_fim=HOJE,
                anterior_inicio=date(2026, 10, 1), anterior_fim=date(2026, 10, 20),
                diferenca=atual-anterior, variacao=variacao(atual, anterior), inclui_hoje=True)
            original = deepcopy(analise)
            barras = barras_evolucao(analise)
            self.assertEqual(analise, original)
            self.assertEqual([i['valor'] for i in barras['itens']], [anterior, atual])
            self.assertEqual(barras['maximo'], max(anterior, atual))
            self.assertTrue(barras['itens'][0]['referencia'])
            self.assertFalse(barras['itens'][1]['referencia'])
            self.assertIn('Outubro', barras['itens'][0]['rotulo'])
            self.assertIn('Novembro', barras['itens'][1]['rotulo'])
            self.assertEqual(barras['itens'][1]['diferenca'], atual-anterior)

    def test_diario_semanal_e_mensal_sem_equivalencia_preservados(self):
        for modo in ('diario', 'semanal', 'mensal'):
            analise = analisar([], date(2026, 11, 1), HOJE, HOJE, modo, 0)
            original = deepcopy(analise)
            barras_evolucao(analise)
            self.assertEqual(analise, original)
            if modo != 'semanal':
                self.assertIs(barras_evolucao(analise), analise)

    def test_visao_mensal_semanal_oculta_apenas_inicio_parcial_sem_alterar_valores(self):
        dias = [{'data_venda': date(2026, 11, d), 'faturamento': D(v)}
                for d, v in ((1, '90'), (2, '100'), (9, '200'), (16, '300'))]
        analise = analisar(dias, date(2026, 11, 1), HOJE, HOJE, 'semanal', 0, referencia_dias=[])
        original = deepcopy(analise)
        grafico = barras_evolucao(analise)
        self.assertEqual([i['inicio'] for i in grafico['itens']], ['2026-11-02', '2026-11-09', '2026-11-16'])
        self.assertEqual(grafico['itens'], original['itens'][1:])
        self.assertEqual(analise, original)
        self.assertEqual(sum(i['valor'] for i in analise['itens']), 690)

    def test_meses_com_todos_os_dias_de_inicio_e_personalizados_curtos(self):
        dias_iniciais = set()
        for mes in range(1, 13):
            inicio, fim = date(2026, mes, 1), date(2026, mes, 20)
            dias_iniciais.add(inicio.weekday())
            analise = analisar([], inicio, fim, date(2027, 1, 1), 'semanal', 0, referencia_dias=[])
            grafico = barras_evolucao(analise)
            self.assertEqual(date.fromisoformat(grafico['itens'][0]['inicio']).weekday(), 0)
            if inicio.weekday() == 0:
                self.assertEqual(grafico['itens'], analise['itens'])
            curto = analisar([], inicio, date(2026, mes, 3), date(2027, 1, 1), 'semanal', 0, referencia_dias=[])
            self.assertIs(barras_evolucao(curto), curto)
        self.assertEqual(dias_iniciais, set(range(7)))
        personalizado = analisar([], date(2026, 11, 4), date(2026, 11, 20), HOJE, 'semanal', 0, referencia_dias=[])
        self.assertIs(barras_evolucao(personalizado), personalizado)

    def test_referencia_invalida_nao_cria_segunda_barra(self):
        analise = analisar([], date(2026, 11, 1), HOJE, HOJE, 'mensal', 0)
        analise['equivalente'] = {'motivo': 'Mês anterior sem os mesmos dias.'}
        self.assertIs(barras_evolucao(analise), analise)


class RelatoriosFinaisDjangoTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('graficos-admin', is_staff=True, is_superuser=True)

    def test_mes_equivalente_tem_duas_barras_e_nao_modifica_cartoes_ou_queries(self):
        from .tests_relatorios_cobertura import completar_registros
        Venda.objects.create(data_venda=date(2026, 10, 20), total=D(100), tipo_pagamento='À vista')
        Venda.objects.create(data_venda=HOJE, total=D(75), tipo_pagamento='À vista')
        completar_registros(date(2026,10,1),HOJE)
        with CaptureQueriesContext(connection) as queries:
            r = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(r['faturamento'], 75)
        self.assertEqual(r['quantidade'], 20)
        self.assertEqual([b['valor'] for b in r['grafico_evolucao']['itens']], [100, 75])
        self.assertEqual(r['analise_evolucao']['equivalente']['diferenca'], -25)

    def test_comparar_tem_ancora_nativa_e_filtro_principal_nao_herda_ancora(self):
        self.client.force_login(self.admin)
        params = dict(periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20',
            modo='mensal', equivalente='on')
        with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
            resposta = self.client.get('/relatorios/vendas/', params)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'action="/relatorios/vendas/#evolucao-vendas"')
        self.assertContains(resposta, 'action="/relatorios/vendas/" class="relatorios-filtros"')
        self.assertContains(resposta, 'id="evolucao-vendas"', count=1)
        self.assertContains(resposta, 'name="data_inicial" value="2026-11-01"')
        self.assertIn('equivalente=on', resposta.context['filtros_query'])

    def test_composicao_consulta_sem_duplicidade_e_cartoes_com_todo_o_mes(self):
        Venda.objects.create(data_venda=date(2026, 11, 1), total=D(90), tipo_pagamento='À vista')
        Venda.objects.create(data_venda=HOJE, total=D(100), tipo_pagamento='À vista')
        Venda.objects.create(data_venda=HOJE, total=D(20), tipo_pagamento='consumo_proprio')
        Venda.objects.create(data_venda=HOJE, total=D(99), tipo_pagamento='consumo_proprio', cancelada=True)
        r = resumo_vendas(date(2026, 11, 1), HOJE, modo='semanal', hoje=HOJE)
        self.assertEqual((r['faturamento'], r['consumo']['valor'], r['total_registros_consulta']), (190, 20, 210))
        self.assertEqual(r['quantidade'], 2)
        self.assertEqual(r['ticket_medio'], 95)
        self.assertEqual(sum(d['faturamento'] for d in r['evolucao']), 190)
        self.assertEqual(r['analise_evolucao']['itens'][0]['valor'], 90)
        self.assertEqual(r['grafico_evolucao']['itens'][0]['inicio'], '2026-11-02')
        self.client.force_login(self.admin)
        with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
            resposta = self.client.get('/relatorios/vendas/', dict(periodo='personalizado',
                data_inicial='2026-11-01', data_final='2026-11-20', modo='semanal'))
        self.assertContains(resposta, 'Total de registros da consulta')
        self.assertContains(resposta, 'R$ 210,00')
        self.assertContains(resposta, 'não integra o faturamento comercial nem o ticket médio')

    @skipUnless(Path(BROWSER).is_file(), 'Edge/Chrome não disponível')
    def test_troca_modos_enquadra_titulo_controles_e_percentuais_na_altura_da_janela(self):
        for dia, valor in ((1, 90), (2, 100), (9, 200), (16, 300)):
            Venda.objects.create(data_venda=date(2026, 11, dia), total=D(valor), tipo_pagamento='À vista')
        Venda.objects.create(data_venda=date(2026, 10, 20), total=D(400), tipo_pagamento='À vista')
        self.client.force_login(self.admin)
        css = (ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        script = (ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        with TemporaryDirectory(prefix='relatorios-enquadramento-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(BROWSER, perfil)
            original = subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen', side_effect=lambda cmd, **kwargs:
                       original([cmd[0], '--no-sandbox', *cmd[1:]], **kwargs)):
                browser.start()
            try:
                for largura, altura in ((1366, 768), (1366, 600), (1024, 720), (390, 740), (390, 640), (320, 667)):
                    for modo, alcance in ((m, a) for m in ('diario', 'semanal', 'mensal')
                                          for a in ('principal', '12', 'personalizado')):
                        with self.subTest(largura=largura, altura=altura, modo=modo, alcance=alcance):
                            params = dict(periodo='personalizado', data_inicial='2026-11-01',
                                data_final='2026-11-20', modo=modo, equivalente='on', dia_semana='0',
                                alcance=alcance, historico_inicio='2026-01-01', historico_fim='2026-11-20',
                                comparacao='ano_anterior' if alcance == '12' else 'anterior')
                            with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
                                resposta = self.client.get('/relatorios/vendas/', params)
                            html = resposta.content.decode()
                            html = html[html.index('<main class="relatorios '):]
                            html = html[:html.index('</main>')+7]
                            tab = browser.tab()
                            try:
                                tab.call('Network.enable')
                                tab.call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*']})
                                tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': altura,
                                    'deviceScaleFactor': 1, 'mobile': largura < 600})
                                tab.evaluate('document.head.innerHTML='+json.dumps('<meta name="viewport" content="width=device-width, initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;font:16px system-ui}'+css+'</style>'))
                                tab.evaluate('document.body.innerHTML='+json.dumps('<div style="height:1200px"></div>'+html))
                                tab.evaluate("location.hash='evolucao-vendas'")
                                tab.evaluate(script)
                                tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
                                def medir():
                                    return tab.evaluate('''(()=>{const rect=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom};};
                                        return {titulo:rect(document.getElementById('titulo-evolucao-vendas')),
                                            controles:rect(document.getElementById('evolucao-controles')),
                                            percentuais:[...document.querySelectorAll('.grafico-barra-variacao')].map(rect),
                                            tela:innerHeight,scroll:scrollY,
                                            largura:document.documentElement.scrollWidth,
                                            inicio:document.getElementById('evolucao-controles').elements.data_inicial.value};})()''')
                                m = medir()
                                self.assertGreaterEqual(m['titulo']['top'], 0)
                                self.assertLess(m['titulo']['top'], 80)
                                self.assertGreaterEqual(m['controles']['top'], m['titulo']['bottom'])
                                self.assertEqual(m['inicio'], '2026-11-01')
                                self.assertLessEqual(m['largura'], largura+1)
                                composicao = tab.evaluate('''[...document.querySelectorAll('.consumo-composicao strong')].map(e=>({
                                    texto:e.innerText, fonte:parseFloat(getComputedStyle(e).fontSize),
                                    peso:parseFloat(getComputedStyle(e).fontWeight), corte:e.scrollWidth>e.clientWidth+1}))''')
                                self.assertEqual(len(composicao), 3)
                                self.assertEqual(composicao[0]['texto'], composicao[2]['texto'])
                                for valor in composicao:
                                    self.assertGreaterEqual(valor['fonte'], 17)
                                    self.assertGreaterEqual(valor['peso'], 700)
                                    self.assertFalse(valor['corte'])
                                for percentual in m['percentuais']:
                                    self.assertGreaterEqual(percentual['top'], m['controles']['bottom'])
                                    if modo != 'mensal':
                                        self.assertLessEqual(percentual['bottom'], altura-8, m)
                                if modo == 'mensal':
                                    # The new monthly scale must remain tall enough
                                    # to distinguish values; never shrink it to fit
                                    # controls, legend and every metric on a phone.
                                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.grafico-barra-variacao')].every(e=>e.getBoundingClientRect().bottom<=e.closest('li').getBoundingClientRect().bottom)"))
                                tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,80));})()')
                                self.assertAlmostEqual(medir()['scroll'], m['scroll'], delta=2)
                                self.assertLessEqual(tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"), 31)
                                if tab.evaluate("!document.querySelector('[data-barras-proximo]').disabled"):
                                    primeiro = tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data")
                                    tab.evaluate("document.querySelector('[data-barras-proximo]').click()")
                                    self.assertNotEqual(tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data"), primeiro)
                                    self.assertLessEqual(tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"), 31)
                                    tab.evaluate("document.querySelector('[data-barras-anterior]').click()")
                                    self.assertEqual(tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data"), primeiro)
                                    self.assertAlmostEqual(medir()['scroll'], m['scroll'], delta=2)
                            finally:
                                tab.call('Page.close');tab.socket.close()
            finally:
                browser.stop()
