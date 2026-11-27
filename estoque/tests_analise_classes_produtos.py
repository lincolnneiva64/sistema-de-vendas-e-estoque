"""Financial and navigation regressions on guarded disposable PostgreSQL only."""
from datetime import date
from decimal import Decimal as D
from urllib.parse import urlparse, parse_qs

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from estoque.models import Produto, Venda, ItemVenda, Categoria
from estoque.services.analise_classes_produtos import analise_consolidada
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from estoque import tests_relatorios_produtos_indicadores as _evidence
from inventario.tests import produto as criar_produto


class AnaliseConsolidadaTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('consolidada-admin', is_staff=True, is_superuser=True)
        for name in ('Classe A','Classe Z','Estivas','Variedades'):
            Categoria.objects.create(nome=name,ativa=True)
        cls.p = criar_produto(nome='Produto A', categoria='Classe A', preco_compra=9999,preco_vista=10000,preco_prazo=10001)

    evidencia = _evidence.IndicadoresProdutoTests.evidencia

    def item(self, *, produto=None, q='2', valor='30', custo='5', custo_total=None, unidade='UN',
             unidade_custo=None, pagamento='Pix', cancelada=False, dia=date(2026,11,15)):
        v = Venda.objects.create(data_venda=dia, total=D(valor), tipo_pagamento=pagamento, cancelada=cancelada)
        return ItemVenda.objects.create(venda=v, produto=produto or self.p, quantidade=D(q), unidade=unidade,
            valor_total=D(valor), preco_unitario=999,
            custo_unitario_snapshot=D(custo) if custo is not None else None,
            custo_total_snapshot=D(custo_total) if custo_total is not None else (D(q)*D(custo)).quantize(D('.01')) if custo is not None else None,
            custo_unidade_snapshot=unidade_custo or unidade, custo_origem_snapshot='preco_compra_atual')

    def analise(self, **kwargs):
        return analise_consolidada(date(2026,11,1),date(2026,11,20),hoje=date(2026,11,26),**kwargs)

    def linhas(self,r):
        return [a for c in r['classes'] for p in c['produtos'] for a in p['apresentacoes']]

    def test_ponderacao_custo_preco_lucro_sem_usar_cadastro_atual(self):
        self.item(); self.item(q='3',valor='60',custo='8')
        r=self.analise();p=self.linhas(r)[0]
        self.assertEqual((p['quantidade'],p['faturamento'],p['custo'],p['lucro']),(5,90,34,56))
        self.assertEqual((p['custo_medio'],p['preco_medio'],p['lucro_unitario'],p['margem']),(D('6.80'),18,D('11.20'),D('62.22')))
        self.assertEqual(r['total']['margem'],p['margem'])
        self.assertEqual(r['diferenca_notas'],0)

    def test_medias_consolidadas_em_embalagem_sem_mudar_totais_ou_apresentacoes(self):
        self.item(unidade='PCT',q='53',valor='2544',custo='41')
        avulsa=self.item(unidade='UN',q='47',valor='164.50',custo='41',
            custo_total='128.47',unidade_custo='PCT')
        self.evidencia(avulsa,15,base='PCT',conversao='divisao')
        r=self.analise();p=r['classes'][0]['produtos'][0]
        self.assertEqual(p['unidade_medias'],'PCT')
        self.assertEqual(p['medias_equivalentes']['quantidade_total_numerador'],842)
        self.assertEqual(p['medias_equivalentes']['quantidade_total_denominador'],15)
        self.assertEqual(p['quantidade'],D(842)/15)
        self.assertEqual((p['custo_medio'],p['preco_medio'],p['lucro_unitario']),
                         (D('41.00'),D('48.25'),D('7.25')))
        self.assertEqual((p['custo'],p['faturamento'],p['lucro']),
                         (D('2301.47'),D('2708.50'),D('407.03')))
        self.assertEqual([(q['unidade'],q['quantidade']) for q in p['quantidades']],
                         [('PCT',53),('UN',47)])
        self.assertEqual({a['unidade']:a['preco_medio'] for a in p['apresentacoes']},
                         {'PCT':48,'UN':D('3.50')})

    def test_preco_total_e_medias_parciais_com_denominadores_correspondentes(self):
        self.item(unidade='FD',q='1',valor='100',custo='90')
        avulsa=self.item(unidade='KG',q='2',valor='8',custo='90',custo_total='6',unidade_custo='FD')
        self.evidencia(avulsa,30,base='FD',conversao='divisao')
        self.item(unidade='KG',q='3',valor='12',custo=None)
        p=self.analise()['classes'][0]['produtos'][0]
        self.assertEqual(p['unidade_medias'],'FD')
        self.assertEqual(p['medias_equivalentes']['quantidade_total'],D(35)/30)
        self.assertEqual(p['preco_medio'],D('102.86'))
        self.assertEqual(p['custo_medio'],D('90.00'))
        self.assertEqual(p['lucro_unitario'],D('11.25'))
        self.assertEqual((p['custo'],p['lucro'],p['com_custo'],p['itens']),(96,12,2,3))

    def test_fatores_historicos_divergentes_preservam_base_por_item(self):
        self.item(unidade='PCT',q='1',valor='40',custo='30')
        for fator in (12,15):
            avulsa=self.item(unidade='UN',q=str(fator),valor='40',custo='30',
                custo_total='30',unidade_custo='PCT')
            self.evidencia(avulsa,fator,base='PCT',conversao='divisao')
        p=self.analise()['classes'][0]['produtos'][0]
        self.assertEqual(p['unidade_medias'],'PCT')
        self.assertEqual(p['medias_equivalentes']['quantidade_total'],3)
        self.assertEqual((p['custo_medio'],p['preco_medio'],p['lucro_unitario']),(30,40,10))

    def test_painel_mantem_cmv_e_resultados_cobertos_sem_lucro_total_incompleto(self):
        from estoque.services.relatorios import resumo_vendas
        from django.template.loader import render_to_string
        self.item(q='2',valor='30',custo='5')
        self.item(q='1',valor='100',custo=None)
        resumo=resumo_vendas(date(2026,11,1),date(2026,11,20),hoje=date(2026,11,26))
        r=resumo['rentabilidade']
        self.assertFalse(r['disponivel'])
        self.assertEqual((r['cmv'],r['receita_coberta'],r['lucro_coberto']), (10,30,20))
        self.assertEqual(r['margem_coberta'],D('66.67'))
        self.assertIsNone(r['lucro']);self.assertIsNone(r['margem'])
        html=render_to_string('estoque/relatorios/vendas.html', dict(resumo=resumo))
        self.assertIn('R$ 10,00</dd>',html)
        self.assertIn('R$ 20,00</dd>',html)
        self.assertIn('66,67%</dd>',html)
        self.assertEqual(html.count('<dd class="indicador-legenda">Parcial</dd>'),3)

    def test_sem_relacao_documentada_mantem_detalhes_e_impedimento(self):
        self.item(unidade='FD',q='1',valor='100',custo='90')
        self.item(unidade='CT',q='1',valor='10',custo='5')
        p=self.analise()['classes'][0]['produtos'][0]
        self.assertIsNone(p['preco_medio']);self.assertIsNone(p['custo_medio'])
        self.assertIsNone(p['lucro_unitario'])
        self.assertTrue(p['medias_equivalentes']['impedimentos'])
        self.assertTrue(all(a['preco_medio'] is not None for a in p['apresentacoes']))

    def test_unidades_heterogeneas_nao_sao_somadas_nem_usadas_em_medias(self):
        self.item(unidade='KG');self.item(unidade='PCT',q='3')
        r=self.analise(ordem='quantidade');c=r['classes'][0]
        self.assertEqual(len(c['produtos']),1)
        self.assertEqual(len(c['produtos'][0]['apresentacoes']),2)
        self.assertIsNone(c['quantidade']);self.assertIsNone(c['custo_medio']);self.assertIsNone(c['preco_medio'])
        self.assertIsNone(r['total']['lucro_unitario'])
        self.assertEqual([{k:q[k] for k in ('unidade','quantidade')} for q in c['quantidades']],
                         [{'unidade':'KG','quantidade':D(2)},{'unidade':'PCT','quantidade':D(3)}])
        self.assertEqual([q['custo_medio'] for q in c['quantidades']],[D('5.00'),D('5.00')])

    def test_parcial_margem_sobre_receita_coberta_sem_inventar_custo(self):
        self.item(); self.item(valor='1000',custo=None)
        p=self.linhas(self.analise())[0]
        self.assertFalse(p['completa'])
        self.assertEqual((p['faturamento'],p['receita_coberta'],p['custo'],p['lucro']),(1030,30,10,20))
        self.assertEqual(p['margem'],D('66.67'));self.assertEqual(p['custo_medio'],5)
        self.assertEqual(p['lucro_unitario'],10)
        self.assertEqual((p['com_custo'],p['itens']),(1,2))
        self.client.force_login(self.admin)
        response=self.client.get(reverse('estoque:relatorios_classes_produtos'),
            dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-20'))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Custo parcial')
        self.assertContains(response,'Snapshot de custo ausente')

    def test_conversao_exige_evidencia_historica_valida_em_lote(self):
        i=self.item(unidade='DZ',q='2',custo='1',custo_total='24',unidade_custo='UN')
        self.assertIsNone(self.linhas(self.analise())[0]['lucro'])
        self.evidencia(i,12)
        p=self.linhas(self.analise())[0]
        self.assertEqual((p['custo'],p['lucro'],p['custo_medio']),(24,6,12))

    def test_snapshot_incompativel_e_unidade_ausente_nao_ganham_custo(self):
        self.item(custo_total='99'); self.item(unidade='')
        r=self.analise()
        self.assertEqual(r['total']['com_custo'],0)
        self.assertIsNone(r['total']['lucro'])
        self.assertIsNone(next(p for p in self.linhas(r) if not p['unidade'])['quantidade'])

    def test_exclui_canceladas_e_consumo_proprio_e_mantem_centavos(self):
        self.item(valor='10.01',q='1',custo='2.3456')
        self.item(valor='900',cancelada=True);self.item(valor='800',pagamento='consumo_proprio')
        r=self.analise()
        self.assertEqual(r['total']['faturamento'],D('10.01'))
        self.assertEqual(r['total']['custo'],D('2.35'))
        self.assertEqual(r['total']['lucro'],D('7.66'))

    def test_negativos_cobertos_e_total_ponderado(self):
        self.item(valor='4')
        p2=criar_produto(nome='Produto B',categoria='Classe A')
        self.item(produto=p2,valor='100',custo='10')
        r=self.analise(ordem='lucro')
        self.assertEqual(r['prejuizos'],1)
        self.assertEqual(r['total']['lucro'],74)
        self.assertEqual(r['total']['margem'],D('71.15'))
        self.assertEqual(self.linhas(r)[0]['nome'],'Produto B')

    def test_intervalo_classes_pesquisa_e_totais_do_filtro(self):
        self.item()
        p2=criar_produto(nome='Produto Z',categoria='Classe Z')
        self.item(produto=p2,valor='50')
        r=self.analise(pesquisa='produto z',classe_de='c:Classe Z',classe_ate='c:Classe Z')
        self.assertEqual(r['total']['faturamento'],50)
        self.assertEqual(r['total_itens_periodo'],80)
        self.assertEqual(r['faturamento_notas'],80)
        self.assertEqual(r['quantidade_linhas'],1)

    def test_opcoes_so_oficiais_ativas_com_mesma_regra_nos_dois_campos(self):
        from estoque.services.analise_classes_produtos import categorias_disponiveis
        from estoque.forms_relatorios import AnaliseClassesProdutosForm
        Categoria.objects.create(nome='Refrigerantes e Afins',ativa=True)
        Categoria.objects.create(nome='Classe desativada',ativa=False)
        for name in ('REFRIGERANTES','BISCOITOS','MIX DIGITAL','TAXAS','SANDALIAS','VASILHAMES'):
            criar_produto(nome='Legado '+name,categoria=name,excluido=True)
        criar_produto(nome='Pilha histórica',categoria='VARIEDADES')
        with self.assertNumQueries(1):choices=categorias_disponiveis()
        self.assertEqual({value for value,label in choices},
            {'c:Classe A','c:Classe Z','c:Estivas','c:Variedades','c:Refrigerantes e Afins'})
        form=AnaliseClassesProdutosForm(classes=choices)
        self.assertEqual(list(form.fields['classe_de'].choices)[1:],choices)
        self.assertEqual(list(form.fields['classe_ate'].choices)[1:],choices)

    def test_classe_oficial_inclui_variedades_historica_sem_perder_outros_legados(self):
        from estoque.services.analise_classes_produtos import categorias_disponiveis
        from estoque.forms_relatorios import AnaliseClassesProdutosForm
        canonical=criar_produto(nome='Produto canônico',categoria='Variedades')
        legacy=criar_produto(nome='Pilha histórica',categoria='VARIEDADES',excluido=True)
        other=criar_produto(nome='Biscoito histórico',categoria='BISCOITOS',excluido=True)
        self.item(produto=canonical,q='1',valor='20',custo='10')
        self.item(produto=legacy,q='1',valor='10',custo='4')
        self.item(produto=other,q='1',valor='5',custo='2')
        before=list(Produto.objects.order_by('pk').values())
        geral=self.analise()
        selected=self.analise(classe_de='c:Variedades')
        self.assertEqual({p['produto_id'] for c in selected['classes'] for p in c['produtos']},
            {canonical.pk,legacy.pk})
        self.assertEqual((selected['total']['faturamento'],selected['total']['custo'],selected['total']['lucro']),
            (30,14,16))
        self.assertEqual(geral['total']['faturamento'],35)
        self.assertIn('c:BISCOITOS',[c['chave'] for c in geral['classes']])
        form=AnaliseClassesProdutosForm(dict(classe_de='c:VARIEDADES',classe_ate='c:VARIEDADES'),classes=categorias_disponiveis())
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data['classe_de'],'c:Variedades')
        self.assertEqual(form.cleaned_data['classe_ate'],'c:Variedades')
        self.assertEqual(before,list(Produto.objects.order_by('pk').values()))

    def classes_para_ordenacao(self):
        self.item()
        estivas=criar_produto(nome='Pacote Estivas',categoria='Estivas')
        variedades=criar_produto(nome='Pacote Variedades',categoria='Variedades')
        self.item(produto=estivas,q='1',valor='100',custo='90')
        self.item(produto=variedades,q='1',valor='50',custo='10')

    def test_seis_ordens_preservam_totais_unidades_e_agrupamento(self):
        self.classes_para_ordenacao()
        base=self.analise()
        expected={
            'faturamento':['Estivas','Variedades','Classe A'],
            'faturamento_menor':['Classe A','Variedades','Estivas'],
            'nome':['Classe A','Estivas','Variedades'],
            'nome_desc':['Variedades','Estivas','Classe A'],
            'lucro':['Variedades','Classe A','Estivas'],
            'margem':['Variedades','Classe A','Estivas'],
        }
        for ordem,nomes in expected.items():
            with self.subTest(ordem=ordem):
                r=self.analise(ordem=ordem)
                self.assertEqual([c['nome'] for c in r['classes']],nomes)
                self.assertEqual(r['total'],base['total'])
                by_key={c['chave']:c for c in base['classes']}
                self.assertEqual({c['chave']:c for c in r['classes']},by_key)

    def test_classe_unica_intervalo_e_pesquisa_combinados(self):
        self.classes_para_ordenacao()
        unico=self.analise(classe_de='c:Estivas')
        self.assertEqual([c['nome'] for c in unico['classes']],['Estivas'])
        self.assertEqual(unico['total']['faturamento'],100)
        intervalo=self.analise(classe_de='c:Estivas',classe_ate='c:Variedades',ordem='nome_desc')
        self.assertEqual([c['nome'] for c in intervalo['classes']],['Variedades','Estivas'])
        self.assertEqual(intervalo['total']['faturamento'],150)
        combinado=self.analise(classe_de='c:Estivas',pesquisa='pacote',comparacao='ano_anterior')
        self.assertEqual(combinado['total'],unico['total'])
        self.assertEqual(self.analise(classe_ate='c:Estivas')['total']['faturamento'],130)

    def test_http_preserva_classe_direta_ordem_e_filtros_no_retorno(self):
        self.classes_para_ordenacao()
        self.client.force_login(self.admin)
        params=dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-20',
            classe_de='c:Estivas',classe_ate='',q='pacote',ordem='nome_desc',comparacao='ano_anterior',modo='mensal')
        response=self.client.get(reverse('estoque:relatorios_classes_produtos'),params)
        self.assertEqual(response.status_code,200)
        self.assertEqual([c['nome'] for c in response.context['analise']['classes']],['Estivas'])
        for field in ('classe_de','classe_ate','q','ordem','comparacao'):
            self.assertEqual(response.context['filtros'][field].value(),params[field])
        p=response.context['analise']['classes'][0]['produtos'][0]
        retorno=parse_qs(parse_qs(urlparse(p['url']).query)['retorno_consolidado'][0],keep_blank_values=True)
        for field in params:
            self.assertEqual(retorno[field],[params[field]])

    def test_comparacao_distingue_receita_quantidade_e_produto_ausente(self):
        from estoque.tests_relatorios_cobertura import completar_registros
        self.item(dia=date(2026,10,15),q='10',valor='100')
        self.item(q='5',valor='150')
        p2=criar_produto(nome='Produto ausente',categoria='Classe A')
        self.item(produto=p2,dia=date(2026,10,15),q='3',valor='30')
        completar_registros(date(2026,10,1),date(2026,11,20))
        r=self.analise(ordem='queda')
        self.assertTrue(r['comparavel'])
        linhas={p['nome']:p for p in self.linhas(r)}
        self.assertEqual(linhas['Produto A']['diferenca'],50)
        self.assertEqual(linhas['Produto A']['diferenca_quantidade'],-5)
        self.assertNotIn(p2.nome,linhas)
        self.assertEqual(r['linhas_sem_vendas_omitidas'],1)
        self.assertEqual(r['produtos_sem_vendas_omitidos'],1)
        self.assertEqual(r['quedas'],1)

    def test_linhas_zeradas_e_classes_sem_venda_nao_aparecem_em_nenhum_periodo(self):
        from estoque.tests_relatorios_cobertura import completar_registros
        from datetime import timedelta
        completar_registros(date(2026,9,1),date(2027,1,1))
        for mes in (10,11,12):
            with self.subTest(mes=mes):
                inicio=date(2026,mes,1);fim=date(2026,mes,24)
                atual=criar_produto(nome=f'Atual {mes}',categoria=f'Classe atual {mes}')
                antigo=criar_produto(nome=f'Anterior {mes}',categoria=f'Classe anterior {mes}')
                self.item(produto=atual,dia=date(2026,mes,15),q='2',valor='30')
                self.item(produto=antigo,dia=inicio-timedelta(days=7),q='2',valor='30')
                self.item(produto=atual,dia=date(2026,mes,16),unidade='KG',q='0',valor='0',custo_total='0')
                r=analise_consolidada(inicio,fim,hoje=date(2027,1,2))
                linhas=self.linhas(r)
                self.assertTrue(all(p['itens']>0 for p in linhas))
                self.assertTrue(all(p['faturamento']!=0 or any(q['quantidade'] for q in p['quantidades']) for p in linhas))
                self.assertTrue(all(c['produtos'] for c in r['classes']))
                self.assertNotIn(antigo.nome,[p['nome'] for p in linhas])
                self.assertEqual(sum(p['faturamento'] for p in linhas),r['total']['faturamento'])
                self.assertEqual(sum(p['custo'] for p in linhas),r['total']['custo'])

    def test_consultas_constantes_com_produtos_e_unidades(self):
        self.item()
        with CaptureQueriesContext(connection) as small:self.analise()
        for n in range(35):
            p=criar_produto(nome=f'Produto {n}',categoria=f'Classe {n%4}')
            self.item(produto=p)
        before=list(ItemVenda.objects.values())
        with CaptureQueriesContext(connection) as large:r=self.analise()
        self.assertEqual(len(small),len(large))
        self.assertLessEqual(len(large),6)
        self.assertEqual(r['quantidade_linhas'],36)
        self.assertEqual(before,list(ItemVenda.objects.values()))
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','DECLARE')) for q in large))

    def test_http_protegido_filtros_invalidos_e_navegacao(self):
        url=reverse('estoque:relatorios_classes_produtos')
        self.assertEqual(self.client.get(url).status_code,302)
        user=get_user_model().objects.create_user('funcionario',is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).status_code,405)
        self.item()
        params=dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-20',comparacao='ano_anterior',modo='mensal')
        response=self.client.get(url,params)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Análise de vendas por classes e produtos')
        self.assertContains(response,'aria-expanded="true"')
        p=response.context['analise']['classes'][0]['produtos'][0]
        query=parse_qs(urlparse(p['url']).query)
        self.assertEqual(query['data_inicial'],['2026-11-01'])
        self.assertEqual(query['comparacao'],['ano_anterior'])
        self.assertIn('modo=mensal',query['retorno_consolidado'][0])
        product=self.client.get(p['url'])
        self.assertEqual(product.status_code,200)
        self.assertContains(product,'Voltar à análise por classes e produtos')
        for extra in ({'classe_de':'invalid'},{'ordem':'invalid'},{'classe_de':'sem-produto','classe_ate':'c:Classe A'},{'data_inicial':'2026-12-31'}):
            response=self.client.get(url,{**params,**extra})
            self.assertEqual(response.status_code,200)
            self.assertNotIn('analise',response.context)

    def test_sem_dados_e_escape_de_nome_nao_geram_lucro_zero(self):
        r=self.analise()
        self.assertEqual(r['total']['faturamento'],0);self.assertIsNone(r['total']['lucro'])
        self.p.nome='<img src=x onerror=alert(1)>';self.p.save(update_fields=['nome'])
        self.item()
        self.client.force_login(self.admin)
        response=self.client.get(reverse('estoque:relatorios_classes_produtos'),dict(periodo='mes_atual'))
        self.assertNotContains(response,'<img src=x onerror=alert(1)>')
        self.assertContains(response,escape(self.p.nome))


