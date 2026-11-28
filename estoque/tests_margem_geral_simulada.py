from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from estoque.services.faturamento_negocio import consolidar_faturamento, simular_margem_geral
from locacoes.tests_resumo_financeiro import DadosResumo


class MargemGeralSimuladaTests(SimpleTestCase):
    def negocio(self, total='62828.59',locacoes='1454.00'):
        return dict(total=D(total),locacoes=dict(faturamento=D(locacoes)))

    def test_referencia_completa_e_parcial_mesmo_lucro_exibido(self):
        for completa in (True,False):
            rent=dict(disponivel=completa,lucro=D('11810.59') if completa else None,
                      lucro_coberto=D('11810.59'),com_custo=1325)
            negocio=self.negocio();original=deepcopy((negocio,rent))
            r=simular_margem_geral(negocio,rent)
            self.assertEqual(r['valor'],D('21.11'))
            self.assertEqual(r['parcial'],not completa)
            self.assertEqual(r['lucro_base'],D('11810.59'))
            self.assertEqual(r['resultado_bruto'],D('13264.59'))
            self.assertEqual((negocio,rent),original)

    def test_lucro_indisponivel_nao_e_substituido_por_receita_das_locacoes(self):
        for total in ('0','1000'):
            r=simular_margem_geral(self.negocio(total),dict(disponivel=False,lucro=None,lucro_coberto=None))
            self.assertIsNone(r['valor'])
            self.assertIsNone(r['lucro_base'])
            self.assertIsNone(r['resultado_bruto'])

    def test_outubro_resultado_corresponde_exatamente_a_base_da_margem(self):
        negocio=self.negocio('73989.03','2296.18')
        r=simular_margem_geral(negocio,dict(disponivel=False,lucro_coberto=D('13385.42')))
        self.assertEqual(r['resultado_bruto'],D('15681.60'))
        self.assertEqual(r['valor'],(r['resultado_bruto']/negocio['total']*100).quantize(D('.01')))

    def test_resultado_zero_conhecido_nao_e_confundido_com_indisponivel(self):
        r=simular_margem_geral(self.negocio('0','0'),dict(disponivel=True,lucro=D('0')))
        self.assertEqual(r['resultado_bruto'],0)
        self.assertEqual(r['valor'],0)

    def test_total_zero_com_lucro_conhecido(self):
        self.assertEqual(simular_margem_geral(self.negocio('0','0'),dict(disponivel=True,lucro=D('0')))['valor'],0)

    def test_estimativa_historica_reutiliza_mesmo_lucro_do_cartao(self):
        rent=dict(disponivel=False,lucro_coberto=D('10'),estimativa_setembro=dict(lucro_parcial=D('30')))
        r=simular_margem_geral(self.negocio('100','20'),rent)
        self.assertEqual((r['valor'],r['parcial'],r['lucro_base']),(D('50.00'),True,D('30')))

    def test_estimativa_sem_lucro_nao_fabrica_valor(self):
        rent=dict(disponivel=False,lucro_coberto=D('10'),estimativa_setembro=dict(lucro_parcial=None))
        self.assertIsNone(simular_margem_geral(self.negocio(),rent)['valor'])

    def test_prejuizo_e_periodo_personalizado_sem_arredondar_valores_base(self):
        r=simular_margem_geral(self.negocio('300','20'),dict(disponivel=True,lucro=D('-40')))
        self.assertEqual(r['valor'],D('-6.67'))


class MargemLocacoesIntegracaoTests(DadosResumo,TestCase):
    def test_cancelamento_altera_percentual_e_simulacao_sem_recalcular_mercadorias(self):
        with patch('locacoes.consulta.timezone.localdate',return_value=date(2026,11,28)):
            locacao=self.criar(status='entregue',total=100)
            intervalo=(date(2026,10,1),date(2026,10,31))
            antes=consolidar_faturamento(*intervalo,D('900'))
            rent=dict(disponivel=True,lucro=D('180'))
            self.assertEqual(antes['participacao_locacoes'],10)
            self.assertEqual(simular_margem_geral(antes,rent)['valor'],28)
            self.assertEqual(simular_margem_geral(antes,rent)['resultado_bruto'],280)
            # Alteração exclusivamente da fixture no PostgreSQL descartável.
            type(locacao).objects.filter(pk=locacao.pk).update(status='cancelada')
            depois=consolidar_faturamento(*intervalo,D('900'))
            self.assertEqual(depois['mercadorias'],900)
            self.assertEqual(depois['total'],900)
            self.assertEqual(depois['participacao_locacoes'],0)
            self.assertEqual(simular_margem_geral(depois,rent)['valor'],20)
            self.assertEqual(simular_margem_geral(depois,rent)['resultado_bruto'],180)
