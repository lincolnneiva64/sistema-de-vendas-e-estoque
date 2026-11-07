"""Audit capacity only; operational writers and conversion formulas stay intact."""
from decimal import Decimal as D
from importlib import import_module
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction, DatabaseError, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, TransactionTestCase, override_settings

from estoque.models import Produto
from . import ajustes, services, contagem_admin, conversao_fisica
from .models import DetalheMovimentoEstoque as Detalhe, MovimentoObservado, AplicacaoAjuste
from .tests import produto
from .tests_rastreabilidade_individual import PilotoFixture
from .tests_detalhe_movimento import (exigir_descartavel, preparar_registro_migrations,
                                    limpar_protecao_detalhe_teste, restaurar_migrations_posteriores)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class LimitesAuditoriaTests(PilotoFixture, TestCase):
    def aplicar_novo(self, **kwargs):
        p, r, inv = self.novo(**kwargs)
        previa = self.previa(r, inv.pk)
        a = ajustes.atualizar(self.usuario, inv.pk, previa, True)[0]
        self.assertEqual(ajustes.atualizar(self.usuario, inv.pk, previa, True)[0].pk, a.pk)
        d = Detalhe.objects.get(documento_id=str(a.pk))
        self.assertEqual(Detalhe.objects.filter(movimento=d.movimento).count(), 1)
        self.assertEqual(d.movimento.depois, a.depois)
        return d

    def test_cem_milhoes_dz_e_um_bilhao_duzentos_milhoes_un(self):
        d = self.aplicar_novo(base='DZ', saldo='100000001', principal='100000000', avulsos='0', fator='12')
        self.assertEqual(d.quantidade_entrada, D('1200000000'))
        self.assertEqual((d.movimento.antes, d.movimento.depois, d.quantidade_movimentada),
                         (D('100000001'), D('100000000'), D('-1')))

    def test_unidades_sem_lista_restritiva_inclusive_unicode_e_barra(self):
        for unidade in ('M³', 'UN/FD', 'UN', 'DZ', 'PCT', 'FD', 'KG', 'MIL', 'A' * 20):
            with self.subTest(unidade=unidade):
                d = self.aplicar_novo(base=unidade, saldo='4', principal='3', avulsos='', fracionado=False)
                self.assertEqual((d.unidade_base, d.unidade_entrada), (unidade, unidade))
                self.assertEqual(d.quantidade_movimentada, -1)

    def test_maior_fator_comercial_com_quantidade_grande(self):
        for base, secundaria, fator in [('DZ', 'UN', '99999999'), ('SC', 'KG', '99999999.99')]:
            with self.subTest(base=base):
                d = self.aplicar_novo(base=base, saldo='999999999', principal='999999998',
                    avulsos='0', fator=fator, secundaria=secundaria)
                self.assertEqual(d.quantidade_entrada, D('999999998') * D(fator))
                self.assertEqual(d.fator, D(fator))

    def test_maior_fator_fisico_confirmavel_e_limite_derivado(self):
        # The conference-only confirmed relation may exceed Produto.fator_conversao.
        p = produto(unidade_compra='DZ', quantidade='999999999', vende_fracionado=False,
                    unidade_venda_2='', fator_conversao=1)
        inv = services.criar_inventario(self.usuario, 'Limite físico fictício', 'manual', [p.pk])
        r = inv.itens.get().rodadas.get()
        conversao_fisica.confirmar(self.usuario, inv.pk, r.pk,
            contagem_admin.autorizacao(self.usuario, inv, r), '999999999')
        r = self.confirmar(inv, '999999998')
        a = ajustes.atualizar(self.usuario, inv.pk, self.previa(r, inv.pk), True)[0]
        d = Detalhe.objects.get(documento_id=str(a.pk))
        self.assertEqual(d.quantidade_entrada, D('999999998') * D('999999999'))
        self.assertEqual(d.fator, D('999999999'))
        self.assertLess(d.quantidade_entrada, D('1e18'))
        # Every accepted exact base count is <1e9 and any allowed factor <1e9.
        from .conversao import MAXIMO
        self.assertEqual(MAXIMO, D('1e9'))
        self.assertEqual(Detalhe._meta.get_field('quantidade_entrada').max_digits, 27)
        self.assertEqual(Detalhe._meta.get_field('quantidade_entrada').decimal_places, 9)
        self.assertEqual(Produto._meta.get_field('quantidade').max_digits, 12)
        self.assertEqual(Produto._meta.get_field('fator_conversao').max_digits, 10)

    def test_limites_avulsos_discretos_e_continuos(self):
        d = self.aplicar_novo(base='DZ', saldo='83333334', principal='0', avulsos='999999999', fator='12')
        self.assertEqual(d.quantidade_entrada, D('999999999'))
        d = self.aplicar_novo(base='SC', saldo='333333334', principal='0',
            avulsos='999999999.999', fator='3', secundaria='KG')
        self.assertEqual(d.quantidade_entrada, D('999999999.999'))

    def test_limite_saldo_continuo_sem_mudar_precisao_operacional(self):
        for unidade in ('KG', 'MIL'):
            d = self.aplicar_novo(base=unidade, saldo='999999999.999', principal='999999999.998',
                                  avulsos='', fracionado=False)
            self.assertEqual(d.quantidade_entrada, D('999999999.998'))
            self.assertEqual(d.quantidade_movimentada, D('-.001'))

    def dados(self, **kwargs):
        dados = dict(operacao_id=uuid4(), ordem=1, acao='ajuste', documento_tipo='teste',
            documento_id='1', contexto='sistema', executor_nome='Teste', unidade_base='UN',
            unidade_entrada='UN', quantidade_entrada=1, conversao='nenhuma')
        dados.update(kwargs)
        return dados

    def test_rejeita_unidades_vazias_e_acima_de_vinte_no_modelo_e_banco(self):
        # No stock operation is involved in validating the audit's own fields.
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=9)
        m = MovimentoObservado.objects.get(produto=self.produto)
        for campo in ('unidade_base', 'unidade_entrada'):
            for valor in ('', '   ', '\t\n', 'A' * 21):
                with self.subTest(campo=campo, valor=valor):
                    d = Detalhe(**self.dados(movimento=m, **{campo: valor}))
                    with self.assertRaises(ValidationError):
                        d.full_clean()
                    with self.assertRaises(DatabaseError), transaction.atomic():
                        d.save()

    def test_limites_numericos_do_complemento_sem_perda_de_casas(self):
        for valor in ('999999999999999999.999999999', '-999999999999999999.999999999'):
            Produto.objects.filter(pk=self.produto.pk).update(quantidade=9 if valor[0] != '-' else 8)
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            d = Detalhe(**self.dados(movimento=m, quantidade_entrada=D(valor)))
            d.full_clean()
            d.save()
            d.refresh_from_db()
            self.assertEqual(d.quantidade_entrada, D(valor))
        for valor in ('1000000000000000000', '-1000000000000000000'):
            with self.assertRaises(DatabaseError), transaction.atomic():
                Detalhe.objects.create(**self.dados(movimento=m, quantidade_entrada=D(valor)))

    def test_falha_real_no_banco_reverte_operacao_completa(self):
        criar = Detalhe.objects.create
        def unidade_invalida(**dados):
            dados['unidade_base'] = ''
            return criar(**dados)
        previa = self.previa()
        with patch('inventario.rastreabilidade_regularizacao.DetalheMovimentoEstoque.objects.create',
                   side_effect=unidade_invalida):
            with self.assertRaises(IntegrityError):
                self.aplicar(previa)
        self.assert_revertido()


