"""Historical analysis uses only disposable test databases; no HTTP server."""
from datetime import date, timedelta
from decimal import Decimal as D
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .analise_evolucao import analisar_historico, periodos_historicos
from .forms_relatorios import EvolucaoRelatorioForm, intervalo_historico
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests
from .tests_relatorios_cobertura import documentados, completar_registros


HOJE = date(2026, 11, 20)


def linhas(*pares):
    return [dict(data_venda=date.fromisoformat(dia), faturamento=D(valor), quantidade=1, sem_valor=0)
            for dia, valor in pares]


def cobrir(dados, inicio, fim):
    por_data = {d['data_venda']: d for d in documentados(inicio, fim)}
    por_data.update({d['data_venda']: d for d in dados})
    return list(por_data.values())


class HistoricoCalendarioTests(SimpleTestCase):
    def test_alcances_calendario_virada_de_ano_e_independencia(self):
        for meses in (3, 6, 12, 24):
            a, b = intervalo_historico({'alcance': str(meses)}, date(2026, 1, 20))
            self.assertEqual((b.year-a.year)*12+b.month-a.month+1, meses)
            self.assertEqual(a.day, 1)
            self.assertEqual(b, date(2026, 1, 20))
        self.assertIsNone(intervalo_historico({'alcance': 'principal'}, HOJE))

    def test_meses_consecutivos_completos_e_parciais_equivalentes(self):
        dados = linhas(('2026-08-01', '80'), ('2026-09-01', '100'),
                       ('2026-10-01', '200'), ('2026-10-25', '50'), ('2026-11-20', '300'))
        dados = cobrir(dados, date(2026,8,1), HOJE)
        r = analisar_historico(dados, date(2026, 9, 1), HOJE, HOJE, 'mensal')
        self.assertEqual([i['valor'] for i in r['itens']], [100, 250, 300])
        self.assertEqual(r['itens'][1]['diferenca'], 150)
        atual = r['itens'][2]
        self.assertTrue(atual['parcial'])
        self.assertEqual(atual['comparacao']['anterior'], 200)
        self.assertEqual(atual['comparacao']['anterior_fim'], '2026-10-20')
        self.assertEqual(atual['variacao'], 50)

    def test_interanual_mes_completo_e_parcial_sem_usar_mes_anterior(self):
        dados = linhas(('2025-10-31', '100'), ('2026-10-31', '120'),
                       ('2025-11-20', '80'), ('2025-11-21', '900'), ('2026-11-20', '100'))
        dados = cobrir(cobrir(dados,date(2025,10,1),date(2025,11,20)),date(2026,10,1),HOJE)
        r = analisar_historico(dados, date(2026, 10, 1), HOJE, HOJE, 'mensal', comparacao='ano_anterior')
        self.assertEqual([i['variacao'] for i in r['itens']], [20, 25])
        self.assertEqual(r['itens'][0]['comparacao']['anterior_fim'], '2025-10-31')
        self.assertEqual(r['itens'][1]['comparacao']['anterior_fim'], '2025-11-20')

    def test_sem_registros_nao_e_zero_e_nulos_nao_sao_ignorados(self):
        dados = linhas(('2026-09-01', '0'), ('2026-10-01', '20'))
        dados = cobrir(dados,date(2026,9,1),date(2026,10,31))
        r = analisar_historico(dados, date(2026, 9, 1), HOJE, HOJE, 'mensal')
        self.assertEqual(r['itens'][0]['valor'], 0)
        self.assertIsNone(r['itens'][0]['comparacao'])
        self.assertEqual(r['itens'][1]['diferenca'], 20)
        self.assertIsNone(r['itens'][1]['variacao'])
        self.assertIsNone(r['itens'][2]['valor'])
        next(d for d in dados if d['data_venda'] == date(2026,10,1))['sem_valor'] = 1
        self.assertIsNone(analisar_historico(dados, date(2026, 10, 1), HOJE, HOJE, 'mensal')['itens'][0]['valor'])

    def test_bissexto_dias_inexistentes_e_meses_completos_diferentes(self):
        for comparacao in ('anterior', 'ano_anterior'):
            dados = linhas(('2024-02-29', '100'), ('2024-01-29', '50'), ('2023-02-28', '50'))
            dados = cobrir(dados,date(2024,1,1),date(2024,2,29))
            r = analisar_historico(dados, date(2024, 2, 1), date(2024, 2, 29), date(2024, 2, 29),
                                  'mensal', comparacao=comparacao)
            self.assertEqual(r['itens'][0]['comparacao'] is None, comparacao == 'ano_anterior')
        dados = linhas(('2026-02-01', '100'), ('2026-03-31', '120'))
        dados = cobrir(dados,date(2026,2,1),date(2026,3,31))
        completo = analisar_historico(dados, date(2026, 3, 1), date(2026, 3, 31), HOJE, 'mensal')['itens'][0]
        self.assertEqual(completo['comparacao']['anterior_fim'], '2026-02-28')
        parcial = analisar_historico(dados, date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 31), 'mensal')['itens'][0]
        self.assertIsNone(parcial['comparacao'])

    def test_semanas_continuas_atravessam_meses_e_ano(self):
        p = list(periodos_historicos(date(2025, 12, 29), date(2026, 2, 5), date(2026, 2, 5), 'semanal', -1))
        self.assertEqual(p[0][:2], (date(2025, 12, 29), date(2026, 1, 4)))
        self.assertEqual(p[4][:2], (date(2026, 1, 26), date(2026, 2, 1)))
        for anterior, atual in zip(p, p[1:]):
            self.assertEqual(atual[0]-anterior[1], timedelta(days=1))
            self.assertEqual(atual[0].weekday(), 0)
        dados = linhas(('2026-01-29', '100'), ('2026-01-30', '800'), ('2026-02-05', '120'))
        ultimo = analisar_historico(dados, date(2025, 12, 29), date(2026, 2, 5), date(2026, 2, 5), 'semanal')['itens'][-1]
        self.assertEqual(ultimo['comparacao']['anterior_fim'], '2026-01-29')
        self.assertEqual(ultimo['variacao'], 20)

    def test_diario_todos_ou_mesmo_dia_semana_e_hoje_parcial(self):
        dados = linhas(('2026-11-05', '50'), ('2026-11-06', '100'), ('2026-11-13', '120'), ('2026-11-20', '130'))
        todos = analisar_historico(dados, date(2026, 11, 5), HOJE, HOJE, 'diario')
        self.assertEqual(len(todos['itens']), 16)
        self.assertEqual(todos['itens'][1]['variacao'], 100)
        sextas = analisar_historico(dados, date(2026, 11, 6), HOJE, HOJE, 'diario', 4)
        self.assertEqual(len(sextas['itens']), 3)
        self.assertEqual(sextas['itens'][1]['variacao'], 20)
        self.assertIsNone(sextas['itens'][-1]['variacao'])
        self.assertTrue(sextas['itens'][-1]['parcial'])

    def test_intervalos_invalidos_futuros_e_calendario_extremo(self):
        form = EvolucaoRelatorioForm(dict(modo='mensal', dia_semana=-1, alcance='personalizado',
            comparacao='anterior', historico_inicio='2026-12-01', historico_fim='2026-11-01'))
        self.assertFalse(form.is_valid())
        self.assertIn('historico_fim', form.errors)
        self.assertEqual(analisar_historico([], date(2027, 1, 1), date(2027, 2, 1), HOJE, 'mensal')['itens'], [])
        r = analisar_historico([], date.min, date(1, 1, 2), HOJE, 'diario')
        self.assertEqual(len(r['itens']), 2)


