"""Batch writes retain row triggers, individual audit and transaction boundaries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.db import connection, transaction, close_old_connections, DatabaseError
from django.test import TransactionTestCase, override_settings

from estoque.services import estoque_manual_lote
from estoque.views import _baixar_estoque_produto
from . import ajustes
from .models import (AplicacaoAjuste, EventoAjuste, EventoInventario,
                     MovimentoObservado, SequenciaEstoque, PropostaAjuste)
from .tests_desempenho_post import DadosPost
from .tests_confirmacao_final import HEADERS


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class EscritasLoteTests(DadosPost, TransactionTestCase):
    def aplicar(self):
        return ajustes.atualizar(self.usuario, self.inv.pk, self.dados['previa'], True)

    def test_vinculos_individuais_sequencias_e_triggers_imutaveis(self):
        registros = self.aplicar()
        self.assertEqual(len(registros), 7)
        self.assertEqual(len({r.decisao_id for r in registros}), 7)
        for r in registros:
            r.refresh_from_db()
            d = r.decisao
            m = d.movimentacao
            seq = SequenciaEstoque.objects.get(produto_id=m.produto_id).sequencia
            observado = MovimentoObservado.objects.get(produto_id=m.produto_id, sequencia=seq)
            self.assertEqual(observado.origem, 'ajuste_manual')
            self.assertEqual(observado.referencia, f'Movimentação manual #{m.pk}')
            self.assertEqual((observado.antes, observado.depois), (r.antes, r.depois))
            self.assertEqual((m.estoque_antes, m.estoque_depois, m.diferenca), (r.antes, r.depois, r.diferenca))
            self.assertEqual(r.proposta.estado, 'aplicado')
            self.assertEqual(d.rodada_id, r.proposta.rodada_id)
            self.assertEqual(d.aprovado_por_id, self.usuario.pk)
            self.assertEqual(m.usuario_id, self.usuario.pk)
            self.assertIsNotNone(m.criado_em)
            self.assertEqual(EventoAjuste.objects.filter(proposta=r.proposta).count(), 3)
            self.assertTrue(EventoInventario.objects.filter(acao='ajuste_aplicado',
                dados__operacao=str(r.pk), dados__decisao=d.pk).exists())
        # QuerySet updates bypass Python save(); database triggers must still reject them.
        for model, updates in [(AplicacaoAjuste, dict(unidade='X')),
                               (EventoAjuste, dict(acao='X')),
                               (PropostaAjuste, dict(justificativa='X'))]:
            with self.assertRaises(DatabaseError), transaction.atomic():
                model.objects.all().update(**updates)

    def test_falha_na_ultima_auditoria_reverte_tambem_os_triggers(self):
        antes = list(MovimentoObservado.objects.order_by('pk').values())
        seqs = list(SequenciaEstoque.objects.order_by('pk').values())
        original = EventoInventario.objects.bulk_create
        def falhar(registros, *args, **kwargs):
            original(registros, *args, **kwargs)
            raise ValueError('Falha após a última auditoria')
        with patch.object(EventoInventario.objects, 'bulk_create', side_effect=falhar):
            resposta = self.client.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertEqual(antes, list(MovimentoObservado.objects.order_by('pk').values()))
        self.assertEqual(seqs, list(SequenciaEstoque.objects.order_by('pk').values()))

    def test_movimento_incompativel_reverte_todo_lote(self):
        original = estoque_manual_lote.conferir_movimento
        def adulterar(entrada, resultado, movimento):
            movimento.antes += 1
            return original(entrada, resultado, movimento)
        with patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=adulterar):
            with self.assertRaisesMessage(ValueError, 'observador não confirmado'):
                self.aplicar()
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_venda_concorrente_aguarda_lote_e_preserva_saida(self):
        travou, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        original = estoque_manual_lote.conferir_movimento
        def pausar(*args):
            travou.set()
            if not liberar.wait(10):
                raise AssertionError('Timeout da fixture')
            return original(*args)
        def thread(func):
            close_old_connections()
            try:
                return func()
            finally:
                close_old_connections()
        def venda():
            iniciou.set()
            with transaction.atomic():
                _baixar_estoque_produto(self.ids[0], 1, unidade='CX')
            terminou.set()
        with patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=pausar), \
                ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(thread, self.aplicar)
            try:
                self.assertTrue(travou.wait(10))
                b = pool.submit(thread, venda)
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.2))
            finally:
                liberar.set()
            a.result(15)
            b.result(15)
        from estoque.models import Produto
        self.assertEqual(Produto.objects.get(pk=self.ids[0]).quantidade, 6)
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)
