"""Monthly metrics and conservative forecast, using disposable fixtures only."""
from datetime import date
from decimal import Decimal as D
from django.test import SimpleTestCase, TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from .analise_evolucao import analisar_historico
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests
from .tests_relatorios_cobertura import documentados


class IndicadoresMensaisTests(SimpleTestCase):
    def analisar(self, dias, inicio, fim, hoje, **kwargs):
        return analisar_historico(dias,inicio,fim,hoje,'mensal',**kwargs)['itens']

    def test_mes_completo_ticket_e_media_sem_previsao(self):
        a,b=date(2026,10,1),date(2026,10,31)
        dias=documentados(a,b,{a:'71692.85'})
        dias[0]['quantidade']=442  # 442 + 30 = 472 commercial documents
        i=self.analisar(dias,a,b,date(2026,11,20))[0]
        m=i['indicadores']
        self.assertEqual(i['valor'],D('71692.85'))
        self.assertEqual(m['quantidade'],472)
        self.assertEqual(m['ticket_medio'],D('151.89'))
        self.assertEqual((m['media_diaria'],m['dias_media']), (D('2312.67'),31))
        self.assertEqual(m['situacao'],'Completo')
        self.assertIsNone(m['previsao'])

    def test_inicial_incompleto_usa_24_dias_e_preserva_valor(self):
        primeiro,b=date(2026,9,7),date(2026,9,30)
        dias=documentados(primeiro,b,{primeiro:'55699.17'})
        dias[0]['quantidade']=379
        i=self.analisar(dias,date(2026,9,1),b,date(2026,11,20),inicio_registros=primeiro)[0]
        m=i['indicadores']
        self.assertEqual(m['situacao'],'Parcial — registros desde 07/09')
        self.assertEqual(m['media_inicio'],'2026-09-07')
        self.assertEqual(m['dias_media'],24)
        self.assertEqual(m['ticket_medio'],D('138.56'))
        self.assertEqual(m['media_diaria'],D('2320.80'))
        self.assertIsNone(m['previsao'])

    def test_ritmo_dias_encerrados_hoje_separado_e_sem_duplicacao(self):
        hoje=date(2026,11,20)
        dias=documentados(date(2026,10,1),hoje)
        for d in dias:d['faturamento']=D(100)
        dias[-1]['faturamento']=D(40)
        i=self.analisar(dias,date(2026,11,1),hoje,hoje)[0]
        m=i['indicadores'];p=m['previsao']
        self.assertEqual(i['valor'],1940)
        self.assertEqual(m['ticket_medio'],97)
        self.assertEqual((m['media_diaria'],m['dias_media']),(100,19))
        self.assertEqual(m['media_fim'],'2026-11-19')
        self.assertEqual(m['situacao'],'Parcial em andamento')
        self.assertEqual((p['encerrados'],p['hoje'],p['dias_restantes']),(1900,40,10))
        self.assertEqual(p['valor'],2940)  # 1900 + 40 + 100*10, never adds today twice
        self.assertEqual(p['anterior'],3100)
        self.assertEqual(p['diferenca'],-160)
        self.assertEqual(p['variacao'],D('-5.16'))

    def test_hoje_sem_registros_nao_virou_dia_encerrado_ou_zero_certificado(self):
        hoje=date(2026,11,20)
        dias=documentados(date(2026,10,1),date(2026,11,19))
        for d in dias:d['faturamento']=D(100)
        m=self.analisar(dias,date(2026,11,1),hoje,hoje)[0]['indicadores']
        self.assertEqual(m['dias_media'],19)
        self.assertIsNone(m['previsao']['hoje'])
        self.assertEqual(m['previsao']['valor'],2900)

    def test_sem_dias_encerrados_lacunas_totais_nulos_ou_inicio_incompleto(self):
        hoje=date(2026,11,1)
        m=self.analisar(documentados(hoje,hoje,{hoje:'50'}),hoje,hoje,hoje)[0]['indicadores']
        self.assertIsNone(m['media_diaria'])
        self.assertIsNone(m['previsao']['valor'])
        hoje=date(2026,11,20)
        dados=documentados(date(2026,11,7),hoje)
        m=self.analisar(dados,date(2026,11,1),hoje,hoje,inicio_registros=date(2026,11,7))[0]['indicadores']
        self.assertEqual(m['dias_media'],13)
        self.assertEqual(m['situacao'],'Parcial — registros desde 07/11')
        self.assertIsNone(m['previsao']['valor'])
        del dados[3]
        m=self.analisar(dados,date(2026,11,1),hoje,hoje)[0]['indicadores']
        self.assertIsNone(m['media_diaria'])
        self.assertIsNone(m['previsao']['valor'])
        dados=documentados(date(2026,11,1),hoje)
        dados[-1]['sem_valor']=1
        m=self.analisar(dados,date(2026,11,1),hoje,hoje)[0]['indicadores']
        self.assertIsNone(m['ticket_medio'])
        self.assertIsNone(m['previsao']['valor'])

    def test_referencia_invalida_zero_e_virada_ano(self):
        hoje=date(2027,1,3)
        dados=documentados(date(2026,12,1),hoje,{hoje:'10',date(2027,1,1):'100'})
        p=self.analisar(dados,date(2027,1,1),hoje,hoje)[0]['indicadores']['previsao']
        self.assertEqual(p['valor'],1510)  # 110 + (100/2)*28
        self.assertEqual(p['diferenca'],1510)
        self.assertIsNone(p['variacao'])
        self.assertEqual(p['anterior_inicio'],'2026-12-01')
        del dados[2]
        p=self.analisar(dados,date(2027,1,1),hoje,hoje)[0]['indicadores']['previsao']
        self.assertEqual(p['valor'],1510)
        self.assertIsNone(p['diferenca'])
        self.assertIn('Períodos não equivalentes',p['motivo_comparacao'])

    def test_precisao_sem_arredondamento_intermediario_e_fevereiro_bissexto(self):
        hoje=date(2024,2,4)
        dados=documentados(date(2024,2,1),hoje,{date(2024,2,1):'1'})
        m=self.analisar(dados,date(2024,2,1),hoje,hoje)[0]['indicadores']
        self.assertEqual(m['media_diaria'],D('.33'))
        self.assertEqual(m['previsao']['dias_restantes'],25)
        self.assertEqual(m['previsao']['valor'],D('9.33'))  # 1+(1/3)*25, not 1+.33*25

    def test_recorte_historico_do_mes_corrente_e_demais_modos_sem_previsao(self):
        dados=documentados(date(2026,11,1),date(2026,11,19))
        self.assertIsNone(self.analisar(dados,date(2026,11,1),date(2026,11,19),date(2026,11,20))[0]['indicadores']['previsao'])
        for modo in ('diario','semanal'):
            r=analisar_historico(dados,date(2026,11,1),date(2026,11,19),date(2026,11,20),modo)
            self.assertTrue(all('indicadores' not in i for i in r['itens']))
        self.assertIsNone(self.analisar([],date(2026,11,1),date(2026,11,19),date(2026,11,20))[0]['indicadores'])


