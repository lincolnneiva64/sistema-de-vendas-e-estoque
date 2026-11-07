"""Phase A: only the new schema, on the identity-guarded disposable PostgreSQL."""
from decimal import Decimal as D
from importlib import import_module
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction, IntegrityError, DatabaseError
from django.db.models.deletion import ProtectedError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from estoque.models import Funcionario, Produto
from .models import DetalheMovimentoEstoque as Detalhe, MovimentoObservado
from .tests import produto

MIGRATION = '0009_detalhe_movimento_estoque'


def exigir_descartavel():
    expected = os.environ.get('INVENTARIO_CONFIABILIDADE_PG_DATA')
    if (connection.vendor != 'postgresql' or not expected
            or connection.settings_dict['HOST'] != '127.0.0.1'
            or connection.settings_dict['NAME'] != 'inventario_confiabilidade'):
        raise RuntimeError('Somente o runner PostgreSQL descartável é aceito.')
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('data_directory'), inet_server_port()")
        directory, port = cursor.fetchone()
    if (Path(directory).resolve() != Path(expected).resolve()
            or port != int(os.environ['INVENTARIO_CONFIABILIDADE_PG_PORT'])):
        raise RuntimeError('Identidade do cluster descartável divergente.')


def restaurar_migrations_posteriores(executor, referencia):
    """Migration tests must restore the current fixture schema for later tests."""
    for folha in executor.loader.graph.leaf_nodes('inventario'):
        plano = executor.loader.graph.forwards_plan(folha)
        if referencia not in plano:
            continue
        for chave in plano[plano.index(referencia) + 1:]:
            if chave[0] != 'inventario':
                continue
            migration = executor.loader.get_migration(*chave)
            pais = [n.key for n in executor.loader.graph.node_map[chave].parents]
            executor.apply_migration(executor.loader.project_state(pais), migration)


def preparar_registro_migrations(executor, referencia):
    """Reproduce a pre-migration baseline only inside the disposable fixture."""
    exigir_descartavel()
    recorder = MigrationRecorder(connection)
    for folha in executor.loader.graph.leaf_nodes('inventario'):
        plano = executor.loader.graph.forwards_plan(folha)
        if referencia in plano:
            for chave in plano[plano.index(referencia):]:
                if chave[0] == 'inventario':
                    recorder.record_unapplied(*chave)


def reverter_migrations_posteriores(executor, referencia):
    exigir_descartavel()
    for folha in executor.loader.graph.leaf_nodes('inventario'):
        plano = executor.loader.graph.forwards_plan(folha)
        if referencia not in plano:
            continue
        for chave in reversed(plano[plano.index(referencia) + 1:]):
            if chave[0] == 'inventario':
                pais = [n.key for n in executor.loader.graph.node_map[chave].parents]
                executor.unapply_migration(executor.loader.project_state(pais),
                    executor.loader.get_migration(*chave))


def limpar_protecao_detalhe_teste():
    """Restore the schema-only runner's protection state between migration tests."""
    exigir_descartavel()
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('inventario_detalhemovimentoestoque'), to_regprocedure('inventario_detalhe_imutavel_fn()')")
        tabela, funcao = cursor.fetchone()
        if tabela is not None and funcao is not None:
            cursor.execute('DROP TRIGGER IF EXISTS inventario_detalhe_imutavel ON inventario_detalhemovimentoestoque')
        if funcao is not None:
            cursor.execute('DROP FUNCTION inventario_detalhe_imutavel_fn()')


class DetalheMovimentoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        exigir_descartavel()
        # The runner builds model tables without migrations. Install only the
        # new table's protection inside this TestCase's rolled-back transaction.
        with connection.schema_editor() as editor:
            import_module('inventario.migrations.' + MIGRATION).proteger(None, editor)
        cls.usuario = get_user_model().objects.create_user('executor-ficticio')
        cls.operador = Funcionario.objects.create(nome='Operador fictício')

    def setUp(self):
        self.produto = produto()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('9.500'))
        self.movimento = MovimentoObservado.objects.get(produto=self.produto)

    def valores(self, **kwargs):
        dados = dict(movimento=self.movimento, operacao_id=uuid4(), ordem=1, acao='saida',
            documento_tipo='venda', documento_id='123', item_referencia='456',
            executor=self.usuario, executor_nome='Executor fictício',
            operador=self.operador, operador_nome='Operador fictício', contexto='autenticado',
            unidade_base='DZ', quantidade_entrada=D('6'), unidade_entrada='UN',
            conversao='divisao', fator=D('12'))
        dados.update(kwargs)
        return dados

    def test_legado_e_movimentos_futuros_sem_complemento_continuam_validos(self):
        self.assertFalse(Detalhe.objects.exists())
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('9.000'))
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('9.000'))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 2)
        self.assertFalse(Detalhe.objects.exists())

    def test_vinculo_autoria_separada_e_quantidade_derivada(self):
        detalhe = Detalhe(**self.valores())
        detalhe.full_clean()
        detalhe.save()
        self.assertEqual(detalhe.quantidade_movimentada, D('-.500'))
        self.assertEqual(detalhe.executor, self.usuario)
        self.assertEqual(detalhe.operador, self.operador)
        self.assertEqual(self.movimento.detalhe, detalhe)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, D('9.500'))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 1)

    def test_unicidade_movimento_e_evento(self):
        detalhe = Detalhe.objects.create(**self.valores())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Detalhe.objects.create(**self.valores())
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('9'))
        outro = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Detalhe.objects.create(**self.valores(movimento=outro, operacao_id=detalhe.operacao_id))
        Detalhe.objects.create(**self.valores(movimento=outro, operacao_id=detalhe.operacao_id, ordem=2))

    def test_protecao_vinculos(self):
        Detalhe.objects.create(**self.valores())
        for objeto in (self.movimento, self.usuario, self.operador):
            with self.assertRaises(ProtectedError):
                objeto.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Detalhe.objects.create(**self.valores(movimento_id=999999, movimento=None))
            connection.check_constraints()

    def test_restricoes_banco_sem_depender_de_full_clean(self):
        casos = [dict(ordem=0), dict(acao='invalida'), dict(documento_id=''),
            dict(documento_tipo=''), dict(contexto='invalido'), dict(executor=None),
            dict(executor_nome=''), dict(operador_nome=''), dict(unidade_base='   '),
            dict(unidade_entrada=''), dict(fator=0), dict(fator=-1), dict(fator=None),
            dict(conversao='invalida'), dict(conversao='nenhuma'),
            dict(conversao='nenhuma', fator=None)]
        for alteracao in casos:
            with self.subTest(alteracao=alteracao):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Detalhe.objects.create(**self.valores(**alteracao))

    def test_banco_recusa_nan_sem_validacao_do_orm(self):
        dados = self.valores()
        for campo in ('quantidade_entrada', 'fator'):
            # DecimalField rejects NaN before SQL; exercise PostgreSQL directly.
            with self.subTest(campo=campo):
                with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"""INSERT INTO inventario_detalhemovimentoestoque
                        (movimento_id,operacao_id,ordem,acao,documento_tipo,documento_id,
                         item_referencia,executor_nome,operador_nome,contexto,unidade_base,
                         quantidade_entrada,unidade_entrada,conversao,fator)
                        VALUES (%s,%s,1,'saida','venda','123','','Sistema','','sistema',
                            'DZ',%s,'UN','divisao',%s)""",
                        [self.movimento.pk, str(dados['operacao_id']),
                         'NaN' if campo == 'quantidade_entrada' else '6',
                         'NaN' if campo == 'fator' else '12'])

    def test_unidades_continuas_discretas_e_sentido_da_conversao(self):
        for unidade in ('KG', 'MIL', 'DZ', 'PCT', 'FD', 'UN'):
            detalhe = Detalhe(**self.valores(unidade_base=unidade, unidade_entrada=unidade,
                quantidade_entrada=D('0.001'), conversao='nenhuma', fator=None))
            detalhe.full_clean()
            self.assertEqual(detalhe.quantidade_entrada, D('0.001'))
        for conversao in ('divisao', 'multiplicacao'):
            Detalhe(**self.valores(conversao=conversao)).full_clean()
        with self.assertRaises(ValidationError):
            Detalhe(**self.valores(unidade_base='   ')).full_clean()

    def test_contexto_local_e_sistema_sem_inventar_executor(self):
        for contexto in ('local', 'sistema'):
            Detalhe(**self.valores(contexto=contexto, executor=None)).full_clean()
        with self.assertRaises(ValidationError):
            Detalhe(**self.valores(contexto='local', operador=None, operador_nome='')).full_clean()

    def test_saldo_desconhecido_nao_vira_zero(self):
        movimento = MovimentoObservado.objects.create(produto=self.produto, sequencia=2,
            antes=None, depois=0, registrado_em=timezone.now())
        detalhe = Detalhe.objects.create(**self.valores(movimento=movimento))
        self.assertIsNone(detalhe.quantidade_movimentada)
        self.assertIsNone(detalhe.movimento.antes)

    def test_imutabilidade_orm_e_banco(self):
        detalhe = Detalhe.objects.create(**self.valores())
        with self.assertRaises(ValueError):
            detalhe.save()
        with self.assertRaises(ValueError):
            detalhe.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=detalhe.pk).update(documento_id='outro')
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=detalhe.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('DELETE FROM inventario_detalhemovimentoestoque WHERE id=%s', [detalhe.pk])

    def test_rollback_estoque_observador_e_complemento(self):
        with self.assertRaisesMessage(RuntimeError, 'falha simulada'):
            with transaction.atomic():
                Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('8'))
                movimento = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
                Detalhe.objects.create(**self.valores(movimento=movimento))
                raise RuntimeError('falha simulada')
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, D('9.500'))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 1)
        self.assertFalse(Detalhe.objects.exists())


