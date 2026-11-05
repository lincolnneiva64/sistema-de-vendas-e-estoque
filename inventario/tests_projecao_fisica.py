"""Frozen physical counts, stock projections and existing transactional writer."""
from decimal import Decimal as D
from types import SimpleNamespace as N
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.template.loader import render_to_string

from . import ajustes, services
from .apresentacao import comparacao_fisica
from .conversao import converter, snapshot
from .conversao_fisica import representacao_fisica, exigir_precisao_ajuste
from .models import ItemInventario, RodadaContagem, AplicacaoAjuste, ConfirmacaoContagem
from .tests import produto
from .tests_ajustes import AjustesFixture
from estoque.models import Produto, MovimentacaoEstoqueManual


class RepresentacaoTests(SimpleTestCase):
    def rodada(self, fator, esperado, principal, avulsos):
        cadastro = dict(base='PCT', secundaria='UN', fator=str(fator), fracionado=True,
                        continua=False, secundaria_continua=False)
        p, a, contado, base = converter(cadastro, str(principal), str(avulsos))
        return N(item=N(cadastro=cadastro), embalagens=p, avulsos=a,
                 contado=contado, contado_base=base, esperado=D(esperado))

    def test_fatores_6_12_24_com_residuo_exato_sem_tolerancia(self):
        for fator, esperado, p, a, total in [(6, '4', 3, 5, 23),
                (12, '7.084', 7, 1, 85), (24, '1', 0, 23, 23)]:
            with self.subTest(fator=fator):
                r = self.rodada(fator, esperado, p, a)
                exigir_precisao_ajuste(r)
                e = representacao_fisica(r)
                self.assertEqual(D(e['total_unidades']), total)
                self.assertEqual(D(e['residuo_unidades']), D(total)-r.contado_base*D(fator))
                r.contado_base += D('.001')
                with self.assertRaisesMessage(ValueError, 'Projeção decimal incompatível'):
                    exigir_precisao_ajuste(r)

    def test_um_inteiro_nao_pode_ser_ocultado_por_arredondamento(self):
        for a, diferenca in [(5, '-1'), (7, '1'), (6, '0')]:
            e = representacao_fisica(self.rodada(6, '4', 3, a))
            self.assertEqual(D(e['diferenca_fisica']), D(diferenca))
        exigir_precisao_ajuste(self.rodada(1000, '1', 0, 1))
        exigir_precisao_ajuste(self.rodada(2000, '1', 1, 0))
        r = self.rodada(1001, '1', 0, 1)
        with self.assertRaises(ValueError):
            exigir_precisao_ajuste(r)

    def test_residuo_da_amstel_nao_e_falta_fisica(self):
        e = representacao_fisica(self.rodada(12, '7.084', 7, 1))
        self.assertEqual(e['natureza'], 'normalizacao_decimal')
        self.assertIsNone(e['diferenca_fisica'])
        self.assertEqual(D(e['diferenca_decimal']), D('-.001'))
        self.assertEqual(D(e['referencia_residuo_unidades']), D('.008'))

    def test_pacotes_sem_relacao_fisica_mantem_regra_simples(self):
        r = self.rodada(6, '3', 3, 0)
        r.item.cadastro.update(fracionado=False, secundaria='')
        r.item._relacao_fisica = None
        self.assertEqual(representacao_fisica(r), {})
        exigir_precisao_ajuste(r)

    def test_contagem_precisa_adulterada_nao_e_aceita(self):
        r = self.rodada(6, '4', 3, 5)
        r.contado += D('.000000001')
        with self.assertRaisesMessage(ValueError, 'Projeção decimal incompatível'):
            exigir_precisao_ajuste(r)

    def test_apresentacao_inteira_e_detalhes_tecnicos_separados(self):
        e = representacao_fisica(self.rodada(6, '3.500', 3, 2))
        c = comparacao_fisica(e)
        self.assertEqual(c['esperado'], '3 PCT + 3 UN = 21 UN')
        self.assertEqual(c['encontrado'], '3 PCT + 2 UN = 20 UN')
        self.assertEqual(c['diferenca'], 'Falta 1 UN')
        html = render_to_string('inventario/comparacao_fisica.html', {'comparacao': c})
        self.assertIn('Falta 1 UN', html)
        self.assertNotIn('−1,002 UN', html)
        self.assertIn('Projeção decimal e auditoria', html)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AplicacaoProjecaoTests(AjustesFixture, TestCase):
    def preparar(self, fator, saldo, principal, avulsos):
        p = produto(nome='Embalagem fictícia', unidade_compra='PCT', unidade_venda_1='PCT',
            unidade_venda_2='UN', fator_conversao=fator, quantidade=D(saldo), vende_fracionado=True)
        p.refresh_from_db()
        i = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p), custo_base=p.preco_compra)
        r = RodadaContagem.objects.create(item=i, numero=1)
        acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [r.pk])
        services.salvar_contagem(acesso.pk, r.pk, 0, str(principal), str(avulsos), True)
        p.refresh_from_db()
        self.assertEqual(p.quantidade, D(saldo))  # An OK never applies stock.
        return p, self.fresca(r.pk)

    def test_ajustes_contagem_auditoria_e_reenvio(self):
        for fator, saldo, p, a in [(6, '4', 3, 5), (6, '3.500', 3, 2),
                (12, '7.084', 7, 1), (24, '1', 0, 23), (6, '3', 3, 1)]:
            with self.subTest(fator=fator, saldo=saldo):
                produto, r = self.preparar(fator, saldo, p, a)
                anterior = ConfirmacaoContagem.objects.values().get(rodada=r)
                aval = ajustes.avaliar(r)
                self.assertFalse(aval['bloqueio'], aval['bloqueio'])
                token = ajustes.previa_atualizacao(self.inv.pk, [aval])
                primeira = ajustes.atualizar(self.usuario, self.inv.pk, token, True)[0]
                segunda = ajustes.atualizar(self.usuario, self.inv.pk, token, True)[0]
                self.assertEqual(primeira.pk, segunda.pk)
                produto.refresh_from_db()
                self.assertEqual(produto.quantidade, r.contado_base)
                self.assertEqual(primeira.antes + primeira.diferenca, primeira.depois)
                e = primeira.evidencia['preparacao']['representacao_fisica']
                self.assertEqual(D(e['total_unidades']), D(p)*fator+D(a))
                self.assertEqual(anterior, ConfirmacaoContagem.objects.values().get(rodada=r))
                if fator == 12:
                    self.assertIsNone(e['diferenca_fisica'])
                    self.assertIn('Normalização decimal', primeira.decisao.movimentacao.observacao)
        self.assertEqual(AplicacaoAjuste.objects.count(), 5)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 5)

    def test_falha_reverte_projecao_movimento_e_auditoria(self):
        p, r = self.preparar(6, '4', 3, 5)
        token = ajustes.previa_atualizacao(self.inv.pk, [ajustes.avaliar(r)])
        with patch('inventario.ajustes.EventoAjuste.objects.create', side_effect=ValueError('Falha fictícia')):
            with self.assertRaisesMessage(ValueError, 'Falha fictícia'):
                ajustes.atualizar(self.usuario, self.inv.pk, token, True)
        p.refresh_from_db()
        self.assertEqual(p.quantidade, 4)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_movimento_posterior_sem_comprovacao_bloqueia(self):
        p, r = self.preparar(6, '4', 3, 5)
        Produto.objects.filter(pk=p.pk).update(quantidade=D('3'))
        aval = ajustes.avaliar(self.fresca(r.pk))
        self.assertEqual(aval['estado'], 'Bloqueado')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_venda_posterior_documentada_preservada(self):
        from django.db import transaction
        from django.utils import timezone
        from estoque.models import Venda, ItemVenda
        from estoque.views import _baixar_estoque_produto
        from .models import MovimentoObservado
        p, r = self.preparar(6, '4', 3, 5)
        with transaction.atomic():
            venda = Venda.objects.create(data_venda=timezone.localdate())
            snap = _baixar_estoque_produto(p.pk, D(1), unidade='PCT')
            ItemVenda.objects.create(venda=venda, produto=p, quantidade=1, unidade='PCT',
                preco_unitario=0, valor_total=0, **snap)
            m = MovimentoObservado.objects.filter(produto=p).latest('sequencia')
            MovimentoObservado.objects.filter(pk=m.pk).update(referencia=f'Venda #{venda.pk}')
        aval = ajustes.avaliar(self.fresca(r.pk))
        self.assertFalse(aval['bloqueio'], aval['bloqueio'])
        registro = ajustes.atualizar(self.usuario, self.inv.pk,
            ajustes.previa_atualizacao(self.inv.pk, [aval]), True)[0]
        self.assertEqual((registro.antes, registro.diferenca, registro.depois),
                         (D('3'), D('-.167'), D('2.833')))

    def test_sem_diferenca_nao_cria_ajuste(self):
        p, r = self.preparar(6, '3.833', 3, 5)
        a = ajustes.avaliar(r)
        self.assertEqual(a['estado'], 'Sem ajuste')
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertEqual(p.quantidade, D('3.833'))

    def test_painel_residuo_nao_rotula_como_falta(self):
        from .views import linhas_revisao
        from .painel import resumo_painel
        p, r = self.preparar(12, '7.084', 7, 1)
        linhas = linhas_revisao(self.inv)
        resumo_painel(linhas)
        linha = next(l for l in linhas if l['rodada'].pk == r.pk)
        self.assertEqual(linha['resultado_label'], 'Correto')
        self.assertEqual(linha['diferenca_texto'], 'Sem diferença')
        self.assertIsNone(linha['percentual_quantidade'])


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConcorrenciaProjecaoTests(AjustesFixture, TransactionTestCase):
    preparar = AplicacaoProjecaoTests.preparar

    def thread(self, func):
        from django.db import connections, close_old_connections
        close_old_connections()
        try:
            return func()
        finally:
            connections.close_all()

    def test_duas_aplicacoes_da_projecao_uma_unica_vez(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        p, r = self.preparar(6, '4', 3, 5)
        token = ajustes.previa_atualizacao(self.inv.pk, [ajustes.avaliar(r)])
        barreira = Barrier(2)

        def aplicar():
            barreira.wait(10)
            return ajustes.atualizar(self.usuario, self.inv.pk, token, True)[0].pk

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [pool.submit(self.thread, aplicar) for _ in range(2)]
            self.assertEqual(a.result(20), b.result(20))
        p.refresh_from_db()
        self.assertEqual(p.quantidade, D('3.833'))
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)

    def test_venda_concorrente_aguarda_projecao_sem_sobrescrita(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import transaction
        from estoque.views import _baixar_estoque_produto
        p, r = self.preparar(6, '4', 3, 5)
        token = ajustes.previa_atualizacao(self.inv.pk, [ajustes.avaliar(r)])
        bloqueou, liberar, iniciou, terminou = Event(), Event(), Event(), Event()
        original = ajustes.conferir_ou_ajustar_estoque

        def segurar(*args, **kwargs):
            bloqueou.set()
            if not liberar.wait(10):
                raise AssertionError('Timeout de teste')
            return original(*args, **kwargs)

        def vender():
            iniciou.set()
            with transaction.atomic():
                _baixar_estoque_produto(p.pk, D(1), unidade='PCT')
            terminou.set()

        with patch.object(ajustes, 'conferir_ou_ajustar_estoque', side_effect=segurar), ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.thread, lambda: ajustes.atualizar(self.usuario, self.inv.pk, token, True))
            self.assertTrue(bloqueou.wait(10))
            b = pool.submit(self.thread, vender)
            try:
                self.assertTrue(iniciou.wait(10))
                self.assertFalse(terminou.wait(.3))
            finally:
                liberar.set()
            a.result(20)
            b.result(20)
        p.refresh_from_db()
        self.assertEqual(p.quantidade, D('2.833'))
        self.assertEqual(AplicacaoAjuste.objects.get().depois, D('3.833'))
