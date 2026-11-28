"""Situação financeira atual, preservando os critérios comerciais históricos."""
from copy import deepcopy
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from statistics import median
from time import perf_counter
from unittest.mock import patch

from django.db import connection
from django.template.loader import get_template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from estoque.models import Cliente, ContaReceber
from estoque.services.inteligencia_clientes import participacao_clientes, situacao_financeira_clientes
from estoque.services.resumo_dividas_cliente import resumir_dividas_cliente, resumir_dividas_clientes
from estoque.tests_inteligencia_clientes import FixturesClientes

AGORA = datetime(2026, 11, 28, 15, tzinfo=dt_timezone.utc)


@patch('django.utils.timezone.now', return_value=AGORA)
class InadimplenciaClientesTests(TestCase):
    def cliente(self, nome='Cliente de teste'):
        return Cliente.objects.create(nome=nome)

    def conta(self, cliente, original='100', saldo='100', vencimento=date(2026, 10, 30), status='aberta'):
        return ContaReceber.objects.create(cliente=cliente, data_emissao=date(2026, 9, 1),
            data_vencimento=vencimento, valor_original=Decimal(original), valor_em_aberto=Decimal(saldo), status=status)

    def test_lote_coincide_com_resumo_individual_em_todos_os_cenarios(self, _):
        varios, futuro, pago, nenhum = [self.cliente() for _ in range(4)]
        self.conta(varios, '420.50', '400.90', status='parcial')
        self.conta(varios, '100', '50', date(2026, 10, 1), 'parcial')
        self.conta(varios, '70', '70', date(2026, 12, 1))
        self.conta(varios, '20', '20', None)
        self.conta(varios, '900', '0', status='paga')
        self.conta(varios, '30', '30', status='cancelada')
        self.conta(futuro, '80', '60', date(2026, 12, 1), 'parcial')
        self.conta(futuro, '10', '10', date(2026, 11, 28))
        self.conta(pago, '500', '0', status='paga')
        clientes = [varios, futuro, pago, nenhum]
        with self.assertNumQueries(1):
            lote = resumir_dividas_clientes(c.pk for c in clientes)
        for cliente in clientes:
            individual = resumir_dividas_cliente(list(ContaReceber.objects.filter(cliente=cliente)))
            for chave, valor in lote[cliente.pk].items():
                self.assertEqual(valor, individual[chave], (cliente.pk, chave))
        r = lote[varios.pk]
        self.assertEqual((r['saldo'], r['vencido'], r['vencidas'], r['maior_atraso'], r['parciais']),
                         (Decimal('540.90'), Decimal('450.90'), 2, 58, 2))
        self.assertEqual(lote[futuro.pk]['saldo'], 70)
        self.assertEqual(lote[futuro.pk]['vencido'], 0)
        self.assertEqual(lote[futuro.pk]['vencidas'], 0)
        self.assertEqual(lote[futuro.pk]['parciais'], 1)
        self.assertEqual(lote[pago.pk]['saldo'], 0)
        self.assertEqual(lote[nenhum.pk]['saldo'], 0)

    def test_referencia_raimundo_parcial_e_vencido_liquido(self, _):
        cliente = self.cliente('Referência fictícia Raimundo')
        self.conta(cliente, '420.50', '400.90', status='parcial')
        r = resumir_dividas_clientes([cliente.pk])[cliente.pk]
        self.assertEqual((r['original'], r['pago'], r['saldo'], r['vencido']),
                         (Decimal('420.50'), Decimal('19.60'), Decimal('400.90'), Decimal('400.90')))
        self.assertEqual((r['vencidas'], r['maior_atraso']), (1, 29))

    def test_balcao_e_coletivos_excluidos_sem_consulta_inutil(self, _):
        with self.assertNumQueries(0):
            self.assertEqual(resumir_dividas_clientes([None, 1126, 1126]), {})
        coletivo = self.cliente()
        self.conta(coletivo)
        resultado = dict(impactos=[dict(id=1126), dict(id=coletivo.pk)], ausentes=[])
        with self.assertNumQueries(0):
            situacao_financeira_clientes(resultado, [coletivo.pk])
        self.assertNotIn('financeiro', resultado['impactos'][0])
        self.assertNotIn('financeiro', resultado['impactos'][1])

    def test_quatro_situacoes_e_historico_insuficiente_independente_da_divida(self, _):
        cadastros=[]
        for index, (notas, vencida, insuficiente) in enumerate(((0,True,True),(2,True,False),(0,False,False),(0,False,True),(2,False,False))):
            cliente = self.cliente()
            if vencida:
                self.conta(cliente)
            elif index == 2:
                self.conta(cliente, vencimento=date(2026, 12, 1))
            cadastros.append(dict(id=cliente.pk, nome=cliente.nome, notas_atual=notas,
                historico=dict(chave='insuficientes' if insuficiente else 'recorrentes')))
        resultado=dict(impactos=cadastros[:2], ausentes=cadastros[2:])
        situacao_financeira_clientes(resultado)
        self.assertEqual(cadastros[0]['situacao_financeira'], 'Sem compras no período atual · dívida vencida.')
        self.assertEqual(cadastros[1]['situacao_financeira'], 'Redução de compras · dívida vencida.')
        self.assertEqual(cadastros[2]['situacao_financeira'], 'Sem compras no período atual · sem dívida vencida.')
        self.assertEqual(cadastros[4]['situacao_financeira'], 'Redução de compras · sem dívida vencida.')
        self.assertTrue(cadastros[0]['historico_insuficiente_ausencia'])
        self.assertTrue(cadastros[3]['historico_insuficiente_ausencia'])
        self.assertIn('Priorizar cobrança',cadastros[0]['prioridade_acompanhamento'])
        html=get_template('estoque/relatorios/cliente_situacao_financeira.html').render(dict(cadastro=cadastros[0]))
        self.assertIn('Histórico insuficiente para avaliar ausência habitual.',html)
        self.assertIn('Em aberto',html)
        self.assertNotIn('<details class="cliente-financeiro" open',html)

    def test_hoje_em_belem_mesmo_com_fuso_ativo_diferente(self, mocked):
        mocked.return_value = datetime(2026, 11, 29, 1, tzinfo=dt_timezone.utc)
        cliente = self.cliente()
        self.conta(cliente, vencimento=date(2026, 11, 28))
        with timezone.override('Asia/Tokyo'):
            r=resumir_dividas_clientes([cliente.pk])[cliente.pk]
        self.assertEqual(r['data_referencia'], date(2026,11,28))
        self.assertEqual((r['vencidas'], r['vencido'], r['maior_atraso']), (0,0,0))

    def test_centenas_de_clientes_uma_select_sem_historico_de_pagamentos(self, _):
        clientes=Cliente.objects.bulk_create([Cliente(nome=f'Volume {n}') for n in range(300)])
        ContaReceber.objects.bulk_create([ContaReceber(cliente=c, data_emissao=date(2026,9,1),
            data_vencimento=date(2026,10,30),valor_original=100,valor_em_aberto=80,status='parcial')
            for c in clientes for _ in range(4)])
        timings={}
        for tamanho in (10,300):
            tempos=[]
            for _ in range(3):
                inicio=perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    r=resumir_dividas_clientes(c.pk for c in clientes[:tamanho])
                tempos.append((perf_counter()-inicio)*1000)
                self.assertEqual(len(queries),1)
                self.assertTrue(queries[0]['sql'].lstrip().upper().startswith('SELECT'))
                self.assertNotIn('estoque_recebimentocontareceber',queries[0]['sql'])
                self.assertEqual(len(r),tamanho)
                self.assertEqual(r[clientes[0].pk]['vencido'],320)
            timings[tamanho]=round(median(tempos),2)
        print('DIVIDAS_LOTE_MEDIANA_MS',timings,flush=True)


