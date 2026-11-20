"""Coverage is documentary evidence, never an inferred system go-live date."""
from datetime import date, timedelta
from decimal import Decimal as D
from django.test import SimpleTestCase, TestCase
from .analise_evolucao import analisar_historico
from .models import Venda
from .services.relatorios import resumo_vendas
from .tests_relatorios import PostgreSQLIsoladoTests


def documentados(inicio, fim, valores=None):
    valores = valores or {}
    return [dict(data_venda=inicio+timedelta(days=n),
                 faturamento=D(valores.get(inicio+timedelta(days=n), '0')), quantidade=1, sem_valor=0)
            for n in range((fim-inicio).days+1)]


def completar_registros(inicio, fim):
    """Explicit zero documents in disposable fixtures, never inferred coverage."""
    existentes = set(Venda.objects.filter(cancelada=False).exclude(tipo_pagamento='consumo_proprio')
                     .values_list('data_venda', flat=True))
    Venda.objects.bulk_create([Venda(data_venda=d['data_venda'], total=0, tipo_pagamento='pix')
        for d in documentados(inicio, fim) if d['data_venda'] not in existentes])


class CoberturaMensalTests(SimpleTestCase):
    def test_inicio_meio_mes_preserva_totais_e_recusa_crescimento_artificial(self):
        primeiro, hoje = date(2026, 9, 7), date(2026, 11, 20)
        dias = documentados(primeiro, hoje, {primeiro:'55699.17', date(2026,10,1):'71692.85', hoje:'43206.21'})
        r = analisar_historico(dias, date(2026,9,1), hoje, hoje, 'mensal', inicio_registros=primeiro)
        setembro, outubro, novembro = r['itens']
        self.assertEqual([i['valor'] for i in r['itens']], [D('55699.17'), D('71692.85'), D('43206.21')])
        self.assertEqual(setembro['status'], 'Parcial — registros desde 07/09')
        self.assertTrue(setembro['parcial'])
        self.assertIsNone(outubro['variacao'])
        self.assertIsNone(outubro['diferenca'])
        self.assertIn('Períodos não equivalentes', outubro['motivo'])
        self.assertEqual(novembro['comparacao']['anterior_inicio'], '2026-10-01')
        self.assertEqual(novembro['comparacao']['anterior_fim'], '2026-10-20')
        self.assertEqual(r['quantidade_completos'], 1)

    def test_equivalencia_personalizada_com_dois_intervalos_documentados(self):
        primeiro, fim = date(2026,9,7), date(2026,10,30)
        dias = documentados(primeiro, fim, {primeiro:'100', date(2026,10,7):'120'})
        i = analisar_historico(dias, date(2026,10,7), fim, date(2026,11,1), 'mensal', inicio_registros=primeiro)['itens'][0]
        self.assertEqual(i['variacao'], 20)
        self.assertEqual(i['comparacao']['inicio'], '2026-10-07')
        self.assertEqual(i['comparacao']['fim'], '2026-10-30')
        self.assertEqual(i['comparacao']['anterior_inicio'], '2026-09-07')
        self.assertEqual(i['comparacao']['anterior_fim'], '2026-09-30')
        self.assertEqual(i['comparacao']['rotulo'], 'Parcial equivalente')

    def test_nao_infere_cobertura_de_primeira_venda_nem_de_extremidades(self):
        dados = documentados(date(2026,9,1), date(2026,10,31))
        dados = [d for d in dados if d['data_venda'] != date(2026,9,15)]
        i = analisar_historico(dados, date(2026,10,1), date(2026,10,31), date(2026,11,1),
                              'mensal', inicio_registros=date(2020,1,1))['itens'][0]
        self.assertIsNone(i['variacao'])
        self.assertIn('cobertura diária da referência', i['motivo'])

    def test_meses_completos_virada_ano_e_interanual(self):
        dados = documentados(date(2025,12,1), date(2026,1,31),
                             {date(2025,12,1):'100',date(2026,1,1):'120'})
        i = analisar_historico(dados,date(2026,1,1),date(2026,1,31),date(2026,2,1),'mensal',
                              inicio_registros=date(2025,12,1))['itens'][0]
        self.assertEqual(i['variacao'], 20)
        self.assertFalse(i['parcial'])
        sem = analisar_historico(dados,date(2026,1,1),date(2026,1,31),date(2026,2,1),'mensal',
            comparacao='ano_anterior', inicio_registros=date(2025,12,1))['itens'][0]
        self.assertIsNone(sem['variacao'])
        self.assertIn('Sem histórico', sem['motivo'])
        anterior = documentados(date(2025,1,1),date(2025,1,31),{date(2025,1,1):'80'})
        com = analisar_historico(anterior+dados,date(2026,1,1),date(2026,1,31),date(2026,2,1),'mensal',
            comparacao='ano_anterior', inicio_registros=date(2025,1,1))['itens'][0]
        self.assertEqual(com['variacao'], 50)

    def test_mes_corrente_sem_registro_hoje_nao_presume_zero(self):
        dados = documentados(date(2026,10,1), date(2026,11,19))
        i = analisar_historico(dados,date(2026,11,1),date(2026,11,20),date(2026,11,20),'mensal',
            inicio_registros=date(2026,10,1))['itens'][0]
        self.assertEqual(i['valor'], 0)
        self.assertTrue(i['parcial'])
        self.assertIsNone(i['variacao'])
        vazio = analisar_historico([],date(2026,11,1),date(2026,11,20),date(2026,11,20),'mensal')['itens'][0]
        self.assertIsNone(vazio['valor'])


class CoberturaIntegradaTests(PostgreSQLIsoladoTests, TestCase):
    def test_limite_global_fora_do_filtro_e_mesmo_avaliador_nos_dois_alcances(self):
        primeiro = date(2026,9,7)
        for d in documentados(primeiro,date(2026,10,31),{primeiro:'100',date(2026,10,1):'120'}):
            Venda.objects.create(data_venda=d['data_venda'],total=d['faturamento'],tipo_pagamento='pix')
        for historico in (None,(date(2026,9,1),date(2026,10,31))):
            r = resumo_vendas(date(2026,10,1),date(2026,10,31),modo='mensal',
                historico=historico,hoje=date(2026,11,20))
            i=r['analise_evolucao']['itens'][-1]
            self.assertEqual(r['faturamento'],120)
            self.assertEqual(r['analise_evolucao']['inicio_registros'], primeiro)
            self.assertIsNone(i['variacao'])
            self.assertIn('Períodos não equivalentes',i['motivo'])
