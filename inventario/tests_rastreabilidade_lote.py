"""Batch attribution only, on identity-guarded disposable PostgreSQL."""
import inspect
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from threading import Barrier, Event
from unittest.mock import patch

from django.db import connection, close_old_connections, transaction, DatabaseError
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from estoque.models import Produto, MovimentacaoEstoqueManual
from estoque.services import estoque_manual_lote
from . import ajustes, services, rastreabilidade_regularizacao as rastreio
from .conversao import snapshot
from .models import (DetalheMovimentoEstoque as Detalhe, ItemInventario, RodadaContagem,
    AplicacaoAjuste, DecisaoAjuste, PropostaAjuste, EventoAjuste, MovimentoObservado)
from .tests import produto
from .tests_rastreabilidade_individual import PilotoFixture


class LoteFixture(PilotoFixture):
    def adicionar(self, base='UN', saldo='5', principal='4', avulsos='', fator='1', secundaria=''):
        p = produto(nome='Produto fictício lote', unidade_compra=base, quantidade=saldo,
            vende_fracionado=bool(secundaria), unidade_venda_2=secundaria, fator_conversao=fator)
        p.refresh_from_db()
        item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p), custo_base=p.preco_compra)
        r = RodadaContagem.objects.create(item=item, numero=1)
        acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [r.pk])
        services.salvar_contagem(acesso.pk, r.pk, 0, principal, avulsos, True)
        return p, self.fresca(r.pk)

    def preparar_lote(self):
        resultado = ajustes.preparar_resultado(self.inv)
        return resultado, ajustes.previa_atualizacao(self.inv.pk, resultado['aptas'])

    def aplicar_lote(self, previa):
        return ajustes.atualizar(self.usuario, self.inv.pk, previa, True)

    def assert_lote_revertido(self, saldos):
        for pk, saldo in saldos.items():
            self.assertEqual(Produto.objects.get(pk=pk).quantidade, saldo)
        for model in (Detalhe, AplicacaoAjuste, DecisaoAjuste, PropostaAjuste,
                      EventoAjuste, MovimentacaoEstoqueManual, MovimentoObservado):
            self.assertFalse(model.objects.exists(), model.__name__)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class RastreabilidadeLoteTests(LoteFixture, TestCase):
    def test_lote_misto_identidade_conversao_lote_e_limites(self):
        self.adicionar(base='KG', saldo='4.001', principal='4.000')
        self.adicionar(base='M³', saldo='4', principal='3')
        self.adicionar(base='UN/FD', saldo='4', principal='3')
        self.adicionar(base='DZ', saldo='100000001', principal='100000000', avulsos='0', fator='12', secundaria='UN')
        self.adicionar(base='FD', saldo='3.501', principal='3', avulsos='6', fator='12', secundaria='UN')
        resultado, previa = self.preparar_lote()
        self.assertEqual(len(resultado['aptas']), 6)
        apps = self.aplicar_lote(previa)
        detalhes = list(Detalhe.objects.select_related('movimento').order_by('pk'))
        por_id = {str(a.pk): a for a in apps}
        self.assertEqual(len(detalhes), 6)
        self.assertEqual(len({a.lote for a in apps}), 1)
        self.assertEqual(len({d.operacao_id for d in detalhes}), 6)
        for d in detalhes:
            a = por_id[d.documento_id]
            self.assertEqual((d.operacao_id, d.ordem), (a.pk, 1))
            self.assertEqual(d.movimento.produto_id, a.decisao.item.produto_id)
            self.assertEqual(d.movimento.sequencia, a.evidencia['preparacao']['sequencia'] + 1)
            self.assertEqual((d.movimento.antes, d.movimento.depois), (a.antes, a.depois))
            self.assertEqual(d.movimento.origem, 'ajuste_manual')
            self.assertEqual(d.movimento.referencia, f'Movimentação manual #{a.decisao.movimentacao_id}')
            self.assertEqual(d.acao, 'regularizacao_conferencia')
            self.assertEqual(d.executor_id, self.usuario.pk)
            self.assertEqual(d.operador_nome, self.usuario.get_username())
            self.assertIsNone(d.operador_id)
            self.assertEqual(d.unidade_base, a.unidade)
            self.assertIn(f'conferencia:{self.inv.pk}', d.item_referencia)
            self.assertIn(f'proposta:{a.proposta_id}', d.item_referencia)
        dz = next(d for d in detalhes if d.unidade_base == 'DZ')
        self.assertEqual((dz.quantidade_entrada, dz.fator, dz.unidade_entrada), (D('1200000000'), D('12'), 'UN'))
        fd = next(d for d in detalhes if d.unidade_base == 'FD')
        self.assertEqual((fd.quantidade_movimentada, fd.quantidade_entrada), (D('-.001'), D('42')))

    def test_sem_diferenca_e_bloqueados_ficam_fora(self):
        self.segunda()
        igual, _ = self.adicionar(saldo='4', principal='4')
        bloqueado, _ = self.adicionar(saldo='6', principal='5')
        Produto.objects.filter(pk=bloqueado.pk).update(quantidade=None)
        resultado, previa = self.preparar_lote()
        self.assertEqual(len(resultado['aptas']), 2)
        self.assertEqual(resultado['resumo']['bloqueados'], 1)
        self.aplicar_lote(previa)
        self.assertEqual(Detalhe.objects.count(), 2)
        self.assertFalse(Detalhe.objects.filter(movimento__produto__in=[igual, bloqueado]).exists())
        self.assertFalse(MovimentoObservado.objects.filter(produto=igual).exists())
        self.assertIsNone(Produto.objects.get(pk=bloqueado.pk).quantidade)

    def test_produto_bloqueado_apos_previa_impede_lote_inteiro(self):
        segunda = self.segunda()
        _, previa = self.preparar_lote()
        Produto.objects.filter(pk=segunda.item.produto_id).update(quantidade=None)
        with self.assertRaises(ValueError):
            self.aplicar_lote(previa)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, 10)
        self.assertFalse(Detalhe.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_falha_real_no_segundo_insert_reverte_lote_inteiro(self):
        segunda = self.segunda()
        _, previa = self.preparar_lote()
        criar = Detalhe.objects.bulk_create
        def falhar(detalhes):
            detalhes = list(detalhes)
            detalhes[1].unidade_base = ''
            return criar(detalhes, batch_size=1)
        with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.bulk_create', side_effect=falhar):
            with self.assertRaises(DatabaseError):
                self.aplicar_lote(previa)
        self.assert_lote_revertido({self.produto.pk: D('10'), segunda.item.produto_id: D('20')})

    def test_falha_apos_criar_complementos_reverte_tudo(self):
        segunda = self.segunda()
        _, previa = self.preparar_lote()
        registrar = rastreio.registrar_lote
        def falhar(*args, **kwargs):
            registrar(*args, **kwargs)
            self.assertEqual(Detalhe.objects.count(), 2)
            raise ValueError('Falha após complementos do lote')
        with patch('inventario.rastreabilidade_regularizacao.registrar_lote', side_effect=falhar):
            with self.assertRaisesMessage(ValueError, 'Falha após complementos'):
                self.aplicar_lote(previa)
        self.assert_lote_revertido({self.produto.pk: D('10'), segunda.item.produto_id: D('20')})

    def test_movimento_trocado_nao_pode_ser_atribuido_por_posicao(self):
        segunda = self.segunda()
        _, previa = self.preparar_lote()
        gravar = estoque_manual_lote.gravar
        def trocar(*args, **kwargs):
            resultados = gravar(*args, **kwargs)
            resultados[1]['movimento'] = resultados[0]['movimento']
            return resultados
        with patch('estoque.services.estoque_manual_lote.gravar', side_effect=trocar):
            with self.assertRaisesMessage(ValueError, 'Movimento incompatível'):
                self.aplicar_lote(previa)
        self.assert_lote_revertido({self.produto.pk: D('10'), segunda.item.produto_id: D('20')})

    def test_repeticao_sem_duplicidade_e_consulta_legada(self):
        self.segunda()
        _, previa = self.preparar_lote()
        apps = self.aplicar_lote(previa)
        self.assertEqual([a.pk for a in self.aplicar_lote(previa)], [a.pk for a in apps])
        token = ajustes.previa_aplicacao(self.inv.pk, [a.proposta for a in apps])
        self.assertEqual([a.pk for a in ajustes.aplicar(self.usuario, self.inv.pk, token, True)], [a.pk for a in apps])
        self.assertEqual(Detalhe.objects.count(), 2)
        self.assertEqual(MovimentoObservado.objects.count(), 2)

    def test_reenvio_lote_antigo_nao_preenche_complementos(self):
        self.segunda()
        _, previa = self.preparar_lote()
        with patch('inventario.rastreabilidade_regularizacao.registrar_lote'):
            apps = self.aplicar_lote(previa)
        self.assertFalse(Detalhe.objects.exists())
        self.assertEqual([a.pk for a in self.aplicar_lote(previa)], [a.pk for a in apps])
        self.assertFalse(Detalhe.objects.exists())

    def test_imutabilidade_complementos_do_lote(self):
        self.segunda()
        _, previa = self.preparar_lote()
        self.aplicar_lote(previa)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.all().update(documento_id='outro')
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.all().delete()

    def test_performance_comparada_sem_consultas_por_produto(self):
        # Reconstruct precisely the prior batch writer in memory, removing only
        # the newly added attribution call. Never modify source files or math.
        source = inspect.getsource(ajustes._gravar_lote)
        bloco = "    from .rastreabilidade_regularizacao import registrar_lote\n    registrar_lote(usuario, [(r, registro, m['movimento'], m['historico'])\n        for (_, r, _, _), registro, m in zip(avaliacoes, registros, movimentos)])\n"
        self.assertIn(bloco, source)
        namespace = dict(vars(ajustes))
        exec(compile(source.replace(bloco, ''), 'batch-before-attribution-in-memory', 'exec'), namespace)
        anterior = namespace['_gravar_lote']
        atual = ajustes._gravar_lote
        resultados = {}
        for tamanho in (2, 20):
            while self.inv.itens.count() < tamanho:
                self.adicionar()
            _, previa = self.preparar_lote()
            amostras, consultas = {False: [], True: []}, {}
            for ativo in (False, True, True, False, False, True, True, False, False, True):
                with transaction.atomic():
                    with patch.object(ajustes, '_gravar_lote', atual if ativo else anterior):
                        with CaptureQueriesContext(connection) as captura:
                            inicio = time.perf_counter()
                            self.aplicar_lote(previa)
                            ms = (time.perf_counter() - inicio) * 1000
                    amostras[ativo].append((ms, len(captura)))
                    consultas[ativo] = [q['sql'] for q in captura]
                    transaction.set_rollback(True)
            self.assertEqual(len(consultas[True]) - len(consultas[False]), 1)
            self.assertEqual([q for q in consultas[True] if 'FOR UPDATE' in q],
                             [q for q in consultas[False] if 'FOR UPDATE' in q])
            self.assertEqual(sum('INSERT INTO "inventario_detalhemovimentoestoque"' in q for q in consultas[True]), 1)
            resultados[tamanho] = {str(ativo): dict(mediana_ms=round(statistics.median(a[0] for a in valores), 3),
                consultas=[a[1] for a in valores]) for ativo, valores in amostras.items()}
        print('PERFORMANCE LOTE ' + json.dumps(resultados, sort_keys=True))


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConcorrenciaLoteTests(LoteFixture, TransactionTestCase):
    def executar(self, funcao):
        close_old_connections()
        try:
            return funcao()
        finally:
            close_old_connections()

    def test_repeticoes_concorrentes_aplicam_lote_uma_vez(self):
        self.segunda()
        _, previa = self.preparar_lote()
        barreira = Barrier(2)
        def aplicar():
            barreira.wait(timeout=10)
            return [a.pk for a in self.aplicar_lote(previa)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futuros = [pool.submit(self.executar, aplicar) for _ in range(2)]
            ids = [f.result(timeout=20) for f in futuros]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(Detalhe.objects.count(), 2)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)
        self.assertEqual(set(Detalhe.objects.values_list('movimento__sequencia', flat=True)), {1})

    def test_escrita_posterior_nao_desloca_sequencias_do_lote(self):
        self.segunda()
        _, previa = self.preparar_lote()
        inserido, tentando = Event(), Event()
        criar = Detalhe.objects.bulk_create
        def pausar(detalhes):
            resultado = criar(detalhes)
            inserido.set()
            if not tentando.wait(10):
                raise RuntimeError('Timeout da concorrência de lote')
            return resultado
        def aplicar():
            with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.bulk_create', side_effect=pausar):
                return self.aplicar_lote(previa)
        def alterar():
            if not inserido.wait(10):
                raise RuntimeError('Timeout da inserção do lote')
            tentando.set()
            with transaction.atomic():
                p = Produto.objects.select_for_update().get(pk=self.produto.pk)
                Produto.objects.filter(pk=p.pk).update(quantidade=p.quantidade - 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.executar, aplicar)
            b = pool.submit(self.executar, alterar)
            a.result(timeout=20)
            b.result(timeout=20)
        d = Detalhe.objects.get(movimento__produto=self.produto)
        self.assertEqual((d.movimento.sequencia, d.movimento.antes, d.movimento.depois), (1, 10, 8))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia').sequencia, 2)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, 7)
