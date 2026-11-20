"""Equivalência mensal no mecanismo existente; somente banco descartável."""
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .analise_evolucao import referencia_equivalente
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests
from .templatetags.relatorios import moeda_sinal_br, numero_sinal_br
from .tests_relatorios_cobertura import completar_registros


HOJE = date(2026, 11, 20)


class ReferenciaMensalTests(SimpleTestCase):
    def test_recorte_fixo_nao_exige_que_fim_seja_hoje(self):
        for hoje in (HOJE, date(2026, 11, 21), date(2026, 12, 5)):
            ref, motivo = referencia_equivalente(date(2026, 11, 1), HOJE, hoje)
            self.assertEqual(ref, (date(2026, 11, 1), HOJE, date(2026, 10, 1), date(2026, 10, 20)))
            self.assertEqual(motivo, '')

    def test_fim_futuro_limita_comparacao_a_hoje(self):
        ref, _ = referencia_equivalente(date(2026, 11, 1), date(2026, 11, 30), HOJE)
        self.assertEqual(ref[1], HOJE)
        self.assertEqual(ref[3], date(2026, 10, 20))

    def test_virada_ano_e_fevereiro_bissexto(self):
        ref, _ = referencia_equivalente(date(2027, 1, 1), date(2027, 1, 20), date(2027, 1, 21))
        self.assertEqual(ref[2:], (date(2026, 12, 1), date(2026, 12, 20)))
        ref, _ = referencia_equivalente(date(2024, 3, 1), date(2024, 3, 29), date(2024, 3, 29))
        self.assertEqual(ref[3], date(2024, 2, 29))
        ref, motivo = referencia_equivalente(date(2026, 3, 1), date(2026, 3, 29), date(2026, 3, 29))
        self.assertIsNone(ref)
        self.assertIn('mesmo número', motivo)

    def test_filtro_sem_inicio_do_mes_nao_inventa_dias(self):
        self.assertIsNone(referencia_equivalente(date(2026, 11, 2), HOJE, HOJE)[0])
        self.assertIsNone(referencia_equivalente(date(2026, 12, 1), date(2026, 12, 20), HOJE)[0])

    def test_formatacao_sinais_sem_recalcular_valores(self):
        self.assertEqual(moeda_sinal_br(D('62.30')), '+R$ 62,30')
        self.assertEqual(moeda_sinal_br(D('-18.50')), '-R$ 18,50')
        self.assertEqual(numero_sinal_br(D('20.00')), '+20,00')
        self.assertEqual(numero_sinal_br(D('-20.00')), '-20,00')


class ComparativoMensalDBTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('mensal-admin', is_staff=True, is_superuser=True)

    def venda(self, dia, total, **kwargs):
        return Venda.objects.create(data_venda=date.fromisoformat(dia), total=D(total),
            tipo_pagamento=kwargs.pop('tipo_pagamento', 'À vista'), **kwargs)

    def test_anterior_externo_exclui_canceladas_consumo_e_preserva_indicadores(self):
        self.venda('2026-11-20', '120')
        self.venda('2026-10-20', '100')
        self.venda('2026-10-21', '1000')
        self.venda('2026-10-20', '900', cancelada=True)
        self.venda('2026-10-20', '800', tipo_pagamento='consumo_proprio')
        self.venda('2026-11-20', '700', tipo_pagamento='consumo_proprio')
        completar_registros(date(2026,10,1),HOJE)
        with CaptureQueriesContext(connection) as queries:
            r = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        c = r['analise_evolucao']['equivalente']
        self.assertEqual((c['atual'], c['anterior'], c['diferenca'], c['variacao']), (120, 100, 20, 20))
        self.assertEqual((r['faturamento'], r['quantidade'], r['ticket_medio']), (120, 20, 6))
        self.assertEqual(r['consumo']['valor'], 700)
        self.assertEqual(sum(d['faturamento'] for d in r['evolucao']), 120)
        item = r['analise_evolucao']['itens'][0]
        self.assertEqual((item['diferenca'], item['variacao']), (20, 20))
        self.assertEqual(item['comparacao']['anterior_fim'], '2026-10-20')

    def test_get_com_opcao_marcada_mostra_comparacao_em_vez_de_aviso(self):
        self.venda('2026-11-20', '120')
        self.venda('2026-10-20', '100')
        completar_registros(date(2026,10,1),HOJE)
        self.client.force_login(self.admin)
        params = dict(periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20',
            modo='mensal', equivalente='on')
        with patch('estoque.views_relatorios.timezone.localdate', return_value=date(2026, 11, 21)):
            r = self.client.get('/relatorios/vendas/', params)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Sem período comparável')
        self.assertContains(r, 'Comparação parcial equivalente')
        self.assertContains(r, '01/10/2026 a 20/10/2026')
        self.assertContains(r, '+R$ 20,00')
        self.assertContains(r, '+20,00%')
        self.assertIn('equivalente=on', r.context['filtros_query'])
        self.assertTrue(r.context['evolucao_form'].cleaned_data['equivalente'])

    def test_zero_anterior_tem_diferenca_sem_percentual_e_queda_tem_sinais(self):
        self.venda('2026-11-20', '80')
        completar_registros(date(2026,10,1),HOJE)
        r = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(r['analise_evolucao']['equivalente']['diferenca'], 80)
        self.assertIsNone(r['analise_evolucao']['equivalente']['variacao'])
        self.assertTrue(r['analise_evolucao']['comparacao_disponivel'])
        self.venda('2026-10-20', '100')
        r = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual((r['analise_evolucao']['equivalente']['diferenca'],
            r['analise_evolucao']['equivalente']['variacao']), (-20, -20))

    def test_anterior_ja_carregado_reutiliza_serie_e_opcao_desmarcada_nao_consulta(self):
        self.venda('2026-11-20', '120')
        self.venda('2026-10-20', '100')
        completar_registros(date(2026,10,1),HOJE)
        with self.assertNumQueries(6):
            r = resumo_vendas(date(2026, 10, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(r['faturamento'], 220)
        self.assertEqual(r['analise_evolucao']['equivalente']['diferenca'], 20)
        with self.assertNumQueries(5):
            r = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', hoje=HOJE)
        self.assertNotIn('equivalente', r['analise_evolucao'])
