"""Inteligência por cadastro: dados fictícios, somente PostgreSQL descartável."""
import base64
import json
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import SimpleTestCase, TestCase, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.template.loader import get_template

from offline.browser_support import Chrome
from .models import Cliente, Venda, ItemVenda
from .services.analise_desempenho import desempenho_comercial
from .services.inteligencia_clientes import (
    participacao_clientes, resumir_clientes, classificar_frequencia, nome_periodo,
)
from .tests_relatorios import PostgreSQLIsoladoTests
from .views_relatorios import vendas as pagina_vendas


INICIO, FIM, HOJE = date(2026, 11, 1), date(2026, 11, 24), date(2026, 11, 25)


def desempenho(anterior='187.00', atual='163.00'):
    return dict(comparavel=True, anterior=D(anterior), atual=D(atual),
                diferenca=D(atual)-D(anterior), inicio=INICIO, fim=FIM,
                referencia_inicio=date(2026,10,1), referencia_fim=date(2026,10,24))


def linha(pk, periodo, valor, *, notas=1, dias=1, nome='Cadastro de teste'):
    return dict(cliente_id=pk, cliente__nome=nome, periodo=periodo,
                faturamento=D(valor), notas=notas, dias=dias, sem_total=0)


def linhas_exemplo():
    return [linha(1,'anterior','100',notas=3,dias=2), linha(1,'atual','80'),
            linha(2,'anterior','50'), linha(3,'atual','40'),
            linha(4,'anterior','20'), linha(4,'atual','30'),
            linha(1126,'anterior','10'), linha(1126,'atual','5'),
            linha(None,'anterior','7'), linha(None,'atual','8')]


