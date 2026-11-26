"""Validação exclusivamente no cluster descartável, sem migrations."""
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from offline.browser_support import Chrome
from .models import Venda, ItemVenda
from .services.analise_desempenho import analisar_desempenho, desempenho_comercial
from .tests_relatorios import PostgreSQLIsoladoTests
from .tests_relatorios_cobertura import documentados, completar_registros


class AnaliseTests(SimpleTestCase):
    def analisar(self, inicio=date(2026, 11, 1), fim=date(2026, 11, 20),
                 hoje=date(2026, 11, 20), dados=None):
        return analisar_desempenho(dados if dados is not None else documentados(
            date(2026, 10, 1), date(2026, 11, 20),
            {date(2026, 10, 1): '100', date(2026, 11, 1): '120', date(2026, 11, 20): '9999'}),
            inicio, fim, hoje)

    def test_hoje_fica_fora_dos_dois_operandos(self):
        r = self.analisar()
        self.assertTrue(r['comparavel'])
        self.assertEqual((r['atual'], r['anterior'], r['diferenca'], r['percentual']), (120, 100, 20, 20))
        self.assertEqual((r['fim'], r['referencia_fim']), (date(2026,11,19), date(2026,10,19)))
        self.assertEqual(r['medidas'][0]['diferenca'], 0)
        self.assertEqual(r['medidas'][1]['direcao'], 'aumentou')

    def test_completo_contra_completo_com_calendarios_diferentes(self):
        r = self.analisar(fim=date(2026,11,30), hoje=date(2026,12,1),
            dados=documentados(date(2026,10,1), date(2026,11,30)))
        self.assertTrue(r['comparavel'])
        self.assertEqual(r['referencia_fim'], date(2026,10,31))
        self.assertEqual(r['medidas'][0]['direcao'], 'caiu')

    def test_lacuna_na_referencia_impede_conclusao(self):
        dados = documentados(date(2026,10,2), date(2026,11,20))
        r = self.analisar(dados=dados)
        self.assertFalse(r['comparavel'])
        self.assertIn('cobertura diária da referência', r['motivo'])

    def test_zero_na_referencia_preserva_diferenca_sem_percentual(self):
        r = self.analisar(dados=documentados(date(2026,10,1), date(2026,11,20),
                                            {date(2026,11,1): '50'}))
        self.assertTrue(r['comparavel'])
        self.assertEqual(r['diferenca'], 50)
        self.assertIsNone(r['percentual'])

    def test_so_hoje_futuro_e_multiplos_meses(self):
        for inicio, fim in ((date(2026,11,20), date(2026,11,20)),
                            (date(2026,12,1), date(2026,12,5)),
                            (date(2026,10,1), date(2026,11,20))):
            with self.subTest(inicio=inicio):
                r = self.analisar(inicio=inicio, fim=fim)
                self.assertFalse(r['comparavel'])
                self.assertTrue(r['motivo'])

    def test_recorte_parcial_e_data_inexistente(self):
        r = self.analisar(inicio=date(2026,11,7))
        self.assertEqual(r['referencia_inicio'], date(2026,10,7))
        r = self.analisar(inicio=date(2026,3,1), fim=date(2026,3,30), hoje=date(2026,4,1),
                          dados=documentados(date(2026,2,1), date(2026,3,30)))
        self.assertFalse(r['comparavel'])
        self.assertIn('calendário', r['motivo'])

    def test_quantidade_sobe_ticket_cai_e_total_nulo_bloqueia(self):
        dados = documentados(date(2026,10,1), date(2026,11,20),
            {date(2026,10,1): '100', date(2026,11,1): '120'})
        next(d for d in dados if d['data_venda'] == date(2026,11,1))['quantidade'] = 20
        r = self.analisar(dados=dados)
        self.assertEqual([m['direcao'] for m in r['medidas']], ['aumentou', 'caiu'])
        dados[0]['sem_valor'] = 1
        dados[0]['faturamento'] = None
        r = self.analisar(dados=dados)
        self.assertFalse(r['comparavel'])
        self.assertIn('sem total', r['motivo'])


