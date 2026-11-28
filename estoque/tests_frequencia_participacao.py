from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from estoque.models import Cliente, Venda
from estoque.services.inteligencia_clientes import historico_ausencias
from estoque.services.faturamento_negocio import consolidar_faturamento


class FrequenciaObservadaTests(TestCase):
    def analisar(self, datas, notas_repetidas=0):
        cliente=Cliente.objects.create(nome='Frequência teste')
        Venda.objects.bulk_create([Venda(cliente=cliente,data_venda=d,total=100) for d in datas])
        if notas_repetidas:
            Venda.objects.bulk_create([Venda(cliente=cliente,data_venda=datas[0],total=100) for _ in range(notas_repetidas)])
        atuais={d for d in datas if date(2026,11,1)<=d<=date(2026,11,27)}
        c=dict(id=cliente.pk,nome=cliente.nome,notas_atual=len(atuais),dias_atual=len(atuais),
               anterior=Decimal('3217.20'),atual=Decimal('1808.00'),variacao_percentual=Decimal('-43.80'))
        r=dict(impactos=[c],ausentes=[] if atuais else [c])
        periodo=dict(inicio=date(2026,11,1),fim=date(2026,11,27),referencia_inicio=date(2026,10,1))
        with patch('estoque.services.inteligencia_clientes.timezone.localdate',return_value=date(2026,11,28)):
            with self.assertNumQueries(1):historico_ausencias(r,periodo)
        return c['historico']

    def test_frequente_em_um_mes_sem_exigir_meses_de_historico(self):
        h=self.analisar([date(2026,10,d) for d in (1,3,5,7,9,11,13,15)])
        self.assertEqual(h['leitura_frequencia']['chave'],'frequentes')
        self.assertEqual(h['chave'],'insuficientes') # Critério anterior não foi redefinido.
        self.assertEqual((h['dias'],h['meses']),(8,1))

    def test_atividade_atual_densa_nao_e_ocultada_por_registro_antigo_esparso(self):
        h=self.analisar([date(2020,1,1)]+[date(2026,11,d) for d in range(1,9)])
        self.assertEqual(h['leitura_frequencia']['chave'],'frequentes')
        self.assertIn('Continua comprando, mas apresentou redução de 43,80%', ' '.join(h['explicacao']))
        self.assertNotIn('Não foram identificadas compras posteriores',' '.join(h['explicacao']))

    def test_raimundo_meses_consecutivos_nao_equiparados_a_alta_frequencia(self):
        h=self.analisar([date(2026,9,22),date(2026,10,1),date(2026,10,10),date(2026,10,23)])
        self.assertEqual(h['leitura_frequencia']['chave'],'mensais')
        self.assertEqual(h['dias_observados'],4)
        self.assertIn('novembro de 2026, até 28/11/2026',' '.join(h['explicacao']))

    def test_meses_semanas_nao_consecutivos_e_dias_distintos(self):
        h=self.analisar([date(2026,7,1),date(2026,7,20),date(2026,10,1),date(2026,10,21)])
        self.assertEqual(h['leitura_frequencia']['chave'],'ocasionais')
        self.assertEqual(len(h['semanas_com_compras']),4)
        self.assertIn('julho e outubro de 2026',' '.join(h['explicacao']))

    def test_notas_repetidas_nao_evidenciam_frequencia(self):
        h=self.analisar([date(2026,10,1)],notas_repetidas=12)
        self.assertEqual(h['leitura_frequencia']['chave'],'insuficiente')
        self.assertEqual((h['notas_observadas'],h['dias_observados'],len(h['semanas_com_compras'])),(13,1,1))

    def test_semana_na_virada_do_mes_nao_e_contada_duas_vezes(self):
        h=self.analisar([date(2026,9,30),date(2026,10,1)])
        self.assertEqual(h['dias'],2)
        self.assertEqual(h['meses'],2)
        self.assertEqual(len(h['semanas_com_compras']),1)

    def test_oito_dias_esparsos_em_anos_nao_viram_frequentes(self):
        h=self.analisar([date(y,10,1) for y in range(2019,2027)])
        self.assertNotEqual(h['leitura_frequencia']['chave'],'frequentes')


class ParticipacaoLocacoesTests(SimpleTestCase):
    def test_percentual_dinamico_sem_alterar_totais_e_sem_segunda_apuracao(self):
        for inicio,fim,mercadorias,locacoes,esperado in (
            (date(2026,11,1),date(2026,11,28),'61330.59','1454.00','2.32'),
            (date(2026,10,1),date(2026,10,31),'900','100','10.00'),
            (date(2026,11,5),date(2026,11,12),'150','50','25.00'),
            (date(2026,11,1),date(2026,11,28),'0','0','0.00'),
            (date(2026,11,1),date(2026,11,28),'0','100','100.00'),
        ):
            apurado=dict(faturamento=Decimal(locacoes),quantidade=19)
            with self.subTest(inicio=inicio,mercadorias=mercadorias):
                with patch('estoque.services.faturamento_negocio.faturamento_locacoes',return_value=apurado) as consulta:
                    r=consolidar_faturamento(inicio,fim,Decimal(mercadorias))
                consulta.assert_called_once_with(inicio,fim)
                self.assertEqual(r['participacao_locacoes'],Decimal(esperado))
                self.assertEqual(r['total'],Decimal(mercadorias)+Decimal(locacoes))
                self.assertEqual(r['mercadorias'],Decimal(mercadorias))
                self.assertIs(r['locacoes'],apurado)
