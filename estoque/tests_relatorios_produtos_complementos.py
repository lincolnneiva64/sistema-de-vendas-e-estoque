"""Quick ranking filter, validation and buyers on disposable PostgreSQL only."""
import json
import re
import subprocess
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Sum
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.translation import override

from inventario.tests import produto
from estoque.forms_relatorios import PeriodoRankingProdutosForm
from estoque.models import Cliente, ItemVenda, Venda
from estoque.services.relatorios_produtos import documentos_produto_dia, itens_comerciais
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from offline.browser_support import Chrome

ROOT=Path(__file__).resolve().parents[1]
DIA=date(2026,11,13)


class ComplementosProdutosTests(PostgreSQLIsoladoTests,TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=get_user_model().objects.create_user('complementos-admin',is_staff=True,is_superuser=True)
        cls.p=produto(nome='Skol de teste complementar',quantidade=0)
        cls.cliente=Cliente.objects.create(nome='Cliente comprador de teste')

    def setUp(self):
        self.client.force_login(self.admin)
        self.periodo=dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-21')
        self.url=reverse('estoque:relatorios_produtos')

    def item(self,*,p=None,venda=None,cliente=None,unidade='DZ',q='1',valor='45'):
        v=venda or Venda.objects.create(data_venda=DIA,total=D(valor),tipo_pagamento='Pix',cliente=cliente)
        return ItemVenda.objects.create(produto=p or self.p,venda=v,unidade=unidade,
            quantidade=D(q),preco_unitario=D(valor)/D(q),valor_total=D(valor))

    def test_filtro_rapido_reutiliza_busca_no_periodo_e_reinicia_pagina(self):
        for n in range(30): self.item(p=produto(nome=f'Produto {n:02}',quantidade=0),valor='100')
        self.item(valor='1')
        inicial=self.client.get(self.url,{**self.periodo,'ordem':'faturamento'})
        self.assertNotIn(self.p.pk,[r['produto_id'] for r in inicial.context['ranking']['linhas']])
        campos=dict(inicial.context['filtro_rapido_campos'])
        self.assertNotIn('pagina',campos);self.assertNotIn('produto_id',campos)
        resposta=self.client.get(self.url,{**campos,'q':'complem'})
        self.assertEqual([r['produto_id'] for r in resposta.context['ranking']['linhas']],[self.p.pk])
        self.assertEqual(resposta.context['ranking']['linhas'].number,1)
        self.assertEqual(resposta.context['inicio'],date(2026,11,1))
        self.assertEqual(resposta.context['busca']['ordem'].value(),'faturamento')
        self.assertEqual(resposta.context['ranking']['comercial'],D('3001'))
        self.assertContains(resposta,reverse('estoque:relatorios_produto',args=[self.p.pk]))
        form=resposta.content.decode().split('id="relatorios-filtrar-produtos"',1)[1].split('</form>',1)[0]
        self.assertNotIn('name="pagina"',form);self.assertNotIn('name="produto_id"',form)
        self.assertIn('name="q"',form)

    def test_filtro_rapido_limpa_id_anterior_e_preserva_ordenacao(self):
        self.item()
        outro=produto(nome='Coca de teste complementar',quantidade=0);self.item(p=outro)
        r=self.client.get(self.url,{**self.periodo,'produto_id':self.p.pk,'q':'skol','ordem':'nome_za'})
        campos=dict(r.context['filtro_rapido_campos'])
        r=self.client.get(self.url,{**campos,'q':'coca'})
        self.assertEqual([x['produto_id'] for x in r.context['ranking']['linhas']],[outro.pk])
        self.assertEqual(r.context['busca']['ordem'].value(),'nome_za')

    def test_periodo_omitido_normaliza_sem_esconder_erro_legitimo(self):
        hoje=date(2026,11,21)
        with patch('estoque.forms_relatorios.timezone.localdate',return_value=hoje):
            f=PeriodoRankingProdutosForm({'q':'skol'})
            self.assertTrue(f.is_valid(),f.errors)
            self.assertEqual((f.cleaned_data['inicio'],f.cleaned_data['fim']),(date(2026,11,1),hoje))
            f=PeriodoRankingProdutosForm({'data_inicial':'2026-10-01','data_final':'2026-10-31'})
            self.assertTrue(f.is_valid());self.assertEqual(f.cleaned_data['periodo'],'personalizado')
        for valor in ('','inexistente'):
            f=PeriodoRankingProdutosForm({**self.periodo,'periodo':valor})
            self.assertFalse(f.is_valid());self.assertIn('periodo',f.errors)
        f=PeriodoRankingProdutosForm({'data_inicial':'2026-11-01'})
        self.assertFalse(f.is_valid());self.assertIn('data_final',f.errors)

    def test_erros_portugues_mesmo_com_idioma_ingles_e_selecao_coerente(self):
        with override('en-us'):
            for parametros in ({'periodo':''},{**self.periodo,'periodo':'invalido'},
                    {**self.periodo,'data_inicial':'invalida'},{**self.periodo,'ordem':'invalida'}):
                r=self.client.get(self.url,parametros)
                self.assertNotContains(r,'This field is required.')
                self.assertNotContains(r,'Select a valid choice.')
                self.assertNotContains(r,'Enter a valid date.')
                self.assertTrue(r.context['form'].errors or r.context['busca'].errors)
            r=self.client.get(self.url,{'periodo':''})
            self.assertContains(r,'Escolha o período.')
            self.assertContains(r,'<option value="" selected>Escolha o período</option>',html=True)
            r=self.client.get(self.url,{'q':'skol'})
            self.assertFalse(r.context['form'].errors)

    def test_cliente_real_sem_duplicar_vendas_nem_criar_consultas_por_linha(self):
        i=self.item(cliente=self.cliente)
        self.item(venda=i.venda,q='2',valor='90')
        self.item(venda=i.venda,unidade='UN',q='4',valor='20')
        self.item()
        generico=Cliente.objects.create(nome='Balcão sem cadastro individual')
        self.item(cliente=generico)
        with CaptureQueriesContext(connection) as sql:
            r=documentos_produto_dia(self.p.pk,DIA)
            linhas=list(r['documentos'])
        self.assertEqual(len(sql),4)
        self.assertEqual((r['faturamento'],r['vendas']),(245,3))
        notas=[x for x in linhas if x['venda_id']==i.venda_id]
        self.assertEqual(len(notas),2)
        self.assertTrue(all(x['venda__cliente__nome']==self.cliente.nome for x in notas))
        url=reverse('estoque:relatorios_produto_dia',args=[self.p.pk])
        html=self.client.get(url,{**self.periodo,'dia':DIA})
        for texto in ('Cliente',self.cliente.nome,'Cliente não identificado',generico.nome,'R$ 245,00'):
            self.assertContains(html,texto)
        self.assertContains(html,reverse('estoque:venda_detalhe',args=[i.venda_id]))

    def test_proposta_clientes_agregada_sem_duplicidade_e_com_unidades_separadas(self):
        # Prototype query only: no new endpoint/service or consolidated UI.
        i=self.item(cliente=self.cliente)
        self.item(venda=i.venda,q='2',valor='90')
        self.item(venda=i.venda,unidade='UN',q='4',valor='20')
        self.item()
        base=itens_comerciais().filter(produto=self.p,venda__data_venda__range=(date(2026,11,1),date(2026,11,21)))
        totais=list(base.values('venda__cliente_id').annotate(vendas=Count('venda_id',distinct=True),faturamento=Sum('valor_total')))
        cliente=next(r for r in totais if r['venda__cliente_id']==self.cliente.pk)
        self.assertEqual((cliente['vendas'],cliente['faturamento']),(1,155))
        self.assertEqual(sum(r['faturamento'] for r in totais),200)
        unidades=list(base.filter(venda__cliente=self.cliente).values('unidade_comercial').annotate(quantidade=Sum('quantidade')))
        self.assertEqual({r['unidade_comercial']:r['quantidade'] for r in unidades},{'DZ':3,'UN':4})

    def test_layout_filtro_compacto_e_cliente_sem_alargar_pagina(self):
        self.item(cliente=self.cliente);self.item()
        ranking=self.client.get(self.url,self.periodo).content.decode()
        detalhe=self.client.get(reverse('estoque:relatorios_produto_dia',args=[self.p.pk]),{**self.periodo,'dia':DIA}).content.decode()
        css=(ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        with TemporaryDirectory(prefix='relatorios-complementos-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',perfil)
            original=subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen',side_effect=lambda cmd,**kw:original([cmd[0],'--no-sandbox',*cmd[1:]],**kw)):
                browser.start()
            try:
                tab=browser.tab()
                for largura in (320,390,768,1366):
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<600))
                    for nome,html in [('ranking',ranking),('dia','<main class="relatorios relatorios-produto">'+detalhe+'</main>')]:
                        main=html[html.index('<main'):html.index('</main>')+7]
                        main=re.sub(r'<script\b[^>]*src=[^>]*>.*?</script>','',main,flags=re.S)
                        tab.evaluate('document.open();document.write('+json.dumps('<meta name="viewport" content="width=device-width, initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;font:16px system-ui}'+css+'</style>'+main)+');document.close();')
                        self.assertLessEqual(tab.evaluate('document.documentElement.scrollWidth'),largura)
                        if nome=='ranking':
                            self.assertTrue(tab.evaluate('document.getElementById("relatorios-filtrar-produtos").nextElementSibling.id==="resultados-produtos"'))
                            self.assertLessEqual(tab.evaluate('document.getElementById("relatorios-filtrar-produtos").getBoundingClientRect().height'),140)
                            self.assertTrue(tab.evaluate('document.getElementById("filtrar-produtos").type==="search"'))
                        else:
                            self.assertTrue(tab.evaluate('document.querySelectorAll("thead th")[1].textContent==="Cliente"'))
                            self.assertTrue(tab.evaluate('[...document.querySelectorAll(".produto-dia-cliente")].some(e=>e.textContent==='+json.dumps(self.cliente.nome)+')'))
                tab.call('Page.close');tab.socket.close()
            finally: browser.stop()