class AnaliseIntegradaTests(PostgreSQLIsoladoTests, TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('analise-admin', is_staff=True, is_superuser=True)
        completar_registros(date(2026,10,1), date(2026,11,20))
        self.venda = Venda.objects.create(data_venda=date(2026,11,1), total=120, tipo_pagamento='A prazo')
        Venda.objects.create(data_venda=date(2026,10,1), total=100, tipo_pagamento='Pix')
        for tipo, cancelada in (('consumo_proprio', False), ('Pix', True)):
            Venda.objects.create(data_venda=date(2026,11,1), total=9999,
                                 tipo_pagamento=tipo, cancelada=cancelada)
        for valor in (50, 70):
            ItemVenda.objects.create(venda=self.venda, quantidade=1, unidade='UN',
                                     preco_unitario=valor, valor_total=valor)

    def test_uma_agregacao_somente_leitura_sem_duplicacao(self):
        with CaptureQueriesContext(connection) as queries:
            r = desempenho_comercial(date(2026,11,1), date(2026,11,20), date(2026,11,20))
        self.assertEqual(len(queries), 1)
        self.assertTrue(queries[0]['sql'].lstrip().upper().startswith('SELECT'))
        self.assertEqual((r['atual'], r['anterior']), (120,100))
        self.assertEqual(r['medidas'][0]['atual'], 20)

    def test_grafico_independente_filtros_e_cards_preservados(self):
        self.client.force_login(self.admin)
        resultados = []
        for modo in ('diario', 'semanal', 'mensal'):
            with patch('estoque.views_relatorios.timezone.localdate', return_value=date(2026,11,20)):
                r = self.client.get('/relatorios/vendas/', dict(periodo='personalizado',
                    data_inicial='2026-11-01', data_final='2026-11-20', modo=modo,
                    alcance='6', semanas_comparacao='4'))
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, 'Análise do desempenho')
            self.assertContains(r, 'até ontem')
            self.assertContains(r, 'data_inicial=2026-11-01')
            self.assertContains(r, 'semanas_comparacao=4')
            self.assertContains(r, 'Cálculo completo indisponível')
            self.assertEqual(r.context['resumo']['faturamento'], 120)
            self.assertEqual(r.context['resumo']['quantidade'], 21)
            resultados.append(r.context['desempenho'])
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(resultados[1], resultados[2])


class AnaliseVisualTests(PostgreSQLIsoladoTests, StaticLiveServerTestCase):
    def test_desktop_mobile_no_relatorio_completo(self):
        admin = get_user_model().objects.create_user('visual-analise', is_staff=True, is_superuser=True)
        completar_registros(date(2026,10,1), date(2026,11,20))
        self.client.force_login(admin)
        edge = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        self.assertTrue(Path(edge).is_file(), 'Edge necessário para validação visual')
        with TemporaryDirectory(prefix='analise-browser-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(edge, perfil).start()
            try:
                tab = browser.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls': ['https://*']})
                tab.call('Network.setCookie', {'name': settings.SESSION_COOKIE_NAME,
                    'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url})
                for largura in (390, 1366):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=900,
                        deviceScaleFactor=1, mobile=largura < 600))
                    tab.call('Page.navigate', {'url': self.live_server_url + '/relatorios/vendas/?periodo=personalizado&data_inicial=2026-11-01&data_final=2026-11-19&modo=mensal'})
                    tab.wait("document.readyState==='complete' && !!document.getElementById('analise-desempenho')")
                    self.assertEqual(tab.evaluate("document.querySelectorAll('.relatorios-indicadores > div').length"), 9)
                    self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    self.assertTrue(tab.evaluate("!!document.querySelector('a[href*=\"evolucao-diaria\"]')"))
                    tab.evaluate("document.getElementById('analise-desempenho').scrollIntoView()")
                    import base64
                    pasta = Path('artifacts/analise-desempenho')
                    pasta.mkdir(parents=True, exist_ok=True)
                    (pasta / f'{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png'})['data']))
            finally:
                browser.stop()
