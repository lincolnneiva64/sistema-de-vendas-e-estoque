"""Theoretical equivalents are estimates, never financial totals."""
from datetime import date
from decimal import Decimal as D
from django.test import SimpleTestCase
from .analise_evolucao import analisar_historico
from .tests_relatorios_cobertura import documentados


class FantasmasMensaisTests(SimpleTestCase):
    def test_tres_meses_realizados_preservados_e_estimativas_distintas(self):
        primeiro,hoje=date(2026,9,7),date(2026,11,20)
        dias=documentados(primeiro,date(2026,11,19),{
            primeiro:'55699.17',date(2026,10,1):'71692.85',date(2026,11,1):'43206.21'})
        r=analisar_historico(dias,date(2026,9,1),hoje,hoje,'mensal',inicio_registros=primeiro)
        setembro,outubro,novembro=r['itens']
        self.assertEqual([i['valor'] for i in r['itens']],[D('55699.17'),D('71692.85'),D('43206.21')])
        eq=setembro['indicadores']['equivalente_teorico']
        self.assertEqual((eq['rotulo'],eq['valor'],eq['dias_base'],eq['dias_calendario']),
            ('Equivalente teórico',D('69623.96'),24,30))
        self.assertIsNone(setembro['indicadores']['previsao'])
        self.assertIsNone(outubro['indicadores']['equivalente_teorico'])
        self.assertIsNone(outubro['indicadores']['previsao'])
        self.assertIsNone(novembro['indicadores']['equivalente_teorico'])
        self.assertEqual(novembro['indicadores']['previsao']['valor'],D('65946.32'))
        self.assertIsNone(outubro['variacao'])

    def test_omitir_lacunas_totais_ausentes_e_recortes_incompletos(self):
        a,b=date(2026,9,7),date(2026,9,30)
        dias=documentados(a,b,{a:'100'})
        for dados,inicio,fim in (
            ([d for d in dias if d['data_venda']!=date(2026,9,15)],date(2026,9,1),b),
            (dias,date(2026,9,10),b), (dias,date(2026,9,1),date(2026,9,20))):
            i=analisar_historico(dados,inicio,fim,date(2026,11,20),'mensal',inicio_registros=a)['itens'][0]
            self.assertIsNone(i['indicadores']['equivalente_teorico'])
        dias[-1]['sem_valor']=1
        i=analisar_historico(dias,date(2026,9,1),b,date(2026,11,20),'mensal',inicio_registros=a)['itens'][0]
        self.assertIsNone(i['indicadores']['equivalente_teorico'])

    def test_zero_calendario_bissexto_e_virada_ano(self):
        for a,b,hoje in ((date(2024,2,10),date(2024,2,29),date(2024,3,1)),
                        (date(2026,12,10),date(2026,12,31),date(2027,1,1))):
            dias=documentados(a,b)
            eq=analisar_historico(dias,a.replace(day=1),b,hoje,'mensal',inicio_registros=a)['itens'][0]['indicadores']['equivalente_teorico']
            self.assertEqual(eq['valor'],0)
            self.assertEqual(eq['dias_calendario'],b.day)
            self.assertEqual(eq['dias_base'],(b-a).days+1)

    def test_nao_extrapola_mes_ainda_em_andamento_ou_sem_limite_inicial(self):
        a,b=date(2026,9,7),date(2026,9,30)
        dias=documentados(a,b,{a:'100'})
        for hoje,limite in ((date(2026,9,30),a),(date(2026,11,20),None)):
            i=analisar_historico(dias,date(2026,9,1),b,hoje,'mensal',inicio_registros=limite)['itens'][0]
            self.assertIsNone(i['indicadores']['equivalente_teorico'])
