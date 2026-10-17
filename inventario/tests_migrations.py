from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from .tests import produto


class ObservadorMigrationTests(TransactionTestCase):
    def test_0004_preserva_confirmacoes_existentes_sem_mexer_no_estoque(self):
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from datetime import timedelta
        from estoque.models import Funcionario
        from .conversao import snapshot
        atual = [("inventario", "0004_acessoinventario_ciclo_inventario_ciclo_and_more")]
        anterior = [("inventario", "0003_movimentoobservado_origem_and_more")]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(anterior)
            apps = executor.loader.project_state(anterior).apps
            usuario = get_user_model().objects.create_user("responsavel-migracao")
            funcionario = Funcionario.objects.create(nome="Contador original")
            p = produto(nome="Adoçante anterior")
            inventario = apps.get_model("inventario", "Inventario").objects.create(titulo="Anterior", criterio="manual", filtro={}, criado_por_id=usuario.pk)
            item = apps.get_model("inventario", "ItemInventario").objects.create(inventario_id=inventario.pk, produto_id=p.pk, cadastro=snapshot(p), custo_base=10)
            horario = timezone.now() - timedelta(hours=1)
            rodada = apps.get_model("inventario", "RodadaContagem").objects.create(item_id=item.pk, numero=1, versao=1, embalagens=6, avulsos=0, contado=6, contado_base=6, esperado=10, sequencia_ok=0, ok_em=horario)
            acesso = apps.get_model("inventario", "AcessoInventario").objects.create(inventario_id=inventario.pk, funcionario_id=funcionario.pk, funcionario_nome=funcionario.nome, token_hash="1" * 64, expira_em=timezone.now() + timedelta(hours=1), criado_por_id=usuario.pk)
            apps.get_model("inventario", "AtribuicaoContagem").objects.create(rodada_id=rodada.pk, acesso_id=acesso.pk)
            MigrationExecutor(connection).migrate(atual)
            from .models import ConfirmacaoContagem, Inventario, MovimentoObservado
            confirmacao = ConfirmacaoContagem.objects.get(rodada_id=rodada.pk)
            self.assertEqual((confirmacao.contado_base, confirmacao.esperado, confirmacao.ok_em, confirmacao.versao_entrada), (6, 10, horario, 0))
            self.assertEqual((confirmacao.acesso_id, confirmacao.responsavel_nome), (acesso.pk, funcionario.nome))
            self.assertIsNone(Inventario.objects.get(pk=inventario.pk).finalizada_em)
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)
            self.assertFalse(MovimentoObservado.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_reversao_e_reaplicacao_preservam_trigger_e_registros(self):
        anterior = [("inventario", "0002_observador_quantidade")]
        atual = [("inventario", "0004_acessoinventario_ciclo_inventario_ciclo_and_more")]
        try:
            MigrationExecutor(connection).migrate(anterior)
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT prosecdef, proconfig FROM pg_catalog.pg_proc WHERE oid='public.inventario_observar_quantidade_fn()'::regprocedure")
                    self.assertEqual(cursor.fetchone(), (True, ["search_path=pg_catalog"]))
            p = produto(nome="Produto migração")
            with connection.cursor() as cursor:
                cursor.execute("UPDATE estoque_produto SET quantidade = quantidade + 1 WHERE id = %s", [p.pk])
            MigrationExecutor(connection).migrate(atual)
            from .models import MovimentoObservado
            movimento = MovimentoObservado.objects.get(produto_id=p.pk)
            self.assertEqual((movimento.antes, movimento.depois, movimento.origem), (10, 11, "nao_identificada"))
            with connection.cursor() as cursor:
                cursor.execute("UPDATE estoque_produto SET quantidade = quantidade + 1 WHERE id = %s", [p.pk])
            self.assertEqual(MovimentoObservado.objects.filter(produto_id=p.pk).count(), 2)
        finally:
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