class AtencaoGerencialTests(PostgreSQLIsoladoTests, TestCase):
    setUpTestData = classmethod(AnaliseConsolidadaTests.setUpTestData.__func__)
    item = AnaliseConsolidadaTests.item
    analise = AnaliseConsolidadaTests.analise
    linhas = AnaliseConsolidadaTests.linhas

    def test_queda_rotulada_percentual_e_periodos_sem_mudar_valores(self):
        from django.template.loader import render_to_string
        from estoque.tests_relatorios_cobertura import completar_registros

        self.item(q='1', valor='2336.50', custo='100', dia=date(2026, 10, 15))
        self.item(q='1', valor='1080', custo='100')
        completar_registros(date(2026, 10, 1), date(2026, 11, 20))
        r = self.analise()
        linha = r['atencao']['quedas'][0]
        self.assertEqual(linha['diferenca'], D('-1256.50'))
        self.assertEqual(linha['variacao_faturamento'], D('-53.78'))
        preview = render_to_string('estoque/relatorios/classes_produtos_queda_valores.html', dict(linha=linha))
        complete = render_to_string('estoque/relatorios/classes_produtos_atencao_lista.html',
                                    dict(linhas=[linha], comparar=True))
        self.assertIn(preview, complete)
        for text in ('Faturamento anterior', 'R$ 2.336,50', 'Faturamento atual',
                     'R$ 1.080,00', 'Queda no faturamento', 'R$ 1.256,50 (53,8%)'):
            self.assertIn(text, preview)
        self.assertNotIn('-R$', preview)
        self.assertEqual(linha['variacao_faturamento'], D('-53.78'))
        missing = render_to_string('estoque/relatorios/classes_produtos_queda_valores.html',
            dict(linha=dict(anterior=D('-10'), atual_comparado=D('-20'),
                           diferenca=D('-10'), variacao_faturamento=None)))
        self.assertIn('Percentual sem base para calcular', missing)
        self.assertNotIn('(0,0%)', missing)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('estoque:relatorios_classes_produtos'), dict(
            periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20', comparacao='anterior'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Anterior: 01/10/2026 a 20/10/2026')
        self.assertContains(response, 'Atual: 01/11/2026 a 20/11/2026')

    def test_alertas_reutilizam_apresentacoes_limites_e_impacto(self):
        other = criar_produto(nome='Produto maior receita', categoria='Classe A')
        self.item(q='1', valor='100', custo='95')
        self.item(produto=other, q='1', valor='1000', custo='910')
        self.item(q='1', valor='100', custo='100', unidade='CX')
        high = criar_produto(nome='Margem dez', categoria='Classe A')
        self.item(produto=high, q='1', valor='100', custo='90')
        loss = criar_produto(nome='Negativo', categoria='Classe A')
        self.item(produto=loss, q='1', valor='100', custo='110')
        missing = criar_produto(nome='Sem custo', categoria='Classe A')
        self.item(produto=missing, custo=None)
        r = self.analise()
        low = r['atencao']['margens']
        self.assertEqual([a['margem'] for a in low], [0, 5, 9])
        self.assertEqual(r['margens_reduzidas'], len(low))
        self.assertEqual(r['atencao']['margens_principais'][0]['produto_id'], other.pk)
        rows = self.linhas(r)
        self.assertTrue(all(any(a is row for row in rows) for a in low))
        self.assertTrue(all(a['alvo_atencao'] for a in low))
        self.assertEqual(r['prejuizos'], len(r['atencao']['prejuizos']))
        self.assertEqual(r['atencao']['prejuizos'][0]['lucro'], -10)
        filtered = self.analise(pesquisa='maior receita', ordem='nome')
        self.assertEqual(filtered['margens_reduzidas'], 1)
        self.assertEqual(filtered['atencao']['margens'][0]['produto_id'], other.pk)

    def test_quedas_incluem_apresentacao_sem_venda_atual_sem_inventar_linha(self):
        self.item(q='1', valor='200', custo='100', dia=date(2026, 10, 15))
        self.item(q='1', valor='50', custo='30')
        absent = criar_produto(nome='Somente anterior', categoria='Classe A')
        self.item(produto=absent, valor='300', dia=date(2026, 10, 15))
        from estoque.tests_relatorios_cobertura import completar_registros
        completar_registros(date(2026, 10, 1), date(2026, 11, 20))
        r = self.analise()
        drops = r['atencao']['quedas']
        self.assertEqual(r['quedas'], len(drops))
        self.assertEqual([a['diferenca'] for a in drops], [-300, -150])
        self.assertEqual([a['variacao_faturamento'] for a in drops], [-100, -75])
        self.assertNotIn('alvo_atencao', drops[0])
        self.assertEqual(drops[1]['atual_comparado'], 50)
        self.assertTrue(any(drops[1] is row for row in self.linhas(r)))

    def test_pagina_atencao_antes_tabela_links_e_zeros_sem_listas(self):
        self.item(q='1', valor='100', custo='95')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('estoque:relatorios_classes_produtos'), dict(
            periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20', comparacao='anterior'))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        self.assertLess(text.index('id="ac-atencao-titulo"'), text.index('id="tabela-consolidada"'))
        self.assertContains(response, 'data-ac-localizar=')
        self.assertContains(response, 'Consultar valores')
        self.assertContains(response, 'Fechar detalhamento')
        self.assertNotContains(response, 'linhas de produto/unidade')
        self.assertNotContains(response, 'produtos/apresentações com prejuízo')
        self.assertContains(response, 'data-ac-ir-classe=')

    def test_atencao_inicia_recolhida_com_resumo_e_tres_topicos(self):
        from html.parser import HTMLParser

        class Disclosures(HTMLParser):
            def __init__(self):
                super().__init__()
                self.outer = []
                self.topics = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'details':
                    if 'ac-atencao-gerencial' in attrs.get('class', '').split():
                        self.outer.append(attrs)
                    if 'data-ac-insight' in attrs:
                        self.topics.append(attrs)

        self.item(q='1', valor='100', custo='95')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('estoque:relatorios_classes_produtos'), dict(
            periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20'))
        parser = Disclosures()
        parser.feed(response.content.decode())
        self.assertEqual(len(parser.outer), 1)
        self.assertEqual(len(parser.topics), 3)
        self.assertTrue(all('open' not in attrs for attrs in [*parser.outer, *parser.topics]))
        self.assertContains(response, '1 margens baixas')
        self.assertContains(response, 'Margens baixas (1)')
        self.assertContains(response, 'Prejuízo bruto (0)')
        self.assertContains(response, 'Quedas de faturamento (')
