"""Competência de entrega e consolidação, só no PostgreSQL descartável."""
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from locacoes.models import Locacao, EventoLocacao
from locacoes.consulta import resumo_financeiro_locacoes
from locacoes.tests_resumo_financeiro import DadosResumo
from estoque.models import Cliente, Venda, ItemVenda
from estoque.services.faturamento_negocio import consolidar_faturamento
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from inventario.tests import produto

INICIO, FIM = date(2026,10,1), date(2026,10,31)


class FaturamentoNegocioTests(DadosResumo, PostgreSQLIsoladoTests, TestCase):
    def setUp(self):
        super().setUp()
        relogio = patch('locacoes.consulta.timezone.localdate', return_value=date(2026,11,28))
        relogio.start()
        self.addCleanup(relogio.stop)

    def calcular(self, inicio=INICIO, fim=FIM, mercadorias=D('100.00')):
        return consolidar_faturamento(inicio,fim,mercadorias)

    def test_competencia_independe_do_estado_operacional_exceto_canceladas(self):
        for estado,_ in Locacao.STATUS_CHOICES:
            self.criar(total=10,status=estado)
        resultado=self.calcular()
        quantidade=len(Locacao.STATUS_CHOICES)-1
        self.assertEqual(resultado['locacoes'],dict(quantidade=quantidade,faturamento=D(10)*quantidade))
        self.assertEqual(resultado['total'],100+10*quantidade)

    def test_reserva_e_saida_vencidas_entram_sem_confirmacao_e_futuras_nao_entram(self):
        self.criar(status='saiu_para_entrega',total=90)
        self.criar(status='reservada',total=30)
        self.criar(total=80,data_entrega=date(2026,12,1))
        self.criar(status='entregue',total=100,data_entrega=date(2026,12,1))
        self.assertEqual(self.calcular()['locacoes']['faturamento'],120)
        self.assertEqual(self.calcular(date(2026,12,1),date(2026,12,31))['locacoes']['quantidade'],0)

    def test_data_de_hoje_inclusiva_sem_modificar_estados_ou_checklists(self):
        from locacoes.models import TarefaOperacionalLocacao, ConferenciaEntregaLocacao
        reserva=self.criar(status='reservada',total=25,data_entrega=date(2026,11,28))
        self.criar(status='reservada',total=75,data_entrega=date(2026,11,29))
        tarefas=TarefaOperacionalLocacao.objects.count()
        conferencias=ConferenciaEntregaLocacao.objects.count()
        self.assertEqual(self.calcular(date(2026,11,1),date(2026,11,30))['locacoes'],dict(quantidade=1,faturamento=D('25')))
        reserva.refresh_from_db()
        self.assertEqual(reserva.status,'reservada')
        self.assertEqual(TarefaOperacionalLocacao.objects.count(),tarefas)
        self.assertEqual(ConferenciaEntregaLocacao.objects.count(),conferencias)

    def test_competencia_entrega_nao_cadastro_evento_ou_pagamento(self):
        locacao=self.criar('2026-09-01',status='entregue',total=200,
            data_entrega=date(2026,10,31),data_evento=date(2026,11,1),
            data_prevista_devolucao=date(2026,11,2))
        self.pagar(locacao,40,'2026-11-02')
        r=self.calcular()
        self.assertEqual(r['locacoes']['faturamento'],200)
        self.assertEqual(self.calcular(date(2026,11,1),date(2026,11,27))['locacoes']['faturamento'],0)

    def test_criterio_novo_difere_explicitamente_do_contratado_por_cadastro(self):
        self.criar('2026-09-01',status='devolvida',total=200)
        self.criar('2026-10-01',total=500,data_entrega=date(2026,12,1))
        oficial=resumo_financeiro_locacoes(inicio=INICIO,fim=FIM)
        self.assertEqual(oficial['contratado'],500)
        self.assertEqual(self.calcular()['locacoes']['faturamento'],200)

    def test_devolucao_posterior_nao_desloca_ou_duplica_receita(self):
        locacao=self.criar(status='entregue',total=150)
        self.assertEqual(self.calcular()['locacoes']['faturamento'],150)
        Locacao.objects.filter(pk=locacao.pk).update(status='devolvida')
        evento=EventoLocacao.objects.create(locacao=locacao,tipo='devolucao',descricao='Fixture de devolução posterior')
        EventoLocacao.objects.filter(pk=evento.pk).update(criado_em=self.instante('2026-11-02'))
        self.assertEqual(self.calcular()['locacoes']['faturamento'],150)
        self.assertEqual(self.calcular(date(2026,11,1),date(2026,11,27))['locacoes']['faturamento'],0)

    def test_sinal_parcelas_e_divida_nao_multiplicam_ou_reduzem_contrato(self):
        locacao=self.criar(status='entregue',total=200)
        self.pagar(locacao,30,'2026-10-10')
        self.pagar(locacao,20,'2026-10-11')
        locacao.refresh_from_db()
        self.assertEqual(locacao.saldo_devedor,150)
        self.assertEqual(self.calcular()['locacoes'],dict(quantidade=1,faturamento=D('200.00')))

    def test_limites_inclusivos_periodo_personalizado_e_vazio(self):
        for dia in (date(2026,9,30),INICIO,FIM,date(2026,11,1)):
            self.criar(status='devolvida',total=25,data_entrega=dia)
        self.assertEqual(self.calcular()['locacoes']['faturamento'],50)
        self.assertEqual(self.calcular(INICIO,INICIO)['locacoes']['faturamento'],25)
        vazio=self.calcular(date(2025,1,1),date(2025,1,31),D('0.00'))
        self.assertEqual((vazio['locacoes']['quantidade'],vazio['total']),(0,0))

    def test_consulta_unica_select_sem_join_com_pagamentos_ou_escritas(self):
        for _ in range(20):
            self.criar(status='devolvida',total=10)
        with CaptureQueriesContext(connection) as queries:
            r=self.calcular()
        self.assertEqual(len(queries),1)
        self.assertTrue(queries[0]['sql'].lstrip().upper().startswith('SELECT'))
        self.assertNotIn('pagamentolocacao',queries[0]['sql'].lower())
        self.assertEqual(r['locacoes']['faturamento'],200)

    def test_view_mantem_balcao_cmv_e_rankings_restritos_a_mercadorias(self):
        admin=get_user_model().objects.create_superuser('negocio-admin','','fixture-only')
        self.client.force_login(admin)
        balcao=Cliente.objects.create(pk=1126,nome='Venda De Balcão')
        p=produto(unidade_compra='UN',unidade_venda_1='UN',vende_fracionado=False)
        venda=Venda.objects.create(cliente=balcao,data_venda=INICIO,total=100,tipo_pagamento='Pix')
        ItemVenda.objects.create(venda=venda,produto=p,quantidade=1,unidade='UN',preco_unitario=100,
            valor_total=100,custo_unitario_snapshot=60,custo_total_snapshot=60,
            custo_unidade_snapshot='UN',custo_origem_snapshot='preco_compra_atual')
        self.criar(status='entregue',total=200,cliente=balcao,tipo_pessoa='cliente')
        response=self.client.get('/relatorios/vendas/',dict(periodo='personalizado',
            data_inicial='2026-10-01',data_final='2026-10-31',modo='mensal'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['faturamento_negocio']['total'],300)
        self.assertEqual(response.context['resumo']['faturamento'],100)
        self.assertEqual(response.context['resumo']['rentabilidade']['cmv'],60)
        self.assertEqual(response.context['resumo']['rentabilidade']['lucro'],40)
        self.assertEqual(response.context['origens_comerciais']['balcao']['faturamento'],100)
        self.assertEqual(response.context['origens_comerciais']['balcao']['operacoes'],1)
        self.assertEqual(response.context['origens_comerciais']['maiores'],[])
        self.assertContains(response,'CMV das mercadorias')
        self.assertContains(response,'Margem bruta das mercadorias')
        self.assertContains(response,'Faturamento total')
        self.assertNotContains(response,'Margem total')
        self.assertContains(response,'data_inicial=2026-10-01')
