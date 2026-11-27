"""Exact display rounding and immutable financial inputs; no database needed."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
from estoque.services.participacao_classes import participacao_classes


class ParticipacaoClassesTests(unittest.TestCase):
    def report(self, values):
        return dict(classes=[dict(chave=str(i),nome='Classe '+str(i),faturamento=D(v))
            for i,v in enumerate(values)],total=dict(faturamento=sum(map(D,values),D(0))))

    def test_top_seis_independente_da_ordem_e_financeiro_inalterado(self):
        r=self.report(['10','40','20','30','5','60','50','1'])
        before=deepcopy(r)
        shares=participacao_classes(r)
        self.assertEqual([s['chave'] for s in shares['segmentos']],['5','6','1','3','2','0',''])
        self.assertEqual([s['cor'] for s in shares['segmentos']],list(range(1,8)))
        self.assertEqual(sum(shares['por_classe'].values()),100)
        self.assertEqual(sum(s['percentual'] for s in shares['segmentos']),100)
        self.assertEqual(shares['segmentos'][-1]['percentual'],shares['por_classe']['4']+shares['por_classe']['7'])
        r['classes'].reverse()
        self.assertEqual(participacao_classes(r),shares)
        r['classes'].reverse()
        self.assertEqual(r,before)

    def test_arredondamento_fecha_cem_e_empates_sao_estaveis(self):
        r=self.report(['1','1','1'])
        s=participacao_classes(r)
        self.assertEqual(s['por_classe'],{'0':D('33.34'),'1':D('33.33'),'2':D('33.33')})

    def test_margem_da_legenda_e_exatamente_a_margem_ja_calculada(self):
        r=self.report(['10','20','30'])
        for c,margin in zip(r['classes'],[D('17.07'),D('-5.45'),None]):
            c['margem']=margin
        before=deepcopy(r)
        s=participacao_classes(r)
        for segment in s['segmentos']:
            self.assertEqual(segment['margem'],next(c['margem'] for c in r['classes'] if c['chave']==segment['chave']))
        self.assertEqual(r,before)

    def test_ate_seis_classes_sem_restante_artificial(self):
        for values in (['5'],['5','5'],['5','3','2'],['5','4','3','2'],['5']*5,['5']*6):
            with self.subTest(values=values):
                s=participacao_classes(self.report(values))
                self.assertEqual(len(s['segmentos']),len(values))
                self.assertEqual(sum(s['percentual'] for s in s['segmentos']),100)

    def test_vazio_zero_negativo_ou_totais_divergentes_nao_inventam_percentual(self):
        for values in ([],['0'],['0','0'],['10','-1']):
            self.assertFalse(participacao_classes(self.report(values))['disponivel'])
        r=self.report(['10']);r['total']['faturamento']=D('11')
        self.assertFalse(participacao_classes(r)['disponivel'])

    def test_classes_zero_e_valores_muito_pequenos(self):
        r=self.report(['10000','0.01','0.01','0.01','0'])
        s=participacao_classes(r)
        self.assertEqual(sum(s['percentual'] for s in s['segmentos']),100)
        self.assertEqual(sum(s['por_classe'].values()),100)
        for c in r['classes']:
            self.assertLess(abs(s['por_classe'][c['chave']]-c['faturamento']/r['total']['faturamento']*100),D('.01'))
