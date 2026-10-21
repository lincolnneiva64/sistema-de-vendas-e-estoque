"""Synthetic period selection and presentation equivalence on disposable PG."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from estoque.models import Compra, ItemCompra, ItemVenda, Venda
from . import tests_saldos_historicos as evidencia
from .tests_historico import HistoricoFixture


class PeriodosHistoricoTests(HistoricoFixture, TestCase):
    def test_b_padrao_a_alternativa_e_parametro_desconhecido_seguro(self):
        for params, visual in (({},'b'), ({'visual':'a'},'a'), ({'visual':'b'},'b'),
                               ({'visual':'desconhecido'},'b')):
            with self.subTest(params=params):
                r = self.consulta(**params)
                self.assertEqual(r.context['visual'],visual)
                self.assertContains(r, f'class="extrato-pagina visual-{visual}"')
                self.assertEqual(r.context['inicio'],self.inicio)
                self.assertEqual(r.context['fim'],self.agora) if visual == 'a' else self.assertGreaterEqual(r.context['fim'],self.agora)

    def consulta(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return r

    def test_padrao_ultima_compra_e_contexto_tres_documentos_distintos(self):
        antigas = self.compras_anteriores()
        ItemCompra.objects.create(compra=self.compra, produto=self.produto,
            quantidade=1, unidade='CX', preco_unitario=0, valor_total=0)
        self.assertEqual(self.consulta().context['inicio'], self.inicio)
        r = self.consulta(periodo='tres_compras', compra=antigas[-1].pk)
        self.assertEqual(r.context['inicio'], antigas[1].estoque_entrada_realizada_em)
        self.assertGreaterEqual(r.context['fim'], self.agora)
        self.assertEqual(r.context['compra'].compra_id, self.compra.pk)
        self.assertEqual({l['documento'] for l in r.context['extrato'] if l['fonte']=='compra'},
                         {self.compra.pk, antigas[0].pk, antigas[1].pk})

    def test_personalizado_datas_inclusivas_fuso_local_e_referencia_fisica_fixa(self):
        inicio = timezone.localtime(self.inicio).date()
        final = inicio+timedelta(days=1)
        r = self.consulta(periodo='personalizado', data_inicial=inicio.isoformat(), data_final=final.isoformat())
        self.assertEqual(timezone.localtime(r.context['inicio']).hour, 0)
        self.assertEqual(timezone.localtime(r.context['fim']).date(), final+timedelta(days=1))
        self.assertEqual(r.context['referencia'], self.agora)
        self.assertEqual((r.context['esperado'], r.context['fisica'], r.context['diferenca']), (10,8,-2))

    def test_fim_personalizado_futuro_limitado_ao_momento_da_consulta(self):
        r = self.consulta(periodo='personalizado', data_inicial=self.inicio.date().isoformat(),
            data_final=(self.agora+timedelta(days=10)).date().isoformat())
        self.assertGreaterEqual(r.context['fim'], self.agora)

    def test_data_final_inclusiva_nao_inclui_venda_do_dia_seguinte(self):
        dia = timezone.localtime(self.inicio).date()
        for deslocamento in (0,1):
            venda = Venda.objects.create(data_venda=dia+timedelta(days=deslocamento))
            Venda.objects.filter(pk=venda.pk).update(criado_em=self.inicio+timedelta(days=deslocamento))
            ItemVenda.objects.create(venda=venda, produto=self.produto, quantidade=1,
                unidade='UN', preco_unitario=0, valor_total=0)
        r = self.consulta(periodo='personalizado',data_inicial=dia.isoformat(),data_final=dia.isoformat())
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='venda']),1)

    def test_datas_invalidas_nao_caem_silenciosamente_em_outro_periodo(self):
        for primeiro, ultimo in (('', ''), ('abc', '2026-10-18'), ('2026-02-31', '2026-10-18'),
                                 ('2099-01-01', '2099-01-02'), ('2026-10-18', '2026-01-01')):
            with self.subTest(primeiro=primeiro, ultimo=ultimo):
                r = self.consulta(periodo='personalizado', data_inicial=primeiro, data_final=ultimo)
                self.assertTrue(r.context['erro_periodo'])
                self.assertEqual(r.context['extrato'].paginator.count, 0)

    def test_sem_compra_exibe_vendas_recentes_e_snapshots_validos_sem_inventar_compra(self):
        Compra.objects.filter(pk=self.compra.pk).update(estoque_entrada_realizada_em=None)
        v = self.venda()
        ItemVenda.objects.filter(pk=v.pk).update(unidade='KG', estoque_unidade_snapshot='KG',
            estoque_antes=12, estoque_movimentado=3, estoque_depois=9)
        r = self.consulta()
        self.assertIsNone(r.context['compra'])
        self.assertEqual(r.context['inicio'], self.agora-timedelta(days=30))
        linha = next(l for l in r.context['extrato'] if l['fonte']=='venda')
        self.assertEqual(linha['saldo'], 9)
        self.assertContains(r, 'últimos 30 dias')
        self.assertContains(r, 'saldo inicial não disponível')

    def test_completo_inclui_registros_anteriores_a_primeira_compra(self):
        antiga = Venda.objects.create(data_venda=(self.inicio-timedelta(days=90)).date())
        Venda.objects.filter(pk=antiga.pk).update(criado_em=self.inicio-timedelta(days=89))
        item = ItemVenda.objects.create(venda=antiga, produto=self.produto, quantidade=1,
            unidade='UN', preco_unitario=0, valor_total=0)
        r = self.consulta(periodo='completo')
        self.assertEqual(timezone.localtime(r.context['inicio']).date(), antiga.data_venda)
        self.assertIn(item.pk, [l['pk'] for l in r.context['extrato'] if l['fonte']=='venda'])
        self.assertContains(r, 'não comprova cobertura integral')

    def test_todas_vendas_do_periodo_acessiveis_sem_limite_entre_compras(self):
        v = self.venda()
        ItemVenda.objects.bulk_create([ItemVenda(venda=v.venda, produto=self.produto, quantidade=1,
            unidade='KG', preco_unitario=0, valor_total=0) for _ in range(119)])
        params = dict(periodo='tres_compras', visual='b')
        linhas = []
        for pagina in range(1,4):
            r = self.consulta(**params, extrato_pagina=pagina)
            linhas.extend((l['fonte'],l['pk']) for l in r.context['extrato'])
            self.assertIn('periodo=tres_compras', r.context['extrato'].proxima_url)
            self.assertIn('visual=b', r.context['extrato'].proxima_url)
        self.assertEqual(len(linhas), 122)
        self.assertEqual(len(set(linhas)),122)

    def test_compra_especifica_e_proxima_compra_preservadas(self):
        antigas = self.compras_anteriores()
        r = self.consulta(periodo='compra', compra=antigas[2].pk, intervalo='entre_compras', visual='b')
        self.assertEqual(r.context['inicio'], antigas[2].estoque_entrada_realizada_em)
        self.assertEqual(r.context['fim'], antigas[1].estoque_entrada_realizada_em)
        self.assertEqual(r.context['visual'], 'b')
        self.assertIn('periodo=compra', r.context['compras'].proxima_url)


class PeriodosEvidenciaTests(HistoricoFixture, TestCase):
    venda_snapshot = evidencia.SaldosHistoricosTests.venda_snapshot
    devolucao_antiga = evidencia.SaldosHistoricosTests.devolucao_antiga

    def setUp(self):
        super().setUp()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=41)
        self.anterior = self.venda_snapshot(-1, 23, 6, 17)
        self.posterior = self.venda_snapshot(1, 58, 7, 51)
        self.ultima = self.venda_snapshot(2, 51, 3, 48)

    def test_a_b_mesmos_registros_e_saldos_com_lacuna_em_contexto_ampliado(self):
        self.devolucao_antiga(self.inicio+timedelta(hours=1))
        for dias in (2,3):
            instante = self.inicio-timedelta(days=dias)
            compra = Compra.objects.create(data_compra=instante.date(),
                estoque_entrada_realizada=True, estoque_entrada_realizada_em=instante)
            ItemCompra.objects.create(compra=compra, produto=self.produto, quantidade=1,
                unidade='CX', preco_unitario=0, valor_total=0)
        respostas = [self.client.get(self.url, {'periodo':'tres_compras', 'visual':v}) for v in ('a','b')]
        a,b = [[l for l in r.context['extrato'] if l['fonte'] != 'conferencia'] for r in respostas]
        self.assertEqual(sum(l['fonte']=='conferencia' for l in respostas[1].context['extrato']), 1)
        self.assertEqual(a,b)
        self.assertEqual(len([l for l in a if l['fonte']=='compra']),3)
        self.assertTrue(all(l['saldo'] is None for l in a if l['fonte']=='compra'))
        self.assertTrue(next(l for l in a if l['pk']==self.posterior.pk and l['fonte']=='venda')['saldo_pontual'])
        for r in respostas:
            self.assertEqual((r.context['esperado'],r.context['fisica']), (10,8))
