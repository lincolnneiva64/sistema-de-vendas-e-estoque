"""Initial quantity card: recorded commercial units, never inferred from price."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventario.tests import produto
from estoque.models import ItemVenda, Venda
from estoque.services.relatorios_produtos import historico_produto, ranking_produtos
from estoque.templatetags.relatorios import quantidade_br
from estoque.tests_relatorios import PostgreSQLIsoladoTests


class QuantidadeInicialTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('quantidade-card-admin', is_staff=True, is_superuser=True)
        cls.p = produto(nome='Skol de teste 12/350Ml', quantidade=0)

    def item(self, quantidade, unidade, *, dia=date(2026,10,15), cancelada=False, pagamento='Pix', produto=None):
        v = Venda.objects.create(data_venda=dia, total=D('100'), cancelada=cancelada, tipo_pagamento=pagamento)
        return ItemVenda.objects.create(venda=v, produto=produto or self.p, quantidade=D(quantidade), unidade=unidade,
                                        preco_unitario=D('.01'), valor_total=D('100'))

    def resumo(self, **kwargs):
        return historico_produto(self.p.pk, date(2026,10,1), date(2026,10,31), hoje=date(2026,11,20), **kwargs)

    def pagina(self, **kwargs):
        self.client.force_login(self.admin)
        return self.client.get(reverse('estoque:relatorios_produto', args=[self.p.pk]), {
            'periodo':'personalizado','data_inicial':'2026-10-01','data_final':'2026-10-31', **kwargs})

    def test_quantidade_real_periodo_produto_cancelamento_e_consumo(self):
        self.item('3.125', ' dz ')
        self.item('2', 'DZ')
        self.item('999', 'DZ', cancelada=True)
        self.item('999', 'DZ', pagamento='Consumo próprio')
        self.item('999', 'DZ', dia=date(2026,11,1))
        self.item('999', 'DZ', produto=produto(nome='Skol de teste semelhante', quantidade=0))
        r = self.resumo()
        self.assertEqual(r['quantidades_vendidas'], [{'unidade_comercial':'DZ','quantidade':D('5.125')}])
        ranking = ranking_produtos(date(2026,10,1), date(2026,10,31), produto_id=self.p.pk)['linhas'][0]
        self.assertEqual(r['quantidades_vendidas'][0]['quantidade'], ranking['quantidade'])
        self.assertEqual((r['faturamento'], r['quantidade']), (200, 2))
        self.assertContains(self.pagina(), '5,125 DZ')

    def test_unidades_separadas_mesmo_com_grafico_em_uma_unidade(self):
        self.item('5', 'DZ'); self.item('2.5', 'PCT'); self.item('.333', 'KG')
        esperado = [{'unidade_comercial':'DZ','quantidade':D('5')},
                    {'unidade_comercial':'KG','quantidade':D('.333')},
                    {'unidade_comercial':'PCT','quantidade':D('2.5')}]
        self.assertEqual(self.resumo()['quantidades_vendidas'], esperado)
        r = self.resumo(medida='quantidade', unidade='DZ')
        self.assertEqual(r['quantidades_vendidas'], esperado)
        self.assertEqual(r['faturamento'], 100)  # Existing chart-unit financial scope is preserved.
        self.assertEqual(r['evolucao'][0]['faturamento'], 5)
        pagina = self.pagina(medida='quantidade', unidade='DZ')
        for valor in ('5 DZ','2,5 PCT','0,333 KG'):
            self.assertContains(pagina, valor)

    def test_cartao_nao_herda_alcance_do_grafico_e_preserva_selecao_id(self):
        self.item('2', 'DZ')
        self.item('7', 'DZ', dia=date(2026,9,1))
        r = self.resumo(historico=(date(2026,9,1),date(2026,10,31)), modo='mensal')
        self.assertEqual(r['quantidades_vendidas'][0]['quantidade'], 2)
        pagina = self.pagina(modo='mensal', alcance='12', q='produto diferente')
        self.assertEqual(pagina.context['produto'].pk, self.p.pk)
        self.assertEqual(pagina.context['resumo']['quantidades_vendidas'][0]['quantidade'], 2)
        html = pagina.content.decode().split('<dl class="produto-indicadores">',1)[1].split('</dl>',1)[0]
        self.assertLess(html.index('Faturamento dos itens'), html.index('Quantidade vendida'))
        self.assertLess(html.index('Quantidade vendida'), html.index('Vendas distintas'))

    def test_formatacao_brasileira_sem_criar_casas_decimais(self):
        for entrada, saida in [('1234.500','1.234,5'),('.001','0,001'),('1000.000','1.000'),('100','100'),('0','0')]:
            self.assertEqual(quantidade_br(D(entrada)), saida)

    def test_sem_vendas_ou_unidade_ausente_nao_inventa_unidade_fisica(self):
        self.assertContains(self.pagina(), 'Sem vendas no período')
        self.item('5', '')
        self.assertEqual(self.resumo()['quantidades_vendidas'], [{'unidade_comercial':'','quantidade':None}])
        self.assertContains(self.pagina(), 'Unidade não informada · quantidade não comparável')