class MigrationLimitesTests(TransactionTestCase):
    def tearDown(self):
        limpar_protecao_detalhe_teste()
        super().tearDown()

    def test_cadeia_real_0009_0010_0011_imutabilidade_e_rollback(self):
        exigir_descartavel()
        executor = MigrationExecutor(connection)
        m9 = executor.loader.get_migration('inventario', '0009_detalhe_movimento_estoque')
        m10 = executor.loader.get_migration('inventario', '0010_acao_regularizacao_conferencia')
        m11 = executor.loader.get_migration('inventario', '0011_capacidade_unidades_detalhe_movimento')
        preparar_registro_migrations(executor, ('inventario', m9.name))
        pais = [n.key for n in executor.loader.graph.node_map[('inventario', m9.name)].parents]
        estado = executor.loader.project_state(pais)
        with connection.schema_editor() as editor:
            editor.delete_model(Detalhe)
        estado9 = executor.apply_migration(estado.clone(), m9)
        Historico = estado9.apps.get_model('inventario', 'DetalheMovimentoEstoque')
        p = produto()
        Produto.objects.filter(pk=p.pk).update(quantidade=9)
        movimento = MovimentoObservado.objects.get(produto=p)
        antigo = Historico.objects.create(movimento_id=movimento.pk, operacao_id=uuid4(), ordem=1,
            acao='regularizacao', documento_tipo='teste', documento_id='1', contexto='sistema',
            executor_nome='Sistema de teste', unidade_base='UN', unidade_entrada='UN',
            quantidade_entrada=1, conversao='nenhuma')
        antes = Historico.objects.values().get(pk=antigo.pk)
        estado10 = executor.apply_migration(estado9.clone(), m10)
        original = connection.SchemaEditorClass.execute
        def falhar(editor, sql, params=()):
            resultado = original(editor, sql, params)
            if 'ALTER COLUMN "quantidade_entrada" TYPE' in str(sql):
                raise RuntimeError('Falha DDL após ampliação')
            return resultado
        with patch.object(connection.SchemaEditorClass, 'execute', falhar):
            with self.assertRaisesMessage(RuntimeError, 'Falha DDL após ampliação'):
                executor.apply_migration(estado10.clone(), m11)
        self.assertNotIn(('inventario', m11.name), MigrationRecorder(connection).applied_migrations())
        self.assertEqual(Historico.objects.values().get(pk=antigo.pk), antes)
        executor.apply_migration(estado10.clone(), m11)
        self.assertEqual(Historico.objects.values().get(pk=antigo.pk), antes)
        Produto.objects.filter(pk=p.pk).update(quantidade=8)
        novo = Historico.objects.create(movimento_id=MovimentoObservado.objects.filter(produto=p).latest('sequencia').pk,
            operacao_id=uuid4(), ordem=1, acao='regularizacao_conferencia', documento_tipo='teste',
            documento_id='2', contexto='sistema', executor_nome='Sistema de teste',
            unidade_base='M³', unidade_entrada='UN/FD', quantidade_entrada=D('1.2e9'),
            conversao='divisao', fator=12)
        self.assertEqual(novo.quantidade_entrada, D('1.2e9'))
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=novo.pk).update(documento_id='3')
        with self.assertRaises(DatabaseError), transaction.atomic():
            Historico.objects.filter(pk=novo.pk).delete()
        self.assertEqual(Produto.objects.get(pk=p.pk).quantidade, 8)
        restaurar_migrations_posteriores(executor, ('inventario', m11.name))