class ParticipacaoPuraTests(SimpleTestCase):
    def test_linguagem_por_periodo_sem_classificar_como_mercadorias(self):
        for anterior,atual in ((date(2026,10,1),date(2026,11,1)),
                              (date(2025,12,1),date(2026,1,1)),
                              (date(2025,11,1),date(2026,11,1))):
            with self.subTest(anterior=anterior,atual=atual):
                analise=desempenho()
                analise.update(referencia_inicio=anterior,referencia_fim=anterior,
                               inicio=atual,fim=atual)
                r=resumir_clientes(linhas_exemplo(),analise)
                html=self.renderizar(r,analise)
                self.assertIn(f'Compraram em {nome_periodo(anterior,anterior)}, mas não em {nome_periodo(atual,atual)}',html)
                self.assertIn(f'Compraram em {nome_periodo(atual,atual)}, mas não em {nome_periodo(anterior,anterior)}',html)
                self.assertIn('Compraram nos dois períodos',html)
                self.assertIn('Como as compras dos clientes influenciaram o faturamento',html)
                self.assertNotIn('Vendas por grupo',html)
                self.assertNotIn('Tipo de cadastro a confirmar',html)
        self.assertEqual(nome_periodo(date(2026,10,25),date(2026,11,2)),
                         '25/10/2026 a 02/11/2026')

    def test_frequencia_requer_dias_meses_e_amplitude(self):
        for dias,meses,primeira,ultima,chave in (
            (4,2,date(2026,9,1),date(2026,10,1),'recorrentes'),
            (3,2,date(2026,9,1),date(2026,10,1),'ocasionais'),
            (2,2,date(2026,9,30),date(2026,10,1),'insuficientes'),
            (7,1,date(2026,10,1),date(2026,10,24),'insuficientes'),
            (1,1,date(2026,10,1),date(2026,10,1),'insuficientes'),
            (0,0,None,None,'insuficientes')):
            self.assertEqual(classificar_frequencia(dias,meses,primeira,ultima)[0],chave)

    def test_lista_completa_das_ausencias_nao_substitui_cinco_maiores(self):
        dados=[linha(i,'anterior',str(i)) for i in range(1,30)]
        r=resumir_clientes(dados,desempenho('435','0'))
        self.assertEqual(len(r['ausentes']),29)
        self.assertEqual(len(r['impactos']),5)
        self.assertEqual(sum(c['anterior'] for c in r['ausentes']),D('435'))
        self.assertIn('#1 ·',self.renderizar(r))

    def test_percentuais_e_comportamentos_dinamicos(self):
        casos = [
            ('100', '150', 3, 3, D('50.00'), 'mantendo a mesma quantidade'),
            ('100', '150', 1, 18, D('50.00'), 'Registrou mais notas'),
            ('100', '150', 3, 2, D('50.00'), 'apesar de registrar menos'),
            ('0', '150', 0, 2, None, 'somente no período atual'),
            ('100', '0', 3, 0, D('-100.00'), 'Sem compras no período atual'),
            ('100', '50', 3, 2, D('-50.00'), 'com menos notas'),
            ('100', '50', 3, 3, D('-50.00'), 'mantendo a mesma quantidade'),
            ('100', '50', 3, 4, D('-50.00'), 'apesar de registrar mais'),
            ('0', '150', 1, 2, None, 'Registrou mais notas'),
            ('3', '4', 1, 1, D('33.33'), 'mantendo a mesma quantidade'),
        ]
        for antes, agora, n_antes, n_agora, percentual, texto in casos:
            with self.subTest(antes=antes, agora=agora, notas=(n_antes,n_agora)):
                dados = [linha(1,'anterior',antes,notas=n_antes), linha(1,'atual',agora,notas=n_agora)]
                r = resumir_clientes(dados,desempenho(antes,agora))
                cadastro = (r['aumentos'] + r['impactos'])[0]
                self.assertEqual(cadastro['variacao_percentual'],percentual)
                self.assertIn(texto,cadastro['comportamento'])
                html = self.renderizar(r)
                self.assertIn('Variação (%)',html)
                self.assertIn('data-label="Antes"',html)
                self.assertNotIn('O grupo que mais',html)
                self.assertNotIn('Principal problema identificado',html)
                if percentual is None:
                    self.assertIn('Sem base para calcular',html)
                else:
                    from .templatetags.relatorios import numero_sinal_br
                    self.assertIn(numero_sinal_br(percentual)+'%',html)

    def test_percentual_nao_altera_ranking_em_reais(self):
        dados = [linha(1,'anterior','1000'),linha(1,'atual','1200'),
                 linha(2,'anterior','1'),linha(2,'atual','101'),
                 linha(3,'anterior','1000'),linha(3,'atual','800'),
                 linha(4,'anterior','100'),linha(4,'atual','0')]
        r = resumir_clientes(dados,desempenho('2101','2101'))
        self.assertEqual([c['id'] for c in r['aumentos']],[1,2])
        self.assertEqual([c['id'] for c in r['impactos']],[3,4])
        self.assertEqual(sum(g['diferenca'] for g in r['grupos']),D('0.00'))

    def renderizar(self, resultado, analise=None):
        return get_template('estoque/relatorios/participacao_clientes.html').render(
            dict(clientes_variacao=resultado, desempenho=analise or desempenho()))

    def test_particao_concilia_ao_centavo_incluindo_coletivo_pendente_e_sem_cliente(self):
        r = resumir_clientes(linhas_exemplo(), desempenho(), [4])
        self.assertTrue(r['disponivel'])
        grupos = {g['chave']:g for g in r['grupos']}
        self.assertEqual([grupos[k]['diferenca'] for k in ('ambos','ausentes','somente_atual','coletivos','sem_cliente')], [-20,-50,40,5,1])
        self.assertEqual(sum(g['diferenca'] for g in r['grupos']), D('-24.00'))
        self.assertEqual((r['anterior'],r['atual']), (187,163))
        self.assertEqual(grupos['ambos']['cadastros'], 1)
        self.assertEqual((r['coletivos'],r['pendentes']), (2,3))
        self.assertFalse(r['cadastro_1126_pendente'])
        self.assertEqual(grupos['ausentes']['pontos'], (D(-50)/187*100).quantize(D('.01')))

    def test_nomes_iguais_nao_unem_ids_e_1126_nao_vira_coletivo_pelo_nome(self):
        dados = [linha(1,'anterior','10',nome='Mesmo Nome'),
                 linha(2,'atual','10',nome='Mesmo Nome'),
                 linha(1126,'anterior','5',nome='Venda A Vista Sta Bárbara')]
        r = resumir_clientes(dados, desempenho('15','10'))
        self.assertEqual(r['coletivos'],1)  # Confirmação pelo ID, independentemente do nome.
        self.assertEqual(r['pendentes'],2)
        self.assertEqual([g['cadastros'] for g in r['grupos'][:3]], [0,1,1])

    def test_presenca_usa_notas_mesmo_com_receita_zero(self):
        r = resumir_clientes([linha(1,'anterior','0'),linha(1,'atual','10')],desempenho('0','10'))
        self.assertEqual(r['grupos'][0]['cadastros'],1)
        self.assertTrue(all(g['pontos'] is None for g in r['grupos']))
        self.assertIsNone(r['pontos'])

    def test_referencia_negativa_nao_produz_pontos_percentuais(self):
        r=resumir_clientes([linha(1,'anterior','-10'),linha(1,'atual','5')],desempenho('-10','5'))
        self.assertTrue(r['disponivel'])
        self.assertIsNone(r['pontos'])

    def test_divergencia_de_um_centavo_bloqueia_atribuicao(self):
        r=resumir_clientes(linhas_exemplo(),desempenho('187','163.01'))
        self.assertFalse(r['disponivel'])
        self.assertIn('não coincidem',r['motivo'])
        self.assertNotIn('grupos',r)

    def test_nota_sem_total_bloqueia_e_incomparavel_nao_avalia_linhas(self):
        dados=linhas_exemplo(); dados[0]['sem_total']=1
        self.assertFalse(resumir_clientes(dados,desempenho())['disponivel'])
        def proibido():
            raise AssertionError('Não deve carregar cadastros de períodos incomparáveis')
            yield
        self.assertFalse(resumir_clientes(proibido(),dict(comparavel=False,motivo='Cobertura insuficiente'))['disponivel'])

    def test_ranking_limita_cinco_negativos_com_desempate_por_id(self):
        dados=[linha(i,'anterior','10') for i in range(8,0,-1)]
        dados += [linha(9,'atual','30'),linha(None,'anterior','100')]
        r=resumir_clientes(dados,desempenho('180','30'))
        self.assertEqual([c['id'] for c in r['impactos']],[1,2,3,4,5])

    def test_cinco_maiores_aumentos_por_reais_e_id_sem_unir_nomes_iguais(self):
        dados=[linha(i,'atual','10',nome='Mesmo Nome') for i in range(8,0,-1)]
        dados += [linha(None,'atual','50'),linha(9,'atual','0')]
        r=resumir_clientes(dados,desempenho('0','130'))
        self.assertEqual([c['id'] for c in r['aumentos']],[1,2,3,4,5])
        self.assertEqual(r['impactos'],[])
        self.assertEqual(r['diferenca'],130)

    def test_resumo_de_queda_aponta_ausentes_sem_inventar_causa(self):
        r=resumir_clientes(linhas_exemplo(),desempenho(),[4])
        resumo=r['comercial']
        self.assertEqual((resumo['movimento'],resumo['valor_mudanca']),('queda',24))
        self.assertEqual(resumo['principal']['chave'],'ausentes')
        self.assertEqual(resumo['problema']['valor'],50)
        self.assertEqual(resumo['positivo']['valor'],40)
        self.assertEqual(resumo['ausentes']['cadastros'],1)
        self.assertEqual((resumo['ambos']['anterior'],resumo['ambos']['atual']),(100,80))
        html=self.renderizar(r)
        self.assertIn('-R$ 24,00',html)
        self.assertNotIn('Essas observações vêm das notas registradas',html)
        self.assertNotIn('não significa perda definitiva',html)
        self.assertNotIn('<details',html[html.index('<summary>')+1:].replace('<details class="cliente-historico">',''))
        self.assertNotIn('Efeito no total (p.p.)',html)
        self.assertEqual(r['pontos'],D('-12.83'))

    def test_crescimento_sem_forcar_problema_negativo(self):
        r=resumir_clientes([linha(1,'anterior','100'),linha(1,'atual','130'),
            linha(2,'anterior','100'),linha(2,'atual','120')],desempenho('200','250'))
        self.assertEqual(r['comercial']['movimento'],'alta')
        self.assertIsNone(r['comercial']['problema'])
        html=self.renderizar(r)
        self.assertIn('+R$ 50,00',html)
        self.assertIn('Não houve redução nas vendas',html)
        self.assertNotIn('O faturamento caiu',html)
        self.assertEqual([c['id'] for c in r['aumentos']],[1,2])

    def test_estabilidade_com_e_sem_movimentos_que_se_compensam(self):
        for dados,movimentos in (([linha(1,'anterior','100'),linha(1,'atual','100')],False),
                ([linha(1,'anterior','100'),linha(1,'atual','50'),linha(2,'atual','50')],True)):
            with self.subTest(movimentos=movimentos):
                r=resumir_clientes(dados,desempenho('100','100'))
                self.assertEqual(r['comercial']['movimento'],'igual')
                self.assertEqual(r['comercial']['houve_movimentos'],movimentos)
                html=self.renderizar(r)
                self.assertIn('Total — confere com o faturamento da loja',html)
                self.assertNotIn('O faturamento caiu',html)
                self.assertNotIn('O faturamento cresceu',html)

    def test_movimentos_dentro_do_mesmo_grupo_nao_desaparecem_no_resumo(self):
        r=resumir_clientes([linha(1,'anterior','60'),linha(1,'atual','40'),
            linha(2,'anterior','40'),linha(2,'atual','60')],desempenho('100','100'))
        self.assertEqual(r['grupos'][0]['diferenca'],0)
        self.assertEqual(r['comercial']['problema']['id'],1)
        self.assertEqual(r['comercial']['positivo']['id'],2)

    def test_sem_cobertura_ou_dados_nao_ha_conclusao_de_queda(self):
        for motivo in ('Cobertura insuficiente','Sem histórico registrado neste período.'):
            r=resumir_clientes([],dict(comparavel=False,motivo=motivo))
            html=self.renderizar(r)
            self.assertIn('Ainda não é possível comparar',html)
            self.assertIn(motivo,html)
            self.assertNotIn('<table>',html)

    def test_coletivos_e_sem_cliente_continuam_visiveis_na_recomendacao(self):
        for pk,confirmados,trecho in ((4,[4],'cadastros coletivos'),(None,[],'sem cliente identificado')):
            r=resumir_clientes([linha(pk,'anterior','10'),linha(pk,'atual','20')],desempenho('10','20'),confirmados)
            self.assertIn(trecho,r['comercial']['investigar'])

    def test_tabelas_completas_e_ids_preservados_sem_secoes_ocultas_adicionais(self):
        r=resumir_clientes(linhas_exemplo(),desempenho(),[4])
        html=self.renderizar(r)
        self.assertEqual(html.count('<details')-html.count('<details class="cliente-historico">'),1)
        self.assertEqual(html.count('<table>'),4)
        self.assertIn('Clientes que merecem atenção',html)
        self.assertIn('Clientes que estão comprando mais',html)
        self.assertIn('#1126',html)
        self.assertIn('Notas',html)
        self.assertIn('data-label="Dias: antes → agora"',html)
        self.assertIn('Total — confere com o faturamento da loja',html)
        self.assertIn('+R$ 40,00',html)
        self.assertIn('-R$ 50,00',html)
        self.assertLess(html.index('Maior redução'),html.index('<table>'))

    def test_tabela_resumida_quatro_colunas_sem_linhas_vazias_preserva_conciliacao(self):
        dados=[linha(1,'anterior','100'),linha(1,'atual','80'),linha(2,'anterior','50'),linha(3,'atual','40')]
        r=resumir_clientes(dados,desempenho('150','120'))
        self.assertEqual(len(r['grupos']),5)  # Parcelas financeiras originais preservadas.
        self.assertEqual([g['chave'] for g in r['grupos_visiveis']],['ambos','ausentes','somente_atual'])
        self.assertEqual(sum(g['diferenca'] for g in r['grupos_visiveis']),D('-30.00'))
        html=self.renderizar(r)
        tabela=html.split('<table>',1)[1].split('</table>',1)[0]
        self.assertEqual(tabela.split('</thead>')[0].count('scope="col"'),4)
        for removido in ('Identificação','Efeito no total','Cadastros coletivos confirmados','Vendas sem cliente identificado','fórmula','ainda não foi confirmado'):
            self.assertNotIn(removido,html)

    def test_notas_sem_cliente_inclusive_valor_zero_nao_somem_da_conciliacao(self):
        for valor in ('0','7'):
            with self.subTest(valor=valor):
                r=resumir_clientes([linha(None,'anterior',valor)],desempenho(valor,'0'))
                html=self.renderizar(r)
                self.assertIn('Notas sem cliente vinculado:',html)
                self.assertIn('Vendas sem cliente identificado',html)
                self.assertEqual(r['sem_cliente']['notas_anterior'],1)
                self.assertEqual(sum(g['anterior'] for g in r['grupos_visiveis']),D(valor))

    def test_1126_contexto_de_balcao_sem_reclassificar_ou_alterar_valores(self):
        r=resumir_clientes([linha(1126,'anterior','10',nome='Venda A Vista Sta Bárbara'),
            linha(1126,'atual','15',nome='Venda A Vista Sta Bárbara')],desempenho('10','15'))
        html=self.renderizar(r)
        self.assertIn('#1126',html)
        self.assertIn('origem comercial coletiva',html)
        self.assertNotIn('falta confirmar',html)
        self.assertNotIn('confira as últimas notas',html.lower())
        self.assertEqual((r['anterior'],r['atual'],r['coletivos']),(10,15,1))
        self.assertEqual(r['aumentos'],[])
        self.assertNotIn('mantendo a mesma quantidade',html)