class InadimplenciaIntegracaoTests(FixturesClientes,TestCase):
    def setUp(self):
        self.criar_cenario()

    @patch('django.utils.timezone.now', return_value=AGORA)
    def test_preserva_dados_rankings_historico_e_consulta_so_situacao_atual(self, _):
        ContaReceber.objects.create(cliente=self.c2,data_emissao=date(2026,11,26),
            data_vencimento=date(2026,11,27),valor_original=420.50,valor_em_aberto=400.90,status='parcial')
        analise=self.analisar()
        with patch('estoque.services.inteligencia_clientes.situacao_financeira_clientes'):
            before=participacao_clientes(analise)
        after=participacao_clientes(analise)
        financeiro=next(c for c in after['ausentes'] if c['id']==self.c2.pk)['financeiro']
        self.assertEqual(financeiro['saldo'],Decimal('400.90'))
        self.assertEqual(financeiro['data_referencia'],date(2026,11,28))
        self.assertGreater(financeiro['data_referencia'],analise['fim'])
        clean=deepcopy(after)
        clean.pop('financeiro_data_referencia')
        for lista in ('impactos','ausentes','aumentos'):
            for cadastro in clean[lista]:
                for chave in ('financeiro','situacao_financeira','historico_insuficiente_ausencia','prioridade_acompanhamento'):
                    cadastro.pop(chave,None)
        self.assertEqual(before,clean)
        self.client.force_login(self.admin)
        response=self.client.get('/relatorios/vendas/',dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-24',modo='mensal'))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Contas de vendas: situação atual em 28/11/2026')
        self.assertContains(response,'cliente-financeiro')
