"""Summary-only regressions with fictitious stock and immutable confirmations."""
from decimal import Decimal as D
from uuid import uuid4

from django.db import connection
from django.db.models import F
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Produto
from . import ajustes, services, views, contagem_admin
from .contexto_ajustes import carregar
from .models import AplicacaoAjuste, ConfirmacaoContagem, MovimentoObservado, RodadaContagem
from .painel import explicar_saldos
from .tests import produto
from .tests_ajustes import AjustesFixture
from . import tests_fluxo_compartilhado as fluxo
from . import tests_tabela_visual as visual


class ResumoMovimentosTests(AjustesFixture, TestCase):
    vender = fluxo.FluxoCompartilhadoTests.vender
    comprar = fluxo.FluxoCompartilhadoTests.comprar

    def configurar(self, unidade, esperado=10, contado=8):
        self.produto = produto(nome=f'Produto fictício {unidade}', quantidade=esperado,
            unidade_compra=unidade, unidade_venda_1=unidade, vende_fracionado=False,
            unidade_venda_2='', fator_conversao=0)
        self.inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=[self.produto.pk])
        self.rodada = self.confirmar(self.inv, contado)

    def pagina(self):
        modelos = (Produto, ConfirmacaoContagem, MovimentoObservado, AplicacaoAjuste, RodadaContagem)
        antes = [list(m.objects.order_by('pk').values()) for m in modelos]
        resposta = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(antes, [list(m.objects.order_by('pk').values()) for m in modelos])
        linha = next(l for l in resposta.context['linhas'] if l['item'].produto_id == self.produto.pk)
        tabela = resposta.content.decode().split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        principal = visual.TextoPrincipal()
        principal.feed(tabela)
        texto = ' '.join(principal.texto)
        return linha, texto

    def test_venda_posterior_mil_explica_igualdade_sem_confundir_com_ok(self):
        self.configurar('MIL', 1, 1)
        self.vender(D('.300'))
        linha, texto = self.pagina()
        e = linha['explicacao_saldos']
        self.assertEqual((e['entradas'], e['saidas'], e['projecao'], e['diferenca_atual']),
                         (0, D('.300'), D('.700'), 0))
        self.assertEqual(linha['diferenca'], 0)
        for rotulo in ('Estoque no OK:', 'Contagem física no OK', 'Saídas após OK:',
                       'Projeção física atual:', 'Diferença no OK', 'Diferença atual:', 'OK:'):
            self.assertIn(rotulo, texto)
        self.assertIn('0,300 MIL', texto)
        self.assertIn('0,700 MIL', texto)
        self.assertEqual(linha['atualizacao']['estado'], 'Sem diferença')

    def test_mesma_regra_para_unidades_discretas_e_continuas(self):
        for unidade in ('UN', 'PCT', 'CT', 'MIL', 'FD', 'RL', 'CX', 'KG'):
            with self.subTest(unidade=unidade):
                self.configurar(unidade)
                self.comprar(3, unidade)
                self.vender(1)
                linha, texto = self.pagina()
                e = linha['explicacao_saldos']
                self.assertEqual((e['entradas'], e['saidas'], e['projecao'], e['diferenca_atual']), (3, 1, 10, -2))
                self.assertEqual(linha['diferenca'], -2)
                self.assertEqual(e['unidade'], unidade)
                self.assertIn('Ajuste pendente', texto)
                self.inv.refresh_from_db()
                self.assertIsNone(self.inv.finalizada_em)

    def test_sem_prova_preserva_numeros_e_explica_revisao(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=F('quantidade')+1)
        m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
        linha, texto = self.pagina()
        e = linha['explicacao_saldos']
        self.assertIsNone(e['projecao'])
        self.assertIsNone(e['diferenca_atual'])
        self.assertEqual(e['sem_comprovacao'], 1)
        self.assertEqual((linha['rodada'].esperado, linha['rodada'].contado_base, linha['diferenca']), (10, 8, -2))
        self.assertIn(f'Movimento #{m.pk}', texto)
        self.assertIn('Revisão necessária', texto)
        self.assertIn('não determinável', texto)
        self.assertIn('Totais parciais', texto)

    def test_totais_comprovados_sobrevivem_a_movimento_intermediario_desconhecido(self):
        self.vender(1)
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=F('quantidade')+2)
        self.comprar(3)
        linha, _ = self.pagina()
        e = linha['explicacao_saldos']
        self.assertEqual((e['entradas'], e['saidas'], e['sem_comprovacao']), (3, 1, 1))
        self.assertIsNone(e['projecao'])

    def test_zero_no_ok_nao_oculta_motivo_de_revisao(self):
        self.configurar('UN', 10, 10)
        services.sinalizar_fisico(self.rodada.pk, 'Saída física ainda não lançada.', uuid4(), usuario=self.usuario)
        linha, texto = self.pagina()
        self.assertEqual(linha['diferenca'], 0)
        self.assertTrue(linha['revisar'])
        self.assertIn('Sem diferença', texto)
        self.assertIn('Revisão necessária', texto)
        self.assertIn('sinalizada sem resolução', texto)
        self.assertIsNone(linha['explicacao_saldos']['diferenca_atual'])

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_ajuste_aplicado_nao_e_novamente_descontado_da_projecao(self):
        p = self.aprovar()
        ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, [p]), True)
        self.vender(1)
        linha, texto = self.pagina()
        self.assertEqual(linha['explicacao_saldos']['projecao'], 7)
        self.assertEqual(linha['explicacao_saldos']['diferenca_atual'], 0)
        self.assertEqual(linha['diferenca'], -2)
        self.assertIn('Ajuste aplicado', texto)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        # An unproved later event does not erase the prior application or count
        # it as a second physical movement.
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=F('quantidade')+1)
        linha, texto = self.pagina()
        e = linha['explicacao_saldos']
        self.assertEqual((e['entradas'], e['saidas'], e['sem_comprovacao']), (0, 1, 1))
        self.assertIsNone(e['projecao'])
        self.assertIn('Ajuste aplicado', texto)
        self.assertIn('Revisão necessária', texto)

    def test_conversao_fisica_congelada_preserva_avulsos_na_contagem(self):
        self.produto = produto(nome='Pacote fictício', unidade_compra='PCT', unidade_venda_1='PCT',
            vende_fracionado=False, unidade_venda_2='UN', fator_conversao=12)
        self.inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=[self.produto.pk])
        r = RodadaContagem.objects.select_related('item__produto').get(item__inventario=self.inv)
        token = contagem_admin.autorizacao(self.usuario, self.inv, r)
        self.rodada = contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, '1', '6')
        self.comprar(1, 'PCT')
        linha, texto = self.pagina()
        self.assertEqual(linha['rodada'].contado_base, D('1.500'))
        self.assertEqual(linha['explicacao_saldos']['projecao'], D('2.500'))
        self.assertEqual(linha['explicacao_saldos']['diferenca_atual'], D('-8.500'))
        self.assertIn('18 UN', texto)

    def test_calculo_reutiliza_lote_sem_novas_consultas(self):
        self.comprar()
        self.vender(1)
        linha = views.linhas_revisao(self.inv)[0]
        with carregar([linha['rodada']]) as lote, CaptureQueriesContext(connection) as consultas:
            explicar_saldos(linha, lote, historico_valido=True)
        self.assertEqual(len(consultas), 0)
