"""Média de dias encerrados, sem conexão ou alterações no banco."""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from .services.relatorios import faturamento_medio_diario


class FaturamentoMedioDiarioTests(SimpleTestCase):
    def media(self, inicio, fim, vendas=(), hoje='2026-11-25'):
        serie = [{'data_venda': date.fromisoformat(dia), 'faturamento': Decimal(valor)}
                 for dia, valor in vendas]
        return faturamento_medio_diario(serie, date.fromisoformat(inicio),
                                       date.fromisoformat(fim), date.fromisoformat(hoje))

    def test_mes_atual_exclui_hoje_do_total_e_do_divisor(self):
        r = self.media('2026-11-01', '2026-11-25',
                       [('2026-11-01', '2400'), ('2026-11-25', '99999')])
        self.assertEqual(r, {'valor': Decimal('100.00'), 'dias': 24, 'inclui_hoje': True})

    def test_mes_encerrado_inclui_dias_sem_vendas(self):
        r = self.media('2026-10-01', '2026-10-31', [('2026-10-31', '3100')])
        self.assertEqual(r, {'valor': Decimal('100.00'), 'dias': 31, 'inclui_hoje': False})

    def test_personalizado_recorta_datas_e_exclui_hoje_e_futuro(self):
        r = self.media('2026-11-22', '2026-11-28', [
            ('2026-11-21', '999'), ('2026-11-22', '30'), ('2026-11-24', '60'),
            ('2026-11-25', '999'), ('2026-11-26', '999')])
        self.assertEqual(r, {'valor': Decimal('30.00'), 'dias': 3, 'inclui_hoje': True})

    def test_personalizado_encerrado_respeita_limites_e_centavos(self):
        r = self.media('2026-11-20', '2026-11-22', [
            ('2026-11-19', '999'), ('2026-11-20', '100'), ('2026-11-23', '999')])
        self.assertEqual(r, {'valor': Decimal('33.33'), 'dias': 3, 'inclui_hoje': False})

    def test_sem_dias_encerrados_nao_divide_por_zero(self):
        for inicio, fim, inclui in [('2026-11-25', '2026-11-25', True),
                                    ('2026-11-26', '2026-11-28', False)]:
            with self.subTest(inicio=inicio):
                self.assertEqual(self.media(inicio, fim),
                                 {'valor': None, 'dias': 0, 'inclui_hoje': inclui})

    def test_dias_encerrados_sem_vendas_mostram_zero(self):
        self.assertEqual(self.media('2026-11-20', '2026-11-22')['valor'], Decimal('0.00'))

    def test_primeiro_dia_do_mes_e_fevereiro_bissexto(self):
        self.assertIsNone(self.media('2026-11-01', '2026-11-01', hoje='2026-11-01')['valor'])
        r = self.media('2024-02-01', '2024-02-29', [('2024-02-29', '2900')])
        self.assertEqual((r['valor'], r['dias']), (Decimal('100.00'), 29))
