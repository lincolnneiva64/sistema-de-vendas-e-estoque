"""Product zeros depend on commercial coverage, never on stock movements."""
from datetime import date, timedelta
from decimal import Decimal as D
from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from inventario.tests import produto
from estoque.models import Venda, ItemVenda
from estoque.services.relatorios_produtos import historico_produto
from estoque.tests_relatorios import PostgreSQLIsoladoTests


class CoberturaProdutosTests(PostgreSQLIsoladoTests, TestCase):
    def setUp(self):
        self.p = produto(nome='Produto raro', quantidade=0)
        self.outro = produto(nome='Produto semelhante', quantidade=0)
        self.hoje = date(2026, 11, 20)
        self.inicio = date(2026, 9, 7)
        self.notas = {}
        d = self.inicio
        while d <= self.hoje:
            self.notas[d] = Venda.objects.create(data_venda=d, total=100, tipo_pagamento='pix')
            d += timedelta(days=1)

    def item(self, dia, q, valor, unidade='DZ', p=None):
        return ItemVenda.objects.create(venda=self.notas[dia], produto=p or self.p,
            unidade=unidade, quantidade=D(q), valor_total=D(valor), preco_unitario=1)

    def resumo(self, **kwargs):
        return historico_produto(self.p.pk, date(2026, 10, 1), date(2026, 10, 31),
            hoje=self.hoje, modo='mensal', historico=kwargs.pop('historico', (date(2026, 9, 1), self.hoje)), **kwargs)

    def test_tres_meses_zero_em_dia_comercial_e_hoje_fora_da_media(self):
        self.item(date(2026, 9, 10), '27', '270')
        self.item(date(2026, 10, 10), '65.5', '655')
        self.item(date(2026, 11, 10), '20', '200')
        self.item(self.hoje, '2', '20')
        self.item(date(2026, 11, 11), '999', '999', unidade='UN')
        r = self.resumo(medida='quantidade', unidade='DZ')
        s, o, n = r['analise_evolucao']['itens']
        self.assertEqual([x['valor'] for x in (s,o,n)], [D(27),D('65.5'),D(22)])
        self.assertEqual(s['indicadores']['media_diaria'], D('1.125'))
        self.assertEqual(s['indicadores']['equivalente_teorico']['valor'], D('33.750'))
        self.assertEqual(o['indicadores']['dias_media'],31)
        self.assertEqual(o['indicadores']['media_diaria'],D('2.113'))
        self.assertIsNone(o['indicadores']['previsao'])
        self.assertEqual(n['indicadores']['dias_media'],19)
        self.assertEqual(n['indicadores']['previsao']['valor'],D('32.526'))
        self.assertEqual(r['faturamento'],655)

    def test_metricas_independentes_e_produto_sem_vendas_tem_zero_comprovado(self):
        self.item(date(2026,10,10),'2','50')
        for medida, esperado in [('faturamento', D('1.61')), ('vendas', D('.03'))]:
            o=self.resumo(medida=medida)['analise_evolucao']['itens'][1]
            self.assertEqual(o['indicadores']['media_diaria'],esperado)
        n=self.resumo(medida='quantidade',unidade='DZ')['analise_evolucao']['itens'][2]
        self.assertEqual(n['valor'],0)
        self.assertEqual(n['indicadores']['previsao']['valor'],0)

    def test_lacuna_consumo_e_cancelamento_nao_certificam_dia(self):
        self.item(date(2026,11,10),'20','200')
        for campo, valor in [('data_venda',date(2025,1,1)),('cancelada',True),('tipo_pagamento','consumo_proprio')]:
            with self.subTest(campo=campo):
                Venda.objects.filter(pk=self.notas[date(2026,11,5)].pk).update(**{campo:valor})
                n=self.resumo()['analise_evolucao']['itens'][2]
                self.assertIsNone(n['indicadores']['media_diaria'])
                self.assertIsNone(n['indicadores']['previsao']['valor'])
                Venda.objects.filter(pk=self.notas[date(2026,11,5)].pk).update(data_venda=date(2026,11,5),total=100,cancelada=False,tipo_pagamento='pix')

    def test_alcances_e_produtos_sem_consultas_por_barra(self):
        self.item(date(2026,10,10),'2','50')
        self.item(date(2026,10,11),'7','70',p=self.outro)
        contagens=[]
        for meses in (3,6,12,24):
            ano, mes = divmod(2026*12+10-(meses-1), 12)
            inicio=date(ano,mes+1,1)
            with CaptureQueriesContext(connection) as sql:
                r=self.resumo(historico=(inicio,self.hoje),medida='quantidade',unidade='DZ')
            contagens.append(len(sql))
            self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','(SELECT')) for q in sql))
            outubro=next(x for x in r['analise_evolucao']['itens'] if x['inicio']=='2026-10-01')
            self.assertEqual(outubro['valor'],2)
            self.assertEqual(outubro['indicadores']['dias_media'],31)
        self.assertEqual(len(set(contagens)),1)
        self.assertLessEqual(contagens[0],9)