class FixturesClientes:
    def criar_cenario(self):
        self.admin=get_user_model().objects.create_user('inteligencia-admin',is_staff=True,is_superuser=True)
        self.cobertura=Cliente.objects.create(nome='Cobertura documental fictícia')
        self.c1=Cliente.objects.create(nome='Mesmo Nome')
        self.c2=Cliente.objects.create(nome='Mesmo Nome')
        self.c3=Cliente.objects.create(nome='Cadastro atual')
        self.coletivo=Cliente.objects.create(nome='Nome sem indicativo de coletivo')
        self.pendente=Cliente.objects.create(pk=1126,nome='Venda A Vista Sta Bárbara')
        for mes in (10,11):
            Venda.objects.bulk_create([Venda(cliente=self.cobertura,data_venda=date(2026,mes,d),
                tipo_pagamento='Pix',total=0) for d in range(1,25)])
        for cliente,ant,atual in ((self.c1,100,80),(self.c2,50,None),(self.c3,None,40),
                                  (self.coletivo,20,30),(self.pendente,10,5),(None,7,8)):
            if ant is not None: self.nota(cliente,10,1,ant)
            if atual is not None: self.nota(cliente,11,1,atual)
        # Duas notas de valores iguais no mesmo dia: uma visita não é presumida.
        Venda.objects.filter(cliente=self.c1,data_venda=date(2026,10,1)).update(total=50)
        self.nota(self.c1,10,1,50)
        for mes in (10,11):
            self.nota(self.c1,mes,1,900,cancelada=True)
            self.nota(self.c1,mes,1,800,tipo_pagamento='consumo_proprio')
        self.nota(self.c1,10,25,777)  # Entre as janelas, não integra a referência.
        self.nota(self.c1,11,25,999)  # Hoje e futuro ficam fora.
        self.nota(self.c1,11,26,999)
        nota=Venda.objects.get(cliente=self.c1,data_venda=date(2026,11,1),total=80)
        for total in (30,50):
            ItemVenda.objects.create(venda=nota,quantidade=1,unidade='UN',preco_unitario=total,valor_total=total)

    def nota(self,cliente,mes,dia,total,**kwargs):
        return Venda.objects.create(cliente=cliente,data_venda=date(2026,mes,dia),total=total,
                                   tipo_pagamento=kwargs.pop('tipo_pagamento','A prazo'),**kwargs)

    def analisar(self):
        return desempenho_comercial(INICIO,FIM,HOJE)


