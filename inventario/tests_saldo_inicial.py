"""Opening presentation reuses validated evidence, never reverse subtraction."""
from datetime import timedelta
from decimal import Decimal

from django.core.paginator import Paginator
from django.test import TestCase
from estoque.models import ItemCompra, ItemVenda, Venda
from . import tests_saldos_historicos as evidencia
from .extrato import evidencia_inicial
from .tests_historico import HistoricoFixture


class SaldoInicialTests(HistoricoFixture, TestCase):
    venda_snapshot = evidencia.SaldosHistoricosTests.venda_snapshot
    devolucao_antiga = evidencia.SaldosHistoricosTests.devolucao_antiga

    def setUp(self):
        super().setUp()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=41)
        self.anterior = self.venda_snapshot(-1,23,6,17)
        self.posterior = self.venda_snapshot(1,58,7,51)

    def abertura(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code,200)
        return r.context['extrato'].saldo_inicial,r

    def test_compra_corroborada_expoe_antes_validado_e_origem_em_a_e_b(self):
        for visual in ('a','b'):
            with self.subTest(visual=visual):
                abertura,r = self.abertura(visual=visual)
                self.assertIsNotNone(abertura)
                self.assertEqual((abertura['valor'],abertura['unidade']),(17,'CX'))
                self.assertEqual(abertura['tipo'],'Reconstruído e corroborado')
                self.assertIn(f"#{self.anterior.venda_id}",abertura['origem'])
                self.assertIn(f"#{self.posterior.venda_id}",abertura['origem'])
                self.assertContains(r,'Origem do saldo inicial')
                self.assertContains(r,'Não foi obtido apenas por subtração')

    def test_saldo_posterior_menos_compra_nao_prova_saldo_inicial(self):
        self.anterior.delete()
        abertura,r = self.abertura()
        self.assertIsNone(abertura)
        self.assertContains(r,'não são retroagidos')
        venda = next(l for l in r.context['extrato'] if l['fonte']=='venda')
        self.assertEqual(venda['saldo'],51)

    def test_lacuna_de_cancelamento_antigo_impede_abertura(self):
        self.devolucao_antiga(self.inicio+timedelta(hours=1))
        self.assertIsNone(self.abertura()[0])

    def test_unidades_incompativeis_nao_preenchem_abertura(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(unidade='UN')
        self.assertIsNone(self.abertura()[0])

    def test_snapshot_editado_ou_incompleto_nao_preenche_abertura(self):
        for campo,valor in (('quantidade',8),('estoque_antes',None)):
            with self.subTest(campo=campo):
                ItemVenda.objects.filter(pk=self.posterior.pk).update(**{campo:valor})
                self.assertIsNone(self.abertura()[0])
                ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=7,estoque_antes=58)

    def test_zero_comprovado_e_exibido_sem_trata_lo_como_ausente(self):
        ItemVenda.objects.filter(pk=self.anterior.pk).update(estoque_antes=6,estoque_depois=0)
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_antes=41,estoque_depois=34)
        abertura,r = self.abertura()
        self.assertEqual(abertura['valor'],0)
        self.assertContains(r,'<strong>0 <span>CX</span></strong>')

    def test_periodo_inicia_antes_da_compra_nao_retroage_evidencia(self):
        dia = (self.inicio-timedelta(days=1)).date().isoformat()
        self.assertIsNone(self.abertura(periodo='personalizado',data_inicial=dia,
            data_final=self.agora.date().isoformat())[0])

    def test_apresentacao_pura_sem_sql_ambiguidade_e_paginacao(self):
        row = dict(fonte='venda',instante=self.inicio,saldo=Decimal(7),saldo_antes=Decimal(9),
            saldo_unidade='KG',saldo_tipo='Snapshot registrado',saldo_evidencia='Snapshot original',
            titulo='Venda fictícia',url='/documento/')
        with self.assertNumQueries(0):
            pagina = Paginator([row],50).page(1)
            self.assertEqual(evidencia_inicial(pagina,self.inicio)['valor'],9)
            self.assertIsNone(evidencia_inicial(Paginator([row,row],50).page(1),self.inicio))
            self.assertIsNone(evidencia_inicial(Paginator([row,row],1).page(2),self.inicio))
            self.assertIsNone(evidencia_inicial(pagina,self.inicio-timedelta(seconds=1)))
