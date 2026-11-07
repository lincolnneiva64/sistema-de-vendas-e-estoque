"""Individual pilot only: fictitious stock on guarded disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from importlib import import_module
from threading import Barrier, Event
from unittest.mock import patch

from django.db import connection, close_old_connections, transaction, DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings

from estoque.models import Produto, MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from . import ajustes, services
from .models import (DetalheMovimentoEstoque as Detalhe, MovimentoObservado,
    AplicacaoAjuste, DecisaoAjuste, PropostaAjuste, EventoAjuste, ConfirmacaoContagem)
from .tests import produto
from .tests_ajustes import AjustesFixture
from .tests_detalhe_movimento import (exigir_descartavel, restaurar_migrations_posteriores,
                                    limpar_protecao_detalhe_teste, reverter_migrations_posteriores)


class PilotoFixture(AjustesFixture):
    def setUp(self):
        super().setUp()
        exigir_descartavel()
        with connection.schema_editor() as editor:
            import_module('inventario.migrations.0009_detalhe_movimento_estoque').proteger(None, editor)

    def tearDown(self):
        if not connection.in_atomic_block:
            with connection.schema_editor() as editor:
                import_module('inventario.migrations.0009_detalhe_movimento_estoque').desproteger(None, editor)
        super().tearDown()

    def previa(self, rodada=None, inventario=None):
        rodada = self.fresca(rodada.pk if rodada else None)
        a = ajustes.avaliar(rodada)
        self.assertFalse(a['bloqueio'], a['bloqueio'])
        return ajustes.previa_atualizacao(inventario or self.inv.pk, [a])

    def aplicar(self, previa=None):
        return ajustes.atualizar(self.usuario, self.inv.pk, previa or self.previa(), True)[0]

    def novo(self, base='DZ', saldo='3.501', principal='3', avulsos='6', fator='12', fracionado=True, secundaria='UN'):
        p = produto(nome='Produto piloto fictício', unidade_compra=base, quantidade=saldo,
            vende_fracionado=fracionado, unidade_venda_2=secundaria if fracionado else '', fator_conversao=fator)
        inv = services.criar_inventario(self.usuario, 'Piloto isolado', 'manual', [p.pk])
        r = inv.itens.get().rodadas.get()
        acesso, _ = services.gerar_acesso(self.usuario, inv.pk, self.funcionario.pk, [r.pk])
        services.salvar_contagem(acesso.pk, r.pk, 0, principal, avulsos, True)
        return p, self.fresca(r.pk), inv

    def assert_revertido(self, saldo=D('10'), movimentos=0):
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, saldo)
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), movimentos)
        for model in (Detalhe, AplicacaoAjuste, DecisaoAjuste, MovimentacaoEstoqueManual,
                      PropostaAjuste, EventoAjuste):
            self.assertFalse(model.objects.exists(), model.__name__)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class RastreabilidadeIndividualTests(PilotoFixture, TestCase):
    def test_complemento_completo_movimento_exato_e_saldos(self):
        ok = ConfirmacaoContagem.objects.values().get(rodada=self.rodada)
        a = self.aplicar()
        d = Detalhe.objects.select_related('movimento').get()
        self.assertEqual((d.movimento.produto_id, d.movimento.sequencia), (self.produto.pk, 1))
        self.assertEqual((d.movimento.antes, d.quantidade_movimentada, d.movimento.depois),
                         (D('10'), D('-2'), D('8')))
        self.assertEqual(d.movimento.origem, 'ajuste_manual')
        self.assertEqual(d.movimento.referencia, f'Movimentação manual #{a.decisao.movimentacao_id}')
        self.assertEqual((d.acao, d.operacao_id, d.ordem), ('regularizacao_conferencia', a.pk, 1))
        self.assertEqual((d.documento_tipo, d.documento_id), ('aplicacao_ajuste', str(a.pk)))
        self.assertIn(f'conferencia:{self.inv.pk}', d.item_referencia)
        self.assertIn(f'proposta:{a.proposta_id}', d.item_referencia)
        self.assertEqual((d.executor_id, d.executor_nome, d.contexto),
                         (self.usuario.pk, self.usuario.get_username(), 'autenticado'))
        self.assertIsNone(d.operador_id)
        self.assertEqual(d.operador_nome, a.decisao.movimentacao.operador_nome)
        self.assertNotEqual(d.operador_nome, self.funcionario.nome)
        self.assertEqual((d.unidade_base, d.quantidade_entrada, d.unidade_entrada, d.fator, d.conversao),
                         ('CX', D('96'), 'UN', D('12'), 'divisao'))
        self.assertEqual(ConfirmacaoContagem.objects.values().get(rodada=self.rodada), ok)

    def test_conversao_congelada_discretos_e_normalizacao(self):
        for base in ('DZ', 'PCT', 'FD'):
            p, r, inv = self.novo(base=base)
            a = ajustes.atualizar(self.usuario, inv.pk, self.previa(r, inv.pk), True)[0]
            d = Detalhe.objects.get(documento_id=str(a.pk))
            self.assertEqual((d.movimento.antes, d.quantidade_movimentada, d.movimento.depois),
                             (D('3.501'), D('-.001'), D('3.500')))
            self.assertEqual((d.unidade_base, d.quantidade_entrada, d.unidade_entrada, d.fator),
                             (base, D('42'), 'UN', D('12')))
            Produto.objects.filter(pk=p.pk).update(fator_conversao=24)
            d.refresh_from_db()
            self.assertEqual(d.fator, 12)
            self.assertEqual(a.evidencia['preparacao']['cadastro']['fator'], '12.00')

    def test_kg_e_mil_sem_conversao_nem_arredondamento_novo(self):
        for base in ('KG', 'MIL'):
            p, r, inv = self.novo(base=base, principal='3.500', avulsos='', fracionado=False)
            a = ajustes.atualizar(self.usuario, inv.pk, self.previa(r, inv.pk), True)[0]
            d = Detalhe.objects.get(documento_id=str(a.pk))
            self.assertEqual(d.quantidade_movimentada, D('-.001'))
            self.assertEqual((d.unidade_base, d.unidade_entrada, d.quantidade_entrada, d.conversao, d.fator),
                             (base, base, D('3.500'), 'nenhuma', None))
            self.assertEqual(Produto.objects.get(pk=p.pk).quantidade, D('3.500'))

    def test_sem_diferenca_nao_cria_movimento_ou_complemento(self):
        p, r, inv = self.novo(saldo='3.500')
        a = ajustes.avaliar(r)
        self.assertEqual(a['estado'], 'Sem ajuste')
        resultado = ajustes.preparar_resultado(inv)
        self.assertEqual(resultado['aptas'], [])
        with self.assertRaises(ValueError):
            ajustes.atualizar(self.usuario, inv.pk,
                ajustes.previa_atualizacao(inv.pk, resultado['aptas']), True)
        self.assertFalse(Detalhe.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentoObservado.objects.filter(produto=p).exists())

    def test_conversao_continua_congelada_sc_para_kg(self):
        p, r, inv = self.novo(base='SC', saldo='5', principal='4', avulsos='15',
                              fator='30', secundaria='KG')
        a = ajustes.atualizar(self.usuario, inv.pk, self.previa(r, inv.pk), True)[0]
        d = Detalhe.objects.get(documento_id=str(a.pk))
        self.assertEqual((d.unidade_base, d.unidade_entrada, d.quantidade_entrada, d.fator),
                         ('SC', 'KG', D('135'), D('30')))
        self.assertEqual((d.quantidade_movimentada, d.movimento.depois), (D('-.500'), D('4.500')))

    def test_fluxo_aprovar_e_aplicar_cria_complemento(self):
        proposta = self.aprovar()
        a = ajustes.aplicar(self.usuario, self.inv.pk,
            ajustes.previa_aplicacao(self.inv.pk, [proposta]), True)[0]
        self.assertEqual(Detalhe.objects.get().documento_id, str(a.pk))

    def test_falha_antes_do_complemento_reverte_tudo(self):
        previa = self.previa()
        with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.create',
                   side_effect=ValueError('antes do complemento')):
            with self.assertRaisesMessage(ValueError, 'antes do complemento'):
                self.aplicar(previa)
        self.assert_revertido()

    def test_falha_depois_do_complemento_reverte_tudo(self):
        previa = self.previa()
        criar = Detalhe.objects.create
        def falhar(**dados):
            d = criar(**dados)
            self.assertTrue(Detalhe.objects.filter(pk=d.pk).exists())
            raise ValueError('depois do complemento')
        with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.create',
                   side_effect=falhar):
            with self.assertRaisesMessage(ValueError, 'depois do complemento'):
                self.aplicar(previa)
        self.assert_revertido()

    def test_falha_posterior_na_auditoria_reverte_complemento(self):
        previa = self.previa()
        with patch('inventario.ajustes.EventoAjuste.objects.create', side_effect=ValueError('evento final')):
            with self.assertRaisesMessage(ValueError, 'evento final'):
                self.aplicar(previa)
        self.assert_revertido()

    def test_repeticao_dos_dois_fluxos_nao_duplica(self):
        previa = self.previa()
        primeiro = self.aplicar(previa)
        self.assertEqual(self.aplicar(previa).pk, primeiro.pk)
        antigo = ajustes.previa_aplicacao(self.inv.pk, [primeiro.proposta])
        self.assertEqual(ajustes.aplicar(self.usuario, self.inv.pk, antigo, True)[0].pk, primeiro.pk)
        self.assertEqual(Detalhe.objects.count(), 1)
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 1)

    def test_repeticao_legada_nao_preenche_complemento_antigo(self):
        previa = self.previa()
        with patch('inventario.rastreabilidade_regularizacao.registrar'):
            antigo = self.aplicar(previa)
        self.assertFalse(Detalhe.objects.exists())
        self.assertEqual(self.aplicar(previa).pk, antigo.pk)
        self.assertFalse(Detalhe.objects.exists())

    def test_saldo_historico_desconhecido_preservado(self):
        p = produto(nome='Histórico desconhecido fictício', quantidade=None)
        Produto.objects.filter(pk=p.pk).update(quantidade=D('10'))
        antigo = MovimentoObservado.objects.get(produto=p)
        self.assertIsNone(antigo.antes)
        inv = services.criar_inventario(self.usuario, 'Após referência conhecida', 'manual', [p.pk])
        r = self.confirmar(inv, 8)
        a = ajustes.atualizar(self.usuario, inv.pk, self.previa(r, inv.pk), True)[0]
        d = Detalhe.objects.get(documento_id=str(a.pk))
        self.assertEqual(d.movimento.sequencia, 2)
        self.assertEqual((d.movimento.antes, d.movimento.depois), (10, 8))
        antigo.refresh_from_db()
        self.assertIsNone(antigo.antes)
        self.assertFalse(Detalhe.objects.filter(movimento=antigo).exists())

    def test_saldo_atual_desconhecido_continua_bloqueado(self):
        previa = self.previa()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=None)
        with self.assertRaises(ValueError):
            self.aplicar(previa)
        self.assertIsNone(Produto.objects.get(pk=self.produto.pk).quantidade)
        self.assertFalse(Detalhe.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_movimento_incompativel_reverte_regularizacao(self):
        previa = self.previa()
        gravar = ajustes.conferir_ou_ajustar_estoque
        def adulterar(*args, **kwargs):
            resultado = gravar(*args, **kwargs)
            MovimentoObservado.objects.filter(produto=self.produto).update(antes=99)
            return resultado
        with patch('inventario.ajustes.conferir_ou_ajustar_estoque', side_effect=adulterar):
            with self.assertRaisesMessage(ValueError, 'Movimento do observador não confirmado'):
                self.aplicar(previa)
        self.assert_revertido()

    def test_lote_tem_complemento_e_manual_comum_continua_sem(self):
        segunda = self.segunda()
        linhas = [ajustes.avaliar(self.fresca()), ajustes.avaliar(segunda)]
        ajustes.atualizar(self.usuario, self.inv.pk, ajustes.previa_atualizacao(self.inv.pk, linhas), True)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)
        self.assertEqual(Detalhe.objects.count(), 2)
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7, usuario=self.usuario)
        self.assertEqual(Detalhe.objects.count(), 2)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConcorrenciaRastreabilidadeTests(PilotoFixture, TransactionTestCase):
    def executar(self, funcao):
        close_old_connections()
        try:
            return funcao()
        finally:
            close_old_connections()

    def test_duas_aplicacoes_concorrentes_mesmo_evento(self):
        previa = self.previa()
        barreira = Barrier(2)
        def aplicar():
            barreira.wait(timeout=10)
            return ajustes.atualizar(self.usuario, self.inv.pk, previa, True)[0].pk
        with ThreadPoolExecutor(max_workers=2) as pool:
            futuros = [pool.submit(self.executar, aplicar) for _ in range(2)]
            ids = [f.result(timeout=20) for f in futuros]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(Detalhe.objects.count(), 1)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(Detalhe.objects.get().movimento.sequencia, 1)

    def test_escrita_concorrente_posterior_nao_desloca_associacao(self):
        previa = self.previa()
        inserido, tentando = Event(), Event()
        criar = Detalhe.objects.create
        def pausar(**dados):
            detalhe = criar(**dados)
            inserido.set()
            if not tentando.wait(10):
                raise RuntimeError('Timeout da escrita concorrente')
            return detalhe
        def aplicar():
            with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.create', side_effect=pausar):
                return ajustes.atualizar(self.usuario, self.inv.pk, previa, True)[0].pk
        def alterar():
            if not inserido.wait(10):
                raise RuntimeError('Timeout do complemento')
            tentando.set()
            with transaction.atomic():
                p = Produto.objects.select_for_update().get(pk=self.produto.pk)
                Produto.objects.filter(pk=p.pk).update(quantidade=p.quantidade-D('1'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.executar, aplicar)
            b = pool.submit(self.executar, alterar)
            a.result(timeout=20)
            b.result(timeout=20)
        d = Detalhe.objects.get()
        self.assertEqual((d.movimento.sequencia, d.movimento.antes, d.movimento.depois), (1, 10, 8))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia').sequencia, 2)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, 7)


class MigrationAcaoTests(TransactionTestCase):
    def tearDown(self):
        limpar_protecao_detalhe_teste()
        super().tearDown()

    def test_migration_real_preserva_registros_antigos_e_libera_acao_nova(self):
        exigir_descartavel()
        executor = MigrationExecutor(connection)
        m = executor.loader.get_migration('inventario', '0010_acao_regularizacao_conferencia')
        pais = [n.key for n in executor.loader.graph.node_map[('inventario', m.name)].parents]
        estado = executor.loader.project_state(pais)
        reverter_migrations_posteriores(executor, ('inventario', m.name))
        executor.unapply_migration(estado.clone(), m)
        Historico = estado.apps.get_model('inventario', 'DetalheMovimentoEstoque')
        p = produto()
        Produto.objects.filter(pk=p.pk).update(quantidade=9)
        movimento = MovimentoObservado.objects.get(produto=p)
        from uuid import uuid4
        dados = dict(movimento_id=movimento.pk, operacao_id=uuid4(), ordem=1,
            acao='regularizacao', documento_tipo='teste', documento_id='1', contexto='sistema',
            executor_nome='Teste isolado', unidade_base='CX', unidade_entrada='CX',
            quantidade_entrada=9, conversao='nenhuma')
        antigo = Historico.objects.create(**dados)
        antes = Historico.objects.values().get(pk=antigo.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            novo = dict(dados, acao='regularizacao_conferencia')
            Historico.objects.create(**novo)
        executor.apply_migration(estado.clone(), m)
        self.assertEqual(Historico.objects.values().get(pk=antigo.pk), antes)
        Produto.objects.filter(pk=p.pk).update(quantidade=8)
        novo.update(movimento_id=MovimentoObservado.objects.filter(produto=p).latest('sequencia').pk, ordem=2)
        Historico.objects.create(**novo)
        self.assertEqual(Detalhe.objects.count(), 2)
        restaurar_migrations_posteriores(executor, ('inventario', m.name))