class ParticipacaoDBTests(FixturesClientes,PostgreSQLIsoladoTests,TestCase):
    def setUp(self):
        self.criar_cenario()

    @override_settings(RELATORIOS_CLIENTES_COLETIVOS_CONFIRMADOS=())
    def test_uma_agregacao_select_sem_duplicar_itens_ou_notas_iguais(self):
        analise=self.analisar()
        with CaptureQueriesContext(connection) as queries:
            r=participacao_clientes(analise)
        self.assertEqual(len(queries),2)  # Recortes + histórico agregado das ausências.
        self.assertTrue(queries[0]['sql'].lstrip().upper().startswith('SELECT'))
        self.assertNotIn('estoque_itemvenda',queries[0]['sql'])
        self.assertTrue(queries[1]['sql'].lstrip().upper().startswith('SELECT'))
        self.assertNotIn('estoque_itemvenda',queries[1]['sql'])
        self.assertEqual((r['anterior'],r['atual'],r['diferenca']),(187,163,-24))
        cadastro=next(c for c in r['impactos'] if c['id']==self.c1.pk)
        self.assertEqual((cadastro['notas_anterior'],cadastro['dias_anterior']),(2,1))
        self.assertEqual((r['coletivos'],r['cadastro_1126_pendente']),(1,False))

    def test_coletivo_explicito_por_id_e_pendente_nao_se_sobrepoem(self):
        with override_settings(RELATORIOS_CLIENTES_COLETIVOS_CONFIRMADOS=(self.coletivo.pk,)):
            r=participacao_clientes(self.analisar())
        grupo=next(g for g in r['grupos'] if g['chave']=='coletivos')
        self.assertEqual((grupo['anterior'],grupo['atual'],grupo['cadastros']),(30,35,2))
        self.assertEqual(sum(g['diferenca'] for g in r['grupos']),-24)
        self.assertFalse(r['cadastro_1126_pendente'])

    def test_cobertura_insuficiente_sem_consulta_adicional(self):
        Venda.objects.filter(data_venda=date(2026,10,12)).delete()
        analise=self.analisar()
        self.assertFalse(analise['comparavel'])
        with self.assertNumQueries(0):
            r=participacao_clientes(analise)
        self.assertFalse(r['disponivel'])
        self.assertIn('cobertura',r['motivo'])

    def test_datas_retroativas_interanual_e_so_hoje(self):
        self.nota(self.c1,10,24,3)
        self.nota(self.c1,11,24,4)
        r=participacao_clientes(self.analisar())
        self.assertEqual((r['anterior'],r['atual']),(190,167))
        interanual=desempenho_comercial(INICIO,FIM,HOJE,'ano_anterior')
        with self.assertNumQueries(0):
            self.assertFalse(participacao_clientes(interanual)['disponivel'])
            self.assertFalse(participacao_clientes(desempenho_comercial(HOJE,HOJE,HOJE))['disponivel'])

    def test_mesmo_periodo_independente_do_grafico_filtros_e_permissoes(self):
        self.client.force_login(self.admin)
        resultados=[]
        for modo in ('diario','semanal','mensal'):
            with patch('estoque.views_relatorios.timezone.localdate',return_value=HOJE):
                resposta=self.client.get('/relatorios/vendas/',dict(periodo='personalizado',
                    data_inicial='2026-11-01',data_final='2026-11-24',modo=modo,
                    alcance='6',semanas_comparacao='4'))
            self.assertEqual(resposta.status_code,200)
            self.assertContains(resposta,'Participação dos clientes na variação')
            self.assertContains(resposta,'origem comercial coletiva')
            self.assertContains(resposta,'data_inicial=2026-11-01')
            self.assertContains(resposta,'semanas_comparacao=4')
            self.assertContains(resposta,'Cálculo completo indisponível')
            self.assertEqual(resposta.context['resumo']['faturamento'],163)
            resultados.append(resposta.context['clientes_variacao'])
        self.assertEqual(resultados[0],resultados[1]); self.assertEqual(resultados[1],resultados[2])
        self.client.logout()
        self.assertEqual(self.client.get('/relatorios/vendas/').status_code,302)
        comum=get_user_model().objects.create_user('comum-inteligencia')
        self.client.force_login(comum)
        self.assertEqual(self.client.get('/relatorios/vendas/').status_code,403)

    def test_base_zero_na_integracao_nao_exibe_percentual_infinito(self):
        Venda.objects.filter(data_venda__range=(date(2026,10,1),date(2026,10,24))).update(total=0)
        r=participacao_clientes(self.analisar())
        self.assertTrue(r['disponivel'])
        self.assertEqual(r['diferenca'],163)
        self.assertIsNone(r['pontos'])

    def test_historico_dinamico_dias_distintos_prioridade_e_compra_posterior(self):
        # O mesmo ID ausente ganha evidência anterior; várias notas num dia não
        # aumentam os dias. Consumo próprio, canceladas e futuro ficam de fora.
        for dia in (1,10,20):
            self.nota(self.c2,9,dia,3)
        self.nota(self.c2,9,1,3)
        self.nota(self.c2,9,21,999,cancelada=True)
        self.nota(self.c2,9,22,999,tipo_pagamento='consumo_proprio')
        self.nota(self.c2,11,26,999)
        with patch('estoque.services.inteligencia_clientes.timezone.localdate',return_value=HOJE):
            r=participacao_clientes(self.analisar())
        self.assertEqual((r['anterior'],r['atual'],r['diferenca']),(187,163,-24))
        h=r['ausentes'][0]['historico']
        self.assertEqual((h['dias'],h['meses'],h['chave']),(4,2,'recorrentes'))
        self.assertEqual(h['anterior_recorte'],date(2026,9,20))
        self.assertIsNone(h['posterior'])
        self.assertEqual(r['historico_ausencias']['prioridades'][0]['id'],self.c2.pk)
        self.nota(self.c2,11,25,12)
        with patch('estoque.services.inteligencia_clientes.timezone.localdate',return_value=HOJE):
            depois=participacao_clientes(self.analisar())
        self.assertEqual(depois['ausentes'][0]['historico']['posterior'],HOJE)
        self.assertEqual(depois['historico_ausencias']['prioridades'],[])
        self.assertEqual((depois['anterior'],depois['atual']),(187,163))

    def test_sem_ausencias_nao_consulta_historico_e_coletivo_nao_vira_ausente_individual(self):
        analise=self.analisar()
        with override_settings(RELATORIOS_CLIENTES_COLETIVOS_CONFIRMADOS=(self.c2.pk,)):
            with self.assertNumQueries(1):
                r=participacao_clientes(analise)
        self.assertEqual(r['ausentes'],[])
        self.assertNotIn('historico_ausencias',r)

    def test_volume_sem_n_mais_um_e_tempo_no_mesmo_cenario(self):
        fixtures=Cliente.objects.bulk_create([Cliente(nome=f'Volume fictício {i}') for i in range(200)])
        Venda.objects.bulk_create([Venda(cliente=fixtures[i % len(fixtures)],data_venda=date(2026,mes,1),
            tipo_pagamento='Pix',total=1) for mes in (10,11) for i in range(5000)],batch_size=2000)
        analise=self.analisar()
        tempos=[]
        for _ in range(3):
            inicio=perf_counter()
            with CaptureQueriesContext(connection) as queries:
                r=participacao_clientes(analise)
            tempos.append((perf_counter()-inicio)*1000)
            self.assertEqual(len(queries),2)
            self.assertEqual(r['diferenca'],-24)
        request=RequestFactory().get('/relatorios/vendas/',dict(periodo='personalizado',
            data_inicial='2026-11-01',data_final='2026-11-24',modo='semanal'))
        request.user=self.admin
        medicoes={}
        for nome,desativar in (('sem_bloco',True),('com_bloco',False)):
            duracoes=[]
            for _ in range(3):
                with patch('estoque.views_relatorios.timezone.localdate',return_value=HOJE):
                    if desativar:
                        with patch('estoque.views_relatorios.participacao_clientes',return_value=None):
                            inicio=perf_counter(); resposta=pagina_vendas(request)
                    else:
                        inicio=perf_counter(); resposta=pagina_vendas(request)
                duracoes.append((perf_counter()-inicio)*1000)
                self.assertEqual(resposta.status_code,200)
            medicoes[nome]=round(median(duracoes),2)
        print('CLIENTES_BENCHMARK '+json.dumps(dict(vendas=Venda.objects.count(),
            agregacao_mediana_ms=round(median(tempos),2),**medicoes)),flush=True)
        self.assertLess(max(tempos),2000)


