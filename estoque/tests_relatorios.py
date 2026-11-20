"""Execute com o validador PostgreSQL descartável existente; nunca banco operacional.

python -m inventario.validar_confiabilidade_postgresql estoque.tests_relatorios
Fixtures de teste não substituem a homologação no Django local completo.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from statistics import median
from time import perf_counter
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils.translation import override

from offline.browser_support import Chrome

from estoque.forms_relatorios import BELEM, PERIODOS, PeriodoRelatorioForm
from estoque.models import Venda
from estoque.services.relatorios import resumo_vendas
from estoque import views_relatorios
from estoque.graficos_relatorios import evolucao_diaria
from estoque.templatetags.relatorios import moeda_br
from estoque.analise_evolucao import analisar, modo_padrao, referencia_equivalente, variacao


HOJE = date(2026, 11, 19)


class PeriodosTests(SimpleTestCase):
    def test_json_compacto_preserva_valores_e_escape_do_script(self):
        from estoque.templatetags.relatorios import json_grafico
        from django.utils.html import json_script
        dados = {'valor': Decimal('123.45'), 'data': HOJE, 'texto': '</script><script>&'}
        compacto = str(json_grafico(dados, 'dados-teste'))
        padrao = str(json_script(dados, 'dados-teste'))
        extrair = lambda html: json.loads(html.split('>', 1)[1].rsplit('</script>', 1)[0])
        self.assertEqual(extrair(compacto), extrair(padrao))
        self.assertNotIn('</script><script>', compacto)
        self.assertIn('\\u003C', compacto)
        self.assertLess(len(compacto), len(padrao))

    def periodo(self, chave, hoje=HOJE, **campos):
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime.combine(hoje, datetime.min.time(), BELEM).astimezone(datetime_timezone.utc)):
            form = PeriodoRelatorioForm({'periodo': chave, **campos})
            self.assertTrue(form.is_valid(), form.errors)
            return form.cleaned_data['inicio'], form.cleaned_data['fim']

    def test_todos_os_periodos(self):
        esperados = {
            'hoje': (HOJE, HOJE), 'dias_7': (date(2026, 11, 13), HOJE),
            'dias_30': (date(2026, 10, 21), HOJE), 'dias_60': (date(2026, 9, 21), HOJE),
            'dias_90': (date(2026, 8, 22), HOJE), 'mes_atual': (date(2026, 11, 1), HOJE),
            'mes_anterior': (date(2026, 10, 1), date(2026, 10, 31)),
            'meses_2': (date(2026, 10, 1), HOJE), 'meses_3': (date(2026, 9, 1), HOJE),
        }
        self.assertEqual(len(PERIODOS), 10)
        for chave, intervalo in esperados.items():
            with self.subTest(chave=chave):
                self.assertEqual(self.periodo(chave), intervalo)

    def test_calendario_virada_ano_e_bissexto(self):
        self.assertEqual(self.periodo('meses_3', date(2026, 1, 10)), (date(2025, 11, 1), date(2026, 1, 10)))
        self.assertEqual(self.periodo('mes_anterior', date(2024, 3, 1)), (date(2024, 2, 1), date(2024, 2, 29)))

    def test_personalizado_inclusivo_e_invalido(self):
        self.assertEqual(self.periodo('personalizado', data_inicial='2026-11-19', data_final='2026-11-19'), (HOJE, HOJE))
        for dados in (
            {'periodo': 'inexistente'}, {'periodo': 'personalizado'},
            {'periodo': 'personalizado', 'data_inicial': '2026-11-31', 'data_final': '2026-12-01'},
            {'periodo': 'personalizado', 'data_inicial': '2026-12-01', 'data_final': '2026-11-19'},
        ):
            self.assertFalse(PeriodoRelatorioForm(dados).is_valid())

    def test_hoje_usa_belem_e_nao_dia_utc(self):
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime(2026, 11, 20, 1, tzinfo=datetime_timezone.utc)):
            form = PeriodoRelatorioForm({'periodo': 'hoje'})
            self.assertTrue(form.is_valid())
            self.assertEqual(form.cleaned_data['inicio'], HOJE)

    def test_datas_explicitas_prevalecem_sobre_periodo_desatualizado(self):
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime(2026, 11, 19, 15, tzinfo=datetime_timezone.utc)):
            form = PeriodoRelatorioForm({'periodo': 'hoje', 'data_inicial': '2026-10-01', 'data_final': '2026-10-31'})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['periodo'], 'personalizado')
            self.assertEqual((form.cleaned_data['inicio'], form.cleaned_data['fim']), (date(2026, 10, 1), date(2026, 10, 31)))
            parcial = PeriodoRelatorioForm({'periodo': 'hoje', 'data_inicial': '2026-10-01'})
            self.assertFalse(parcial.is_valid())
            self.assertIn('data_final', parcial.errors)

    def test_presets_preenchem_datas_iso_que_sao_enviadas_ao_consultar(self):
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime(2026, 11, 19, 15, tzinfo=datetime_timezone.utc)):
            for chave, _ in PERIODOS:
                if chave == 'personalizado':
                    continue
                with self.subTest(periodo=chave):
                    form = PeriodoRelatorioForm({'periodo': chave})
                    self.assertTrue(form.is_valid(), form.errors)
                    self.assertEqual(form['data_inicial'].value(), form.cleaned_data['inicio'].isoformat())
                    self.assertEqual(form['data_final'].value(), form.cleaned_data['fim'].isoformat())
                    reenviado = PeriodoRelatorioForm(form.data)
                    self.assertTrue(reenviado.is_valid())
                    self.assertEqual(reenviado.cleaned_data['periodo'], chave)
                    self.assertEqual(reenviado.cleaned_data, form.cleaned_data)

    def test_moeda_pt_br_independente_do_idioma(self):
        with override('en'):
            self.assertEqual(moeda_br(Decimal('1234567.89')), 'R$ 1.234.567,89')
            self.assertEqual(moeda_br(Decimal('-1234.50')), 'R$ -1.234,50')
            self.assertEqual(moeda_br(None), 'R$ 0,00')

    def test_barras_dados_compactos_e_media_inclui_dias_sem_vendas(self):
        inicio = date(2026, 11, 1)
        dias = [{'data_venda': inicio+timedelta(days=1), 'faturamento': Decimal('100')},
                {'data_venda': inicio+timedelta(days=3), 'faturamento': Decimal('50')}]
        grafico = evolucao_diaria(dias, inicio, inicio+timedelta(days=4))
        self.assertEqual(len(grafico['serie']), 2)
        self.assertEqual(grafico['dias_periodo'], 5)
        self.assertEqual(grafico['media'], Decimal('30.00'))
        self.assertEqual(grafico['total'], Decimal('150'))
        longo = evolucao_diaria(dias, date(1900, 1, 1), date(9999, 12, 31))
        self.assertEqual(len(longo['serie']), 2)
        vazio = evolucao_diaria([], inicio, inicio)
        self.assertEqual(vazio['total'], 0)
        self.assertEqual(vazio['dias_com_vendas'], 0)
        self.assertEqual(vazio['media'], 0)

    def test_grafico_preserva_valores_negativos_e_dia_unico(self):
        dias = [{'data_venda': HOJE, 'faturamento': Decimal('-10')}]
        grafico = evolucao_diaria(dias, HOJE, HOJE)
        self.assertEqual(grafico['minimo'], Decimal('-10'))
        self.assertEqual(grafico['maximo'], 0)
        self.assertEqual(grafico['serie'], [[HOJE.isoformat(), '-10']])
        self.assertEqual(grafico['media'], Decimal('-10.00'))

    def test_hoje_parcial_usa_belem_inclusive_na_virada_do_dia_utc(self):
        with patch('estoque.graficos_relatorios.timezone.now', return_value=datetime(2026, 11, 20, 1, tzinfo=datetime_timezone.utc)):
            atual = evolucao_diaria([], HOJE, HOJE)
            passado = evolucao_diaria([], HOJE-timedelta(days=1), HOJE-timedelta(days=1))
        self.assertEqual(atual['hoje'], '2026-11-19')
        self.assertTrue(atual['inclui_hoje'])
        self.assertFalse(passado['inclui_hoje'])


class AnaliseEvolucaoTests(SimpleTestCase):
    def linhas(self, *valores):
        return [{'data_venda': date.fromisoformat(dia), 'faturamento': Decimal(valor)} for dia, valor in valores]

    def test_diario_compara_so_mesmo_dia_e_mantem_zero(self):
        dias = self.linhas(('2026-11-02', '100'), ('2026-11-03', '9000'), ('2026-11-09', '200'))
        resultado = analisar(dias, date(2026, 11, 1), HOJE, HOJE, 'diario', 0)
        self.assertEqual([i['inicio'] for i in resultado['itens']], ['2026-11-02', '2026-11-09', '2026-11-16'])
        self.assertEqual([i['valor'] for i in resultado['itens']], [100, 200, 0])
        self.assertEqual([i['variacao'] for i in resultado['itens']], [None, 100, -100])
        self.assertEqual(resultado['media'], Decimal('100.00'))

    def test_hoje_diario_parcial_nao_entra_em_media_ou_variacao(self):
        dias = self.linhas(('2026-11-12', '100'), ('2026-11-19', '200'))
        resultado = analisar(dias, date(2026, 11, 12), HOJE, HOJE, 'diario', 3)
        self.assertEqual(resultado['media'], Decimal('100'))
        self.assertIsNone(resultado['itens'][-1]['variacao'])
        self.assertEqual(resultado['itens'][-1]['status'], 'Hoje (parcial)')

    def test_semanas_segunda_domingo_e_parciais(self):
        dias = self.linhas(('2026-11-01', '999'), ('2026-11-02', '100'), ('2026-11-09', '200'))
        resultado = analisar(dias, date(2026, 11, 1), date(2026, 11, 22), date(2026, 12, 1), 'semanal', 0)
        self.assertEqual([i['parcial'] for i in resultado['itens']], [True, False, False, False])
        self.assertEqual([i['variacao'] for i in resultado['itens']], [None, None, 100, -100])
        self.assertEqual(resultado['media'], Decimal('100'))
        self.assertEqual(resultado['itens'][1]['inicio'], '2026-11-02')
        self.assertEqual(resultado['itens'][1]['fim'], '2026-11-08')

    def test_semana_que_termina_hoje_ainda_e_parcial_e_virada_de_ano(self):
        resultado = analisar([], date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 8), 'semanal', 0)
        self.assertTrue(resultado['itens'][0]['parcial'])
        virada = analisar(self.linhas(('2026-12-31', '100'), ('2027-01-04', '200')),
                          date(2026, 12, 28), date(2027, 1, 10), date(2027, 1, 11), 'semanal', 0)
        self.assertEqual([i['valor'] for i in virada['itens']], [100, 200])
        self.assertEqual(virada['itens'][1]['variacao'], 100)

    def test_meses_calendario_completos_e_parciais(self):
        dias = self.linhas(('2024-01-31', '100'), ('2024-02-29', '200'), ('2024-03-01', '400'))
        resultado = analisar(dias, date(2024, 1, 1), date(2024, 3, 31), date(2024, 4, 1), 'mensal', 0)
        self.assertEqual([i['variacao'] for i in resultado['itens']], [None, 100, 100])
        parcial = analisar(dias, date(2024, 1, 15), date(2024, 3, 20), date(2024, 4, 1), 'mensal', 0)
        self.assertEqual([i['parcial'] for i in parcial['itens']], [True, False, True])
        self.assertTrue(all(i['variacao'] is None for i in parcial['itens']))

    def test_zero_nao_inventa_crescimento_e_ocorrencias_ausentes(self):
        self.assertIsNone(variacao(Decimal('100'), Decimal('0')))
        self.assertIsNone(variacao(Decimal('0'), Decimal('0')))
        self.assertIsNone(variacao(Decimal('10'), Decimal('-5')))
        resultado = analisar([], date(2026, 11, 1), HOJE, HOJE, 'semanal', 0)
        self.assertFalse(resultado['comparavel'])
        self.assertEqual(resultado['media'], 0)
        vazio = analisar([], date(2026, 11, 17), date(2026, 11, 18), HOJE, 'diario', 0)
        self.assertEqual(vazio['itens'], [])
        self.assertIsNone(vazio['media'])

    def test_padrao_semanal_so_quando_cabe_duas_semanas_completas(self):
        self.assertEqual(modo_padrao(date(2026, 11, 1), HOJE, HOJE), 'semanal')
        self.assertEqual(modo_padrao(date(2026, 11, 9), HOJE, HOJE), 'diario')

    def test_equivalencia_nao_inventa_dias_em_mes_mais_curto(self):
        referencia, _ = referencia_equivalente(date(2026, 11, 1), HOJE, HOJE)
        self.assertEqual(referencia, (date(2026, 11, 1), HOJE, date(2026, 10, 1), date(2026, 10, 19)))
        self.assertIsNone(referencia_equivalente(date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 31))[0])
        self.assertIsNone(referencia_equivalente(date(2026, 11, 5), HOJE, HOJE)[0])


class PostgreSQLIsoladoTests:
    @classmethod
    def setUpClass(cls):
        # Recusa explícita antes de criar fixtures, inclusive se chamado pelo runner errado.
        if (settings.SETTINGS_MODULE != 'inventario.test_settings_confiabilidade_pg'
                or connection.vendor != 'postgresql'
                or connection.settings_dict['HOST'] != '127.0.0.1'
                or connection.settings_dict['NAME'] != 'inventario_confiabilidade'
                or set(settings.DATABASES) != {'default'}):
            raise RuntimeError('Use somente o validador PostgreSQL descartável existente.')
        super().setUpClass()


class ResumoVendasTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username='relatorios-admin', is_staff=True, is_superuser=True)

    def venda(self, valor='10.00', pagamento='À vista', dia=HOJE, **campos):
        return Venda.objects.create(data_venda=dia, tipo_pagamento=pagamento, total=Decimal(valor), **campos)

    def test_botao_financeiro_abre_modulo_e_preserva_acessos(self):
        from django.urls import reverse
        self.client.force_login(self.admin)
        painel = self.client.get(reverse('estoque:home'))
        self.assertEqual(painel.status_code, 200)
        html = painel.content.decode()
        financeiro = html.split('aria-labelledby="produtos-nav-financeiro"', 1)[1].split('</section>', 1)[0]
        destino = reverse('estoque:relatorios_inicio')
        self.assertIn('Relatórios Gerenciais', financeiro)
        self.assertLess(financeiro.index('Resultado Gerencial'), financeiro.index('Relatórios Gerenciais'))
        for nome in ('painel_financeiro', 'painel_resultado_gerencial', 'contas_pagar', 'caixa_banco'):
            self.assertIn(reverse('estoque:' + nome), financeiro)
        self.assertIn('href="' + destino + '"', financeiro)
        inicio = self.client.get(destino)
        self.assertContains(inicio, reverse('estoque:relatorios_vendas'))
        self.assertEqual(self.client.get(reverse('estoque:relatorios_vendas')).status_code, 200)

    def request(self, path='/relatorios/vendas/', params=None, view=views_relatorios.vendas):
        request = RequestFactory().get(path, params or {'periodo': 'personalizado', 'data_inicial': HOJE.isoformat(), 'data_final': HOJE.isoformat()})
        request.user = self.admin
        return view(request)

    def test_totais_ticket_consumo_canceladas_limites(self):
        self.venda('100.01', 'À vista')
        self.venda('200.00', ' A PRAZO ')
        self.venda('30.00', 'carteira')
        self.venda('40.00', 'Consumo próprio')
        self.venda('20.00', 'consumo_proprio')
        self.venda('900.00', cancelada=True)
        self.venda('800.00', dia=HOJE-timedelta(days=1))
        self.venda('700.00', dia=HOJE+timedelta(days=1))
        with self.assertNumQueries(3):
            resumo = resumo_vendas(HOJE, HOJE)
        self.assertEqual(resumo['faturamento'], Decimal('330.01'))
        self.assertEqual(resumo['quantidade'], 3)
        self.assertEqual(resumo['ticket_medio'], Decimal('110.00'))
        self.assertEqual(resumo['vista']['valor'], Decimal('100.01'))
        self.assertEqual(resumo['prazo']['valor'], Decimal('230'))
        self.assertEqual(resumo['consumo']['valor'], Decimal('60'))
        self.assertEqual(resumo['evolucao'][0]['faturamento'], resumo['faturamento'])
        self.assertEqual(resumo['evolucao'][0]['vista'], Decimal('100.01'))
        self.assertEqual(resumo['evolucao'][0]['prazo'], Decimal('230'))

    def test_aliases_e_formas_desconhecidas_sem_presumir_vista(self):
        for tipo in ('À vista', 'A Vista', 'avista', 'dinheiro', 'ESPÉCIE', 'pix', 'banco', 'TRANSFERÊNCIA', 'CARTÃO', 'DÉBITO', 'CRÉDITO', 'boleto', 'DEPÓSITO'):
            self.venda(pagamento=tipo)
        for tipo in ('A prazo', 'aprazo', 'carteira', 'fiado', 'a\t prazo'):
            self.venda(pagamento=tipo)
        for tipo in ('Consumo próprio', 'CONSUMO_PROPRIO', 'consumoproprio', 'consumo   próprio'):
            self.venda(pagamento=tipo)
        for tipo in ('', 'não informado'):
            self.venda(pagamento=tipo)
        resumo = resumo_vendas(HOJE, HOJE)
        self.assertEqual([resumo[n]['quantidade'] for n in ('vista', 'prazo', 'consumo', 'nao_classificado')], [13, 5, 4, 2])
        self.assertEqual(resumo['faturamento'], Decimal('200'))
        dia = resumo['evolucao'][0]
        self.assertEqual(dia['faturamento'], dia['vista'] + dia['prazo'] + dia['nao_classificado'])
        self.assertEqual(dia['nao_classificado'], Decimal('20'))

    def test_abertura_mes_atual_datas_editadas_e_intervalos_distintos(self):
        self.venda('10')
        self.venda('40', dia=HOJE-timedelta(days=10))
        self.venda('80', dia=HOJE-timedelta(days=40))
        self.client.force_login(self.admin)
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime(2026, 11, 19, 15, tzinfo=datetime_timezone.utc)):
            resposta = self.client.get('/relatorios/vendas/')
            self.assertEqual(resposta.context['form']['periodo'].value(), 'mes_atual')
            self.assertEqual(resposta.context['form']['data_inicial'].value(), '2026-11-01')
            self.assertEqual(resposta.context['form']['data_final'].value(), '2026-11-19')
            self.assertEqual(resposta.context['resumo']['faturamento'], Decimal('50'))
            for params, esperado in (
                ({'periodo': 'hoje'}, '10'),
                ({'periodo': 'hoje', 'data_inicial': '2026-10-01', 'data_final': '2026-11-19'}, '130'),
                ({'periodo': 'personalizado', 'data_inicial': '2000-01-01', 'data_final': '2000-01-02'}, '0'),
            ):
                with self.subTest(params=params):
                    resposta = self.client.get('/relatorios/vendas/', params)
                    self.assertEqual(resposta.context['resumo']['faturamento'], Decimal(esperado))
                    self.assertEqual(resposta.context['inicio'].isoformat(), resposta.context['form']['data_inicial'].value())
                    self.assertEqual(resposta.context['fim'].isoformat(), resposta.context['form']['data_final'].value())
                    if 'data_inicial' in params:
                        self.assertEqual(resposta.context['form']['periodo'].value(), 'personalizado')
                        self.assertEqual(resposta.context['inicio'].isoformat(), params['data_inicial'])
                    if esperado == '0':
                        self.assertContains(resposta, 'Nenhuma venda comercial neste período.')

    def test_paginacao_preserva_intervalo_corrigido_e_colunas_diarias(self):
        for indice in range(40):
            self.venda('10', dia=HOJE-timedelta(days=indice))
        self.client.force_login(self.admin)
        params = {'periodo': 'hoje', 'data_inicial': '2026-10-01', 'data_final': '2026-11-19'}
        resposta = self.client.get('/relatorios/vendas/', params)
        self.assertIn('periodo=personalizado', resposta.context['filtros_query'])
        self.assertIn('data_inicial=2026-10-01', resposta.context['filtros_query'])
        self.assertContains(resposta, '<th scope="col">À vista</th>', html=True)
        self.assertContains(resposta, '<th scope="col">A prazo</th>', html=True)
        self.assertEqual(resposta.context['resumo']['faturamento'], Decimal('400'))
        self.assertEqual(len(resposta.context['resumo']['evolucao']), 31)

    def test_vazio_e_somente_consumo_ou_canceladas(self):
        for povoar in (False, True):
            if povoar:
                self.venda('100', 'consumo_proprio')
                self.venda('500', cancelada=True)
            resumo = resumo_vendas(HOJE, HOJE)
            self.assertEqual(resumo['faturamento'], Decimal('0'))
            self.assertEqual(resumo['ticket_medio'], Decimal('0'))
            self.assertEqual(resumo['quantidade'], 0)
            self.assertEqual(list(resumo['evolucao']), [])

    def test_nulos_legados_sinalizados_sem_alterar_dados(self):
        # O model atual proíbe total nulo. Simula legado apenas dentro do savepoint
        # PostgreSQL descartável: DDL e fixture são revertidos pelo TestCase.
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE estoque_venda ALTER COLUMN total DROP NOT NULL')
            cursor.execute('ALTER TABLE estoque_venda ALTER COLUMN tipo_pagamento DROP NOT NULL')
        venda = self.venda('12.34', cliente=None, data_vencimento=None)
        Venda.objects.filter(pk=venda.pk).update(total=None, tipo_pagamento=None)
        resumo = resumo_vendas(HOJE, HOJE)
        self.assertEqual(resumo['quantidade'], 1)
        self.assertEqual(resumo['sem_valor'], 1)
        self.assertEqual(resumo['faturamento'], Decimal('0'))
        self.assertEqual(resumo['nao_classificado']['quantidade'], 1)
        self.assertContains(self.request(), 'podem estar incompletos')

    def test_paginacao_diaria_e_totais_globais(self):
        for indice in range(80):
            self.venda('0.10', dia=HOJE-timedelta(days=indice))
        with CaptureQueriesContext(connection) as capturadas:
            resumo = resumo_vendas(HOJE-timedelta(days=90), HOJE, 2)
        self.assertEqual(len(capturadas), 5)  # série e uma referência semanal em lote
        self.assertIn('LIMIT 31 OFFSET 31', capturadas[2]['sql'])
        self.assertEqual(resumo['evolucao'][0]['data_venda'], HOJE-timedelta(days=31))
        self.assertEqual(len(resumo['evolucao']), 31)
        self.assertEqual(resumo['faturamento'], Decimal('8.00'))
        self.assertEqual(resumo['ticket_medio'], Decimal('0.10'))
        self.assertEqual(resumo['grafico_diario']['dias_com_vendas'], 80)
        self.assertEqual(resumo['grafico_diario']['total'], Decimal('8.00'))
        self.assertEqual(resumo['grafico_diario']['serie'], resumo_vendas(HOJE-timedelta(days=90), HOJE, 1)['grafico_diario']['serie'])
        for pagina in ('inválida', '-1', '9999999999'):
            self.assertTrue(resumo_vendas(HOJE-timedelta(days=90), HOJE, pagina)['evolucao'])

    def test_menu_zero_queries_e_filtro_invalido_zero_queries(self):
        with self.assertNumQueries(0):
            resposta = self.request('/relatorios/', {}, views_relatorios.inicio)
        self.assertContains(resposta, 'Resumo de vendas')
        self.assertContains(resposta, 'Em preparação', count=5)
        self.assertNotContains(resposta, 'relatorios-graficos')
        with self.assertNumQueries(0):
            resposta = self.request(params={'periodo': 'personalizado', 'data_inicial': 'erro'})
        self.assertContains(resposta, 'Informe uma data')
        self.assertNotContains(resposta, 'Faturamento comercial diário')

    def test_resposta_somente_select_sem_cache_e_tempos(self):
        self.venda()
        with CaptureQueriesContext(connection) as capturadas:
            resposta = self.request()
        self.assertEqual(len(capturadas), 3)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in capturadas))
        self.assertIn('no-store', resposta['Cache-Control'])
        self.assertIn('private', resposta['Cache-Control'])
        self.assertIn('Cookie', resposta['Vary'])
        self.assertIn('report-db;dur=', resposta['Server-Timing'])
        self.assertIn('report-app;dur=', resposta['Server-Timing'])
        self.assertNotContains(resposta, 'onchange=')

    def test_percentuais_e_graficos_reusam_totais_e_excluem_consumo_canceladas(self):
        self.venda('100', 'À vista')
        self.venda('300', 'A prazo')
        self.venda('100', '')
        self.venda('900', 'consumo_proprio')
        self.venda('900', cancelada=True)
        with self.assertNumQueries(3):
            resumo = resumo_vendas(HOJE, HOJE)
        self.assertEqual([resumo[g]['percentual'] for g in ('vista', 'prazo', 'nao_classificado')], [20, 60, 20])
        self.assertEqual(resumo['grafico_diario']['total'], Decimal('500'))
        self.assertEqual(sum(item['valor'] for item in resumo['participacoes']), resumo['faturamento'])
        resposta = self.request()
        self.assertContains(resposta, '20,00% do faturamento')
        self.assertContains(resposta, '60,00% do faturamento')
        self.assertContains(resposta, 'R$ 500,00')
        self.assertContains(resposta, 'relatorios-barras-dados')
        self.assertNotContains(resposta, '<polyline')
        self.assertLess(resposta.content.index(b'relatorios-indicadores'), resposta.content.index(b'relatorios-graficos'))

    def test_graficos_sem_faturamento_nao_dividem_por_zero(self):
        resumo = resumo_vendas(HOJE, HOJE)
        self.assertFalse(resumo['participacao_disponivel'])
        self.assertEqual(resumo['vista']['percentual'], 0)
        self.assertEqual(resumo['prazo']['percentual'], 0)
        resposta = self.request()
        self.assertContains(resposta, 'Sem base positiva')
        self.assertNotContains(resposta, '<svg')

    def test_equivalente_somente_quando_solicitado_e_sem_alterar_cards(self):
        from .tests_relatorios_cobertura import completar_registros
        self.venda('110')
        self.venda('100', dia=date(2026, 10, 19))
        self.venda('900', dia=date(2026, 10, 25))
        self.venda('800', 'consumo_proprio', dia=date(2026, 10, 19))
        self.venda('700', dia=date(2026, 10, 19), cancelada=True)
        completar_registros(date(2026,10,1),HOJE)
        with self.assertNumQueries(5):
            sem = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', hoje=HOJE)
        self.assertNotIn('equivalente', sem['analise_evolucao'])
        with CaptureQueriesContext(connection) as queries:
            com = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().startswith('SELECT') for q in queries))
        self.assertEqual(com['faturamento'], sem['faturamento'])
        self.assertEqual(com['faturamento'], Decimal('110'))
        comparacao = com['analise_evolucao']['equivalente']
        self.assertEqual((comparacao['atual'], comparacao['anterior'], comparacao['variacao']), (110, 100, 10))
        self.assertTrue(com['analise_evolucao']['itens'][0]['parcial'])
        self.assertEqual(com['analise_evolucao']['itens'][0]['variacao'], 10)
        self.assertEqual(com['analise_evolucao']['itens'][0]['diferenca'], 10)
        with self.assertNumQueries(6):
            dentro = resumo_vendas(date(2026, 10, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertEqual(dentro['analise_evolucao']['equivalente']['variacao'], 10)

    def test_equivalente_base_zero_e_intervalo_sem_prefixo_completo(self):
        from .tests_relatorios_cobertura import completar_registros
        self.venda('100')
        completar_registros(date(2026,10,1),HOJE)
        resultado = resumo_vendas(date(2026, 11, 1), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertIsNone(resultado['analise_evolucao']['equivalente']['variacao'])
        with self.assertNumQueries(5):
            incompleto = resumo_vendas(date(2026, 11, 5), HOJE, modo='mensal', equivalente=True, hoje=HOJE)
        self.assertIn('Inclua', incompleto['analise_evolucao']['equivalente']['motivo'])

    def test_analise_preservada_em_intervalo_personalizado_e_pagina(self):
        for i in range(70):
            self.venda('10', dia=HOJE-timedelta(days=i))
        self.client.force_login(self.admin)
        params = {'periodo': 'personalizado', 'data_inicial': '2026-09-01', 'data_final': '2026-11-19',
                  'modo': 'diario', 'dia_semana': '0', 'pagina': '2'}
        resposta = self.client.get('/relatorios/vendas/', params)
        self.assertEqual(resposta.context['resumo']['evolucao'].number, 2)
        analise = resposta.context['resumo']['analise_evolucao']
        self.assertTrue(all(date.fromisoformat(i['inicio']).weekday() == 0 for i in analise['itens']))
        self.assertIn('modo=diario', resposta.context['filtros_query'])
        self.assertIn('dia_semana=0', resposta.context['filtros_query'])
        self.assertEqual(resposta.context['resumo']['faturamento'], Decimal('700'))

    def test_parcial_em_belem_e_acesso_da_nova_analise(self):
        self.venda('100')
        self.client.force_login(self.admin)
        with patch('estoque.forms_relatorios.timezone.now', return_value=datetime(2026, 11, 20, 1, tzinfo=datetime_timezone.utc)):
            resposta = self.client.get('/relatorios/vendas/', {'periodo': 'hoje', 'modo': 'diario', 'dia_semana': '3'})
        self.assertEqual(resposta.context['resumo']['analise_evolucao']['itens'][0]['status'], 'Hoje (parcial)')
        self.assertContains(resposta, 'Sem período comparável')
        self.client.logout()
        self.assertEqual(self.client.get('/relatorios/vendas/?modo=mensal&equivalente=on').status_code, 302)

    def test_permissoes_backend_inclusive_alias(self):
        self.venda()
        usuarios = [
            ('comum', False, False, True), ('staff', True, False, True),
            ('super_sem_staff', False, True, True), ('inativo', True, True, False),
        ]
        for nome, staff, superuser, ativo in usuarios:
            usuario = get_user_model().objects.create_user(username=nome, is_staff=staff, is_superuser=superuser, is_active=ativo)
            request = RequestFactory().get('/relatorios/vendas/')
            request.user = usuario
            with self.assertNumQueries(0):
                self.assertEqual(views_relatorios.vendas(request).status_code, 403)
        for url in ('/relatorios/', '/relatorios/vendas/', '/estoque/relatorios/vendas/'):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/relatorios/').status_code, 200)
        self.assertEqual(self.client.get('/estoque/relatorios/vendas/').status_code, 200)
        self.assertEqual(self.client.post('/relatorios/vendas/').status_code, 405)
        comum = get_user_model().objects.get(username='comum')
        self.client.force_login(comum)
        self.assertEqual(self.client.get('/relatorios/vendas/').status_code, 403)


class DesempenhoRelatoriosTests(PostgreSQLIsoladoTests, StaticLiveServerTestCase):
    """100 mil vendas em três anos. Meta de 2 s medida, não presumida."""
    def setUp(self):
        self.admin = get_user_model().objects.create_user(username='volume-admin', is_staff=True, is_superuser=True)
        tipos = ('À vista', 'A prazo', 'carteira', 'consumo_proprio', 'Pix', '')
        for inicio in range(0, 100_000, 2000):
            Venda.objects.bulk_create([
                Venda(data_venda=HOJE-timedelta(days=i % 1095), total=Decimal(i % 10000 + 1)/100,
                      tipo_pagamento=tipos[i % len(tipos)], cancelada=(i % 17 == 0))
                for i in range(inicio, inicio+2000)
            ], batch_size=2000)
        # Estatísticas do otimizador apenas das fixtures locais; nenhum índice criado.
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE estoque_venda')

    def test_volume_queries_tempos_e_exatidao(self):
        resultados = {}
        factory = RequestFactory()
        for dias, modo in ((1, 'diario'), (90, 'semanal'), (1095, 'diario'), (1095, 'semanal'), (1095, 'mensal')):
            inicio = HOJE-timedelta(days=dias-1)
            esperado = Decimal('0')
            quantidade = 0
            for i in range(100_000):
                if i % 1095 < dias and i % 17 != 0 and i % 6 != 3:
                    esperado += Decimal(i % 10000+1)/100
                    quantidade += 1
            resumo = resumo_vendas(inicio, HOJE)
            self.assertEqual(resumo['faturamento'], esperado)
            self.assertEqual(resumo['quantidade'], quantidade)
            tempos, sql_tempos, app_tempos = [], [], []
            for _ in range(7):
                request = factory.get('/relatorios/vendas/', {'periodo': 'personalizado', 'data_inicial': inicio.isoformat(), 'data_final': HOJE.isoformat(), 'modo': modo})
                request.user = self.admin
                with CaptureQueriesContext(connection) as queries:
                    antes = perf_counter()
                    resposta = views_relatorios.vendas(request)
                    tempos.append((perf_counter()-antes)*1000)
                self.assertEqual(len(queries), (3 if dias == 1 else 4) + int(modo == 'semanal') + 2 * int(modo == 'mensal'))
                partes = dict(parte.strip().split(';dur=') for parte in resposta['Server-Timing'].split(','))
                sql_tempos.append(float(partes['report-db']))
                app_tempos.append(float(partes['report-app']))
            resultados[f'{dias}_{modo}'] = {'vendas_base': 100000, 'consultas': (3 if dias == 1 else 4) + int(modo == 'semanal') + 2 * int(modo == 'mensal'), 'mediana_sql_ms': round(median(sql_tempos), 2),
                                      'mediana_app_ms': round(median(app_tempos), 2), 'mediana_html_ms': round(median(tempos), 2),
                                      'max_html_ms': round(max(tempos), 2), 'html_bytes': len(resposta.content),
                                      'meta_2s_observada': max(tempos) < 2000}
            self.assertLess(len(resposta.content), 100_000, f'{dias} dias, {modo}')
        request = factory.get('/relatorios/')
        request.user = self.admin
        with self.assertNumQueries(0):
            antes = perf_counter()
            views_relatorios.inicio(request)
            resultados['menu_ms'] = round((perf_counter()-antes)*1000, 2)
        resultados['navegador'] = self.medir_navegador()
        print('RELATORIOS_BENCHMARK '+json.dumps(resultados, ensure_ascii=True), flush=True)

    def medir_navegador(self):
        executavel = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        if not Path(executavel).is_file():
            return {'limite': 'Chrome/Edge não disponível; carregamento no navegador não medido.'}
        self.client.force_login(self.admin)
        medicoes = {}
        with TemporaryDirectory(prefix='relatorios-browser-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(executavel, perfil).start()
            try:
                tab = browser.tab()
                tab.call('Network.enable')
                tab.call('Network.setCacheDisabled', {'cacheDisabled': True})
                # Apenas Django/arquivos locais: não depende de CDN externa para o benchmark.
                tab.call('Network.setBlockedURLs', {'urls': ['https://*']})
                tab.call('Network.setCookie', {'name': settings.SESSION_COOKIE_NAME,
                         'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                         'url': self.live_server_url})
                for largura in (400, 1366):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 850,
                             'deviceScaleFactor': 1, 'mobile': largura < 980})
                    for nome, caminho in (
                        ('menu', '/relatorios/'),
                        ('90_dias', '/relatorios/vendas/?'+urlencode({'periodo': 'personalizado', 'data_inicial': '2026-08-22', 'data_final': HOJE.isoformat()})),
                    ):
                        tab.call('Page.navigate', {'url': self.live_server_url+caminho})
                        tab.wait("document.readyState==='complete' && !!document.querySelector('.relatorios h1') && performance.getEntriesByType('navigation')[0]?.loadEventEnd>0", timeout=20)
                        medidas = tab.evaluate('''(()=>{
                            const n=performance.getEntriesByType('navigation')[0];
                            return {http_ms:n.responseEnd-n.requestStart,dom_pronto_ms:n.domContentLoadedEventEnd,
                                pagina_carregada_ms:n.loadEventEnd,server_timing:Object.fromEntries(n.serverTiming.map(s=>[s.name,s.duration])),
                                sem_overflow:document.documentElement.scrollWidth<=innerWidth,
                                linhas:document.querySelectorAll('.relatorios tbody tr').length};
                        })()''')
                        self.assertTrue(medidas['sem_overflow'])
                        if nome != 'menu':
                            self.assertEqual(medidas['linhas'], 31)
                            tab.wait("document.querySelectorAll('.grafico-barra-dia').length>0")
                            dados_analise = tab.evaluate("JSON.parse(document.getElementById('relatorios-barras-dados').textContent)")
                            self.assertEqual(dados_analise['modo'], 'semanal')
                            self.assertEqual(tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"), min(31, len(dados_analise['itens'])))
                            self.assertTrue(tab.evaluate("[...document.querySelectorAll('.grafico-barra-dia')].every(c=>{const v=c.querySelector('.grafico-barra-valor').getBoundingClientRect(),d=c.querySelector('.grafico-barra-data').getBoundingClientRect(),p=c.querySelector('.grafico-barra-variacao').getBoundingClientRect();return v.bottom<=d.top && d.bottom<=p.top && p.bottom<=c.getBoundingClientRect().bottom;})"))
                            primeira = tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data")
                            dia_tabela = tab.evaluate("document.querySelector('.relatorios tbody th').textContent")
                            if len(dados_analise['itens']) > 31:
                                tab.evaluate("document.querySelector('[data-barras-proximo]').click()")
                                self.assertNotEqual(tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data"), primeira)
                            self.assertEqual(tab.evaluate("document.querySelector('.relatorios tbody th').textContent"), dia_tabela)
                            tab.evaluate("document.querySelector('[data-barras-anterior]').click()")
                            self.assertEqual(tab.evaluate("document.querySelector('.grafico-barra-dia').dataset.data"), primeira)
                            self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('.relatorios')).maxWidth"), '1240px')
                            # Mudar filtro não consulta nem altera URL; requer Consultar.
                            url = tab.evaluate('location.href')
                            tab.evaluate("document.querySelector('[name=periodo]').value='dias_7';document.querySelector('[name=periodo]').dispatchEvent(new Event('change',{bubbles:true}));")
                            self.assertEqual(tab.evaluate('location.href'), url)
                        medicoes[f'{nome}_{largura}'] = medidas
                self.conferir_filtros_navegador(tab)
                tab.call('Page.close')
                tab.socket.close()
            finally:
                browser.stop()
        return medicoes

    def conferir_filtros_navegador(self, tab):
        destino = self.live_server_url+'/relatorios/vendas/'
        tab.call('Page.navigate', {'url': destino})
        tab.wait("location.href==="+json.dumps(destino)+" && document.readyState==='complete' && document.querySelector('[name=periodo]')?.value==='mes_atual'")
        dados = tab.evaluate("JSON.parse(document.getElementById('relatorios-periodos-datas').textContent)")
        tab.wait("document.querySelectorAll('.grafico-barra-dia').length>0")
        barras = tab.evaluate("JSON.parse(document.getElementById('relatorios-barras-dados').textContent)")
        self.assertEqual(tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"), min(31, len(barras['itens'])))
        primeiro = barras['itens'][0]
        self.assertEqual(tab.evaluate("document.querySelector('.grafico-barra-data').textContent"), primeiro['rotulo'] + (' · '+primeiro['status'] if primeiro['parcial'] else ''))
        self.assertTrue(tab.evaluate("!!document.querySelector('.grafico-media-linha').style.top"))
        cards = tab.evaluate("document.querySelector('.relatorios-indicadores').textContent")
        for modo in ('diario', 'mensal', 'semanal'):
            tab.evaluate("document.getElementById('evolucao-controles').elements.modo.value="+json.dumps(modo)+";document.getElementById('evolucao-controles').elements.modo.dispatchEvent(new Event('change'));document.getElementById('evolucao-controles').elements.dia_semana.value='0';")
            self.assertEqual(tab.evaluate("document.querySelector('[data-evolucao-dia]').hidden"), modo != 'diario')
            self.assertEqual(tab.evaluate("document.querySelector('[data-evolucao-equivalente]').hidden"), modo != 'mensal')
            tab.evaluate("document.querySelector('#evolucao-controles button').click()")
            tab.wait("document.readyState==='complete' && location.search.includes('modo="+modo+"') && JSON.parse(document.getElementById('relatorios-barras-dados')?.textContent||'{}').modo==="+json.dumps(modo))
            analise = tab.evaluate("JSON.parse(document.getElementById('relatorios-barras-dados').textContent)")
            if modo == 'diario':
                self.assertTrue(all(date.fromisoformat(i['inicio']).weekday() == 0 for i in analise['itens']))
            self.assertEqual(tab.evaluate("document.querySelector('.relatorios-indicadores').textContent"), cards)
            if modo == 'mensal':
                tab.evaluate("document.getElementById('evolucao-controles').elements.equivalente.checked=true;document.querySelector('#evolucao-controles button').click()")
                tab.wait("document.readyState==='complete' && location.search.includes('equivalente=on') && !!document.querySelector('.evolucao-equivalente')")
                self.assertIn('Comparação parcial equivalente', tab.evaluate("document.querySelector('.evolucao-equivalente').textContent"))
                self.assertEqual(tab.evaluate("document.querySelector('.relatorios-indicadores').textContent"), cards)
        self.assertEqual(tab.evaluate("[document.querySelector('[name=data_inicial]').value,document.querySelector('[name=data_final]').value]"), [dados['mes_atual']['inicio'], dados['mes_atual']['fim']])
        for chave, intervalo in dados.items():
            tab.evaluate("document.querySelector('[name=periodo]').value="+json.dumps(chave)+";document.querySelector('[name=periodo]').dispatchEvent(new Event('change',{bubbles:true}));")
            self.assertEqual(tab.evaluate("[document.querySelector('[name=data_inicial]').value,document.querySelector('[name=data_final]').value]"), [intervalo['inicio'], intervalo['fim']])
            self.assertEqual(tab.evaluate('location.href').split('?')[0], destino)
        # Simula edição de cada data, incluindo teclado (input) e calendário (change).
        tab.evaluate("document.querySelector('[name=data_inicial]').value='2000-01-01';document.querySelector('[name=data_inicial]').dispatchEvent(new Event('input',{bubbles:true}));")
        self.assertEqual(tab.evaluate("document.querySelector('[name=periodo]').value"), 'personalizado')
        tab.evaluate("document.querySelector('[name=data_final]').value='2000-01-02';document.querySelector('[name=data_final]').dispatchEvent(new Event('change',{bubbles:true}));")
        self.assertEqual(tab.evaluate('location.href').split('?')[0], destino)
        tab.evaluate("document.querySelector('.relatorios-filtros button').click();")
        tab.wait("location.search.includes('periodo=personalizado') && document.readyState==='complete' && document.querySelector('.relatorios-tabela')?.textContent.includes('Nenhuma venda comercial')")
        self.assertIn('data_inicial=2000-01-01', tab.evaluate('location.search'))
        self.assertIn('data_final=2000-01-02', tab.evaluate('location.search'))
        self.assertEqual(tab.evaluate("[document.querySelector('[name=data_inicial]').value,document.querySelector('[name=data_final]').value]"), ['2000-01-01', '2000-01-02'])
        tab.wait("document.querySelectorAll('.grafico-barra-dia').length>0")
        self.assertTrue(all(v == 'R$ 0,00' for v in tab.evaluate("Array.from(document.querySelectorAll('.grafico-barra-valor'),e=>e.textContent)")))
        self.assertTrue(tab.evaluate("document.querySelector('[data-barras-proximo]').disabled"))
