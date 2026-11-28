"""Separação por ID, custos aprovados e experiência; apenas PostgreSQL descartável."""
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from estoque.models import Cliente, Venda, ItemVenda
from estoque.services.inteligencia_clientes import resumo_balcao_clientes, resumir_clientes
from estoque.services.relatorios import resumo_vendas
from estoque.services.analise_classes_produtos import analise_consolidada
from estoque.services.investigacao_comercial import investigar_clientes_produto
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from inventario.tests import produto

INICIO, FIM, HOJE = date(2026, 11, 1), date(2026, 11, 27), date(2026, 11, 28)


class BalcaoClientesTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('balcao-fixture', '', 'fixture-only')
        cls.balcao = Cliente.objects.create(pk=1126, nome='Nome independente de pagamento')
        cls.comum = Cliente.objects.create(nome='Comprador individual')
        cls.p = produto(nome='Produto comercial de teste', unidade_compra='UN',
            unidade_venda_1='UN', vende_fracionado=False, categoria='Classe A')

    def nota(self, cliente, valor='100', custo='60', *, dia=INICIO, tipo='Pix', cancelada=False):
        venda = Venda.objects.create(cliente=cliente, data_venda=dia, total=D(valor),
            tipo_pagamento=tipo, cancelada=cancelada)
        ItemVenda.objects.create(venda=venda, produto=self.p, unidade='UN', quantidade=1,
            preco_unitario=D(valor), valor_total=D(valor),
            custo_unitario_snapshot=D(custo) if custo is not None else None,
            custo_total_snapshot=D(custo) if custo is not None else None,
            custo_unidade_snapshot='UN', custo_origem_snapshot='preco_compra_atual')
        return venda

    def quadro(self, inicio=INICIO, fim=FIM):
        resumo = resumo_vendas(inicio, fim, hoje=HOJE)
        return resumo_balcao_clientes(inicio, fim, resumo), resumo

    def test_id_independe_de_nome_e_condicao_comercial(self):
        self.nota(self.balcao, tipo='A prazo')
        self.nota(self.comum, valor='300', custo='200', tipo='À vista')
        self.nota(self.balcao, valor='9000', tipo='consumo_proprio')
        self.nota(self.comum, valor='8000', cancelada=True)
        for nome in ('Venda A Vista Sta Bárbara', 'Venda De Balcão', 'Outro nome'):
            Cliente.objects.filter(pk=1126).update(nome=nome)
            r, resumo = self.quadro()
            self.assertTrue(r['disponivel'])
            grupos = {g['chave']:g for g in r['grupos']}
            self.assertEqual((grupos['balcao']['faturamento'],grupos['balcao']['operacoes']), (100,1))
            self.assertEqual((grupos['cadastrados']['faturamento'],grupos['cadastrados']['operacoes']), (300,1))
            self.assertEqual(grupos['balcao']['participacao'], 25)
            self.assertEqual(r['total']['faturamento'], resumo['faturamento'])
            self.assertEqual([n['cliente_id'] for n in r['maiores']], [self.comum.pk])

    def test_duas_consultas_reutilizam_custos_sem_duplicar_notas(self):
        venda = self.nota(self.balcao)
        venda.total = 200
        venda.save(update_fields=['total'])
        ItemVenda.objects.create(venda=venda, produto=self.p, unidade='UN', quantidade=1,
            preco_unitario=100, valor_total=100, custo_unitario_snapshot=60,
            custo_total_snapshot=60,custo_unidade_snapshot='UN',custo_origem_snapshot='preco_compra_atual')
        resumo = resumo_vendas(INICIO, FIM, hoje=HOJE)
        with patch('estoque.services.relatorios_produtos_indicadores.validar_custos', side_effect=AssertionError('Não recalcular custos')):
            with CaptureQueriesContext(connection) as consultas:
                r = resumo_balcao_clientes(INICIO, FIM, resumo)
        self.assertEqual(len(consultas),2)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))
        self.assertEqual((r['balcao']['faturamento'],r['balcao']['operacoes'],r['balcao']['itens']), (200,1,2))
        self.assertEqual((r['balcao']['cmv'],r['balcao']['lucro'],r['balcao']['margem']), (120,80,40))

    def test_concilia_com_vendas_e_classes_sem_recalcular_regras(self):
        self.nota(self.balcao)
        self.nota(self.comum, valor='300', custo='150')
        r, resumo = self.quadro()
        classes = analise_consolidada(INICIO,FIM,hoje=HOJE)
        self.assertEqual(r['total']['faturamento'],classes['faturamento_notas'])
        self.assertEqual(r['total']['cmv'],classes['total']['custo'])
        self.assertEqual(r['total']['lucro'],classes['total']['lucro'])
        self.assertEqual(r['total']['margem'],resumo['rentabilidade']['margem'])
        for campo in ('faturamento','cmv','lucro','operacoes'):
            self.assertEqual(sum(g[campo] for g in r['grupos'] if g['chave']!='total'),r['total'][campo])

    def test_cobertura_parcial_nao_exibe_resultado_total(self):
        self.nota(self.balcao)
        self.nota(self.balcao,custo=None)
        r, _ = self.quadro()
        grupo=r['balcao']
        self.assertFalse(grupo['completa'])
        self.assertIsNone(grupo['cmv']);self.assertIsNone(grupo['lucro']);self.assertIsNone(grupo['margem'])
        self.assertEqual((grupo['cmv_conhecido'],grupo['receita_coberta'],grupo['lucro_coberto'],grupo['cobertura']), (60,100,40,50))
        html=get_template('estoque/relatorios/balcao_clientes.html').render(dict(origens_comerciais=r))
        self.assertIn('Somente parcela coberta',html)
        self.assertIn('custos validados em 1 de 2 itens',html)
        self.assertIn('não representam o resultado total',html)

    def test_periodo_vazio_e_filtros_nao_dependem_de_comparacao(self):
        self.nota(self.balcao, dia=date(2026,10,31))
        r, _=self.quadro()
        self.assertTrue(r['disponivel'])
        self.assertEqual((r['total']['faturamento'],r['total']['operacoes'],r['total']['cmv']), (0,0,0))
        self.assertIsNone(r['total']['margem']);self.assertIsNone(r['total']['participacao'])
        outubro, _=self.quadro(date(2026,10,1),date(2026,10,31))
        self.assertEqual(outubro['balcao']['faturamento'],100)
        self.client.force_login(self.user)
        response=self.client.get('/relatorios/vendas/',dict(periodo='personalizado',
            data_inicial='2026-11-01',data_final='2026-11-27'))
        self.assertContains(response,'Balcão × Clientes cadastrados')
        self.assertEqual(response.context['origens_comerciais']['total']['operacoes'],0)

    def test_sem_vinculo_e_outros_coletivos_nao_sao_clientes_individuais(self):
        outro=Cliente.objects.create(nome='Outro coletivo já confirmado')
        self.nota(None);self.nota(outro);self.nota(self.comum)
        with override_settings(RELATORIOS_CLIENTES_COLETIVOS_CONFIRMADOS=(outro.pk,)):
            r,_=self.quadro()
        grupos={g['chave']:g for g in r['grupos']}
        self.assertEqual(grupos['sem_cliente']['faturamento'],100)
        self.assertEqual(grupos['outros_coletivos']['faturamento'],100)
        self.assertEqual(grupos['cadastrados']['faturamento'],100)
        self.assertEqual([c['cliente_id'] for c in r['maiores']], [self.comum.pk])

    def test_ranking_faturamento_individual_diferente_de_crescimento(self):
        self.nota(self.balcao,valor='10000')
        self.nota(self.comum,valor='200')
        novo=Cliente.objects.create(nome='Comprador menor')
        self.nota(novo,valor='100')
        r,_=self.quadro()
        self.assertEqual([n['cliente_id'] for n in r['maiores']],[self.comum.pk,novo.pk])
        self.assertEqual([n['posicao'] for n in r['maiores']],[1,2])
        html=get_template('estoque/relatorios/balcao_clientes.html').render(dict(origens_comerciais=r))
        self.assertIn('Sem posição',html)
        self.assertIn('não é ranking de crescimento',html)

    def test_balcao_nao_vira_ausente_ou_ranking_de_crescimento(self):
        from estoque.tests_inteligencia_clientes import desempenho, linha
        for linhas, ant, atual in (([linha(1126,'anterior','100')],'100','0'),
                ([linha(1126,'atual','100')],'0','100')):
            r=resumir_clientes(linhas,desempenho(ant,atual))
            self.assertEqual((r['ausentes'],r['impactos'],r['aumentos']),([],[],[]))
            self.assertFalse(r['cadastro_1126_pendente'])

    def test_ausencia_no_produto_exclui_coletivo_mantendo_faturamento(self):
        self.nota(self.balcao,dia=date(2026,10,1))
        self.nota(self.comum,dia=date(2026,10,1))
        from estoque.tests_inteligencia_clientes import desempenho
        r=investigar_clientes_produto(desempenho('200','0'),self.p.pk)
        self.assertEqual(r['ausentes'],1)
        self.assertTrue(r['por_id'][1126]['coletivo'])
        self.assertEqual(r['resumo']['anterior'],200)

    def test_divergencia_impede_resultado_total_e_mudanca_entre_consultas(self):
        v=self.nota(self.balcao)
        ItemVenda.objects.filter(venda=v).update(valor_total=90)
        r,resumo=self.quadro()
        self.assertEqual(r['balcao']['divergencia'],-10)
        self.assertIsNone(r['balcao']['lucro'])
        resumo['faturamento']+=1
        self.assertFalse(resumo_balcao_clientes(INICIO,FIM,resumo)['disponivel'])