class ParticipacaoVisualTests(FixturesClientes,PostgreSQLIsoladoTests,StaticLiveServerTestCase):
    def test_details_teclado_navegacao_mobile_desktop_e_nove_indicadores(self):
        self.criar_cenario()
        self.client.force_login(self.admin)
        edge=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        self.assertTrue(Path(edge).is_file())
        with TemporaryDirectory(prefix='clientes-browser-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(edge,perfil).start()
            try:
                tab=browser.tab(); tab.call('Page.bringToFront')
                tab.call('Emulation.setFocusEmulationEnabled',{'enabled':True})
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs',{'urls':['https://*']})
                tab.call('Network.setCookie',dict(name=settings.SESSION_COOKIE_NAME,
                    value=self.client.cookies[settings.SESSION_COOKIE_NAME].value,url=self.live_server_url))
                with patch('estoque.views_relatorios.timezone.localdate',return_value=HOJE):
                    for largura in (320,390,1366):
                        tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,
                            deviceScaleFactor=1,mobile=largura<600))
                        url=self.live_server_url+'/relatorios/vendas/?periodo=personalizado&data_inicial=2026-11-01&data_final=2026-11-24&modo=semanal'
                        tab.call('Page.navigate',{'url':url})
                        tab.wait("document.readyState==='complete' && !!document.getElementById('participacao-clientes')")
                        self.assertFalse(tab.evaluate("document.getElementById('participacao-clientes').open"))
                        tab.evaluate("document.querySelector('#participacao-clientes > summary').focus()")
                        self.assertTrue(tab.evaluate("document.activeElement.matches('#participacao-clientes > summary')"))
                        tab.call('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'))
                        tab.call('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13))
                        tab.wait("document.getElementById('participacao-clientes').open")
                        self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('#participacao-clientes .relatorios-tabela')].every(e=>e.scrollWidth <= e.clientWidth + 1)"))
                        self.assertEqual(tab.evaluate("document.querySelectorAll('.relatorios-indicadores > div').length"),9)
                        self.assertEqual(tab.evaluate("document.querySelectorAll('#participacao-clientes a').length"),0)
                        self.assertTrue(tab.evaluate("!!document.querySelector('#participacao-clientes [data-clientes-total]')"))
                        self.assertEqual(tab.evaluate("document.querySelectorAll('#participacao-clientes details:not(.cliente-historico)').length"),1)
                        self.assertEqual(tab.evaluate("document.querySelectorAll('#participacao-clientes table').length"),6)
                        self.assertTrue(tab.evaluate("document.querySelector('#clientes-aumentos').getBoundingClientRect().height > 0"))
                        tab.evaluate("document.getElementById('participacao-clientes').scrollIntoView()")
                        pasta=Path('artifacts/auditoria-clientes-apresentacao'); pasta.mkdir(parents=True,exist_ok=True)
                        (pasta/f'{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                        for secao in ('clientes-atencao','clientes-aumentos','clientes-ausentes'):
                            tab.evaluate(f"document.getElementById('{secao}').scrollIntoView()")
                            self.assertTrue(tab.evaluate(f"[...document.querySelectorAll('#participacao-clientes .relatorios-tabela')].filter(e=>e.getBoundingClientRect().top>=document.getElementById('{secao}').getBoundingClientRect().top)[0].innerText.includes('Variação (%)')"))
                            self.assertTrue(tab.evaluate("[...document.querySelectorAll('.cliente-comportamento')].every(e=>e.getBoundingClientRect().height>0 && getComputedStyle(e).whiteSpace==='normal')"))
                            (pasta/f'{largura}-{secao}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                        tab.evaluate("document.querySelector('#analise-desempenho').closest('section').querySelector('a').click()")
                        tab.wait("location.hash==='#evolucao-diaria' && document.readyState==='complete'")
                        self.assertTrue(tab.evaluate("location.search.includes('data_inicial=2026-11-01') && location.search.includes('data_final=2026-11-24')"))
                        # O ranking compartilha o padrão responsivo; conferir sua preservação.
                        tab.call('Page.navigate',{'url':self.live_server_url+'/relatorios/produtos/?periodo=personalizado&data_inicial=2026-11-01&data_final=2026-11-24'})
                        tab.wait("document.readyState==='complete' && !!document.querySelector('.ranking-produtos tbody tr')")
                        self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                        if largura < 700:
                            self.assertTrue(tab.evaluate("[...document.querySelectorAll('.ranking-produtos td[data-label]')].every(e=>getComputedStyle(e).display==='flex')"))
            finally:
                browser.stop()
