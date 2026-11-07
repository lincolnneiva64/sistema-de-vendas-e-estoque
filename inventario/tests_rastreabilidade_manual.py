"""Sales manual correction only, on guarded disposable PostgreSQL."""
import json
import statistics
import time
from unittest.mock import patch

from django.db import connection, transaction, DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque import views
from estoque.models import Produto, MovimentacaoEstoqueManual
from .models import DetalheMovimentoEstoque as Detalhe, MovimentoObservado
from .tests import produto
from .tests_rastreabilidade_individual import PilotoFixture
from .tests_detalhe_movimento import (exigir_descartavel, reverter_migrations_posteriores,
                                    limpar_protecao_detalhe_teste)


class RastreabilidadeManualTests(PilotoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manual = produto(nome='Produto manual fictício', unidade_compra='UN',
            quantidade=23, vende_fracionado=False, unidade_venda_2='')

    def corrigir(self, quantidade='20', operador='', motivo='Contagem', observacao='Registro de teste'):
        return self.client.post(reverse('estoque:conferencia_estoque_corrigir', args=[self.manual.pk]),
            json.dumps(dict(novo_estoque=quantidade, motivo=motivo, observacao=observacao)),
            content_type='application/json', HTTP_X_OPERADOR_VENDA=operador)

    def assert_revertido_manual(self):
        p = Produto.objects.get(pk=self.manual.pk)
        self.assertEqual(p.quantidade, 23)
        self.assertFalse(p.estoque_conferido)
        self.assertFalse(Detalhe.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.filter(produto=p).exists())

    def test_reducao_23_para_20_vinculo_exato_e_data(self):
        inicio = timezone.now()
        self.assertEqual(self.corrigir().status_code, 200)
        d = Detalhe.objects.select_related('movimento').get()
        h = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual((d.acao, d.documento_tipo, d.documento_id),
                         ('ajuste', 'movimentacao_estoque_manual', str(h.pk)))
        self.assertEqual((d.movimento.produto_id, d.movimento.sequencia), (self.manual.pk, 1))
        self.assertEqual((d.movimento.antes, d.quantidade_movimentada, d.movimento.depois), (23, -3, 20))
        self.assertEqual(d.movimento.origem, 'ajuste_manual')
        self.assertEqual(d.movimento.referencia, f'Movimentação manual #{h.pk}')
        self.assertGreaterEqual(d.movimento.registrado_em, inicio)
        self.assertEqual(str(timezone.get_default_timezone()), 'America/Belem')
        self.assertEqual((d.unidade_base, d.unidade_entrada, d.conversao, d.fator),
                         ('UN', 'UN', 'nenhuma', None))
        self.assertEqual(d.quantidade_entrada, 20)  # Absolute operand, not a second delta.
        self.assertNotIn('perda', d.acao)

    def test_operador_declarado_diferente_do_executor_sem_inferir_fk(self):
        self.assertEqual(self.corrigir(operador=self.funcionario.nome).status_code, 200)
        d = Detalhe.objects.get()
        self.assertEqual(d.executor_id, self.usuario.pk)
        self.assertEqual(d.executor_nome, self.usuario.get_username())
        self.assertEqual(d.operador_nome, self.funcionario.nome)
        self.assertIsNone(d.operador_id)
        self.assertEqual(MovimentacaoEstoqueManual.objects.get().operador_nome, self.usuario.get_username())

    def test_executor_da_autorizacao_curta_nao_e_substituido_pelo_usuario_da_sessao(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from estoque.autorizacao_estoque import CHAVE
        autorizado = get_user_model().objects.create_superuser('gestor-estoque-manual', '', 'teste')
        sessao = self.client.session
        sessao[CHAVE] = dict(usuario=autorizado.pk, backend=settings.AUTHENTICATION_BACKENDS[0],
            expira=timezone.now().timestamp() + 900, hash=autorizado.get_session_auth_hash())
        sessao.save()
        self.assertEqual(self.corrigir(operador=self.funcionario.nome).status_code, 200)
        d = Detalhe.objects.get()
        self.assertEqual(d.executor_id, autorizado.pk)
        self.assertNotEqual(d.executor_id, self.usuario.pk)
        self.assertEqual(d.operador_nome, self.funcionario.nome)

    def test_declaracao_nao_duplica_dados_na_evidencia(self):
        declarado = 'Nome declarado ' * 20
        self.assertEqual(self.corrigir(operador=declarado).status_code, 200)
        d = Detalhe.objects.get()
        self.assertEqual(d.operador_nome, declarado.strip()[:150])
        self.assertEqual(d.evidencia, {'motivo': 'Contagem', 'observacao': 'Registro de teste'})

    def test_alteracoes_reais_sucessivas_tem_eventos_independentes(self):
        self.assertEqual(self.corrigir().status_code, 200)
        self.assertEqual(self.corrigir('25').status_code, 200)
        detalhes = list(Detalhe.objects.select_related('movimento').order_by('movimento__sequencia'))
        self.assertEqual([d.quantidade_movimentada for d in detalhes], [-3, 5])
        self.assertNotEqual(detalhes[0].operacao_id, detalhes[1].operacao_id)
        self.assertEqual([d.movimento.sequencia for d in detalhes], [1, 2])

    def test_motivo_observacao_longos_congelados_e_imutaveis(self):
        motivo, observacao = 'Motivo livre ' * 70, 'Observação livre ' * 100
        self.assertEqual(self.corrigir(motivo=motivo, observacao=observacao).status_code, 200)
        d = Detalhe.objects.get()
        h = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual(d.evidencia['motivo'], h.motivo)
        self.assertEqual(d.evidencia['observacao'], h.observacao)
        MovimentacaoEstoqueManual.objects.filter(pk=h.pk).update(motivo='Outro texto', observacao='Outro')
        d.refresh_from_db()
        self.assertEqual(d.evidencia['motivo'], motivo.strip())
        self.assertEqual(d.evidencia['observacao'], observacao.strip())
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=d.pk).update(evidencia={})
        with self.assertRaises(DatabaseError), transaction.atomic():
            Detalhe.objects.filter(pk=d.pk).delete()

    def test_caracteres_especiais_copiados_do_registro_sem_campos_extras(self):
        motivo = 'Conferência: ação, M³, UN/FD, "aspas" e <texto> 🧾'
        observacao = 'Linha 1\nLinha 2\tBarra \\ e apóstrofo: d\'água'
        self.assertEqual(self.corrigir(motivo=motivo, observacao=observacao,
            operador=self.funcionario.nome).status_code, 200)
        h = MovimentacaoEstoqueManual.objects.get()
        d = Detalhe.objects.get()
        self.assertEqual(d.evidencia, {'motivo': h.motivo, 'observacao': h.observacao})
        self.assertEqual(set(d.evidencia), {'motivo', 'observacao'})
        self.assertEqual(d.evidencia['motivo'], motivo)
        self.assertEqual(d.evidencia['observacao'], observacao)

    def test_aumento_e_motivo_vazio_sem_validacao_nova(self):
        self.assertEqual(self.corrigir('25', motivo='', observacao='').status_code, 200)
        d = Detalhe.objects.get()
        self.assertEqual(d.quantidade_movimentada, 2)
        self.assertEqual(d.evidencia['motivo'], '')
        self.assertEqual(d.evidencia['observacao'], '')
        self.assertEqual(set(d.evidencia), {'motivo', 'observacao'})

    def test_sem_mudanca_e_repeticao_nao_duplicam_complemento(self):
        self.assertEqual(self.corrigir('23').status_code, 200)
        self.assertFalse(Detalhe.objects.exists())
        self.assertEqual(self.corrigir().status_code, 200)
        primeiro = Detalhe.objects.get()
        self.assertEqual(self.corrigir().status_code, 200)
        self.assertEqual(Detalhe.objects.count(), 1)
        self.assertEqual(Detalhe.objects.get().pk, primeiro.pk)
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.manual).count(), 1)
        # Keep the original confirmation/history behavior for unchanged targets.
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 3)

    def test_unidades_continuas_e_especiais_sem_converter_catalogo(self):
        for unidade in ('KG', 'MIL', 'DZ', 'M³', 'UN/FD'):
            p = produto(unidade_compra=unidade, quantidade=23, vende_fracionado=False)
            self.manual = p
            self.assertEqual(self.corrigir('20.125').status_code, 200)
            d = Detalhe.objects.get(movimento__produto=p)
            self.assertEqual(d.unidade_base, unidade)
            self.assertIsNone(d.fator)
            self.assertEqual(str(d.movimento.depois), '20.125')

    def test_cadastro_sem_unidade_preserva_desconhecimento(self):
        Produto.objects.filter(pk=self.manual.pk).update(unidade_compra=None, unidade_venda_1=None)
        self.assertEqual(self.corrigir().status_code, 200)
        d = Detalhe.objects.get()
        self.assertEqual(d.unidade_base, 'Não informada')
        self.assertEqual(set(d.evidencia), {'motivo', 'observacao'})

    def test_saldo_anterior_desconhecido_nao_vira_zero_na_auditoria(self):
        self.manual = produto(quantidade=None, unidade_compra='UN', vende_fracionado=False)
        self.assertEqual(self.corrigir('0').status_code, 200)
        d = Detalhe.objects.get()
        self.assertIsNone(d.movimento.antes)
        self.assertEqual(d.movimento.depois, 0)
        self.assertIsNone(d.quantidade_movimentada)
        # The legacy writer's own record is preserved, not retrospectively fixed.
        self.assertEqual(MovimentacaoEstoqueManual.objects.get().estoque_antes, 0)

    def test_erro_antes_e_depois_do_complemento_reverte(self):
        criar = Detalhe.objects.create
        for depois in (False, True):
            def falhar(**dados):
                if depois:
                    criar(**dados)
                raise ValueError('Falha fictícia de auditoria')
            with patch('estoque.services.rastreabilidade_manual.DetalheMovimentoEstoque.objects.create', side_effect=falhar):
                self.assertEqual(self.corrigir().status_code, 400)
            self.assert_revertido_manual()

    def test_falha_real_check_reverte(self):
        criar = Detalhe.objects.create
        def falhar(**dados):
            dados['unidade_base'] = ''
            return criar(**dados)
        with patch('estoque.services.rastreabilidade_manual.DetalheMovimentoEstoque.objects.create', side_effect=falhar):
            with self.assertRaises(DatabaseError):
                self.corrigir()
        self.assert_revertido_manual()

    def test_nome_de_operador_nao_autoriza_ajuste(self):
        self.client.logout()
        self.assertEqual(self.corrigir(operador=self.funcionario.nome).status_code, 403)
        self.assert_revertido_manual()

    def test_botao_confirmar_e_servico_direto_nao_instrumentados(self):
        from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
        self.assertEqual(self.client.post(reverse('estoque:conferencia_estoque_confirmar', args=[self.manual.pk])).status_code, 200)
        conferir_ou_ajustar_estoque(self.manual.pk, 'ajuste', novo_estoque=21, usuario=self.usuario)
        self.assertFalse(Detalhe.objects.exists())

    def test_desempenho_e_consultas(self):
        original = views.conferir_ou_ajustar_estoque
        def anterior(*args, **kwargs):
            kwargs.pop('auditoria', None)
            return original(*args, **kwargs)
        medidas, traces = {False: [], True: []}, {}
        for ativo in (False, True, True, False, False, True, True, False, False, True):
            with transaction.atomic():
                with patch.object(views, 'conferir_ou_ajustar_estoque', original if ativo else anterior):
                    with CaptureQueriesContext(connection) as q:
                        inicio = time.perf_counter()
                        self.assertEqual(self.corrigir().status_code, 200)
                        ms = (time.perf_counter() - inicio) * 1000
                medidas[ativo].append((ms, len(q)))
                traces[ativo] = [r['sql'] for r in q]
                transaction.set_rollback(True)
        self.assertEqual(len(traces[True]) - len(traces[False]), 2)
        self.assertEqual([q for q in traces[True] if 'FOR UPDATE' in q], [q for q in traces[False] if 'FOR UPDATE' in q])
        print('PERFORMANCE MANUAL ' + json.dumps({str(k): dict(mediana_ms=round(statistics.median(x[0] for x in v), 3),
            consultas=[x[1] for x in v]) for k, v in medidas.items()}))