class HistoricoIntegradoTests(PostgreSQLIsoladoTests, TestCase):
    def test_cards_consumo_cancelamentos_nulos_e_escopo_separados_somente_select(self):
        for dia, valor, pagamento, cancelada in [
            ('2025-10-01', '100', 'pix', False), ('2026-10-01', '120', 'pix', False),
            ('2026-11-20', '50', 'pix', False), ('2026-11-20', '7', 'consumo_proprio', False),
            ('2026-11-20', '900', 'pix', True)]:
            Venda.objects.create(data_venda=dia, total=D(valor), tipo_pagamento=pagamento, cancelada=cancelada)
        completar_registros(date(2025,10,1),date(2025,10,31))
        completar_registros(date(2026,10,1),date(2026,10,31))
        with CaptureQueriesContext(connection) as queries:
            r = resumo_vendas(HOJE, HOJE, modo='mensal', historico=(date(2026, 10, 1), HOJE),
                             comparacao='ano_anterior', hoje=HOJE)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().startswith('SELECT') for q in queries))
        self.assertEqual((r['faturamento'], r['ticket_medio'], r['consumo']['valor'], r['total_registros_consulta']), (50, 50, 7, 57))
        self.assertEqual(r['grafico_evolucao']['itens'][0]['variacao'], 20)
        self.assertIsNone(r['grafico_evolucao']['itens'][1]['comparacao'])

    def test_controles_preservam_filtros_ancora_paginacao_e_datas(self):
        admin = get_user_model().objects.create_user('historico-admin', is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
            for modo in ('diario', 'semanal', 'mensal'):
                r = self.client.get('/relatorios/vendas/', dict(periodo='personalizado', data_inicial='2026-11-20',
                    data_final='2026-11-20', alcance='12', modo=modo, dia_semana=-1, comparacao='ano_anterior'))
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, 'action="/relatorios/vendas/#evolucao-vendas"')
                self.assertContains(r, 'name="alcance" value="12"')
                self.assertIn('alcance=12', r.context['filtros_query'])
                self.assertEqual(r.context['inicio'], HOJE)
                self.assertEqual(r.context['resumo']['analise_evolucao']['inicio'], date(2025, 12, 1))
            invalido = self.client.get('/relatorios/vendas/', dict(periodo='hoje', alcance='personalizado',
                historico_inicio='2026-12-01', historico_fim='2026-11-01'))
            self.assertContains(invalido, 'O gráfico não foi atualizado')
            self.assertNotContains(invalido, 'id="relatorios-barras-dados"')
            completar_registros(date(2026,10,1),HOJE)
            par = self.client.get('/relatorios/vendas/', dict(periodo='personalizado', data_inicial='2026-11-01',
                data_final='2026-11-20', alcance='principal', modo='mensal', dia_semana=-1, equivalente='on'))
            self.assertTrue(par.context['resumo']['grafico_evolucao']['par_mensal'])

    def test_desempenho_100mil_registros_queries_constantes_24_meses(self):
        inicio = date(2023, 11, 1)
        Venda.objects.bulk_create([Venda(data_venda=inicio+timedelta(days=i%1116), total=D('1.01'), tipo_pagamento='pix')
                                  for i in range(100000)], batch_size=1000)
        for modo in ('diario', 'semanal', 'mensal'):
            with CaptureQueriesContext(connection) as queries:
                antes = perf_counter()
                r = resumo_vendas(HOJE, HOJE, modo=modo, dia_semana=-1, hoje=HOJE,
                    historico=(date(2024, 12, 1), HOJE), comparacao='ano_anterior')
                ms = (perf_counter()-antes)*1000
            self.assertEqual(len(queries), 5 if modo == 'mensal' else 4)
            self.assertLessEqual(len(r['analise_evolucao']['itens']), 730)
            self.assertEqual(r['faturamento'], D('1.01')*Venda.objects.filter(data_venda=HOJE).count())
            print(f'Historical {modo}: 100000 documents; {len(queries)} SELECTs; {ms:.1f} ms')
