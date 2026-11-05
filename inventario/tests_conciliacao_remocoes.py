"""Synthetic sale documents in disposable PostgreSQL only."""
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from estoque.models import EventoVenda, ItemVenda, ItemVendaRemovido, Venda
from . import ajustes
from .contexto_ajustes import carregar
from .models import RodadaContagem
from .tests import produto
from .tests_ajustes import AjustesFixture
from .tests_conciliacao_documental import ConciliacaoDocumentalTests


class ConciliacaoRemocoesTests(AjustesFixture, TestCase):
    vender = ConciliacaoDocumentalTests.vender

    def preparar(self):
        venda, _, _ = self.vender(1)
        outro = produto(nome='Óleo removido fictício')
        registro = ItemVendaRemovido.objects.create(venda=venda, produto=outro,
            produto_nome_snapshot=outro.nome, item_venda_original_id=999001,
            quantidade_snapshot=2, unidade_snapshot='UN', preco_unitario_snapshot=5,
            valor_total_snapshot=10)
        evento = EventoVenda.objects.create(venda=venda, tipo_evento='item_removido_da_nota',
            descricao=f'Item removido da nota: {outro.nome}, quantidade 2.000 UN, valor abatido R$ 10.00. '
                f'Registro de remocao #{registro.pk}. Total da venda: R$ 30.00 -> R$ 20.00. '
                'Sem devolucao de estoque para este item.')
        EventoVenda.objects.filter(pk=evento.pk).update(criado_em=registro.criado_em+timedelta(seconds=1))
        return venda, registro, evento

    def test_valida_com_mesma_evidencia_em_lote_e_individual_sem_consultas_extras(self):
        self.preparar()
        r = self.fresca()
        individual = ajustes.avaliar(r)
        self.assertFalse(individual['bloqueio'], individual['bloqueio'])
        self.assertEqual(len(individual['documentos'][0]['remocoes_outros_produtos']), 1)
        with carregar([r]) as lote, CaptureQueriesContext(connection) as queries:
            batched = ajustes.avaliar(r, _lote=lote)
        self.assertEqual(len(queries), 0)
        self.assertEqual(individual['evidencia'], batched['evidencia'])
        self.assertEqual(individual['assinatura'], batched['assinatura'])

    def test_remocao_alvo_ausente_evento_repetido_venda_invalida_bloqueiam(self):
        venda, registro, evento = self.preparar()
        for modelo, pk, campos in (
                (ItemVendaRemovido, registro.pk, dict(produto_id=self.produto.pk)),
                (ItemVendaRemovido, registro.pk, dict(produto_nome_snapshot='Outro nome')),
                (ItemVendaRemovido, registro.pk, dict(status='revertido')),
                (Venda, venda.pk, dict(cancelada=True))):
            with self.subTest(campos=campos), transaction.atomic():
                modelo.objects.filter(pk=pk).update(**campos)
                self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])
                transaction.set_rollback(True)
        with transaction.atomic():
            EventoVenda.objects.create(venda=venda, tipo_evento=evento.tipo_evento, descricao=evento.descricao)
            self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])
            transaction.set_rollback(True)
        registro.delete()
        self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])

    def test_mais_produtos_nao_acrescentam_consultas_de_remocoes(self):
        self.preparar()
        def medir():
            rodadas = [self.fresca(r.pk) for r in RodadaContagem.objects.filter(item__inventario=self.inv)]
            with CaptureQueriesContext(connection) as queries:
                with carregar(rodadas) as lote:
                    for r in rodadas:
                        self.assertFalse(ajustes.avaliar(r, _lote=lote)['bloqueio'])
            return len(queries)
        poucas = medir()
        self.segunda()
        self.assertEqual(medir(), poucas)

    def test_mesma_venda_com_varios_produtos_valida_so_remocao_comprovada(self):
        from estoque.views import _baixar_estoque_produto
        outra = self.segunda()
        venda, _, _ = self.preparar()
        with transaction.atomic():
            snap = _baixar_estoque_produto(outra.item.produto_id, 1, unidade='CX')
            ItemVenda.objects.create(venda=venda, produto_id=outra.item.produto_id,
                quantidade=1, unidade='CX', preco_unitario=0, valor_total=0, **snap)
            from .models import MovimentoObservado
            m = MovimentoObservado.objects.filter(produto_id=outra.item.produto_id).latest('sequencia')
            MovimentoObservado.objects.filter(pk=m.pk).update(referencia=f'Venda #{venda.pk}')
        rodadas = [self.fresca(), self.fresca(outra.pk)]
        with carregar(rodadas) as lote, CaptureQueriesContext(connection) as queries:
            resultados = [ajustes.avaliar(r, _lote=lote) for r in rodadas]
        self.assertEqual(len(queries), 0)
        for r in resultados:
            self.assertFalse(r['bloqueio'], r['bloqueio'])
            self.assertEqual(len(r['documentos'][0]['remocoes_outros_produtos']), 1)

    def test_mudanca_na_remocao_invalida_previa_sem_alterar_saldos(self):
        _, registro, _ = self.preparar()
        r = self.fresca()
        previa = ajustes.avaliar(r)
        ItemVendaRemovido.objects.filter(pk=registro.pk).update(item_venda_original_id=999002)
        atual = ajustes.avaliar(self.fresca())
        self.assertFalse(atual['bloqueio'], atual['bloqueio'])
        self.assertNotEqual(previa['assinatura'], atual['assinatura'])
        with self.assertRaisesMessage(ValueError, 'Dados alterados'):
            ajustes.decidir(self.usuario, self.inv.pk, r.pk, previa['token'],
                'Revisão sintética', confirmacao=True)
        ItemVendaRemovido.objects.filter(pk=registro.pk).update(valor_total_snapshot=12)
        atual = ajustes.avaliar(self.fresca())
        self.assertTrue(atual['bloqueio'])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal(9))
