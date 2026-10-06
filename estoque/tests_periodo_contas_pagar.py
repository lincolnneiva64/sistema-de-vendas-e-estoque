from datetime import date
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from .models import Compra, ContaPagar, Fornecedor, PagamentoContaPagar


class PeriodoContasPagarTests(TestCase):
    def setUp(self):
        self.url = reverse('estoque:contas_pagar')
        self.fornecedor = Fornecedor.objects.create(nome='Souza Cruz')
        outro = Fornecedor.objects.create(nome='Outro fornecedor')
        self.contas = []
        for fornecedor, compra_data, vencimento in [
            (self.fornecedor, date(2026, 9, 28), date(2026, 10, 20)),
            (self.fornecedor, date(2026, 9, 1), date(2026, 10, 6)),
            (self.fornecedor, None, date(2026, 9, 30)),
            (outro, date(2026, 10, 6), date(2026, 10, 6)),
        ]:
            compra = Compra.objects.create(fornecedor=fornecedor, data_compra=compra_data) if compra_data else None
            self.contas.append(ContaPagar.objects.create(compra=compra, fornecedor=fornecedor,
                data_emissao=date(2026, 9, 1), data_vencimento=vencimento,
                valor_original=100, valor_em_aberto=100, documento_legado=f'DOC-{len(self.contas)}'))

    def test_criterios_combinacoes_e_datas(self):
        periodo = {'data_inicio': '2026-09-28', 'data_fim': '2026-10-06'}
        fornecedor = {'fornecedor': str(self.fornecedor.pk)}
        for params, indices in [
            ({}, [0, 1, 2, 3]), (periodo, [0, 3]),
            (periodo | fornecedor, [0]),
            (periodo | {'data_por': 'vencimento'}, [1, 2, 3]),
            (periodo | fornecedor | {'data_por': 'vencimento'}, [1, 2]),
            (periodo | {'compra': 'DOC-0'}, [0]),
            (periodo | {'data_por': 'invalido'}, [0, 3]),
            ({'data_inicio': '2026-10-06'}, [3]),
            ({'data_fim': '2026-09-28'}, [0, 1]),
        ]:
            with self.subTest(params=params):
                resposta = self.client.get(self.url, params, secure=True)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual({c.pk for c in resposta.context['contas']}, {self.contas[i].pk for i in indices})
                self.assertEqual(resposta.context['data_por'], params.get('data_por') if params.get('data_por') == 'vencimento' else 'compra')
                if 'data_inicio' in params:
                    self.assertContains(resposta, '<strong>Data por:</strong>')
                elif not params:
                    self.assertNotContains(resposta, '<strong>Data por:</strong>')
        resposta = self.client.get(self.url, periodo, secure=True)
        self.assertContains(resposta, '<strong>Início:</strong> 28/09/2026')
        self.assertContains(resposta, '<strong>Fim:</strong> 06/10/2026')
        self.assertContains(resposta, 'value="2026-09-28"')
        self.assertNotContains(resposta, '0002')

    def test_ano_incompleto_e_data_invalida_nao_sao_aplicados(self):
        for valor in ['0002-09-28', '0202-09-28', '2026-02-30']:
            with self.subTest(valor=valor):
                resposta = self.client.get(self.url, {'data_inicio': valor}, secure=True)
                self.assertEqual(resposta.context['data_inicio'], '')
                self.assertNotContains(resposta, valor)

    @patch('estoque.views.timezone.localdate', return_value=date(2026, 10, 6))
    def test_atalhos_preservam_vencimento_e_pagamento(self, hoje):
        for atalho, indices in [('hoje', [1, 3]), ('vencidas', [2]), ('proximos_7', []), ('proximos_30', [0])]:
            with self.subTest(atalho=atalho):
                resposta = self.client.get(self.url, {'atalho': atalho, 'data_por': 'compra'}, secure=True)
                self.assertEqual(resposta.context['periodo_tipo'], 'vencimento')
                self.assertEqual({c.pk for c in resposta.context['contas']}, {self.contas[i].pk for i in indices})
        conta = self.contas[2]
        conta.status = ContaPagar.STATUS_PAGA
        conta.valor_em_aberto = 0
        conta.save()
        PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 6), valor=100, juros_bancarios=5)
        for atalho in ['pagas', 'com_juros']:
            resposta = self.client.get(self.url, {'atalho': atalho, 'data_por': 'compra', 'data_inicio': '2026-10-06', 'data_fim': '2026-10-06'}, secure=True)
            self.assertEqual(resposta.context['periodo_tipo'], 'pagamento')
            self.assertEqual([c.pk for c in resposta.context['contas']], [conta.pk])
