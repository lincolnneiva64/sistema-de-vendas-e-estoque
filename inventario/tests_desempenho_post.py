from estoque.services import estoque_manual_lote
"""Synthetic POST benchmark. Real transactions on the guarded disposable PG only."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
from threading import Barrier, Event
import time
from unittest.mock import patch

from django.db import connection, transaction, close_old_connections
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from estoque.models import Venda, ItemVenda, EventoVenda, MovimentacaoEstoqueManual
from estoque.views import _baixar_estoque_produto
from . import ajustes, conciliacao_documental
from .models import (AplicacaoAjuste, MovimentoObservado, ConfirmacaoContagem,
                     PropostaAjuste, EventoAjuste, EventoInventario, RodadaContagem)
from .tests_ajustes import AjustesFixture
from .tests_confirmacao_final import Fluxo, HEADERS


class DadosPost(Fluxo, AjustesFixture):
    def setUp(self):
        super().setUp()
        for _ in range(6):
            self.segunda()
        self.ids = list(self.inv.itens.order_by('pk').values_list('produto_id', flat=True))
        venda = Venda.objects.create(data_venda=timezone.localdate())
        with transaction.atomic():
            for produto_id in self.ids[:2]:
                snap = _baixar_estoque_produto(produto_id, Decimal(1), unidade='CX')
                ItemVenda.objects.create(venda=venda, produto_id=produto_id, quantidade=1,
                    unidade='CX', preco_unitario=0, valor_total=0, **snap)
                movimento = MovimentoObservado.objects.filter(produto_id=produto_id).latest('sequencia')
                MovimentoObservado.objects.filter(pk=movimento.pk).update(referencia=f'Venda #{venda.pk}')
        EventoVenda.objects.create(venda=venda, tipo_evento='venda_gravada')
        self.venda = venda
        self.preparar(finalizada=True)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class DesempenhoPostTests(DadosPost, TransactionTestCase):
    def medir(self, atraso):
        confirmar = list(ConfirmacaoContagem.objects.order_by('pk').values())
        duracoes = []
        def instrumentar(execute, sql, params, many, context):
            if atraso:
                time.sleep(atraso)
            inicio = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                duracoes.append(time.perf_counter() - inicio)
        with patch.object(ajustes, 'avaliar', wraps=ajustes.avaliar) as avaliar, \
                patch.object(ajustes, 'bloquear', wraps=ajustes.bloquear) as bloquear, \
                patch.object(conciliacao_documental, 'conciliar', wraps=conciliacao_documental.conciliar) as conciliar, \
                CaptureQueriesContext(connection) as queries, connection.execute_wrapper(instrumentar):
            inicio = time.perf_counter()
            response = self.client.post(self.painel, self.dados, **HEADERS)
            decorrido = time.perf_counter() - inicio
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 7)
        self.assertEqual(confirmar, list(ConfirmacaoContagem.objects.order_by('pk').values()))
        medidas = dict(etapa=os.environ.get('INVENTARIO_POST_MEDIDA', 'depois'),
            produtos=7, movimentos_posteriores=2, atraso_artificial_por_sql_ms=atraso*1000,
            segundos=round(decorrido, 4), consultas=len(queries),
            segundos_sql_local=round(sum(duracoes), 4), maior_sql_local_ms=round(max(duracoes)*1000, 3),
            avaliacoes=avaliar.call_count, conciliacoes=conciliar.call_count, bloqueios=bloquear.call_count,
            comandos=dict(Counter(q['sql'].split()[0] for q in queries)))
        print('MEDIDA_POST=' + json.dumps(medidas), flush=True)
        destino = os.environ.get('INVENTARIO_POST_MEDIDAS')
        if destino:
            with Path(destino).open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(medidas) + '\n')
        if medidas['etapa'] == 'depois':
            self.assertEqual(avaliar.call_count, 7, 'A validação integral deve ocorrer uma vez por produto sob locks.')
            self.assertEqual(bloquear.call_count, 1, 'O lote já travado não deve ser bloqueado por produto novamente.')

    def test_medida_local(self):
        self.medir(0)

    def test_medida_com_latencia_controlada(self):
        self.medir(.05)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class SegurancaPostTests(DadosPost, TransactionTestCase):
    def enviar(self):
        return self.client.post(self.painel, self.dados, **HEADERS)

    def sem_aplicacao(self):
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(EventoAjuste.objects.exists())
        self.assertFalse(EventoInventario.objects.filter(acao='ajuste_aplicado').exists())
        self.assertEqual(MovimentoObservado.objects.count(), 2)
        self.assertEqual(list(self.inv.itens.order_by('pk').values_list('produto__quantidade', flat=True)),
                         [Decimal(9), Decimal(19)] + [Decimal(20)] * 5)

    def test_valida_todos_antes_de_gravar_e_preserva_auditoria(self):
        oks = list(ConfirmacaoContagem.objects.order_by('pk').values())
        original_avaliar, original_gravar = ajustes.avaliar, estoque_manual_lote.conferir_movimento
        avaliadas = []
        def avaliar(*args, **kwargs):
            self.assertFalse(PropostaAjuste.objects.exists())
            self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
            avaliadas.append(args[0].pk)
            return original_avaliar(*args, **kwargs)
        def gravar(*args, **kwargs):
            self.assertEqual(len(avaliadas), 7)
            return original_gravar(*args, **kwargs)
        with patch.object(ajustes, 'avaliar', side_effect=avaliar), \
                patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=gravar):
            resposta = self.enviar()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(oks, list(ConfirmacaoContagem.objects.order_by('pk').values()))
        self.assertEqual(EventoAjuste.objects.filter(acao='aprovado').count(), 7)
        self.assertEqual(EventoAjuste.objects.filter(acao='confirmado_gestor').count(), 7)
        self.assertEqual(EventoAjuste.objects.filter(acao='aplicado').count(), 7)
        self.assertEqual(EventoInventario.objects.filter(acao='ajuste_aplicado').count(), 7)
        self.assertEqual(AplicacaoAjuste.objects.values('lote').distinct().count(), 1)
        self.assertFalse(AplicacaoAjuste.objects.exclude(usuario=self.usuario).exists())
        self.assertEqual(list(self.inv.itens.order_by('pk').values_list('produto__quantidade', flat=True)),
                         [Decimal(7), Decimal(17)] + [Decimal(18)] * 5)

    def test_ultimo_produto_alterado_nao_registra_primeiros(self):
        rodada = self.inv.itens.order_by('pk').last().rodadas.latest('numero')
        RodadaContagem.objects.filter(pk=rodada.pk).update(versao=rodada.versao+1)
        resposta = self.enviar()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn('Dados alterados', resposta.json()['erro'])
        self.sem_aplicacao()

    def test_falha_no_setimo_produto_reverte_lote_inteiro(self):
        original = estoque_manual_lote.conferir_movimento
        chamadas = []
        def gravar(*args, **kwargs):
            chamadas.append(args[0])
            if len(chamadas) == 7:
                raise ValueError('Falha fictícia na sétima gravação')
            return original(*args, **kwargs)
        with patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=gravar):
            resposta = self.enviar()
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(len(chamadas), 7)
        self.sem_aplicacao()

    def test_falha_auditoria_reverte_movimento(self):
        with patch.object(AplicacaoAjuste.objects, 'bulk_create', side_effect=RuntimeError('Auditoria fictícia indisponível')):
            with self.assertRaises(RuntimeError):
                self.enviar()
        self.sem_aplicacao()

    def test_reutiliza_aprovacao_existente_sem_duplicar(self):
        propostas = [self.aprovar(r) for r in RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk')]
        self.assertEqual(self.enviar().status_code, 200)
        self.assertEqual(set(PropostaAjuste.objects.values_list('pk', flat=True)), {p.pk for p in propostas})
        self.assertEqual(EventoAjuste.objects.filter(acao='aprovado').count(), 7)
        eventos = EventoAjuste.objects.count()
        self.assertEqual(self.enviar().status_code, 200)
        self.assertEqual(EventoAjuste.objects.count(), eventos)
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)

    def test_protecao_auditoria_revalidada_antes_de_mover_estoque(self):
        original = ajustes._registrar_decisao
        chamadas = []
        def registrar(*args, **kwargs):
            proposta = original(*args, **kwargs)
            chamadas.append(proposta.pk)
            if len(chamadas) == 7:
                # Disposable PG only: DDL is inside the request transaction and
                # must roll back along with every approval when execution aborts.
                with connection.cursor() as cursor:
                    cursor.execute('ALTER TABLE inventario_aplicacaoajuste DISABLE TRIGGER inventario_aplicacao_imutavel')
            return proposta
        with patch.object(ajustes, '_registrar_decisao', side_effect=registrar):
            resposta = self.enviar()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn('Proteções da auditoria', resposta.json()['erro'])
        self.sem_aplicacao()
        ajustes.verificar_execucao()  # The test's ALTER was rolled back as well.

    def thread(self, func):
        close_old_connections()
        try:
            return func()
        finally:
            close_old_connections()

    def test_duas_confirmacoes_simultaneas_retornam_mesmo_lote(self):
        barreira = Barrier(2)
        def enviar():
            barreira.wait(10)
            return sorted(str(a.pk) for a in ajustes.atualizar(self.usuario, self.inv.pk,
                self.dados['previa'], confirmacao=True))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.thread, enviar)
            b = pool.submit(self.thread, enviar)
            self.assertEqual(a.result(20), b.result(20))
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 7)
        self.assertEqual(EventoAjuste.objects.count(), 21)

    def test_documento_travado_por_outro_usuario_bloqueia_sem_escritas(self):
        travou, liberar = Event(), Event()
        def manter_documento():
            with transaction.atomic():
                Venda.objects.select_for_update().get(pk=self.venda.pk)
                travou.set()
                if not liberar.wait(10):
                    raise AssertionError('Timeout da fixture de documento')
        with ThreadPoolExecutor(max_workers=1) as pool:
            futuro = pool.submit(self.thread, manter_documento)
            try:
                self.assertTrue(travou.wait(10))
                resposta = self.enviar()
                self.assertEqual(resposta.status_code, 400)
                self.assertIn('Documento em alteração concorrente', resposta.json()['erro'])
            finally:
                liberar.set()
            futuro.result(10)
        self.sem_aplicacao()
