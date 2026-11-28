"""Investigação comercial: exclusivamente dados fictícios e PostgreSQL isolado."""
import base64
import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from offline.browser_support import Chrome
from inventario.tests import produto as criar_produto
from .models import Categoria, Cliente, Produto, Venda, ItemVenda
from .services.analise_desempenho import desempenho_comercial
from .services.investigacao_comercial import (
    investigar_classes, investigar_produtos, investigar_clientes_produto,
    outros_produtos_somente_atual, chave_classe, ordenar_coluna,
)
from .tests_relatorios import PostgreSQLIsoladoTests

HOJE = date(2026, 11, 25)
FILTROS = dict(periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-24',
               investigar='classes', modo='semanal', semanas_comparacao='4', comparacao='anterior')


class CenarioInvestigacao:
    def criar(self):
        self.admin = get_user_model().objects.create_user('investigacao-admin', is_staff=True, is_superuser=True)
        self.c1 = Cliente.objects.create(nome='Mesmo Nome')
        self.c2 = Cliente.objects.create(nome='Mesmo Nome')
        self.c3 = Cliente.objects.create(nome='Cadastro com ausência do produto')
        self.p1 = criar_produto(nome='Refrigerante fictício', categoria='Refrigerantes e Afins')
        self.p2 = criar_produto(nome='Produto em crescimento', categoria=self.p1.categoria)
        self.p3 = criar_produto(nome='Produto em KG', categoria='Classe B')
        self.p4 = criar_produto(nome='Produto somente atual', categoria=self.p1.categoria)
        self.sem_vendas = criar_produto(nome='Sem vendas', categoria='Classe sem vendas')
        for mes in (10, 11):
            Venda.objects.bulk_create([Venda(data_venda=date(2026, mes, dia), total=0, tipo_pagamento='Pix')
                                      for dia in range(1, 25)])
        self.nota(self.c1, 10, [(self.p1, 'PCT', 6, 372), (self.p1, ' pct ', 7, 434)])
        self.nota(self.c2, 10, [(self.p1, 'UN', 2, 20), (self.p2, 'PCT', 1, 50)])
        self.nota(self.c3, 10, [(self.p1, 'UN', 3, 30)])
        self.nota(self.c1, 10, [(self.p3, 'KG', 10, 100)])
        self.nota(self.c1, 11, [(self.p1, 'PCT', 2, 126), (self.p3, 'KG', 2, 20)])
        self.nota(self.c2, 11, [(self.p1, 'UN', 1, 11), (self.p2, 'PCT', 4, 200)])
        self.nota(self.c3, 11, [(self.p4, 'UN', 1, 42)])
        for mes in (10, 11):
            self.nota(self.c1, mes, [(self.p1, 'PCT', 1, 9999)], cancelada=True)
            self.nota(self.c1, mes, [(self.p1, 'PCT', 1, 8888)], tipo_pagamento=' CONSUMO PRÓPRIO ')

    def nota(self, cliente, mes, itens, dia=1, **kwargs):
        venda = Venda.objects.create(cliente=cliente, data_venda=date(2026, mes, dia),
            total=sum((D(str(i[3])) for i in itens), D(0)), tipo_pagamento=kwargs.pop('tipo_pagamento', 'Pix'), **kwargs)
        for produto, unidade, quantidade, valor in itens:
            ItemVenda.objects.create(venda=venda, produto=produto, unidade=unidade,
                quantidade=D(str(quantidade)), valor_total=D(str(valor)), preco_unitario=D('999.99'))
        return venda

    def analise(self):
        return desempenho_comercial(date(2026,11,1), date(2026,11,24), HOJE)

    def classes(self):
        return investigar_classes(self.analise())

    def classe(self):
        return self.classes()['por_chave'][chave_classe(self.p1.categoria)]

    def get(self, url, **params):
        with patch('estoque.views_relatorios.timezone.localdate', return_value=HOJE):
            return self.client.get(url, {**FILTROS, **params})