class IndicadoresIntegradosTests(PostgreSQLIsoladoTests,TestCase):
    def test_ultimo_dia_busca_mes_anterior_inteiro_mesmo_sem_dia_equivalente(self):
        hoje=date(2026,3,31)
        Venda.objects.bulk_create([Venda(data_venda=d['data_venda'],total=100,tipo_pagamento='pix')
            for d in documentados(date(2026,2,1),hoje)])
        r=resumo_vendas(date(2026,3,1),hoje,modo='mensal',hoje=hoje)
        i=r['analise_evolucao']['itens'][0]
        self.assertIsNone(i['comparacao'])  # February has no day 31
        p=i['indicadores']['previsao']
        self.assertEqual((p['valor'],p['anterior'],p['diferenca']),(3100,2800,300))
        self.assertEqual(p['dias_restantes'],0)

    def test_indicadores_excluem_canceladas_consumo_e_reutilizam_agregado(self):
        hoje=date(2026,11,20)
        Venda.objects.bulk_create([Venda(data_venda=d['data_venda'],total=100,tipo_pagamento='pix')
            for d in documentados(date(2026,10,1),hoje)])
        Venda.objects.create(data_venda=hoje,total=900,tipo_pagamento='pix',cancelada=True)
        Venda.objects.create(data_venda=hoje,total=700,tipo_pagamento='consumo_proprio')
        with CaptureQueriesContext(connection) as queries:
            r=resumo_vendas(date(2026,11,1),hoje,modo='mensal',equivalente=True,hoje=hoje)
        self.assertEqual(len(queries),5)
        self.assertTrue(all(q['sql'].lstrip().startswith('SELECT') for q in queries))
        self.assertEqual((r['faturamento'],r['quantidade'],r['ticket_medio'],r['consumo']['valor']),(2000,20,100,700))
        anterior,atual=r['grafico_evolucao']['itens']
        self.assertEqual(atual['indicadores']['ticket_medio'],r['ticket_medio'])
        self.assertEqual(atual['indicadores']['previsao']['valor'],3000)
        self.assertIsNone(anterior['indicadores']['previsao'])
        self.assertEqual(anterior['indicadores']['dias_media'],20)