class PropostaClientesDesempenhoTests(PostgreSQLIsoladoTests,TestCase):
    def test_prototipo_60000_itens_em_tres_consultas_sem_carregar_notas(self):
        p=produto(nome='Produto denso para proposta',quantidade=0)
        clientes=Cliente.objects.bulk_create([Cliente(nome=f'Cliente {n:03}') for n in range(200)])
        vendas=Venda.objects.bulk_create([Venda(data_venda=DIA,total=45,tipo_pagamento='Pix',cliente=clientes[n%200]) for n in range(10000)])
        ItemVenda.objects.bulk_create([ItemVenda(produto=p,venda=vendas[n%10000],quantidade=1,unidade='DZ' if n%2 else 'UN',preco_unitario=1,valor_total=1) for n in range(60000)],batch_size=2000)
        base=itens_comerciais().filter(produto=p,venda__data_venda__range=(DIA,DIA))
        with CaptureQueriesContext(connection) as sql:
            inicio=perf_counter()
            pagina=Paginator(base.values('venda__cliente_id','venda__cliente__nome').annotate(vendas=Count('venda_id',distinct=True),faturamento=Sum('valor_total')).order_by('-faturamento','venda__cliente_id'),25).get_page(1)
            linhas=list(pagina)
            unidades=list(base.filter(venda__cliente_id__in=[r['venda__cliente_id'] for r in linhas]).values('venda__cliente_id','unidade_comercial').annotate(quantidade=Sum('quantidade')))
            ms=(perf_counter()-inicio)*1000
        self.assertEqual(len(sql),3)
        self.assertEqual(len(linhas),25)
        self.assertEqual(pagina.paginator.count,200)
        self.assertEqual(linhas[0]['vendas'],50)
        self.assertEqual(linhas[0]['faturamento'],300)
        self.assertTrue(unidades)
        print('PROPOSTA_CLIENTES_BENCHMARK '+json.dumps(dict(itens=60000,vendas=10000,clientes=200,consultas=len(sql),ms=round(ms,2))))
