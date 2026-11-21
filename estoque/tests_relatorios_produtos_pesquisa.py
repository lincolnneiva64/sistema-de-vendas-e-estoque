"""Product search regression on guarded disposable PostgreSQL only."""
from datetime import date
from decimal import Decimal
import base64
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from inventario.tests import produto
from estoque.models import ItemVenda, Produto, Venda
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from estoque import views_relatorios
from offline.browser_support import Chrome


class PesquisaProdutosTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user('pesquisa-admin', is_staff=True, is_superuser=True)
        cls.skol = produto(nome='Skol Lata 12/350Ml', quantidade=0)
        cls.semelhante = produto(nome='Skol Lata 15/269Ml', quantidade=0)
        cls.outro = produto(nome='Coca Cola 6/1L', quantidade=0)
        cls.sem_vendas = produto(nome='Skol cadastrada sem vendas', quantidade=0)
        for p, valor in ((cls.skol, '100'), (cls.semelhante, '900'), (cls.outro, '300')):
            v = Venda.objects.create(data_venda=date(2026,11,20), total=Decimal(valor), tipo_pagamento='Pix')
            ItemVenda.objects.create(produto=p, venda=v, unidade='PCT', quantidade=1,
                preco_unitario=Decimal(valor), valor_total=Decimal(valor))

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('estoque:relatorios_produtos')
        self.periodo = dict(periodo='personalizado', data_inicial='2026-11-01', data_final='2026-11-20')

    def test_reproducao_apos_consultar_sko_filtra_apenas_ranking(self):
        antes = self.client.get(self.url, self.periodo)
        depois = self.client.get(self.url, {**self.periodo, 'q': 'sko'})
        self.assertEqual(antes.context['ranking']['linhas'].paginator.count, 3)
        linhas = list(depois.context['ranking']['linhas'])
        self.assertEqual({l['produto_id'] for l in linhas}, {self.skol.pk, self.semelhante.pk})
        self.assertTrue(all('sko' in l['produto__nome'].lower() for l in linhas))
        self.assertEqual(depois.context['ranking']['comercial'], Decimal('1300'))
        self.assertEqual(sum(l['faturamento'] for l in linhas), Decimal('1000'))
        tabela = depois.content.decode().split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertNotIn('Coca Cola', tabela)

    def test_reproducao_consultar_sem_correspondencia_nao_mostra_outros(self):
        r = self.client.get(self.url, {**self.periodo, 'q': 'nenhumresultado'})
        self.assertEqual(r.context['ranking']['linhas'].paginator.count, 0)
        self.assertContains(r, 'Nenhum item comercial encontrado')

    def test_sugestoes_buscam_cadastro_inclusive_sem_vendas_e_sem_dados_financeiros(self):
        url = reverse('estoque:relatorios_produtos_sugestoes')
        r = self.client.get(url, {'q': 'SKO'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual({p['id'] for p in r.json()['produtos']}, {self.skol.pk, self.semelhante.pk, self.sem_vendas.pk})
        self.assertTrue(all(set(p) == {'id', 'nome'} for p in r.json()['produtos']))
        self.assertIn('private', r['Cache-Control'])
        self.assertIn('no-store', r['Cache-Control'])

    def test_sugestoes_respeitam_exclusao_logica_independentemente_de_ativo(self):
        inativo = produto(nome='Skol inativa não excluída', quantidade=0)
        excluido_com = produto(nome='Skol excluída com histórico', quantidade=0)
        excluido_sem = produto(nome='Skol excluída sem histórico', quantidade=0)
        Produto.objects.filter(pk=inativo.pk).update(ativo=False)
        Produto.objects.filter(pk=excluido_com.pk).update(ativo=True, excluido=True)
        Produto.objects.filter(pk=excluido_sem.pk).update(ativo=False, excluido=True)
        v = Venda.objects.create(data_venda=date(2026,11,20), total=100, tipo_pagamento='Pix')
        ItemVenda.objects.create(produto=excluido_com, venda=v, unidade='DZ', quantidade=2,
            preco_unitario=50, valor_total=100)
        antes = list(Produto.objects.filter(pk__in=[inativo.pk,excluido_com.pk,excluido_sem.pk]).values())
        r = RequestFactory().get('/', {'q':'skol'});r.user = self.admin
        with self.assertNumQueries(1):
            dados = json.loads(views_relatorios.produtos_sugestoes(r).content)
        ids = {p['id'] for p in dados['produtos']}
        self.assertIn(self.skol.pk,ids)
        self.assertIn(inativo.pk,ids)
        self.assertNotIn(excluido_com.pk,ids)
        self.assertNotIn(excluido_sem.pk,ids)
        self.assertEqual(antes,list(Produto.objects.filter(pk__in=[inativo.pk,excluido_com.pk,excluido_sem.pk]).values()))

    def test_excluido_com_vendas_preserva_ranking_nome_id_analise_e_notas(self):
        Produto.objects.filter(pk=self.skol.pk).update(ativo=True,excluido=True)
        for filtro in ({'q':'skol'},{'q':'outro nome','produto_id':self.skol.pk}):
            r = self.client.get(self.url,{**self.periodo,**filtro})
            linha = next(i for i in r.context['ranking']['linhas'] if i['produto_id']==self.skol.pk)
            self.assertEqual((linha['faturamento'],linha['quantidade'],linha['vendas']),(100,1,1))
        analise = self.client.get(reverse('estoque:relatorios_produto',args=[self.skol.pk]),self.periodo)
        self.assertEqual(analise.context['resumo']['faturamento'],100)
        self.assertContains(analise,'Cadastro excluído')
        self.assertContains(analise,'data-produto-dia')
        notas = self.client.get(reverse('estoque:relatorios_produto_dia',args=[self.skol.pk]),
            {**self.periodo,'dia':'2026-11-20'})
        self.assertContains(notas,'R$ 100,00')
        self.assertContains(notas,reverse('estoque:venda_detalhe',args=[self.skol.itens_venda.first().venda_id]))

    def test_produto_inativo_nao_excluido_preserva_pesquisa_exata_e_analise(self):
        Produto.objects.filter(pk=self.skol.pk).update(ativo=False,excluido=False)
        r = self.client.get(self.url,{**self.periodo,'produto_id':self.skol.pk})
        self.assertEqual(r.context['ranking']['linhas'][0]['produto_id'],self.skol.pk)
        analise = self.client.get(r.context['analise_url'])
        self.assertEqual(analise.context['resumo']['faturamento'],100)
        self.assertNotContains(analise,'Cadastro excluído')

    def test_excluido_sem_vendas_nao_vaza_nas_sugestoes_com_nome_identico(self):
        excluido = produto(nome=self.skol.nome,quantidade=0)
        Produto.objects.filter(pk=excluido.pk).update(ativo=True,excluido=True)
        sugestoes = self.client.get(reverse('estoque:relatorios_produtos_sugestoes'),{'q':self.skol.nome}).json()
        self.assertNotIn(excluido.pk,{p['id'] for p in sugestoes['produtos']})
        selecionado = self.client.get(self.url,{**self.periodo,'produto_id':excluido.pk})
        self.assertEqual(selecionado.context['ranking']['linhas'].paginator.count,0)
        self.assertEqual(selecionado.context['produto_selecionado'].pk,excluido.pk)

    def test_limite_8_sugestoes_uma_consulta_e_termo_curto_sem_consulta(self):
        for n in range(24):
            produto(nome=f'Skol sugestão limitada {n:02}', quantidade=0)
        factory = RequestFactory()
        r = factory.get('/', {'q': 'sko'}); r.user = self.admin
        with self.assertNumQueries(1):
            resposta = views_relatorios.produtos_sugestoes(r)
        dados = json.loads(resposta.content)
        self.assertEqual(len(dados['produtos']), 8)
        self.assertTrue(dados['mais'])
        r = factory.get('/', {'q': 's'}); r.user = self.admin
        with self.assertNumQueries(0):
            self.assertEqual(json.loads(views_relatorios.produtos_sugestoes(r).content)['produtos'], [])

    def test_selecao_id_tem_prioridade_sem_misturar_nomes_semelhantes(self):
        r = self.client.get(self.url, {**self.periodo, 'q': 'Coca', 'produto_id': self.skol.pk})
        linhas = list(r.context['ranking']['linhas'])
        self.assertEqual({p['produto_id'] for p in linhas}, {self.skol.pk})
        self.assertEqual(sum(l['faturamento'] for l in linhas), 100)
        self.assertEqual(r.context['busca']['q'].value(), self.skol.nome)
        analise = self.client.get(r.context['analise_url'])
        self.assertEqual(analise.context['produto'].pk, self.skol.pk)
        self.assertEqual(analise.context['resumo']['faturamento'], 100)
        self.assertEqual(analise.context['resumo']['quantidade'], 1)

    def test_nomes_identicos_continuam_distintos_pelo_id(self):
        duplicado = produto(nome=self.skol.nome, quantidade=0)
        venda = Venda.objects.create(data_venda=date(2026,11,20), total=500, tipo_pagamento='Pix')
        ItemVenda.objects.create(produto=duplicado, venda=venda, unidade='PCT', quantidade=5,
                                preco_unitario=100, valor_total=500)
        sugestoes = self.client.get(reverse('estoque:relatorios_produtos_sugestoes'), {'q':self.skol.nome}).json()['produtos']
        self.assertEqual({p['id'] for p in sugestoes}, {self.skol.pk, duplicado.pk})
        for p, total in ((self.skol, 100), (duplicado, 500)):
            r = self.client.get(self.url, {**self.periodo, 'q':p.nome, 'produto_id':p.pk})
            self.assertEqual({l['produto_id'] for l in r.context['ranking']['linhas']}, {p.pk})
            self.assertEqual(self.client.get(r.context['analise_url']).context['resumo']['faturamento'], total)

    def test_selecao_preserva_periodo_ordem_e_parametros_dos_graficos(self):
        p = {**self.periodo, 'q':'sko', 'produto_id':self.semelhante.pk, 'ordem':'vendas',
             'modo':'mensal', 'dia_semana':'0', 'alcance':'12', 'comparacao':'ano_anterior',
             'historico_inicio':'2026-01-01', 'historico_fim':'2026-11-20'}
        r = self.client.get(self.url, p)
        qs = parse_qs(urlparse(r.context['analise_url']).query)
        for nome in ('periodo','data_inicial','data_final','ordem','modo','alcance','comparacao'):
            self.assertEqual(qs[nome], [p[nome]])
        analise = self.client.get(r.context['analise_url'])
        self.assertEqual(analise.context['resumo']['faturamento'], 900)
        self.assertEqual(analise.context['evolucao_form']['alcance'].value(), '12')

    def test_id_invalido_nao_faz_fallback_para_busca_parcial(self):
        for identidade in ('invalido', '-1', '999999999'):
            r = self.client.get(self.url, {**self.periodo, 'q':'sko', 'produto_id':identidade})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.context['busca'].errors)
            self.assertNotIn('ranking', r.context)

    def test_catalogo_sem_vendas_tem_analise_vazia_sem_outros_produtos(self):
        r = self.client.get(self.url, {**self.periodo, 'q':'sko', 'produto_id':self.sem_vendas.pk})
        self.assertEqual(r.context['ranking']['linhas'].paginator.count, 0)
        analise = self.client.get(r.context['analise_url'])
        self.assertEqual(analise.context['resumo']['faturamento'], 0)
        self.assertEqual(analise.context['resumo']['quantidade'], 0)

    def test_paginacao_selecao_exata_preserva_id_e_nao_inclui_outros(self):
        venda = Venda.objects.filter(itens__produto=self.skol).first()
        ItemVenda.objects.bulk_create([ItemVenda(produto=self.skol, venda=venda, unidade=f'UN{n:02}',
            quantidade=1, preco_unitario=1, valor_total=1) for n in range(29)])
        r = self.client.get(self.url, {**self.periodo, 'q':'sko', 'produto_id':self.skol.pk, 'pagina':2, 'ordem':'vendas'})
        self.assertEqual(r.context['ranking']['linhas'].number, 2)
        self.assertEqual({l['produto_id'] for l in r.context['ranking']['linhas']}, {self.skol.pk})
        self.assertIn('produto_id='+str(self.skol.pk), r.context['filtros_query'])

    def test_sugestoes_exigem_administrador_e_nao_aceitam_post(self):
        url = reverse('estoque:relatorios_produtos_sugestoes')
        self.client.logout()
        self.assertEqual(self.client.get(url, {'q':'sko'}).status_code, 302)
        comum = get_user_model().objects.create_user('pesquisa-comum', is_staff=True)
        self.client.force_login(comum)
        self.assertEqual(self.client.get(url, {'q':'sko'}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_interface_sugestoes_teclado_toque_e_corrida_de_requisicoes(self):
        root = Path(__file__).resolve().parents[1]
        html = self.client.get(self.url, self.periodo).content.decode()
        dados = self.client.get(reverse('estoque:relatorios_produtos_sugestoes'), {'q':'sko'}).json()
        css = (root/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        css_base = (root/'static/estoque/home.css').read_text(encoding='utf-8')
        bootstrap = root/'artifacts/relatorios-produtos-20261120/bootstrap.min.css'
        if bootstrap.exists(): css_base = bootstrap.read_text(encoding='utf-8') + css_base
        js = (root/'static/estoque/relatorios-produtos-pesquisa.js').read_text(encoding='utf-8')
        with TemporaryDirectory(prefix='pesquisa-produto-ui-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', perfil)
            original = subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen', side_effect=lambda cmd, **kw: original([cmd[0], '--no-sandbox', *cmd[1:]], **kw)):
                browser.start()
            try:
                tab = browser.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls':['http://*','https://*']})
                for largura in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=740,deviceScaleFactor=1,mobile=largura<768))
                    tab.evaluate('document.open();document.write('+json.dumps(html)+');document.close()')
                    tab.evaluate("document.querySelectorAll('link,script[src]').forEach(e=>e.remove());document.head.insertAdjacentHTML('afterbegin','<base href=\"http://127.0.0.1:8000/\"><style id=teste-css></style>')")
                    estilos = css_base + css
                    for i in range(0,len(estilos),8000):
                        tab.evaluate('document.getElementById("teste-css").textContent+='+json.dumps(estilos[i:i+8000]))
                    tab.evaluate('window.__dados='+json.dumps(dados)+';window.__buscas=[];window.fetch=async url=>{__buscas.push(String(url));return {ok:true,json:async()=>__dados}}')
                    tab.evaluate(js)
                    tab.evaluate("document.getElementById('id_q').focus();document.getElementById('id_q').value='sko';document.getElementById('id_q').dispatchEvent(new Event('input',{bubbles:true}))")
                    tab.wait("document.querySelectorAll('#relatorios-produtos-sugestoes button').length===3")
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('#relatorios-produtos-sugestoes button')].every(e=>e.textContent.toLowerCase().includes('sko'))"))
                    self.assertEqual(tab.evaluate("document.getElementById('id_produto_id').value"), '')
                    self.assertTrue(tab.evaluate("document.querySelector('.ranking-produtos').textContent.includes('Coca Cola')"))
                    self.assertTrue(tab.evaluate("document.querySelector('[data-produto-analisar]').hidden"))
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    if os.environ.get('PESQUISA_VISUAL_ARTIFACTS'):
                        out=Path(os.environ['PESQUISA_VISUAL_ARTIFACTS']); out.mkdir(parents=True,exist_ok=True)
                        (out/f'sugestoes-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                    if largura == 1366:
                        tab.evaluate("document.getElementById('id_q').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}))")
                        escolhido = str(dados['produtos'][0]['id'])
                    else:
                        tab.call('Emulation.setTouchEmulationEnabled', {'enabled':True})
                        alvo = "document.querySelector('[data-produto-id=\""+str(self.semelhante.pk)+"\"]')"
                        pos = tab.evaluate('(()=>{const r='+alvo+'.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()')
                        tab.call('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[pos]})
                        tab.call('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
                        escolhido = str(self.semelhante.pk)
                    tab.wait("document.getElementById('id_produto_id').value==="+json.dumps(escolhido))
                    self.assertTrue(tab.evaluate("document.activeElement.hasAttribute('data-produto-analisar')"))
                    href = tab.evaluate("document.querySelector('[data-produto-analisar]').href")
                    self.assertEqual(urlparse(href).path, reverse('estoque:relatorios_produto',args=[int(escolhido)]))
                    self.assertEqual(parse_qs(urlparse(href).query)['data_inicial'], ['2026-11-01'])
                    tab.evaluate("document.getElementById('id_data_inicial').value='2026-10-01';document.getElementById('id_data_inicial').dispatchEvent(new Event('input',{bubbles:true}))")
                    self.assertEqual(parse_qs(urlparse(tab.evaluate("document.querySelector('[data-produto-analisar]').href")).query)['data_inicial'], ['2026-10-01'])
                    tab.evaluate("document.getElementById('id_q').value='co';document.getElementById('id_q').dispatchEvent(new Event('input',{bubbles:true}))")
                    self.assertEqual(tab.evaluate("document.getElementById('id_produto_id').value"), '')
                    self.assertTrue(tab.evaluate("document.querySelector('[data-produto-analisar]').hidden"))
                    # An older response arriving last cannot replace the current suggestions.
                    tab.evaluate("document.getElementById('id_q').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));window.__pendentes=[];window.fetch=url=>new Promise(resolve=>__pendentes.push({url:String(url),resolve}));document.getElementById('id_q').value='sk';document.getElementById('id_q').dispatchEvent(new Event('input',{bubbles:true}))")
                    tab.wait('__pendentes.length===1')
                    tab.evaluate("document.getElementById('id_q').value='co';document.getElementById('id_q').dispatchEvent(new Event('input',{bubbles:true}))")
                    tab.wait('__pendentes.length===2')
                    tab.evaluate('__pendentes[1].resolve({ok:true,json:async()=>('+json.dumps({'produtos':[{'id':self.outro.pk,'nome':self.outro.nome}],'mais':False})+')})')
                    tab.wait("document.querySelector('#relatorios-produtos-sugestoes').textContent.includes('Coca Cola')")
                    tab.evaluate('__pendentes[0].resolve({ok:true,json:async()=>__dados})')
                    self.assertNotIn('Skol', tab.evaluate("document.querySelector('#relatorios-produtos-sugestoes').textContent"))
                    tab.evaluate("document.getElementById('relatorios-filtros-vendas').addEventListener('submit',e=>{e.preventDefault();window.__enviado=Object.fromEntries(new FormData(e.target))});document.getElementById('relatorios-filtros-vendas').requestSubmit()")
                    self.assertEqual(tab.evaluate('__enviado.q'), 'co')
                    self.assertEqual(tab.evaluate('__enviado.produto_id'), '')
                tab.call('Page.close'); tab.socket.close()
            finally:
                browser.stop()
