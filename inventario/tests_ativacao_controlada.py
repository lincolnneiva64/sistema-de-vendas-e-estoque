"""Activation rehearsal: disposable PostgreSQL and synthetic stock only."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from estoque.models import Produto, Compra, ItemCompra, MovimentacaoEstoqueManual
from . import ajustes, services
from .conversao import snapshot
from .models import (ItemInventario, RodadaContagem, ConfirmacaoContagem,
                     PropostaAjuste, AplicacaoAjuste, EventoAjuste)
from .rastreabilidade import identificar
from .tests import produto
from .tests_ajustes import AjustesFixture


class ConfiguracaoAtivacaoTests(SimpleTestCase):
    def test_padrao_desabilitado_ativacao_e_reversao_sem_acesso_ao_banco(self):
        for valor, esperado in ((None, 'False'), ('False', 'False'), ('True', 'True'), ('False', 'False')):
            with self.subTest(valor=valor):
                env = os.environ.copy()
                env.pop('INVENTARIO_AJUSTES_HABILITADOS', None)
                if valor is not None:
                    env['INVENTARIO_AJUSTES_HABILITADOS'] = valor
                resultado = subprocess.run([sys.executable, '-c',
                    'import sistema.settings as s; print(s.INVENTARIO_AJUSTES_HABILITADOS)'],
                    env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertEqual(resultado.stdout.strip(), esperado)

    def test_apenas_true_exato_habilita_sem_interromper_configuracao(self):
        for valor in ('', 'false', 'true', 'TRUE', '1', 'yes', 'on', '0', ' True',
                      'True ', ' True ', 'valor-invalido'):
            with self.subTest(valor=valor):
                env = dict(os.environ, INVENTARIO_AJUSTES_HABILITADOS=valor)
                resultado = subprocess.run([sys.executable, '-c',
                    'import sistema.settings as s; print(s.INVENTARIO_AJUSTES_HABILITADOS)'],
                    env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertEqual(resultado.stdout.strip(), 'False')


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class EnsaioAtivacaoTests(AjustesFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.rodadas = [self.rodada]
        for n, (saldo, contado) in enumerate(((20,18), (30,27), (14,10), (7,7), (5,5)), 2):
            p = produto(nome=f'Produto fictício de ativação {n}', quantidade=saldo)
            p.refresh_from_db()
            item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p))
            r = RodadaContagem.objects.create(item=item, numero=1)
            self.confirmar(self.inv, contado)
            self.rodadas.append(self.fresca(r.pk))
        self.antes = list(Produto.objects.filter(pk__in=[r.item.produto_id for r in self.rodadas])
            .order_by('pk').values_list('pk', 'quantidade'))

    def previa(self):
        pagina = self.client.get(self.url, {'confirmar': 'todos'})
        self.assertEqual(len(pagina.context['confirmacoes']), 4)
        return pagina.context['token_aplicacao']

    def aplicar(self, previa):
        return ajustes.atualizar(self.usuario, self.inv.pk, previa, confirmacao=True)

    def test_habilitar_e_abrir_tela_nao_aplica_nada(self):
        confirmacoes = list(ConfirmacaoContagem.objects.values())
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=False):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.previa()
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(confirmacoes, list(ConfirmacaoContagem.objects.values()))
        self.assertEqual(self.antes, list(Produto.objects.filter(pk__in=[p for p,_ in self.antes])
            .order_by('pk').values_list('pk', 'quantidade')))

    def test_quatro_em_lote_repeticao_horarios_responsavel_e_sem_ajuste(self):
        previa = self.previa()
        confirmacoes = list(ConfirmacaoContagem.objects.values())
        inicio = timezone.now()
        resultados = self.aplicar(previa)
        fim = timezone.now()
        repetidos = self.aplicar(previa)
        self.assertEqual({r.pk for r in resultados}, {r.pk for r in repetidos})
        self.assertEqual(len({r.lote for r in resultados}), 1)
        self.assertEqual(len(resultados), 4)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 4)
        self.assertEqual(EventoAjuste.objects.filter(acao='confirmado_gestor').count(), 4)
        for r, saldo in zip(self.rodadas, (8,18,27,10,7,5)):
            self.assertEqual(self.fresca(r.pk).item.produto.quantidade, saldo)
        for a in resultados:
            self.assertEqual(a.usuario_id, self.usuario.pk)
            self.assertTrue(inicio <= a.aplicado_em <= fim)
            self.assertEqual(a.depois, a.antes + a.diferenca)
            self.assertEqual(a.proposta.confirmacao_id, a.proposta.rodada.confirmacoes.get().pk)
            self.assertEqual(a.decisao.aprovado_por_id, self.usuario.pk)
            self.assertTrue(inicio <= a.decisao.aprovado_em <= fim)
        self.assertEqual(confirmacoes, list(ConfirmacaoContagem.objects.values()))
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=False):
            with self.assertRaisesMessage(ValueError, 'desabilitada'):
                self.aplicar(previa)
        self.assertEqual(AplicacaoAjuste.objects.count(), 4)

    def test_falha_no_quarto_reverte_aprovacoes_movimentos_e_auditoria(self):
        previa = self.previa()
        original = ajustes.conferir_ou_ajustar_estoque
        chamadas = 0
        def falhar(*args, **kwargs):
            nonlocal chamadas
            chamadas += 1
            if chamadas == 4:
                raise ValueError('Falha fictícia no quarto produto')
            return original(*args, **kwargs)
        with patch.object(ajustes, 'conferir_ou_ajustar_estoque', side_effect=falhar):
            with self.assertRaisesMessage(ValueError, 'quarto produto'):
                self.aplicar(previa)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(EventoAjuste.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(self.antes, list(Produto.objects.filter(pk__in=[p for p,_ in self.antes])
            .order_by('pk').values_list('pk', 'quantidade')))

    def test_venda_apos_tela_invalida_lote_e_nova_previa_recalcula(self):
        from .tests_conciliacao_documental import ConciliacaoDocumentalTests
        previa = self.previa()
        ConciliacaoDocumentalTests.vender(self, 1)
        with self.assertRaises(ValueError):
            self.aplicar(previa)
        self.assertFalse(PropostaAjuste.objects.exists())
        resultados = self.aplicar(self.previa())
        alvo = next(r for r in resultados if r.proposta.rodada_id == self.rodada.pk)
        self.assertEqual((alvo.antes, alvo.diferenca, alvo.depois), (9,-2,7))

    def test_compra_apos_tela_bloqueia_lote_e_exclui_somente_alvo_na_nova_previa(self):
        previa = self.previa()
        with transaction.atomic():
            p = Produto.objects.select_for_update().get(pk=self.produto.pk)
            compra = Compra.objects.create(data_compra=timezone.localdate(), estoque_entrada_realizada=True,
                estoque_entrada_realizada_em=timezone.now())
            ItemCompra.objects.create(compra=compra, produto=p, quantidade=1, unidade='CX', preco_unitario=0, valor_total=0)
            Produto.objects.filter(pk=p.pk).update(quantidade=11)
            identificar(p.pk,p.quantidade,11,'compra',f'Compra #{compra.pk}')
        with self.assertRaises(ValueError):
            self.aplicar(previa)
        self.assertFalse(PropostaAjuste.objects.exists())
        pagina = self.client.get(self.url, {'confirmar':'todos'})
        self.assertEqual(pagina.context['resumo']['bloqueados'], 1)
        self.assertEqual(len(pagina.context['confirmacoes']), 3)
        self.assertEqual(self.fresca().item.produto.quantidade, 11)

    def test_duas_confirmacoes_concorrentes_do_lote_geram_quatro_movimentos(self):
        previa = self.previa()
        barreira = Barrier(2)
        def executar():
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                return {r.pk for r in self.aplicar(previa)}
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b = [pool.submit(executar) for _ in range(2)]
            self.assertEqual(a.result(timeout=30), b.result(timeout=30))
        self.assertEqual(AplicacaoAjuste.objects.count(), 4)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 4)

    def test_ativacao_nao_concede_permissao(self):
        previa = self.previa()
        sem_permissao = get_user_model().objects.create_user('operador-sem-aprovacao')
        with self.assertRaises(PermissionDenied):
            ajustes.atualizar(sem_permissao, self.inv.pk, previa, confirmacao=True)
        self.assertFalse(PropostaAjuste.objects.exists())
