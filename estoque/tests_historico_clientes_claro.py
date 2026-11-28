from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.template.loader import get_template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from estoque.models import Cliente, Venda
from estoque.services.inteligencia_clientes import historico_ausencias


class HistoricoClientesClaroTests(TestCase):
    def setUp(self):
        self.cliente=Cliente.objects.create(nome='Cliente teste')
        self.periodo=dict(inicio=date(2026,11,1),fim=date(2026,11,27),referencia_inicio=date(2026,10,1))

    def nota(self,data,**extras):
        return Venda.objects.create(cliente=self.cliente,data_venda=data,total=100,**extras)

    def historico(self, notas_atual=0, dias_atual=0):
        c=dict(id=self.cliente.pk,nome=self.cliente.nome,anterior=Decimal('100'),
               notas_atual=notas_atual,dias_atual=dias_atual)
        r=dict(impactos=[c],ausentes=[] if notas_atual else [c])
        with patch('estoque.services.inteligencia_clientes.timezone.localdate',return_value=date(2026,11,28)):
            with self.assertNumQueries(1):historico_ausencias(r,self.periodo)
        return c['historico'],r

    def test_dias_distintos_meses_consecutivos_e_classificacao_preservada(self):
        for data in (date(2026,9,22),date(2026,9,22),date(2026,10,1),date(2026,10,10),date(2026,10,23)):
            self.nota(data)
        self.nota(date(2026,8,1),cancelada=True)
        self.nota(date(2026,8,2),tipo_pagamento='consumo_proprio')
        h,r=self.historico()
        self.assertEqual((h['dias'],h['meses'],h['chave']),(4,2,'recorrentes'))
        texto=' '.join(h['explicacao'])
        self.assertIn('4 dias diferentes',texto)
        self.assertEqual((h['primeira'],h['ultima']),(date(2026,9,22),date(2026,10,23)))
        self.assertIn('setembro e outubro de 2026',texto)
        self.assertEqual(h['leitura_frequencia']['chave'],'mensais')
        self.assertNotIn('não bastam para concluir regularidade mensal',texto)
        self.assertIn('novembro de 2026, até 28/11/2026',texto)
        self.assertEqual(r['historico_ausencias']['contagem']['recorrentes'],1)

    def test_meses_nao_consecutivos(self):
        for d in (date(2026,8,1),date(2026,8,10),date(2026,10,1),date(2026,10,10)):self.nota(d)
        h,_=self.historico()
        self.assertIn('agosto e outubro de 2026',' '.join(h['explicacao']))
        self.assertEqual(h['leitura_frequencia']['chave'],'ocasionais')
        self.assertNotIn('meses consecutivos',' '.join(h['explicacao']))
        self.assertEqual(h['chave'],'recorrentes')

    def test_meses_consecutivos_na_virada_do_ano(self):
        self.periodo.update(inicio=date(2026,2,1),fim=date(2026,2,20),referencia_inicio=date(2026,1,1))
        self.nota(date(2025,12,10));self.nota(date(2026,1,10))
        h,_=self.historico()
        self.assertIn('dezembro de 2025 e janeiro de 2026',' '.join(h['explicacao']))
        self.assertEqual(h['leitura_frequencia']['chave'],'mensais')

    def test_historico_insuficiente_e_compra_posterior_inclusive_hoje(self):
        self.nota(date(2026,10,23));self.nota(date(2026,11,28));self.nota(date(2026,12,1))
        h,_=self.historico()
        self.assertEqual((h['dias'],h['meses'],h['chave']),(1,1,'insuficientes'))
        texto=' '.join(h['explicacao'])
        self.assertIn('1 dia, em outubro de 2026',texto)
        self.assertEqual(h['primeira'],date(2026,10,23))
        self.assertIn('de 01/11/2026 a 27/11/2026',texto)
        self.assertIn('Comprou novamente em 28/11/2026',texto)
        self.assertNotIn('Não foram identificadas novas compras em novembro',texto)
        self.assertNotIn('01/12/2026',texto)

    def test_recorte_parcial_nao_afirma_ausencia_no_mes_inteiro(self):
        self.periodo.update(inicio=date(2026,11,12),fim=date(2026,11,20))
        self.nota(date(2026,11,1))
        h,_=self.historico()
        texto=' '.join(h['explicacao'])
        self.assertIn('de 12/11/2026 a 20/11/2026',texto)
        self.assertNotIn('Não foram identificadas novas compras em novembro',texto)

    def test_reducao_tem_historico_sem_virar_ausencia(self):
        self.nota(date(2026,10,10));self.nota(date(2026,11,10))
        h,r=self.historico(notas_atual=2,dias_atual=1)
        self.assertIn('2 dias diferentes',' '.join(h['explicacao']))
        self.assertNotIn('historico_ausencias',r)


class AvisosDividasClientesTests(TestCase):
    def renderizar(self,saldo,vencido,vencidas):
        c=dict(situacao_financeira='Situação teste',financeiro=dict(saldo=Decimal(saldo),
            vencido=Decimal(vencido),vencidas=vencidas,maior_atraso=29 if vencidas else 0,parciais=1),
            prioridade_acompanhamento='Acompanhamento')
        return get_template('estoque/relatorios/cliente_situacao_financeira.html').render(dict(cadastro=c))

    def test_vencido_aviso_e_apenas_dois_valores_em_destaque_no_detalhe(self):
        html=self.renderizar('500','400.90',1)
        self.assertIn('Contas vencidas · R$ 400,90 em atraso',html)
        self.assertEqual(html.count('class="cliente-divida-vencida"'),3) # Summary + dois valores.
        self.assertIn('<dd>R$ 500,00</dd>',html)
        self.assertIn('<dd>1</dd>',html)

    def test_abertas_nao_vencidas_neutras(self):
        html=self.renderizar('90','0',0)
        self.assertIn('Contas em aberto · R$ 90,00 · sem vencidas',html)
        self.assertNotIn('cliente-divida-vencida',html)
        self.assertIn('<details',html)

    def test_sem_contas_nao_renderiza_financeiro(self):
        self.assertEqual(self.renderizar('0','0',0).strip(),'')
