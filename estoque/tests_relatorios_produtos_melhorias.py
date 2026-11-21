"""Navigation and lazy daily documents on guarded disposable PostgreSQL."""
import json
import re
import subprocess
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from inventario.tests import produto
from estoque.models import ItemVenda, Venda
from estoque.services.relatorios_produtos import documentos_produto_dia, historico_produto, ranking_produtos
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parents[1]
DIA = date(2026,11,13)


class MelhoriasProdutosTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('melhorias-admin',is_staff=True,is_superuser=True)
        cls.p = produto(nome='Skol de teste',quantidade=0)
        cls.outro = produto(nome='Coca de teste',quantidade=0)
        cls.ovo = produto(nome='ovo de teste',quantidade=0)

    def setUp(self):
        self.client.force_login(self.admin)
        self.parametros = dict(periodo='personalizado',data_inicial='2026-09-01',data_final='2026-11-21')
        self.url = reverse('estoque:relatorios_produto_dia',args=[self.p.pk])

    def nota(self, q='1', valor='45', unidade='DZ', *, venda=None, p=None, dia=DIA, **kwargs):
        venda = venda or Venda.objects.create(data_venda=dia,total=D(valor),tipo_pagamento='Pix',**kwargs)
        return ItemVenda.objects.create(venda=venda,produto=p or self.p,quantidade=D(q),unidade=unidade,
            valor_total=D(valor),preco_unitario=D(valor)/D(q))

    def test_ordem_nome_nos_dois_sentidos_e_padrao_faturamento(self):
        for p,v in [(self.p,'100'),(self.outro,'300'),(self.ovo,'200')]:
            self.nota(p=p,valor=v)
        base = dict(inicio=DIA,fim=DIA)
        nomes = lambda ordem: [r['produto__nome'] for r in ranking_produtos(**base,ordem=ordem)['linhas']]
        self.assertEqual(nomes('nome_az'),[self.outro.nome,self.ovo.nome,self.p.nome])
        self.assertEqual(nomes('nome_za'),[self.p.nome,self.ovo.nome,self.outro.nome])
        self.assertEqual(nomes('faturamento'),[self.outro.nome,self.ovo.nome,self.p.nome])
        pagina = self.client.get(reverse('estoque:relatorios_produtos'),self.parametros)
        self.assertContains(pagina,'#resultados-produtos')
        self.assertContains(pagina,'Nome do produto (A–Z)')

    def test_retorno_preserva_identidade_pesquisa_ordem_periodo_e_pagina(self):
        for n in range(28):
            self.nota(unidade='U'+str(n))
        parametros = {**self.parametros,'produto_id':self.p.pk,'q':'sko','ordem':'nome_za','pagina':2}
        ranking = self.client.get(reverse('estoque:relatorios_produtos'),parametros)
        analise = self.client.get(ranking.context['analise_url'])
        retorno = parse_qs(analise.context['retorno_ranking'])
        for campo in ('produto_id','ordem','pagina','data_inicial','data_final'):
            self.assertEqual(retorno[campo],[str(parametros[campo])])
        self.assertEqual(retorno['q'],[self.p.nome])
        self.assertContains(analise,'name="ranking"')
        self.assertContains(analise,'#resultados-produtos')

    def test_dia_concilia_quantidade_receita_e_vendas_sem_duplicar_itens(self):
        i = self.nota()
        self.nota(q='2',valor='90',venda=i.venda)
        self.nota(q='4',valor='20',unidade='UN',venda=i.venda)
        self.nota(valor='999',p=self.outro)
        self.nota(valor='999',cancelada=True)
        self.nota(valor='999',dia=DIA-timedelta(days=1))
        r = documentos_produto_dia(self.p.pk,DIA)
        self.assertEqual((r['faturamento'],r['vendas']),(D('155'),1))
        self.assertEqual(len(r['documentos']),2)
        dz = next(x for x in r['documentos'] if x['unidade_comercial']=='DZ')
        self.assertEqual((dz['quantidade'],dz['valor_total'],dz['preco_efetivo'],dz['itens']),(3,135,45,2))
        h = historico_produto(self.p.pk,DIA,DIA,hoje=DIA,medida='quantidade',unidade='DZ')
        self.assertEqual(h['evolucao'][0]['faturamento'],3)
        r = self.client.get(self.url,{**self.parametros,'dia':DIA,'medida':'quantidade','unidade':'DZ'})
        self.assertContains(r,'R$ 135,00'); self.assertNotContains(r,'R$ 155,00')
        self.assertContains(r,reverse('estoque:venda_detalhe',args=[i.venda_id]))

    def test_paginacao_das_notas_e_periodo_extenso_equivalentes(self):
        ids = {self.nota().venda_id for _ in range(53)}
        encontrados = set()
        for pagina in (1,2,3):
            r = documentos_produto_dia(self.p.pk,DIA,pagina=pagina)
            encontrados.update(x['venda_id'] for x in r['documentos'])
            self.assertLessEqual(len(r['documentos']),25)
            self.assertEqual((r['faturamento'],r['vendas']),(2385,53))
        self.assertEqual(encontrados,ids)
        for n in range(40):
            self.nota(dia=DIA-timedelta(days=n+1))
        h = historico_produto(self.p.pk,DIA-timedelta(days=40),DIA,hoje=DIA,pagina=2)
        self.assertEqual(len(h['evolucao']),10)
        for d in h['evolucao']:
            notas = documentos_produto_dia(self.p.pk,d['data_venda'])
            self.assertEqual((notas['faturamento'],notas['vendas']),(d['faturamento'],d['quantidade']))

    def test_notas_somente_leitura_sem_n_mais_um_e_restricoes(self):
        for _ in range(30): self.nota()
        antes = list(ItemVenda.objects.values())
        with CaptureQueriesContext(connection) as queries:
            documentos_produto_dia(self.p.pk,DIA)
        self.assertEqual(len(queries),4)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(antes,list(ItemVenda.objects.values()))
        for dia in ('invalida','2026-08-31','2026-12-01'):
            self.assertEqual(self.client.get(self.url,{**self.parametros,'dia':dia}).status_code,400)
        self.assertEqual(self.client.post(self.url).status_code,405)
        self.client.logout()
        self.assertEqual(self.client.get(self.url,{**self.parametros,'dia':DIA}).status_code,302)
        usuario = get_user_model().objects.create_user('nao-admin')
        self.client.force_login(usuario)
        self.assertEqual(self.client.get(self.url,{**self.parametros,'dia':DIA}).status_code,403)

    def test_expansao_layout_busca_ao_clicar_cache_e_falha_recuperavel(self):
        self.nota()
        html = self.client.get(reverse('estoque:relatorios_produto',args=[self.p.pk]),self.parametros).content.decode()
        detalhe = self.client.get(self.url,{**self.parametros,'dia':DIA}).content.decode()
        browser_path = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        with TemporaryDirectory(prefix='relatorios-dias-',ignore_cleanup_errors=True) as perfil:
            browser = Chrome(browser_path,perfil)
            original = subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen',side_effect=lambda cmd,**kw:original([cmd[0],'--no-sandbox',*cmd[1:]],**kw)):
                browser.start()
            try:
                tab=browser.tab()
                css=(ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
                script=(ROOT/'static/estoque/relatorios-produto-dias.js').read_text(encoding='utf-8')
                for largura in (320,390,768,1366):
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<600))
                    tab.evaluate('document.open();document.write('+json.dumps(html)+');document.close();')
                    tab.evaluate("document.querySelectorAll('link,script[src]').forEach(e=>e.remove());")
                    tab.evaluate('document.head.insertAdjacentHTML("beforeend",'+json.dumps('<meta name="viewport" content="width=device-width, initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;font:16px system-ui}'+css+'</style>')+')')
                    tab.evaluate('window.__pedidos=0;window.fetch=async()=>{__pedidos++;return {ok:true,redirected:false,text:async()=>'+json.dumps(detalhe)+'}}')
                    tab.evaluate(script)
                    self.assertEqual(tab.evaluate('__pedidos'),0)
                    tab.evaluate('document.querySelector("[data-produto-dia]").click()')
                    tab.wait('document.querySelector("[data-dia-conteudo]").dataset.carregado==="true"')
                    self.assertEqual(tab.evaluate('__pedidos'),1)
                    self.assertTrue(tab.evaluate('document.querySelector("[data-dia-conteudo]").innerText.includes("R$ 45,00")'))
                    self.assertLessEqual(tab.evaluate('document.querySelector(".produto-dia-detalhe").getBoundingClientRect().width'),largura-32)
                    self.assertTrue(tab.evaluate('[...document.querySelectorAll(".produto-dia-detalhe p,.produto-dia-detalhe small")].every(e=>e.scrollWidth<=e.clientWidth+1 || getComputedStyle(e).display==="inline")'))
                    tab.evaluate('document.querySelector("[data-produto-dia]").click();document.querySelector("[data-produto-dia]").click()')
                    self.assertEqual(tab.evaluate('__pedidos'),1)
                    # The nested notes scroll inside the day, rather than widening the page.
                    self.assertLessEqual(tab.evaluate('document.documentElement.scrollWidth'),largura)
                    tab.evaluate('delete document.querySelector("[data-dia-conteudo]").dataset.carregado;window.fetch=async()=>{throw Error("offline")};document.querySelector("[data-produto-dia]").click();document.querySelector("[data-produto-dia]").click()')
                    tab.wait('document.querySelector("[data-dia-conteudo]").innerText.includes("Tentar novamente")')
                tab.call('Page.close');tab.socket.close()
            finally:
                browser.stop()

    def test_ranking_consultar_proxima_anterior_posicionam_resultados_mobile_desktop(self):
        for n in range(28): self.nota(unidade='U'+str(n))
        url=reverse('estoque:relatorios_produtos')
        paginas={n:self.client.get(url,{**self.parametros,'pagina':n}).content.decode() for n in (1,2)}
        css=(ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        script=(ROOT/'static/estoque/relatorios-produtos-pesquisa.js').read_text(encoding='utf-8')
        with TemporaryDirectory(prefix='relatorios-posicao-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',perfil)
            original=subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen',side_effect=lambda cmd,**kw:original([cmd[0],'--no-sandbox',*cmd[1:]],**kw)):
                browser.start()
            try:
                tab=browser.tab()
                for largura in (320,390,768,1366):
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<600))
                    for origem,pagina in [('Consultar',1),('Próxima',2),('Anterior',1)]:
                        html=paginas[pagina]
                        main=html[html.index('<main'):html.index('</main>')+7]
                        main=re.sub(r'<script\b[^>]*src=[^>]*>.*?</script>','',main,flags=re.S)
                        tab.evaluate('document.open();document.write('+json.dumps('<meta name="viewport" content="width=device-width, initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;font:16px system-ui}'+css+'</style>'+main)+');document.close();')
                        self.assertTrue(tab.evaluate('document.querySelector("form").getAttribute("action").endsWith("#resultados-produtos")'))
                        self.assertTrue(tab.evaluate('[...document.querySelectorAll(".relatorios-paginacao a")].every(a=>a.getAttribute("href").endsWith("#resultados-produtos"))'))
                        tab.evaluate('location.hash="";scrollTo(0,0);location.hash="resultados-produtos"')
                        tab.evaluate(script)
                        tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
                        topo=tab.evaluate('document.getElementById("resultados-produtos").getBoundingClientRect().top')
                        self.assertGreaterEqual(topo,0,(origem,largura,topo))
                        # A short last page may reach the document bottom
                        # before its table can align with the viewport top.
                        self.assertLessEqual(topo,tab.evaluate('innerHeight/3'),(origem,largura,topo))
                        self.assertEqual(tab.evaluate('document.activeElement.id'),'resultados-produtos')
                tab.call('Page.close');tab.socket.close()
            finally:
                browser.stop()
