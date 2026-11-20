"""Semanas equivalentes; dados de teste somente no PostgreSQL descartável."""
from datetime import date, timedelta
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .analise_evolucao import analisar
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests


HOJE = date(2026, 11, 19)  # quinta-feira


class ComparativoSemanalTests(SimpleTestCase):
    def analisar(self, inicio, fim, dias=(), anteriores=(), hoje=HOJE):
        linhas = lambda dados: [{'data_venda': date.fromisoformat(d), 'faturamento': D(v)} for d, v in dados]
        return analisar(linhas(dias), date.fromisoformat(inicio), date.fromisoformat(fim),
                        hoje, 'semanal', 0, referencia_dias=linhas(anteriores))

    def test_completas_comparam_sete_dias_imediatamente_anteriores(self):
        r = self.analisar('2026-11-02', '2026-11-15',
            [('2026-11-02', '150'), ('2026-11-08', '50'), ('2026-11-09', '120')],
            [('2026-10-26', '100')])
        a, b = r['itens']
        self.assertEqual((a['diferenca'], a['variacao']), (100, 100))
        self.assertEqual((b['diferenca'], b['variacao']), (-80, -40))
        self.assertEqual(a['comparacao']['anterior_inicio'], '2026-10-26')
        self.assertEqual(a['comparacao']['anterior_fim'], '2026-11-01')
        self.assertTrue(all(i['comparacao']['tipo'] == 'completa' for i in r['itens']))

    def test_semana_atual_compara_segunda_ate_hoje_sem_usar_fim_de_semana(self):
        r = self.analisar('2026-11-09', '2026-11-22',
            [('2026-11-09', '100'), ('2026-11-13', '9000'), ('2026-11-16', '125'),
             ('2026-11-20', '9999')])
        atual = r['itens'][-1]
        self.assertEqual((atual['valor'], atual['diferenca'], atual['variacao']), (125, 25, 25))
        self.assertEqual(atual['fim'], '2026-11-19')
        self.assertEqual(atual['comparacao']['anterior_fim'], '2026-11-12')
        self.assertEqual(atual['comparacao']['tipo'], 'parcial')
        self.assertTrue(atual['comparacao']['inclui_hoje'])
        self.assertEqual(r['quantidade_completos'], 1)
        self.assertEqual(r['media'], 9100)

    def test_personalizado_usa_mesmos_dias_e_nao_a_barra_anterior_recortada(self):
        r = self.analisar('2026-11-04', '2026-11-15',
            [('2026-11-04', '30'), ('2026-11-09', '160')],
            [('2026-10-28', '20'), ('2026-11-02', '50')])
        parcial, completa = r['itens']
        self.assertEqual((parcial['diferenca'], parcial['variacao']), (10, 50))
        self.assertEqual(parcial['comparacao']['anterior_inicio'], '2026-10-28')
        self.assertEqual(parcial['comparacao']['anterior_fim'], '2026-11-01')
        self.assertEqual(completa['comparacao']['anterior'], 80)
        self.assertEqual((completa['diferenca'], completa['variacao']), (80, 100))
        self.assertEqual(completa['comparacao']['tipo'], 'completa')

    def test_zero_e_ausencia_de_vendas_nao_dividem_por_zero(self):
        for dias, diferenca in (([], 0), ([('2026-11-16', '80')], 80)):
            r = self.analisar('2026-11-16', '2026-11-19', dias)
            self.assertEqual(r['itens'][0]['diferenca'], diferenca)
            self.assertIsNone(r['itens'][0]['variacao'])
            self.assertIn('não calculável', r['itens'][0]['motivo'])
            self.assertTrue(r['comparacao_disponivel'])

    def test_nao_pula_semana_imediata_sem_vendas_para_comparar_com_outra(self):
        r = self.analisar('2026-11-02', '2026-11-19',
            [('2026-11-02', '100'), ('2026-11-16', '50')])
        ultima = r['itens'][-1]
        self.assertEqual(ultima['comparacao']['anterior'], 0)
        self.assertEqual(ultima['diferenca'], 50)
        self.assertIsNone(ultima['variacao'])

    def test_virada_de_mes_compara_semana_sem_cortar_em_mes(self):
        r = self.analisar('2026-10-26', '2026-11-08',
            [('2026-10-31', '100'), ('2026-11-01', '50'), ('2026-11-02', '180')])
        self.assertEqual(r['itens'][0]['valor'], 150)
        self.assertEqual(r['itens'][1]['comparacao']['anterior'], 150)
        self.assertEqual((r['itens'][1]['diferenca'], r['itens'][1]['variacao']), (30, 20))

    def test_domingo_atual_parcial_e_segunda_ja_completa(self):
        for hoje, tipo in ((date(2026, 11, 15), 'parcial'), (date(2026, 11, 16), 'completa')):
            r = self.analisar('2026-11-09', '2026-11-15', [('2026-11-09', '100')],
                [('2026-11-02', '80')], hoje=hoje)
            self.assertEqual(r['itens'][0]['comparacao']['tipo'], tipo)
            self.assertEqual(r['itens'][0]['variacao'], 25)

    def test_futuro_sem_comparacao_e_referencia_externa_nao_presumida(self):
        r = self.analisar('2026-11-23', '2026-11-29', [('2026-11-23', '100')])
        self.assertIsNone(r['itens'][0]['comparacao'])
        self.assertEqual(r['itens'][0]['valor'], 0)
        r = analisar([], date(2026, 11, 16), HOJE, HOJE, 'semanal', 0)
        self.assertIsNone(r['itens'][0]['diferenca'])


class ComparativoSemanalDBTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('comparativo-admin', is_staff=True, is_superuser=True)

    def venda(self, dia, valor, **kwargs):
        return Venda.objects.create(data_venda=date.fromisoformat(dia), total=D(valor),
            tipo_pagamento=kwargs.pop('tipo_pagamento', 'À vista'), **kwargs)

    def test_referencia_fora_do_filtro_nao_contamina_cartoes_e_exclui_consumo_canceladas(self):
        self.venda('2026-11-09', '80')
        self.venda('2026-11-12', '20', tipo_pagamento='A prazo')
        self.venda('2026-11-13', '9000')  # sexta anterior não participa da parcial
        self.venda('2026-11-16', '120')
        self.venda('2026-11-16', '700', cancelada=True)
        self.venda('2026-11-09', '800', tipo_pagamento='consumo_proprio')
        self.venda('2026-11-16', '900', tipo_pagamento='consumo_proprio')
        with CaptureQueriesContext(connection) as queries:
            r = resumo_vendas(date(2026, 11, 16), HOJE, modo='semanal', hoje=HOJE)
        self.assertEqual(len(queries), 4)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        c = r['analise_evolucao']['itens'][0]
        self.assertEqual((c['diferenca'], c['variacao']), (20, 20))
        self.assertEqual(r['faturamento'], 120)
        self.assertEqual(r['consumo']['valor'], 900)
        self.assertEqual(r['quantidade'], 1)
        self.assertEqual(sum(i['faturamento'] for i in r['evolucao']), 120)

    def test_uma_consulta_adicional_independente_da_quantidade_de_semanas(self):
        for n in range(80):
            self.venda((HOJE-timedelta(days=n)).isoformat(), '10')
        with CaptureQueriesContext(connection) as curto:
            resumo_vendas(HOJE-timedelta(days=13), HOJE, modo='semanal', hoje=HOJE)
        with CaptureQueriesContext(connection) as longo:
            resumo_vendas(HOJE-timedelta(days=79), HOJE, modo='semanal', hoje=HOJE)
        self.assertEqual((len(curto), len(longo)), (4, 5))
        for queries in (curto, longo):
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))

    def test_django_completo_preserva_filtros_paginacao_e_comparacao(self):
        for n in range(50):
            self.venda((HOJE-timedelta(days=n)).isoformat(), '10')
        self.client.force_login(self.admin)
        params = dict(periodo='personalizado', data_inicial='2026-10-01', data_final='2026-11-19', modo='semanal')
        with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
            primeira = self.client.get('/relatorios/vendas/', params)
            segunda = self.client.get('/relatorios/vendas/', dict(params, pagina='2'))
        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(primeira.context['resumo']['analise_evolucao'], segunda.context['resumo']['analise_evolucao'])
        self.assertContains(segunda, 'Semanas de segunda a domingo')
        self.assertContains(segunda, 'modo=semanal')
        self.assertContains(segunda, 'data_inicial=2026-10-01')
        self.assertEqual(segunda.context['resumo']['faturamento'], 500)