class InvestigacaoComercialTests(CenarioInvestigacao, PostgreSQLIsoladoTests, TestCase):
    def setUp(self):
        self.criar()

    def test_concilia_classes_notas_e_produtos_sem_duplicar_venda_com_varios_itens(self):
        analise = self.analise()
        with self.assertNumQueries(2):
            r = investigar_classes(analise)
        self.assertTrue(r['disponivel'])
        self.assertEqual((r['anterior'], r['atual'], r['diferenca']), (1006, 399, -607))
        self.assertEqual(sum(l['diferenca'] for l in r['linhas']), D('-607'))
        self.assertEqual(r['queda']['nome'], self.p1.categoria)
        classe = r['por_chave'][chave_classe(self.p1.categoria)]
        self.assertEqual((classe['anterior'], classe['atual']), (906, 379))
        self.assertIn(chave_classe('Classe sem vendas'), r['por_chave'])
        with self.assertNumQueries(2):
            p = investigar_produtos(analise, classe)
        self.assertEqual((p['queda']['id'],p['alta']['id']), (self.p1.pk,self.p2.pk))
        self.assertEqual(sum(l['diferenca'] for l in p['linhas']), classe['diferenca'])

    def test_quantidades_unidades_preco_efetivo_notas_e_cadastros_distintos(self):
        r = investigar_produtos(self.analise(), self.classe())
        p = next(l for l in r['linhas'] if l['id']==self.p1.pk)
        self.assertEqual((p['notas_anterior'],p['clientes_anterior'],p['notas_atual'],p['clientes_atual']), (3,3,2,2))
        u = {l['unidade_comercial']:l for l in p['unidades']}
        self.assertEqual(set(u), {'PCT','UN'})
        self.assertEqual((u['PCT']['quantidade_anterior'],u['PCT']['quantidade_atual']), (13,2))
        self.assertEqual((u['PCT']['preco_anterior'],u['PCT']['preco_atual']), (62,63))
        self.assertEqual((u['UN']['quantidade_anterior'],u['UN']['quantidade_atual']), (5,1))
        self.assertEqual(p['diferenca'],-719)

    def test_clientes_mesmo_nome_nao_unem_ids_ausencia_e_cesta_nova_nao_provam_substituicao(self):
        analise=self.analise()
        with self.assertNumQueries(4):
            r=investigar_clientes_produto(analise,self.p1.pk)
        self.assertEqual(len(r['por_id']),3)
        self.assertEqual(r['ausentes'],1)
        self.assertEqual(r['por_id'][self.c1.pk]['diferenca'],-680)
        self.assertEqual(r['por_id'][self.c2.pk]['diferenca'],-9)
        self.assertEqual(r['por_id'][self.c3.pk]['notas_atual'],0)
        self.assertEqual(sum(c['diferenca'] for c in r['linhas']),-719)
        outros=outros_produtos_somente_atual(analise,self.p1.pk,self.c3.pk)
        self.assertEqual([l['produto_id'] for l in outros],[self.p4.pk])
        self.assertEqual(outros[0]['atual'],42)

    def test_unidade_escolhida_restringe_investigacao_e_notas(self):
        r=investigar_clientes_produto(self.analise(),self.p1.pk,unidade='UN')
        self.assertEqual((r['resumo']['anterior'],r['resumo']['atual']),(50,11))
        self.assertNotIn(self.c1.pk,r['por_id'])
        self.client.force_login(self.admin)
        resposta=self.get(reverse('estoque:relatorios_produto',args=[self.p1.pk]),medida='quantidade',unidade='PCT',cliente_investigado=str(self.c1.pk))
        self.assertEqual(resposta.status_code,200)
        self.assertEqual({n['unidade_comercial'] for n in resposta.context['notas_antes']},{'PCT'})
        self.assertEqual(resposta.context['clientes_produto_investigados']['resumo']['anterior'],806)

    def test_zero_ausencia_unidade_vazia_e_sem_vinculo_preservam_valores(self):
        r=investigar_produtos(self.analise(),self.classe())
        novo=next(l for l in r['linhas'] if l['id']==self.p4.pk)
        self.assertIsNone(novo['percentual'])
        self.assertEqual(novo['anterior'],0)
        self.nota(None,11,[(self.p1,'',1,7)])
        r=investigar_clientes_produto(self.analise(),self.p1.pk)
        self.assertIn(None,r['por_id'])
        self.assertEqual(r['resumo']['clientes_atual'],2)
        u=next(u for u in r['por_id'][None]['unidades'] if u['unidade_comercial']=='')
        self.assertIsNone(u['quantidade_atual']);self.assertIsNone(u['preco_atual'])
        self.client.force_login(self.admin)
        response=self.get(reverse('estoque:relatorios_produto',args=[self.p1.pk]),cliente_investigado='sem-cliente')
        self.assertNotIn('outros_produtos_investigados',response.context)

    def test_classes_literais_sem_classe_e_sem_produto_nao_colidem(self):
        for categoria in ('Biscoitos','BISCOITOS',None,'Sem classe informada'):
            p=criar_produto(nome='Mesmo produto',categoria=categoria)
            self.nota(self.c1,10,[(p,'UN',1,3)])
        self.nota(self.c1,11,[(None,'UN',1,5)])
        r=self.classes()
        for chave in ('c:Biscoitos','c:BISCOITOS','sem-classe','c:Sem classe informada','sem-produto'):
            self.assertIn(chave,r['por_chave'])
        self.assertEqual(r['por_chave']['sem-produto']['atual'],5)
        self.assertEqual(sum(l['diferenca'] for l in r['linhas']),r['diferenca'])

    def test_variedades_mapeamento_confirmado_concilia_sem_escrever_cadastros(self):
        self.assertEqual(chave_classe('VARIEDADES'),'c:Variedades')
        for texto in ('variedades', 'Variedade', ' VARIEDADES ', 'BISCOITOS', 'HORTEFRUTEGRANJEIROS'):
            self.assertEqual(chave_classe(texto),'c:'+texto)
        Categoria.objects.create(pk=34,nome='Variedades',ativa=True)
        oficial=criar_produto(nome='Variedades fictício',categoria='Variedades')
        legado=criar_produto(nome='Pilha fictícia',categoria='VARIEDADES')
        self.nota(self.c1,10,[(oficial,'UN',1,D('849.10'))])
        self.nota(self.c1,11,[(oficial,'UN',1,D('842.60'))])
        self.nota(self.c1,10,[(legado,'CX',1,D('58.00'))])
        notas=[self.nota(self.c1,11,[(legado,unidade,q,D(valor))])
               for unidade,q,valor in [('UN',1,'4.80'),('UN',D('.5'),'2.40'),('CX',1,'58.00')]]
        a=self.analise()
        with self.assertNumQueries(2): r=investigar_classes(a)
        self.assertNotIn('c:VARIEDADES',r['por_chave'])
        classe=r['por_chave']['c:Variedades']
        self.assertEqual((classe['anterior'],classe['atual'],classe['diferenca']),(D('907.10'),D('907.80'),D('.70')))
        self.assertEqual(sum(l['anterior'] for l in r['linhas']),a['anterior'])
        self.assertEqual(sum(l['atual'] for l in r['linhas']),a['atual'])
        produtos=investigar_produtos(a,classe)
        por_id={l['id']:l for l in produtos['linhas']}
        self.assertEqual(set(por_id),{oficial.pk,legado.pk})
        self.assertEqual(por_id[legado.pk]['anterior'],D('58.00'))
        self.assertEqual(por_id[legado.pk]['atual'],D('65.20'))
        self.assertEqual({u['unidade_comercial'] for u in por_id[legado.pk]['unidades']},{'CX','UN'})
        self.client.force_login(self.admin)
        url=reverse('estoque:relatorios_produtos')
        for chave in ('c:Variedades','c:VARIEDADES'):
            resp=self.get(url,classe=chave,ordenar_por='atual',ordem_coluna='desc')
            self.assertEqual(resp.status_code,200)
            self.assertEqual(resp.context['classe_selecionada']['chave'],'c:Variedades')
            self.assertIn('classe=c%3AVariedades',resp.context['investigacao_query'])
            self.assertEqual({l['id'] for l in resp.context['produtos_investigados']['linhas']},{oficial.pk,legado.pk})
            resp=self.get(reverse('estoque:relatorios_produto',args=[legado.pk]),classe=chave,cliente_investigado=self.c1.pk)
            self.assertEqual(resp.status_code,200)
            self.assertIn('classe=c%3AVariedades',resp.context['classes_produto_url'])
            self.assertEqual({n['venda_id'] for n in resp.context['notas_agora']},{n.pk for n in notas})
        legado.refresh_from_db();self.assertEqual(legado.categoria,'VARIEDADES')
        self.assertTrue(Categoria.objects.get(pk=34).ativa)

    def test_divergencia_de_um_centavo_interrompe_sem_rateio_nem_alteracao(self):
        venda=Venda.objects.filter(cliente=self.c1,cancelada=False,data_venda=date(2026,10,1),total=806).get()
        Venda.objects.filter(pk=venda.pk).update(total=D('806.01'))
        analise=self.analise()
        with self.assertNumQueries(1):
            r=investigar_classes(analise)
        self.assertFalse(r['disponivel']);self.assertTrue(r['divergencia'])
        self.assertNotIn('linhas',r)
        self.assertEqual(Venda.objects.get(pk=venda.pk).total,D('806.01'))

    def test_periodos_sem_cobertura_e_incomparaveis_nao_consultam_itens(self):
        Venda.objects.filter(data_venda=date(2026,10,12)).delete()
        a=self.analise()
        self.assertFalse(a['comparavel'])
        with self.assertNumQueries(0):
            self.assertFalse(investigar_classes(a)['disponivel'])
        for inicio,fim in ((HOJE,HOJE),(date(2026,10,1),date(2026,11,24))):
            a=desempenho_comercial(inicio,fim,HOJE)
            with self.assertNumQueries(0):
                self.assertFalse(investigar_classes(a)['disponivel'])
                self.assertFalse(investigar_produtos(a, {})['disponivel'])
                self.assertFalse(investigar_clientes_produto(a,self.p1.pk)['disponivel'])

    def test_crescimento_de_classe_com_base_zero_e_ordem_por_reais(self):
        p=criar_produto(nome='Crescimento fictício',categoria='Classe positiva')
        self.nota(self.c1,11,[(p,'UN',1,500)])
        r=self.classes()
        self.assertEqual(r['alta']['nome'],'Classe positiva')
        self.assertEqual(r['alta']['diferenca'],500)
        self.assertIsNone(r['alta']['percentual'])
        self.assertEqual(r['linhas'][0]['nome'],self.p1.categoria)
        self.assertEqual(investigar_classes(self.analise(),'altas')['linhas'][0]['nome'],'Classe positiva')
        ItemVenda.objects.filter(venda__data_venda__month=10).update(valor_total=0)
        Venda.objects.filter(data_venda__month=10).update(total=0)
        r=self.classes()
        self.assertTrue(r['disponivel'])
        self.assertIsNone(r['percentual'])
        self.assertTrue(all(l['pontos'] is None for l in r['linhas']))
        clientes=investigar_clientes_produto(self.analise(),self.p1.pk)
        self.assertIsNone(clientes['por_id'][self.c1.pk]['percentual'])
        self.assertNotIn('somente',clientes['por_id'][self.c1.pk]['comportamento'])

    def test_notas_paginadas_e_retorno_da_classe_preservam_contexto(self):
        for _ in range(26):
            self.nota(self.c1,10,[(self.p1,'PCT',1,62)])
        self.client.force_login(self.admin)
        url=reverse('estoque:relatorios_produto',args=[self.p1.pk])
        from urllib.parse import urlencode
        origem=urlencode({**FILTROS,'classe':chave_classe(self.p1.categoria),'pagina_investigacao':'2'})
        response=self.get(url,cliente_investigado=str(self.c1.pk),notas_antes='2',ranking=origem)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['notas_antes'].number,2)
        self.assertEqual(len(response.context['notas_antes']),2)
        self.assertContains(response,'notas_antes=1')
        self.assertIn('pagina_investigacao=2',response.context['classes_produto_url'])
        self.assertNotIn('cliente_investigado',dict(response.context['campos_investigacao_produto']))
        self.assertEqual(self.get(url,comparacao='invalida').status_code,400)

    def test_periodos_completos_parciais_e_fronteiras(self):
        self.nota(self.c1,10,[(self.p1,'UN',1,10)],dia=25)
        self.nota(self.c1,11,[(self.p1,'UN',1,11)],dia=25)
        parcial=desempenho_comercial(date(2026,11,1),HOJE,HOJE)
        self.assertTrue(parcial['inclui_hoje'])
        self.assertEqual((investigar_classes(parcial)['anterior'],investigar_classes(parcial)['atual']),(1006,399))
        for mes,ultimo in ((10,31),(11,30)):
            Venda.objects.bulk_create([Venda(data_venda=date(2026,mes,d),total=0,tipo_pagamento='Pix') for d in range(26,ultimo+1)])
        completo=desempenho_comercial(date(2026,11,1),date(2026,11,30),date(2026,12,1))
        self.assertTrue(completo['comparavel'])
        self.assertEqual((completo['referencia_inicio'],completo['referencia_fim']),(date(2026,10,1),date(2026,10,31)))
        r=investigar_classes(completo)
        self.assertEqual((r['anterior'],r['atual']),(1016,410))

    def test_rotas_protegidas_filtros_ranking_legado_e_conciliacao(self):
        url=reverse('estoque:relatorios_produtos')
        self.assertEqual(self.get(url).status_code,302)
        comum=get_user_model().objects.create_user('investigacao-comum')
        self.client.force_login(comum);self.assertEqual(self.get(url).status_code,403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url, FILTROS).status_code,405)
        response=self.get(url)
        self.assertContains(response,'Quais classes mudaram as vendas?')
        self.assertContains(response,'Total — confere ao centavo')
        response=self.get(url,classe=chave_classe(self.p1.categoria))
        self.assertContains(response,'13 → 2')
        self.assertContains(response,'semanas_comparacao=4')
        self.assertEqual(self.get(url,classe='c:inexistente').status_code,400)
        produto_url=reverse('estoque:relatorios_produto',args=[self.p1.pk])
        response=self.get(produto_url,classe=chave_classe(self.p1.categoria),cliente_investigado=str(self.c3.pk))
        self.assertContains(response,'não comprova substituição')
        self.assertContains(response,self.p4.nome)
        self.assertContains(response,'Notas anteriores')
        self.assertNotContains(response,'data-comprador-pagina')
        self.assertEqual(self.get(produto_url,cliente_investigado='9999999').status_code,400)
        self.assertEqual(self.get(produto_url,classe='c:outra').status_code,400)
        legado=self.client.get(url,{k:v for k,v in FILTROS.items() if k!='investigar'})
        self.assertContains(legado,'Vendas por produto')
        self.assertNotContains(legado,'Quais classes mudaram as vendas?')

    def test_volume_consultas_constantes_e_paginacao_sem_n_mais_um(self):
        produtos=Produto.objects.bulk_create([Produto(nome=f'Volume fictício {i}',categoria=self.p1.categoria) for i in range(150)])
        vendas=Venda.objects.bulk_create([Venda(data_venda=date(2026,mes,1),cliente=self.c1,total=1,tipo_pagamento='Pix') for mes in (10,11) for p in produtos])
        ItemVenda.objects.bulk_create([ItemVenda(venda=v,produto=produtos[i%len(produtos)],unidade='UN',quantidade=1,preco_unitario=1,valor_total=1) for i,v in enumerate(vendas)])
        a=self.analise();c=investigar_classes(a)['por_chave'][chave_classe(self.p1.categoria)]
        inicio=perf_counter()
        with CaptureQueriesContext(connection) as sql:
            r=investigar_produtos(a,c,pagina=2)
        duracao=(perf_counter()-inicio)*1000
        self.assertEqual(len(sql),2)
        self.assertEqual(len(r['linhas']),25)
        self.assertTrue(r['linhas'].has_next())
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in sql))
        self.assertLess(duracao,2000)
        print('INVESTIGACAO_BENCHMARK '+json.dumps(dict(produtos=r['linhas'].paginator.count,consultas=len(sql),ms=round(duracao,2))),flush=True)


    def test_classes_zeradas_opcionais_sem_alterar_totais_ou_historico(self):
        self.client.force_login(self.admin)
        antiga = criar_produto(nome='Produto histórico', categoria='Classe que zerou', ativo=False)
        self.nota(self.c1, 10, [(antiga, 'UN', 1, 17)])
        url = reverse('estoque:relatorios_produtos')
        resposta = self.get(url)
        contexto = resposta.context
        nomes = [l['nome'] for l in contexto['classes_exibidas']]
        self.assertIn('Classe que zerou', nomes)
        self.assertIn('Classe B', nomes)
        self.assertNotIn('Classe sem vendas', nomes)
        self.assertContains(resposta, 'Mostrar classes sem vendas')
        self.assertNotContains(resposta, 'Impacto (p.p.)')
        self.assertEqual(sum(l['anterior'] for l in contexto['classes_exibidas']), contexto['investigacao']['anterior'])
        self.assertEqual(sum(l['atual'] for l in contexto['classes_exibidas']), contexto['investigacao']['atual'])
        self.assertIn(chave_classe('Classe sem vendas'), contexto['investigacao']['por_chave'])
        aberta = self.get(url, classes_sem_vendas='1', direcao='quedas')
        self.assertIn('Classe sem vendas', [l['nome'] for l in aberta.context['classes_exibidas']])
        self.assertEqual(aberta.context['investigacao']['anterior'], contexto['investigacao']['anterior'])
        self.assertEqual(aberta.context['investigacao']['atual'], contexto['investigacao']['atual'])
        self.assertContains(aberta, 'Ocultar classes sem vendas')
        for opcao in aberta.context['ordens_investigacao']:
            self.assertIn('classes_sem_vendas=1', opcao['url'])
            self.assertIn('data_inicial=2026-11-01', opcao['url'])
        self.assertTrue(all('classes_sem_vendas=1' in l['url'] for l in aberta.context['classes_exibidas']))

    def test_produtos_compactos_preservam_evidencias_de_varias_unidades(self):
        self.client.force_login(self.admin)
        r = self.get(reverse('estoque:relatorios_produtos'), classe=chave_classe(self.p1.categoria))
        self.assertContains(r, 'produto-investigacao-detalhes')
        self.assertContains(r, 'Quantidades, preços e notas')
        self.assertContains(r, 'Clientes cadastrados:')
        self.assertContains(r, '<strong>PCT</strong>')
        self.assertContains(r, '<strong>UN</strong>')
        self.assertContains(r, '13 → 2')
        self.assertContains(r, 'Preço médio:')
        self.assertContains(r, 'Investigar clientes e notas')
        self.assertNotContains(r, '<th scope="col">Quantidade / preço</th>')


    def test_ordenacao_numerica_nulos_empates_e_integridade_sem_mutacao(self):
        linhas = [dict(id=i, nome=n, anterior=D(a), atual=D(b), diferenca=D(b)-D(a), percentual=p,
                       unidades=[{'unidade': 'UN', 'produto': i}])
                  for i,n,a,b,p in [(1,'Água','100','35',D('-65')), (2,'Biscoito','1000','820',D('-18')),
                                    (3,'Cerveja','0','1200',None), (4,'Doce','200','200',D('0')),
                                    (5,'Estiva','100','190',D('90')), (6,'Farinha','0','0',None)]]
        original = [dict(l) for l in linhas]
        for coluna in ('nome','anterior','atual','diferenca','percentual'):
            for sentido in ('asc','desc'):
                r = ordenar_coluna(linhas,coluna,sentido)
                self.assertEqual({id(l) for l in r},{id(l) for l in linhas})
                self.assertEqual(linhas,original)
                presentes=[l[coluna] for l in r if l[coluna] is not None]
                if coluna!='nome': self.assertEqual(presentes,sorted(presentes,reverse=sentido=='desc'))
                if coluna=='percentual': self.assertEqual([l['id'] for l in r[-2:]],[3,6])
        self.assertEqual([l['id'] for l in ordenar_coluna(linhas,'nome','asc')],list(range(1,7)))
        self.assertEqual([l['id'] for l in ordenar_coluna(linhas,'percentual','asc')][:2],[1,2])
        with self.assertRaises(ValueError): ordenar_coluna(linhas,'sql')

    def test_ordenacao_global_antes_da_paginacao_sem_consultas_adicionais(self):
        produtos=[criar_produto(nome=f'Item {i:02}',categoria=self.p1.categoria) for i in range(30)]
        self.nota(self.c1,10,[(p,'UN',1,100+i) for i,p in enumerate(produtos)])
        self.nota(self.c1,11,[(p,'UN',1,i*10) for i,p in enumerate(produtos)])
        analise=self.analise();classe=investigar_classes(analise)['por_chave'][chave_classe(self.p1.categoria)]
        for coluna in ('nome','anterior','atual','diferenca','percentual'):
            for sentido in ('asc','desc'):
                paginas=[]
                for numero in (1,2):
                    with self.assertNumQueries(2):
                        r=investigar_produtos(analise,classe,pagina=numero,coluna=coluna,sentido=sentido)
                        paginas.extend(list(r['linhas']))
                self.assertEqual(len(paginas),33)
                self.assertEqual(len({l['id'] for l in paginas}),33)
                self.assertEqual([l['id'] for l in paginas],[l['id'] for l in ordenar_coluna(paginas,coluna,sentido)])
                self.assertTrue(all('unidades' in l for l in paginas))

    def test_cabecalhos_filtros_validacao_e_presets_sem_conflito(self):
        self.client.force_login(self.admin)
        url=reverse('estoque:relatorios_produtos')
        for coluna in ('nome','anterior','atual','diferenca','percentual'):
            for sentido in ('asc','desc'):
                r=self.get(url,ordenar_por=coluna,ordem_coluna=sentido)
                linhas=r.context['classes_exibidas']
                self.assertEqual(linhas,ordenar_coluna(linhas,coluna,sentido))
                self.assertContains(r,'aria-sort="'+('ascending' if sentido=='asc' else 'descending')+'"')
                for opcao in r.context['ordens_investigacao']:
                    self.assertFalse(opcao['selecionada'])
                    self.assertNotIn('ordenar_por=',opcao['url'])
                for h in r.context['cabecalhos_ordenacao']:
                    self.assertIn('data_inicial=2026-11-01',h['url'])
                    self.assertNotIn('pagina_investigacao=',h['url'])
        self.assertEqual(self.get(url,ordenar_por='invalida').status_code,400)
        self.assertEqual(self.get(url,ordenar_por='nome',ordem_coluna='invalida').status_code,400)


