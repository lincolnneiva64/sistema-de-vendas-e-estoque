"""Alphabetical display with real mixed states, without changing stored counts."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unicodedata import normalize
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.db.models import Prefetch
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario, Produto
from offline.browser_support import Chrome
from . import services, views, contagem_admin
from .conversao import snapshot
from .models import (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem,
                     EventoInventario, AcessoInventario, AtribuicaoContagem, MovimentoObservado)
from .ordenacao import chave_produto
from .tests import produto
from .tests_browser import CHROME

NOMES = ['Zulu sabonete', '\u00c1gua mineral', 'banana', 'A\u00e7\u00facar refinado',
         'agua mineral', '\u00d3leo 10', '\u00f3leo 2', 'Arroz']
ESPERADOS = [NOMES[i] for i in (3, 1, 4, 7, 2, 5, 6, 0)]


class ChaveNomeTests(SimpleTestCase):
    def test_acentos_caixa_espacos_unicode_equivalente_e_desempate(self):
        def item(nome, pk=1, produto_id=1):
            return SimpleNamespace(cadastro={'nome':nome}, pk=pk, produto_id=produto_id)
        self.assertEqual(chave_produto(item('  \u00c1GUA   mineral ')), chave_produto(item('agua mineral')))
        self.assertEqual(chave_produto(item(normalize('NFD', '\u00e7a\u00ed'))), chave_produto(item('CAI')))
        iguais = [item('Agua', 7, 3), item('\u00e1gua', 8, 2), item('AGUA', 4, 2)]
        self.assertEqual([i.pk for i in sorted(iguais,key=chave_produto)], [4,8,7])


class Fixture:
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('ordem-sintetica', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador sintetico')
        self.produtos = []
        for nome in NOMES:
            p = produto(nome=nome, unidade_compra='UN', unidade_venda_1='UN', vende_fracionado=False)
            Produto.objects.filter(pk=p.pk).update(nome=nome)  # Exercise casing exactly as stored.
            p.refresh_from_db(); self.produtos.append(p)
        self.inv = services.criar_inventario(self.gestor, 'Ordem sintetica', 'manual', [p.pk for p in self.produtos])
        self.rodadas = {r.item.cadastro['nome']:r for r in RodadaContagem.objects.filter(item__inventario=self.inv).select_related('item')}
        self.acesso, self.token = self.atribuir()
        for nome, valor in [(NOMES[0],'10'),(NOMES[1],'10'),(NOMES[2],'8'),(NOMES[4],'12'),(NOMES[7],'10')]:
            services.salvar_contagem(self.acesso.pk,self.rodadas[nome].pk,0,valor,'',True)
        nova = services.recontar(self.gestor,self.rodadas[NOMES[5]].pk,'Recontagem sintetica')
        self.rodadas[NOMES[5]] = nova
        self.acesso, self.token = self.atribuir()
        self.client.force_login(self.gestor)
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.mobile = Client(); self.entrar()

    def atribuir(self):
        ids = list(RodadaContagem.objects.filter(item__inventario=self.inv,encerrada_em__isnull=True).values_list('pk',flat=True))
        if hasattr(self,'acesso'):
            # Synthetic assignment of the newly opened round to this same active access.
            for rodada_id in ids:
                AtribuicaoContagem.objects.get_or_create(rodada_id=rodada_id,defaults={'acesso':self.acesso})
            return self.acesso,self.token
        return services.gerar_acesso(self.gestor,self.inv.pk,self.funcionario.pk,ids)

    def entrar(self):
        convite = reverse('inventario:convite',args=[self.token])
        self.mobile.get(convite); self.mobile.post(convite)

    def estado(self):
        return [list(m.objects.order_by('pk').values()) for m in (Produto,Inventario,ItemInventario,
            RodadaContagem,ConfirmacaoContagem,EventoInventario,AcessoInventario,AtribuicaoContagem,MovimentoObservado)]

    def nomes_desktop(self):
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code,200)
        return [l['item'].cadastro['nome'] for l in resposta.context['linhas']]

    def json_mobile(self):
        resposta = self.mobile.get(reverse('inventario:mobile'),HTTP_ACCEPT='application/json')
        self.assertEqual(resposta.status_code,200)
        return resposta.json()


class OrdemHTTPTests(Fixture, TestCase):
    def test_desktop_todas_situacoes_e_mobile_html_json_na_mesma_ordem(self):
        self.assertEqual(self.nomes_desktop(),ESPERADOS)
        linhas = views.linhas_revisao(self.inv)
        self.assertEqual({l['destaque'] for l in linhas},{'falta','sobra','neutro'})
        self.assertEqual(next(l for l in linhas if l['item'].cadastro['nome']==NOMES[5])['rodada'].numero,2)
        self.assertEqual([p['nome'] for p in self.json_mobile()['produtos']],ESPERADOS)
        html = self.mobile.get(reverse('inventario:mobile'))
        self.assertEqual([p['nome'] for p in html.context['produtos']],ESPERADOS)
        self.assertEqual(self.json_mobile()['progresso'],dict(total=8,conferidos=5,faltam=3))

    def test_get_e_ordenacao_nao_alteram_dados_nem_consultam_por_item(self):
        self.nomes_desktop(); self.json_mobile()
        antes = self.estado()
        self.nomes_desktop(); self.json_mobile()
        self.assertEqual(antes,self.estado())
        itens = list(ItemInventario.objects.filter(inventario=self.inv))
        with self.assertNumQueries(0):
            ordenados = sorted(itens,key=chave_produto)
        self.assertEqual([i.cadastro['nome'] for i in ordenados],ESPERADOS)

    def test_correcao_admin_e_recontagem_nao_mudam_posicao_do_produto(self):
        r = self.rodadas[NOMES[3]]; r.refresh_from_db(); self.inv.refresh_from_db()
        contagem_admin.confirmar(self.gestor,self.inv.pk,r.pk,contagem_admin.autorizacao(self.gestor,self.inv,r),'8','')
        self.assertEqual(self.nomes_desktop(),ESPERADOS)
        nova = services.recontar(self.gestor,self.rodadas[NOMES[2]].pk,'Nova rodada sintetica')
        self.assertGreater(nova.pk,max(r.pk for r in self.rodadas.values()))
        self.acesso,self.token=self.atribuir(); self.entrar()
        self.assertEqual(self.nomes_desktop(),ESPERADOS)
        self.assertEqual([p['nome'] for p in self.json_mobile()['produtos']],ESPERADOS)
        self.assertEqual(len(self.json_mobile()['produtos']),8)

    def test_snapshot_nome_preservado_e_escopo_do_funcionario(self):
        Produto.objects.filter(pk=self.produtos[0].pk).update(nome='AAAA nome atual diferente')
        self.assertEqual(self.nomes_desktop(),ESPERADOS)
        self.assertEqual([p['nome'] for p in self.json_mobile()['produtos']],ESPERADOS)
        outro=Funcionario.objects.create(nome='Outro contador sintetico')
        novas=[services.recontar(self.gestor,self.rodadas[nome].pk,'Redistribuicao sintetica') for nome in [NOMES[0],NOMES[3]]]
        _, token=services.gerar_acesso(self.gestor,self.inv.pk,outro.pk,[r.pk for r in novas])
        self.token=token; self.entrar()
        self.assertEqual([p['nome'] for p in self.json_mobile()['produtos']],[NOMES[3],NOMES[0]])
        self.assertEqual(self.json_mobile()['progresso']['total'],2)

    def test_consultas_constantes_8_vs_30_produtos(self):
        def medir(cliente,url,**kwargs):
            cliente.get(url,**kwargs)
            with CaptureQueriesContext(connection) as queries:
                resposta=cliente.get(url,**kwargs)
            self.assertEqual(resposta.status_code,200)
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
            return len(queries)
        pequeno=medir(self.client,self.url)
        mobile_pequeno=medir(self.mobile,reverse('inventario:mobile'),HTTP_ACCEPT='application/json')
        for n in range(22):
            p=produto(nome=f'Produto extra {n:02}',unidade_compra='UN',unidade_venda_1='UN',vende_fracionado=False)
            item=ItemInventario.objects.create(inventario=self.inv,produto=p,cadastro=snapshot(p))
            r=RodadaContagem.objects.create(item=item,numero=1)
            AtribuicaoContagem.objects.create(acesso=self.acesso,rodada=r)
        self.assertLessEqual(medir(self.client,self.url),pequeno+1)
        self.assertLessEqual(medir(self.mobile,reverse('inventario:mobile'),HTTP_ACCEPT='application/json'),mobile_pequeno+1)
        self.assertEqual(self.json_mobile()['progresso']['total'],30)

    def test_ultimo_ok_em_lote_igual_ao_individual_inclusive_corte_de_versao(self):
        r=RodadaContagem.objects.get(pk=self.rodadas[NOMES[1]].pk)
        normal=views.payload_cego(r)
        lote=RodadaContagem.objects.select_related('item').prefetch_related(Prefetch('confirmacoes',
            queryset=ConfirmacaoContagem.objects.order_by('-versao_entrada'),to_attr='confirmacoes_mobile')).get(pk=r.pk)
        with self.assertNumQueries(0): self.assertEqual(views.payload_cego(lote),normal)
        r.versao=0; lote.versao=0
        self.assertEqual(views.payload_cego(lote),views.payload_cego(r))
        self.assertIsNone(views.payload_cego(lote)['ultimo_ok'])


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
class OrdemBrowserTests(Fixture, StaticLiveServerTestCase):
    def nomes(self,tab,seletor):
        return tab.evaluate('Array.from(document.querySelectorAll('+repr(seletor)+'),e=>e.textContent.trim())')

    def test_desktop_apos_modal_e_recontagem(self):
        with TemporaryDirectory(prefix='ordem-desktop-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab(); tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride',{'width':1366,'height':900,'deviceScaleFactor':1,'mobile':False})
                tab.call('Page.navigate',{'url':self.live_server_url+self.url})
                tab.wait('window.inventarioContagemAdminPronta===true')
                seletor='.resumo-linha th > a[data-detalhe]'
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                r=self.rodadas[NOMES[3]]
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin=edicao-admin-"+str(r.pk)+"]').click();window.formOrdem=document.querySelector('#edicao-admin-"+str(r.pk)+" form');formOrdem.elements.embalagens.value='8';formOrdem.querySelector('button[type=submit]').click();true")
                tab.wait("document.body.textContent.includes('OK administrativo: ordem-sintetica')")
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                services.recontar(self.gestor,self.rodadas[NOMES[2]].pk,'Browser recontagem sintetica')
                tab.call('Page.navigate',{'url':self.live_server_url+self.url})
                tab.wait('window.inventarioContagemAdminPronta===true')
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
            finally: chrome.stop()

    def test_mobile_apos_ok_verificar_recarregar_e_nova_rodada(self):
        with TemporaryDirectory(prefix='ordem-mobile-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                def abrir():
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:convite',args=[self.token])})
                    tab.wait("!!document.querySelector('form button')")
                    tab.evaluate("document.querySelector('form button').click();true")
                    tab.wait('window.inventarioContagemPronta===true')
                abrir()
                seletor='article.card > h2'
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                r=self.rodadas[NOMES[3]]
                tab.evaluate("window.formOrdem=document.getElementById('produto-"+str(r.pk)+"').querySelector('form.contagem');formOrdem.elements.embalagens.value='10';formOrdem.querySelector('button[value=ok]').click();true")
                tab.wait("formOrdem.dataset.confirmado==='true'")
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                self.assertIn('6 de 8',tab.evaluate("document.querySelector('#progresso-contagem').textContent"))
                tab.evaluate("document.querySelector('#verificar-acesso').click();true")
                tab.wait("!document.querySelector('#verificar-acesso').disabled")
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                tab.call('Page.reload'); tab.wait('window.inventarioContagemPronta===true')
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
                services.recontar(self.gestor,self.rodadas[NOMES[2]].pk,'Mobile recontagem sintetica')
                self.acesso,self.token=self.atribuir(); abrir()
                self.assertEqual(self.nomes(tab,seletor),ESPERADOS)
            finally: chrome.stop()
