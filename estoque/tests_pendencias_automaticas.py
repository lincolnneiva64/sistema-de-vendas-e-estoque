from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from estoque.models import Cliente, Produto, Venda, ItemVenda, SeparacaoVenda, SeparacaoVendaItem, EventoVenda
from estoque.views import _recalcular_status_separacao_da_venda


class PendenciasAutomaticasTests(TestCase):
    def setUp(self):
        cliente = Cliente.objects.create(nome="Cliente teste")
        produto = Produto.objects.create(nome="Produto teste", quantidade=30, preco_compra=5, preco_vista=10, preco_prazo=10)
        self.venda = Venda.objects.create(cliente=cliente, data_venda=date.today(), tipo_pagamento="A prazo", total=20)
        self.item = ItemVenda.objects.create(venda=self.venda, produto=produto, quantidade=2, unidade="KG", preco_unitario=10, valor_total=20)
        self.sep = SeparacaoVenda.objects.create(venda=self.venda, finalizado_em=timezone.now(), revisao_pendente=True)
        self.fisico = SeparacaoVendaItem.objects.create(separacao=self.sep, item_venda=self.item, produto_nome_snapshot=produto.nome, unidade_snapshot="KG", quantidade_solicitada=3, quantidade_separada=2, status=SeparacaoVendaItem.STATUS_CONFERIDO)

    def eventos(self):
        return EventoVenda.objects.filter(venda=self.venda, tipo_evento="alteracao_separacao_atendida")

    def test_peso_corrigido_atende_com_auditoria_sem_duplicar(self):
        _recalcular_status_separacao_da_venda(self.venda, "operador teste")
        _recalcular_status_separacao_da_venda(self.venda, "operador teste")
        self.assertEqual(self.eventos().count(), 1)
        self.assertIn("Atendida automaticamente", self.eventos().get().descricao)
        self.assertEqual(self.eventos().get().usuario, "operador teste")
        self.sep.refresh_from_db()
        self.assertFalse(self.sep.revisao_pendente)
        self.fisico.refresh_from_db()
        self.assertEqual(self.fisico.quantidade_solicitada, Decimal("3"))

    def test_peso_incorreto_nao_atende(self):
        self.item.quantidade = Decimal("2.5")
        self.item.save()
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)

    def test_correcao_sem_entrada_especifica_de_ajuste(self):
        self.sep.revisao_pendente = False
        self.sep.save()
        EventoVenda.objects.create(venda=self.venda, tipo_evento="quantidade_item_alterada", descricao="Correcao do peso")
        _recalcular_status_separacao_da_venda(self.venda)
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertEqual(self.eventos().count(), 1)

    def test_quantidade_insuficiente_exige_confirmacao_fisica(self):
        self.fisico.status = SeparacaoVendaItem.STATUS_QUANTIDADE_INSUFICIENTE
        self.fisico.save()
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)

    def test_edicao_nao_atende_falta_fisica(self):
        self.fisico.status = SeparacaoVendaItem.STATUS_NAO_ENCONTRADO
        self.fisico.save()
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)

    def test_item_adicionado_sem_conferencia_nao_atende(self):
        ItemVenda.objects.create(venda=self.venda, produto=self.item.produto, quantidade=1, unidade="KG", preco_unitario=10, valor_total=10)
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)

    def test_observacao_livre_nao_atende(self):
        self.fisico.observacao = "Verificar embalagem"
        self.fisico.save()
        _recalcular_status_separacao_da_venda(self.venda)
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)

    def test_rollback_remove_resolucao_e_evento(self):
        from django.db import transaction
        with self.assertRaises(ValueError):
            with transaction.atomic():
                _recalcular_status_separacao_da_venda(self.venda)
                raise ValueError("Falha ao salvar")
        self.assertFalse(self.eventos().exists())
        self.sep.refresh_from_db()
        self.assertTrue(self.sep.revisao_pendente)
