"""Synthetic evidence, unrelated to production product IDs or balances."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from estoque.models import (EdicaoCompra, EventoVenda, ItemCompra, ItemVenda,
                            ItemVendaRemovido, MovimentacaoEstoqueManual, Venda)
from .models import MovimentoObservado
from .tests_historico import HistoricoFixture


class SaldosHistoricosTests(HistoricoFixture, TestCase):
    def setUp(self):
        super().setUp()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=41)
        self.anterior = self.venda_snapshot(-1, 23, 6, 17)
        self.posterior = self.venda_snapshot(1, 58, 7, 51)
        self.ultima = self.venda_snapshot(2, 51, 3, 48)

    def venda_snapshot(self, dia, antes, movimento, depois, unidade='CX'):
        instante = self.inicio + timedelta(days=dia)
        venda = Venda.objects.create(data_venda=instante.date())
        Venda.objects.filter(pk=venda.pk).update(criado_em=instante, atualizado_em=instante)
        return ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=movimento,
            unidade=unidade, estoque_antes=antes, estoque_movimentado=movimento,
            estoque_depois=depois, estoque_unidade_snapshot=unidade,
            preco_unitario=0, valor_total=0)

    def linhas(self, **params):
        resposta = self.client.get(self.url, params)
        self.assertEqual(resposta.status_code, 200)
        return {r['pk']: r for r in resposta.context['extrato'] if r['fonte'] == 'venda'}, [
            r for r in resposta.context['extrato'] if r['fonte'] == 'compra'], resposta

    def test_snapshots_e_ponte_corroborada_sem_estoque_inicial_zero(self):
        vendas, compras, resposta = self.linhas()
        self.assertEqual(vendas[self.posterior.pk]['saldo_antes'], 58)
        self.assertEqual(vendas[self.posterior.pk]['saldo'], 51)
        self.assertEqual(vendas[self.ultima.pk]['saldo'], 48)
        self.assertEqual(compras[0]['saldo'], 58)
        self.assertEqual(compras[0]['saldo_antes'], 17)
        self.assertEqual(compras[0]['saldo_tipo'], 'Reconstruído e corroborado')
        self.assertContains(resposta, 'Snapshot registrado')
        self.assertContains(resposta, 'Antes: 58 CX')
        self.assertIn('Compatível', vendas[self.ultima.pk]['saldo_aviso'])

    def test_snapshot_incompleto_interrompe_ponte_mas_permite_referencia_posterior(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_antes=None)
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])
        self.assertEqual(vendas[self.ultima.pk]['saldo'], 48)
        self.assertIn('Referência pontual', vendas[self.ultima.pk]['saldo_aviso'])

    def test_inconsistencia_aritmetica_ou_quantidade_editada_nao_certifica(self):
        for campo, valor in (('estoque_depois', 49), ('quantidade', 8)):
            with self.subTest(campo=campo):
                ItemVenda.objects.filter(pk=self.posterior.pk).update(**{campo: valor})
                vendas, compras, _ = self.linhas()
                self.assertIsNone(vendas[self.posterior.pk]['saldo'])
                self.assertIsNone(compras[0]['saldo'])
                ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_depois=51, quantidade=7)

    def test_edicao_do_produto_ou_evento_ambiguo_bloqueia_snapshot(self):
        for tipo, texto in (('quantidade_item_alterada', 'Quantidade alterada na nota: Produto Inventário. De 6 CX para 7 CX.'),
                            ('evento_desconhecido', 'Operação sem atribuição segura')):
            with self.subTest(tipo=tipo):
                evento = EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento=tipo, descricao=texto)
                vendas, compras, _ = self.linhas()
                self.assertIsNone(vendas[self.posterior.pk]['saldo'])
                self.assertIsNone(compras[0]['saldo'])
                evento.delete()

    def test_edicao_comprovada_de_outro_item_nao_invalida_snapshot(self):
        from .tests import produto
        outro = produto(nome='Outro produto fictício')
        ItemVenda.objects.create(venda=self.posterior.venda, produto=outro, quantidade=2,
                                 unidade='CX', preco_unitario=0, valor_total=0)
        EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {outro.nome}. De 1 CX para 2 CX.')
        vendas, compras, _ = self.linhas()
        self.assertEqual(vendas[self.posterior.pk]['saldo'], 51)
        self.assertEqual(compras[0]['saldo'], 58)

    def test_nome_diferente_sem_documento_independente_nao_basta(self):
        EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento='quantidade_item_alterada',
            descricao='Quantidade alterada na nota: Nome antigo desconhecido. De 1 CX para 2 CX.')
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])

    def test_conversao_atual_nao_completa_conversao_historica(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(unidade='UN', quantidade=84)
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])
        self.assertIn('conversão histórica', vendas[self.posterior.pk]['saldo_aviso'])

    def test_mudanca_de_unidade_quebra_continuidade_sem_converter_saldo(self):
        ItemVenda.objects.filter(pk=self.ultima.pk).update(unidade='UN', estoque_unidade_snapshot='UN')
        vendas, _, _ = self.linhas()
        self.assertEqual(vendas[self.ultima.pk]['saldo'], 48)
        self.assertEqual(vendas[self.ultima.pk]['saldo_unidade'], 'UN')
        self.assertIn('Referência pontual', vendas[self.ultima.pk]['saldo_aviso'])

    def test_compra_sem_ancora_anterior_ou_sem_correspondencia_nao_reconstroi(self):
        self.anterior.delete()
        _, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])

    def test_compra_com_saldo_nao_correspondente_nao_reconstroi(self):
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=40)
        _, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])

    def test_edicao_de_compra_bloqueia_reconstrucao(self):
        from uuid import uuid4
        EdicaoCompra.objects.create(compra=self.compra, operacao=uuid4(), motivo='Correção fictícia',
            payload_hash='ficticio', antes={}, depois={})
        _, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])

    def test_movimento_desconhecido_ou_ajuste_intermediario_interrompe_ponte(self):
        m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1, antes=17, depois=17,
            registrado_em=self.inicio + timedelta(hours=1), origem='nao_identificada')
        _, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        m.delete()
        MovimentacaoEstoqueManual.objects.create(produto=self.produto, tipo='conferencia',
            estoque_antes=17, estoque_depois=17, diferenca=0)
        MovimentacaoEstoqueManual.objects.update(criado_em=self.inicio + timedelta(hours=1))
        _, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])

    def test_cancelamento_e_remocao_impedem_certificacao(self):
        Venda.objects.filter(pk=self.posterior.venda_id).update(cancelada=True)
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])
        Venda.objects.filter(pk=self.posterior.venda_id).update(cancelada=False)
        ItemVendaRemovido.objects.create(venda=self.posterior.venda, produto=self.produto,
            produto_nome_snapshot=self.produto.nome, quantidade_snapshot=1, unidade_snapshot='CX',
            preco_unitario_snapshot=0, valor_total_snapshot=0, estoque_devolvido=True)
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])

    def test_paginacao_nao_propagada_em_memoria(self):
        for i in range(60):
            self.venda_snapshot(2 + i / 100, 48 - Decimal(i) / 10, Decimal('0.1'),
                                Decimal('47.9') - Decimal(i) / 10)
        vendas, _, _ = self.linhas(extrato_pagina=2)
        self.assertTrue(vendas)
        self.assertTrue(all(r['saldo'] is not None for r in vendas.values()))

    def test_teto_de_evidencias_falha_fechado(self):
        from unittest.mock import patch
        with patch('inventario.saldos_historicos.LIMITE_EVIDENCIAS', 1):
            vendas, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])

    def test_instantes_ambiguos_nao_inventam_ordem(self):
        outra = self.venda_snapshot(1, 51, 1, 50)
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(vendas[outra.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])

    def test_snapshot_posterior_ao_ok_nao_e_retroativo(self):
        Venda.objects.filter(pk=self.posterior.venda_id).update(criado_em=self.agora+timedelta(hours=1))
        vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])

    def test_duas_compras_entre_ancoras_nao_reconstroem_por_rateio(self):
        from estoque.models import Compra
        outra = Compra.objects.create(data_compra=self.inicio.date(), estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=self.inicio+timedelta(hours=1))
        ItemCompra.objects.create(compra=outra, produto=self.produto, quantidade=1, unidade='CX', preco_unitario=0, valor_total=0)
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=40)
        _, compras, _ = self.linhas(compra=self.compra.pk)
        self.assertTrue(all(r['saldo'] is None for r in compras))

    def test_consulta_do_extrato_so_executa_select(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as consultas:
            self.linhas()
        self.assertTrue(consultas)
        # PostgreSQL renders UNION arms as parenthesized SELECTs.
        outras = [q['sql'] for q in consultas if not q['sql'].lstrip(' (\r\n\t').upper().startswith('SELECT')]
        self.assertEqual(outras, [])

    def devolucao_antiga(self, instante, confirmada=True):
        item = self.venda_snapshot(-5, 30, 2, 28)
        Venda.objects.filter(pk=item.venda_id).update(cancelada=True,
            estoque_devolvido_cancelamento=confirmada,
            estoque_devolvido_cancelamento_em=instante)
        return item

    def test_cancelamento_antigo_dentro_do_intervalo_impede_ponte_mesmo_com_extremos_iguais(self):
        instante = self.inicio + timedelta(hours=1)
        antiga = self.devolucao_antiga(instante)
        vendas, compras, resposta = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        self.assertEqual(vendas[self.posterior.pk]['saldo'], 51)
        self.assertTrue(vendas[self.posterior.pk]['saldo_pontual'])
        retornos = [r for r in resposta.context['extrato'] if r['fonte'] == 'cancelamento']
        self.assertEqual(len(retornos), 1)
        self.assertEqual(retornos[0]['documento'], antiga.venda_id)
        self.assertEqual(retornos[0]['instante'], instante)
        self.assertIsNone(retornos[0]['entrada'])
        self.assertIsNone(retornos[0]['saldo'])
        self.assertIn(str(antiga.venda_id), retornos[0]['url'])
        self.assertNotIn(antiga.pk, vendas)

    def test_devolucao_antiga_entre_snapshots_interrompe_continuidade(self):
        self.devolucao_antiga(self.inicio + timedelta(days=1, hours=1))
        vendas, compras, _ = self.linhas()
        self.assertEqual(compras[0]['saldo'], 58)
        self.assertTrue(vendas[self.ultima.pk]['saldo_pontual'])

    def test_devolucao_sem_data_nao_usa_data_original_ou_cancelamento(self):
        antiga = self.devolucao_antiga(None)
        Venda.objects.filter(pk=antiga.venda_id).update(cancelada_em=self.inicio+timedelta(hours=1))
        vendas, compras, resposta = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        self.assertTrue(vendas[self.ultima.pk]['saldo_pontual'])
        self.assertTrue(resposta.context['extrato'].devolucao_sem_data)
        self.assertFalse(any(r['fonte'] == 'cancelamento' for r in resposta.context['extrato']))
        self.assertContains(resposta, 'sem data efetiva comprovada')

    def test_devolucao_fora_das_ancoras_nao_interrompe_trecho_corroborado(self):
        for instante in (self.inicio-timedelta(days=2), self.agora+timedelta(days=1)):
            with self.subTest(instante=instante):
                antiga = self.devolucao_antiga(instante)
                vendas, compras, resposta = self.linhas()
                self.assertEqual(compras[0]['saldo'], 58)
                self.assertFalse(vendas[self.ultima.pk]['saldo_pontual'])
                self.assertFalse(any(r['fonte'] == 'cancelamento' for r in resposta.context['extrato']))
                antiga.delete()

    def test_devolucao_com_dados_incompletos_ou_conflitantes_nao_infere_quantidade(self):
        antiga = self.devolucao_antiga(self.inicio+timedelta(hours=1), confirmada=False)
        ItemVenda.objects.filter(pk=antiga.pk).update(estoque_antes=None,
            estoque_movimentado=None, estoque_unidade_snapshot='', unidade='UN')
        _, compras, resposta = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        retorno = next(r for r in resposta.context['extrato'] if r['fonte'] == 'cancelamento')
        self.assertIsNone(retorno['entrada'])
        self.assertIsNone(retorno['quantidade'])
        self.assertIsNone(retorno['saldo'])

    def test_devolucao_unica_por_venda_produto_sem_somar_observador(self):
        instante = self.inicio+timedelta(hours=1)
        antiga = self.devolucao_antiga(instante)
        ItemVenda.objects.create(venda_id=antiga.venda_id, produto=self.produto,
            quantidade=1, unidade='CX', preco_unitario=0, valor_total=0)
        MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=17, depois=19, registrado_em=instante, origem='nao_identificada',
            referencia=f'Venda #{antiga.venda_id}')
        _, compras, resposta = self.linhas()
        retornos = [r for r in resposta.context['extrato'] if r['fonte'] == 'cancelamento']
        self.assertEqual(len(retornos), 1)
        self.assertIsNone(retornos[0]['entrada'])
        self.assertIsNone(compras[0]['saldo'])

    def test_devolucao_no_instante_da_ancora_nao_presume_ordem(self):
        self.devolucao_antiga(self.inicio+timedelta(days=1))
        vendas, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        self.assertTrue(vendas[self.posterior.pk]['saldo_pontual'])