class MigrationEvidenciaTests(TransactionTestCase):
    def tearDown(self):
        limpar_protecao_detalhe_teste()
        super().tearDown()

    def test_migration_opcional_preserva_legado_e_escritor_antigo(self):
        exigir_descartavel()
        from importlib import import_module
        from uuid import uuid4
        executor = MigrationExecutor(connection)
        migration = executor.loader.get_migration('inventario', '0012_evidencia_complementar_movimento')
        pais = [n.key for n in executor.loader.graph.node_map[('inventario', migration.name)].parents]
        estado = executor.loader.project_state(pais)
        executor.unapply_migration(estado.clone(), migration)
        Historico = estado.apps.get_model('inventario', 'DetalheMovimentoEstoque')
        with connection.schema_editor() as editor:
            import_module('inventario.migrations.0009_detalhe_movimento_estoque').proteger(None, editor)
        p = produto()
        Produto.objects.filter(pk=p.pk).update(quantidade=9)
        m = MovimentoObservado.objects.get(produto=p)
        antigo = Historico.objects.create(movimento_id=m.pk, operacao_id=uuid4(), ordem=1,
            acao='ajuste', documento_tipo='teste', documento_id='1', contexto='sistema',
            executor_nome='Teste', unidade_base='UN', unidade_entrada='UN',
            quantidade_entrada=9, conversao='nenhuma')
        antes = Historico.objects.values().get(pk=antigo.pk)
        executor.apply_migration(estado.clone(), migration)
        atual = Detalhe.objects.values().get(pk=antigo.pk)
        self.assertEqual(atual.pop('evidencia'), {})
        self.assertEqual(atual, antes)
        Produto.objects.filter(pk=p.pk).update(quantidade=8)
        outro = MovimentoObservado.objects.filter(produto=p).latest('sequencia')
        novo = Historico.objects.create(movimento_id=outro.pk, operacao_id=uuid4(), ordem=1,
            acao='ajuste', documento_tipo='teste', documento_id='2', contexto='sistema',
            executor_nome='Teste', unidade_base='UN', unidade_entrada='UN',
            quantidade_entrada=8, conversao='nenhuma')
        self.assertEqual(Detalhe.objects.get(pk=novo.pk).evidencia, {})