class MigrationDetalheTests(TransactionTestCase):
    def tearDown(self):
        limpar_protecao_detalhe_teste()
        super().tearDown()

    def test_migration_aditiva_real_rollback_e_legado_preservado(self):
        exigir_descartavel()
        p = produto()
        Produto.objects.filter(pk=p.pk).update(quantidade=D('9'))
        recorder = MigrationRecorder(connection)
        recorder.ensure_schema()
        tabela = Detalhe._meta.db_table
        antigas = sorted(set(connection.introspection.table_names()) - {tabela, 'django_migrations'})

        def fotografia():
            with connection.cursor() as cursor:
                dados = []
                for nome in antigas:
                    cursor.execute(f"SELECT count(*), md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY to_jsonb(t)::text), '')) FROM {connection.ops.quote_name(nome)} t")
                    dados.append((nome, cursor.fetchone()))
                cursor.execute("""SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull
                    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                    WHERE c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2""", [antigas])
                colunas = cursor.fetchall()
                cursor.execute("""SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
                    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                    WHERE c.relname=ANY(%s) AND NOT t.tgisinternal ORDER BY 1,2""", [antigas])
                triggers = cursor.fetchall()
                cursor.execute("""SELECT c.relname,con.conname,pg_get_constraintdef(con.oid)
                    FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                    WHERE c.relname=ANY(%s) ORDER BY 1,2""", [antigas])
                return dados, colunas, triggers, cursor.fetchall()

        antes = fotografia()
        # Only the empty new fixture table is removed, after checking identity.
        self.assertFalse(Detalhe.objects.exists())
        with connection.schema_editor() as editor:
            editor.delete_model(Detalhe)
        executor = MigrationExecutor(connection)
        migration = executor.loader.get_migration('inventario', MIGRATION)
        preparar_registro_migrations(executor, ('inventario', MIGRATION))
        pais = [n.key for n in executor.loader.graph.node_map[('inventario', MIGRATION)].parents]
        estado = executor.loader.project_state(pais)
        original = connection.SchemaEditorClass.execute

        def falhar(editor, sql, params=()):
            resultado = original(editor, sql, params)
            if str(sql).lstrip().startswith('CREATE FUNCTION inventario_detalhe_imutavel_fn'):
                raise RuntimeError('falha DDL simulada')
            return resultado

        with patch.object(connection.SchemaEditorClass, 'execute', falhar):
            with self.assertRaisesMessage(RuntimeError, 'falha DDL simulada'):
                executor.apply_migration(estado.clone(), migration)
        self.assertNotIn(tabela, connection.introspection.table_names())
        self.assertNotIn(('inventario', MIGRATION), recorder.applied_migrations())
        self.assertEqual(fotografia(), antes)
        executor.apply_migration(estado.clone(), migration)
        self.assertEqual(fotografia(), antes)
        self.assertFalse(Detalhe.objects.exists())
        self.assertTrue(MovimentoObservado.objects.filter(produto=p).exists())
        self.assertEqual(Produto.objects.get(pk=p.pk).quantidade, D('9'))
        restaurar_migrations_posteriores(executor, ('inventario', MIGRATION))
        Produto.objects.filter(pk=p.pk).update(quantidade=D('8'))
        self.assertEqual(MovimentoObservado.objects.filter(produto=p).count(), 2)
        self.assertFalse(Detalhe.objects.exists())
        movimento = MovimentoObservado.objects.filter(produto=p).latest('sequencia')
        detalhe = Detalhe.objects.create(movimento=movimento, operacao_id=uuid4(), ordem=1,
            acao='ajuste', documento_tipo='teste', documento_id='1', contexto='sistema',
            executor_nome='Sistema de teste', unidade_base='CX', unidade_entrada='CX',
            quantidade_entrada=D('-1'), conversao='nenhuma')
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=detalhe.pk).update(documento_id='2')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Detalhe.objects.create(movimento=movimento, operacao_id=uuid4(), ordem=1,
                acao='ajuste', documento_tipo='teste', documento_id='1', contexto='sistema',
                executor_nome='Sistema de teste', unidade_base='CX', unidade_entrada='CX',
                quantidade_entrada=D('-1'), conversao='nenhuma')
