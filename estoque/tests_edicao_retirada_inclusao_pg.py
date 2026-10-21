"""Fictitious sale: removing an egg carton and adding 3 physical oil units."""
import copy
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from inventario.models import MovimentoObservado, SequenciaEstoque
from .models import (Cliente, Produto, Venda, ItemVenda, ItemVendaRemovido,
    ContaReceber, MovimentoFinanceiro, EventoVenda, SeparacaoVenda, SeparacaoVendaItem)
from .services.vendas import criar_ou_atualizar_venda


class EdicaoRetiradaInclusaoPostgreSQLTests(TestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Esta regressão exige PostgreSQL descartável.')
        self.usuario = get_user_model().objects.create_superuser('edicao-ficticia', '', 'ficticio')
        self.client.force_login(self.usuario)
        cliente = Cliente.objects.create(nome='Cliente fictício')
        self.ovo = Produto.objects.create(nome='Ovo Especial 12Cb fictício', quantidade=10,
            unidade_compra='CB', unidade_venda_1='CB', unidade_venda_2='UN',
            vende_fracionado=True, fator_conversao=12, preco_compra=5, preco_vista=10, preco_prazo=10)
        self.oleo = Produto.objects.create(nome='Óleo Soya 20/900Ml fictício', quantidade=10,
            unidade_compra='PCT', unidade_venda_1='PCT', unidade_venda_2='UN',
            vende_fracionado=True, fator_conversao=20, preco_compra=100, preco_vista=120, preco_prazo=120)
        self.dados = dict(cliente_id=cliente.pk, data_venda='2026-10-21',
            data_vencimento='2026-11-21', tipo_pagamento='A prazo', operador='Teste',
            itens=[dict(produto_id=self.ovo.pk, produto_nome=self.ovo.nome, quantidade='1',
                unidade='CB', preco_unitario='10')])
        self.venda = criar_ou_atualizar_venda(usuario=self.usuario, dados=self.dados).venda
        self.item = self.venda.itens.get()
        self.separacao = SeparacaoVenda.objects.create(venda=self.venda,
            finalizado_em=timezone.now(), status=SeparacaoVenda.STATUS_SEPARADA)
        self.item_fisico = SeparacaoVendaItem.objects.create(separacao=self.separacao,
            item_venda=self.item, produto_nome_snapshot=self.ovo.nome, unidade_snapshot='CB',
            quantidade_solicitada=1, quantidade_separada=1, status=SeparacaoVendaItem.STATUS_CONFERIDO)
        self.edicao = copy.deepcopy(self.dados)
        self.edicao.update(venda_id=self.venda.pk, itens=[dict(produto_id=self.oleo.pk,
            produto_nome=self.oleo.nome, quantidade='3', unidade='UN', preco_unitario='6')])

    def postar(self):
        return self.client.post('/vendas/gravar/', json.dumps(self.edicao), content_type='application/json')

    def test_retirar_ovo_incluir_tres_un_oleo_com_separacao_finalizada(self):
        resposta = self.postar()
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()['sucesso'])
        self.assertEqual(self.venda.itens.count(), 1)
        novo = self.venda.itens.get()
        self.assertEqual((novo.produto_id, novo.quantidade, novo.unidade), (self.oleo.pk, 3, 'UN'))
        self.assertEqual(novo.estoque_movimentado, Decimal('.150'))
        self.ovo.refresh_from_db(); self.oleo.refresh_from_db(); self.venda.refresh_from_db()
        self.assertEqual(self.ovo.quantidade, Decimal('10'))
        self.assertEqual(self.oleo.quantidade, Decimal('9.850'))
        self.assertEqual(self.venda.total, Decimal('18'))
        conta = ContaReceber.objects.get(venda=self.venda)
        self.assertEqual((conta.valor_original, conta.valor_em_aberto), (18, 18))
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertTrue(ItemVendaRemovido.objects.get(venda=self.venda).estoque_devolvido)
        # The physical evidence is retained; editing is not a new physical OK.
        self.item_fisico.refresh_from_db(); self.separacao.refresh_from_db()
        self.assertIsNone(self.item_fisico.item_venda_id)
        self.assertEqual((self.item_fisico.status, self.item_fisico.quantidade_separada), ('conferido', 1))
        self.assertTrue(self.separacao.revisao_pendente)
        self.assertFalse(EventoVenda.objects.filter(venda=self.venda,
            tipo_evento='alteracao_separacao_atendida').exists())

    def test_falha_apos_financeiro_reverte_venda_estoque_auditoria_e_conta(self):
        modelos = (Venda, ItemVenda, ItemVendaRemovido, ContaReceber, MovimentoFinanceiro,
            EventoVenda, SeparacaoVenda, SeparacaoVendaItem, MovimentoObservado, SequenciaEstoque)
        antes = {m: list(m.objects.order_by('pk').values()) for m in modelos}
        saldos = list(Produto.objects.order_by('pk').values())
        with patch('estoque.views._recalcular_status_separacao_da_venda',
                   side_effect=RuntimeError('Falha fictícia após conta a receber')):
            resposta = self.postar()
        self.assertEqual(resposta.status_code, 500)
        self.assertFalse(resposta.json()['sucesso'])
        for modelo in modelos:
            self.assertEqual(list(modelo.objects.order_by('pk').values()), antes[modelo], modelo.__name__)
        self.assertEqual(list(Produto.objects.order_by('pk').values()), saldos)

    def test_separacao_ainda_aberta_mantem_fluxo_existente(self):
        self.separacao.finalizado_em = None
        self.separacao.status = SeparacaoVenda.STATUS_EM_SEPARACAO
        self.separacao.save()
        self.assertEqual(self.postar().status_code, 200)
        self.separacao.refresh_from_db()
        self.assertFalse(self.separacao.revisao_pendente)
        self.assertEqual(list(self.separacao.itens.values_list('item_venda__produto_id', flat=True)), [self.oleo.pk])