class InvestigacaoVisualTests(CenarioInvestigacao, PostgreSQLIsoladoTests, StaticLiveServerTestCase):
    def conferir_ordenacao(self, tab, produtos=False):
        tab.evaluate("window.sentinelaOrdenacao='documento-preservado'")
        capturar=r"""JSON.stringify([...document.querySelectorAll('.tabela-ordenavel tbody tr')].map(r=>[r.dataset.registro||r.cells[0].textContent.trim(),r.textContent.replace(/\s+/g,' ').trim()]).sort((a,b)=>a[0].localeCompare(b[0])))"""
        original=tab.evaluate(capturar)
        total=tab.evaluate("document.querySelector('.tabela-ordenavel tfoot')?.innerText||''")
        if produtos:
            tab.evaluate("document.querySelector('.produto-investigacao-detalhes').open=true")
            aberto=tab.evaluate("document.querySelector('tr[data-registro]:has(details[open])').dataset.registro")
        for coluna in ('nome','anterior','atual','diferenca','percentual'):
            for sentido in (('asc','desc') if coluna=='nome' else ('desc','asc')):
                if coluna == 'nome' and sentido == 'asc':
                    tab.evaluate("document.querySelector('[data-coluna=nome]').focus()")
                    tab.call('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'))
                    tab.call('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13))
                else:
                    tab.evaluate(f"document.querySelector('[data-coluna=\"{coluna}\"]').click()")
                tab.wait(f"new URL(location.href).searchParams.get('ordenar_por')==='{coluna}' && new URL(location.href).searchParams.get('ordem_coluna')==='{sentido}' && !document.getElementById('investigacao-resultados').hasAttribute('aria-busy')")
                self.assertEqual(tab.evaluate("window.sentinelaOrdenacao"),'documento-preservado')
                if coluna != 'nome':
                    indice = ('nome','anterior','atual','diferenca','percentual').index(coluna)
                    valores=tab.evaluate(f"[...document.querySelectorAll('.tabela-ordenavel tbody tr')].map(r=>{{const t=r.cells[{indice}].textContent;return t.includes('Sem base')?null:Number(t.replace(/[^0-9,-]/g,'').replace(',','.'))}})")
                    presentes=[v for v in valores if v is not None]
                    self.assertEqual(presentes,sorted(presentes,reverse=sentido=='desc'))
                    self.assertEqual(valores,presentes+[None]*(len(valores)-len(presentes)))
                self.assertEqual(tab.evaluate(capturar),original)
                self.assertEqual(tab.evaluate("document.querySelector('.tabela-ordenavel tfoot')?.innerText||''"),total)
                self.assertEqual(tab.evaluate("document.querySelector('th[aria-sort]').getAttribute('aria-sort')"),'ascending' if sentido=='asc' else 'descending')
                self.assertTrue(tab.evaluate("document.activeElement.matches('[data-ordenacao-link]')"))
                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                if produtos: self.assertEqual(tab.evaluate("document.querySelector('tr[data-registro]:has(details[open])').dataset.registro"),aberto)
        # Cliques rápidos não podem restaurar a resposta antiga nem recarregar o documento.
        tab.evaluate("document.querySelector('[data-coluna=anterior]').click();document.querySelector('[data-coluna=atual]').click()")
        tab.wait("new URL(location.href).searchParams.get('ordenar_por')==='atual' && !document.getElementById('investigacao-resultados').hasAttribute('aria-busy')")
        self.assertEqual(tab.evaluate("document.querySelector('input[name=ordenar_por]').value"),'atual')
        tab.evaluate("history.back()")
        tab.wait("new URL(location.href).searchParams.get('ordenar_por')==='percentual' && new URL(location.href).searchParams.get('ordem_coluna')==='asc' && !document.getElementById('investigacao-resultados').hasAttribute('aria-busy')")
        self.assertEqual(tab.evaluate("window.sentinelaOrdenacao"),'documento-preservado')
        if produtos: tab.evaluate("document.querySelector('details[open]').open=false")
        tab.evaluate("[...document.querySelectorAll('.investigacao-ordem a')].find(a=>a.textContent==='Maior mudança em reais').click()")
        tab.wait("!new URL(location.href).searchParams.has('ordenar_por') && !document.getElementById('investigacao-resultados').hasAttribute('aria-busy')")
        self.assertEqual(tab.evaluate("document.querySelectorAll('th[aria-sort]').length"),0)
        self.assertTrue(tab.evaluate("location.search.includes('data_inicial=2026-11-01') && location.search.includes('semanas_comparacao=4')"))

    def test_fluxo_completo_teclado_desktop_mobile_e_notas(self):
        self.criar();self.client.force_login(self.admin)
        pasta=Path('artifacts/investigacao-comercial/isolado');pasta.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(prefix='investigacao-browser-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',perfil).start()
            try:
                tab=browser.tab();tab.call('Network.enable')
                tab.call('Network.setBlockedURLs',{'urls':['https://*']})
                tab.call('Network.setCookie',dict(name=settings.SESSION_COOKIE_NAME,value=self.client.cookies[settings.SESSION_COOKIE_NAME].value,url=self.live_server_url))
                tab.call('Page.bringToFront');tab.call('Emulation.setFocusEmulationEnabled',{'enabled':True})
                from urllib.parse import urlencode
                with patch('estoque.views_relatorios.timezone.localdate',return_value=HOJE):
                    for largura in (320,390,1366):
                        tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<600))
                        tab.call('Page.navigate',{'url':self.live_server_url+'/relatorios/vendas/?'+urlencode(FILTROS)})
                        tab.wait("document.readyState==='complete' && !!document.getElementById('analise-desempenho')")
                        self.assertEqual(tab.evaluate("document.querySelectorAll('.relatorios-indicadores > div').length"),10)
                        tab.evaluate("document.querySelector('nav[aria-label=\"Investigar vendas\"] a').focus()")
                        self.assertEqual(tab.evaluate("document.activeElement.textContent"),'Investigar vendas por classes e produtos')
                        self.assertTrue(tab.evaluate("document.activeElement.getBoundingClientRect().bottom<=innerHeight"))
                        tab.call('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'))
                        tab.call('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13))
                        tab.wait("document.readyState==='complete' && document.querySelector('h1')?.textContent.includes('Quais classes')")
                        for etapa in ('classes','produtos','clientes','notas'):
                            if etapa=='classes':
                                self.conferir_ordenacao(tab)
                            if etapa=='produtos':
                                tab.evaluate("[...document.querySelectorAll('.investigacao-tabela a')].find(a=>a.textContent==='Refrigerantes e Afins').click()")
                                tab.wait("document.readyState==='complete' && !!document.getElementById('produtos-investigados')")
                                self.assertEqual(tab.evaluate("document.querySelector('.investigacao-tabela thead tr').children.length"),5)
                                self.conferir_ordenacao(tab, produtos=True)
                                tab.evaluate("document.querySelector('.produto-investigacao-detalhes summary').focus()")
                                tab.call('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'))
                                tab.call('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13))
                                tab.wait("document.querySelector('.produto-investigacao-detalhes').open")
                                self.assertTrue(tab.evaluate("document.querySelector('.produto-investigacao-evidencias').innerText.includes('PCT') && document.querySelector('.produto-investigacao-evidencias').innerText.includes('UN')"))
                                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                                tab.evaluate("document.querySelector('.produto-investigacao-detalhes summary').click()")
                                self.assertFalse(tab.evaluate("document.querySelector('.produto-investigacao-detalhes').open"))
                                tab.evaluate("document.querySelector('.investigacao-tabela').scrollIntoView()")
                                if largura == 1366:
                                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.investigacao-tabela thead th')].every((h,i)=>getComputedStyle(h).textAlign===(i===0?'left':'right') && h.scrollWidth<=h.clientWidth+1)"))
                                    self.assertLess(tab.evaluate("document.querySelector('.investigacao-tabela tbody tr').getBoundingClientRect().height"),100)

                            elif etapa=='clientes':
                                tab.evaluate("[...document.querySelectorAll('.investigacao-tabela a')].find(a=>a.textContent.toLocaleLowerCase().includes('refrigerante fictício')).click()")
                                tab.wait("document.readyState==='complete' && !!document.querySelector('.investigacao-produto')")
                                tab.evaluate("document.getElementById('compradores-produto').scrollIntoView()")
                            elif etapa=='notas':
                                tab.evaluate("[...document.querySelectorAll('.investigacao-produto .investigacao-tabela a')].find(a=>a.textContent.toLocaleLowerCase().includes('cadastro com ausência')).click()")
                                tab.wait("document.readyState==='complete' && !!document.getElementById('evidencias-cliente')")
                                tab.evaluate("document.getElementById('evidencias-cliente').scrollIntoView()")
                                self.assertTrue(tab.evaluate("document.getElementById('outros-produtos').parentElement.innerText.toLocaleLowerCase().includes('produto somente atual')"))
                            (pasta/f'{largura}-{etapa}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                            self.assertTrue(tab.evaluate("document.documentElement.scrollWidth<=innerWidth"),f'{largura} {etapa}')
                            excedentes=tab.evaluate("[...document.querySelectorAll('.investigacao-tabela')].filter(e=>e.scrollWidth>e.clientWidth+1).map(e=>({largura:e.clientWidth,conteudo:e.scrollWidth,celulas:[...e.querySelectorAll('tbody tr:first-child > *')].map(c=>({texto:c.innerText.slice(0,80),largura:c.getBoundingClientRect().width,conteudo:c.scrollWidth,whiteSpace:getComputedStyle(c).whiteSpace,display:getComputedStyle(c).display}))}))")
                            self.assertEqual(excedentes,[],f'tabela {largura} {etapa}')
                            self.assertTrue(tab.evaluate("location.search.includes('semanas_comparacao=4') && location.search.includes('data_inicial=2026-11-01')"))
                            (pasta/f'{largura}-{etapa}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                        tab.evaluate("[...document.querySelectorAll('a')].find(a=>a.textContent.includes('Voltar aos produtos da classe')).click()")
                        tab.wait("document.readyState==='complete' && !!document.getElementById('produtos-investigados')")
            finally:
                browser.stop()
