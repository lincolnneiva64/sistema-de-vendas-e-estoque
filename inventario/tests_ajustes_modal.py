import base64
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from estoque.models import Produto, MovimentacaoEstoqueManual
from offline.browser_support import Chrome
from . import services, ajustes
from .models import AplicacaoAjuste, ConfirmacaoContagem, RodadaContagem
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME


class Inputs(HTMLParser):
    def __init__(self,html):
        super().__init__();self.dados={};self.feed(html)
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        if tag=='input' and d.get('name'):self.dados[d['name']]=d.get('value','')


class Fixture(AjustesFixture):
    def setUp(self):
        super().setUp()
        self.modal=reverse('inventario:ajustes_modal',args=[self.inv.pk])
        self.detalhe=reverse('inventario:detalhe',args=[self.inv.pk])
    def prever(self):
        r=self.client.post(self.modal,{'acao':'prever'})
        self.assertEqual(r.status_code,200,r.content)
        return r.json(),Inputs(r.json()['html']).dados
    def finalizar(self):
        for a in self.inv.acessos.all():
            self.inv.refresh_from_db();services.finalizar_conferencia(a.pk,self.inv.versao)
        self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
    def bloqueada(self):
        r=self.segunda()
        services.sinalizar_fisico(r.pk,'Ocorrencia sintetica sem documento',str(uuid4()),usuario=self.usuario)
        return r


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class PrecisaoModalTests(Fixture, TestCase):
    def preparar_oito(self, saldo_probak='1.072'):
        from decimal import Decimal
        from .conversao import snapshot
        from .models import ItemInventario
        from .tests import produto
        self.precisao = []
        casos = [(f'Apto de teste {i}', 'CX', 12, '10', '8', '0', 'UN') for i in range(4)]
        casos += [('Probak de teste', 'PCT', 28, saldo_probak, '1', '2', 'UN'),
                  ('Bic de teste', 'PCT', 12, '1.333', '1', '5', 'UN'),
                  ('Sal de teste', 'FD', 30, '4.967', '4', '28', 'KG')]
        for nome, base, fator, saldo, principal, avulsos, secundaria in casos:
            p = produto(nome=nome, unidade_compra=base, unidade_venda_1=base,
                unidade_venda_2=secundaria, fator_conversao=fator, quantidade=Decimal(saldo))
            p.refresh_from_db()
            item = ItemInventario.objects.create(inventario=self.inv, produto=p,
                cadastro=snapshot(p), custo_base=p.preco_compra)
            r = RodadaContagem.objects.create(item=item, numero=1)
            acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [r.pk])
            services.salvar_contagem(acesso.pk, r.pk, 0, principal, avulsos, True)
            if nome in ['Probak de teste', 'Bic de teste', 'Sal de teste']:
                self.precisao.append((p.pk, r.pk))
        self.finalizar()

    def test_cinco_aptos_tres_pendentes_confirmacao_e_reenvio_sem_perdas(self):
        self.preparar_oito()
        contagens = list(RodadaContagem.objects.order_by('pk').values())
        oks = list(ConfirmacaoContagem.objects.order_by('pk').values())
        saldos = dict(Produto.objects.values_list('pk', 'quantidade'))
        payload, dados = self.prever()
        self.assertEqual(payload['resumo']['aptos'], 5)
        self.assertEqual(len(payload['resumo']['bloqueados']), 3)
        self.assertIn('5 produtos que serão atualizados', payload['html'])
        self.assertIn('3 produtos pendentes por precisão', payload['html'])
        for total in ['30 UN', '17 UN', '148 KG']:
            self.assertIn(total, payload['html'])
        entradas = ajustes.ler_token(dados['previa'])['rodadas']
        self.assertEqual(len(entradas), 5)
        self.assertTrue({r for _, r in self.precisao}.isdisjoint(e['rodada'] for e in entradas))
        self.assertEqual(saldos, dict(Produto.objects.values_list('pk', 'quantidade')))
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertNotContains(self.client.post(self.modal, {**dados, 'confirmacao': ''}), '"ok": true', status_code=400)
        resposta = self.client.post(self.modal, dados)
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertEqual(resposta.json()['quantidade'], 5)
        self.assertEqual(resposta.json()['resumo']['pendentes'], 3)
        self.assertEqual(AplicacaoAjuste.objects.count(), 5)
        repetida = self.client.post(self.modal, dados)
        self.assertEqual(repetida.status_code, 200, repetida.content)
        self.assertEqual(repetida.json()['aplicacoes'], resposta.json()['aplicacoes'])
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 5)
        for produto_id, _ in self.precisao:
            self.assertEqual(Produto.objects.get(pk=produto_id).quantidade, saldos[produto_id])
        self.assertEqual(list(RodadaContagem.objects.order_by('pk').values()), contagens)
        self.assertEqual(list(ConfirmacaoContagem.objects.order_by('pk').values()), oks)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)

    def test_previa_antiga_com_oito_recusada_integralmente_no_servidor(self):
        self.preparar_oito()
        with patch('inventario.ajustes.exigir_precisao_ajuste'):
            _, dados = self.prever()
        self.assertEqual(len(ajustes.ler_token(dados['previa'])['rodadas']), 8)
        saldos = list(Produto.objects.order_by('pk').values_list('quantidade', flat=True))
        resposta = self.client.post(self.modal, dados)
        self.assertEqual(resposta.status_code, 400, resposta.content)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(saldos, list(Produto.objects.order_by('pk').values_list('quantidade', flat=True)))

    def test_mudanca_apos_previa_recusa_todo_lote_dos_cinco(self):
        self.preparar_oito()
        _, dados = self.prever()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        saldos = list(Produto.objects.order_by('pk').values_list('quantidade', flat=True))
        resposta = self.client.post(self.modal, dados)
        self.assertEqual(resposta.status_code, 400, resposta.content)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(saldos, list(Produto.objects.order_by('pk').values_list('quantidade', flat=True)))

    def test_projecao_preliminar_tambem_separa_precisao_sem_bloquear_igualdade(self):
        from inventario.views import linhas_revisao
        self.preparar_oito(saldo_probak='1.071')
        resultado = ajustes.projetar_resultado(linhas_revisao(self.inv))
        self.assertEqual(sum(l.get('bloqueio_codigo') == 'precisao' for l in resultado['linhas']), 2)
        sem_diferenca = next(l for l in resultado['linhas'] if l['rodada'].pk == self.precisao[0][1])
        self.assertEqual(sem_diferenca['estado'], 'Sem diferença')
        payload, _ = self.prever()
        self.assertEqual(payload['resumo']['aptos'], 5)
        self.assertEqual(len(payload['resumo']['bloqueados']), 2)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AjustesModalHTTPTests(Fixture,TestCase):
    def test_previa_apenas_aptos_contado_saldos_e_bloqueados_separados(self):
        bloqueada=self.bloqueada()
        antes=list(Produto.objects.order_by('pk').values());oks=list(ConfirmacaoContagem.objects.values())
        payload,dados=self.prever()
        self.assertEqual((payload['resumo']['aptos'],len(payload['resumo']['bloqueados'])),(1,1))
        lista=payload['html'].split('<ul class="previa-lote">')[1].split('</ul>')[0]
        self.assertIn(self.produto.nome,lista)
        self.assertNotIn(bloqueada.item.cadastro['nome'],lista)
        self.assertIn(bloqueada.item.cadastro['nome'],payload['html'])
        self.assertIn('Contagem f',lista)
        self.assertEqual(antes,list(Produto.objects.order_by('pk').values()))
        self.assertEqual(oks,list(ConfirmacaoContagem.objects.values()))
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_finalizada_autorizacao_historica_no_clique_e_bloqueio_sem_token(self):
        self.finalizar()
        pagina=self.client.get(self.detalhe)
        self.assertContains(pagina,'Revisar e atualizar estoque')
        payload,dados=self.prever()
        self.assertTrue(dados['revisao_historica'])
        r=self.client.post(self.modal,{**dados,'revisao_historica':''})
        self.assertEqual(r.status_code,403)
        r=self.client.post(self.modal,dados)
        self.assertEqual(r.status_code,200,r.content)
        self.assertEqual(r.json()['quantidade'],1)
        self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(RodadaContagem.objects.count(),1)

    def test_todos_aptos_sucesso_real_recibo_e_idempotencia(self):
        self.segunda();_,dados=self.prever()
        resposta=self.client.post(self.modal,dados)
        self.assertEqual(resposta.status_code,200,resposta.content)
        p=resposta.json()
        self.assertTrue(p['ok']);self.assertEqual(p['quantidade'],2)
        self.assertEqual(p['conferencia_id'],self.inv.pk)
        self.assertEqual(p['conferencia_nome'],self.inv.titulo)
        self.assertEqual((p['resumo']['aptos'],p['resumo']['pendentes']),(0,0))
        self.assertEqual(len(p['aplicacoes']),2)
        repetir=self.client.post(self.modal,dados).json()
        self.assertEqual(set(repetir['aplicacoes']),set(p['aplicacoes']))
        self.assertEqual(AplicacaoAjuste.objects.count(),2)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(),2)
        recibo=self.client.get(self.modal,{'resultado_atualizacao':dados['previa']}).json()
        self.assertTrue(recibo['ok']);self.assertEqual(recibo['quantidade'],2)
        pagina=self.client.get(self.detalhe)
        self.assertNotContains(pagina,'id="revisar-atualizacao"')
        self.assertFalse(list(pagina.context['messages']))

    def test_parcialmente_aplicados_nao_entram_novamente_e_bloqueado_nao_muda(self):
        _,primeiro=self.prever();self.client.post(self.modal,primeiro)
        aplicado=AplicacaoAjuste.objects.get()
        self.segunda();bloqueada=self.bloqueada()
        p,dados=self.prever()
        self.assertEqual((p['resumo']['aplicados'],p['resumo']['aptos']),(1,1))
        self.assertEqual(len(ajustes.ler_token(dados['previa'])['rodadas']),1)
        r=self.client.post(self.modal,dados).json()
        self.assertEqual(r['quantidade'],1)
        self.assertEqual(AplicacaoAjuste.objects.count(),2)
        self.assertEqual(AplicacaoAjuste.objects.get(pk=aplicado.pk).depois,8)
        self.assertEqual(Produto.objects.get(pk=bloqueada.item.produto_id).quantidade,20)
        self.assertEqual(len(r['resumo']['bloqueados']),1)

    def test_dados_mudaram_previa_antiga_recusada_sem_aplicacao(self):
        _,dados=self.prever()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        r=self.client.post(self.modal,dados)
        self.assertEqual(r.status_code,400)
        self.assertFalse(r.json()['ok']);self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,11)

    def test_servidor_falha_rollback_e_recibo_nao_inventa_sucesso(self):
        _,dados=self.prever()
        c=Client(raise_request_exception=False);c.force_login(self.usuario)
        with patch('inventario.ajustes._gravar_aplicacoes',side_effect=RuntimeError('Falha sintetica')):
            self.assertEqual(c.post(self.modal,dados).status_code,500)
        recibo=self.client.get(self.modal,{'resultado_atualizacao':dados['previa']}).json()
        self.assertFalse(recibo['ok']);self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,10)

    def test_csrf_permissoes_metodos_e_recibo_forjado(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(self.modal,{'acao':'prever'}).status_code,403)
        self.assertEqual(self.client.put(self.modal).status_code,405)
        self.assertEqual(self.client.get(self.modal,{'resultado_atualizacao':'falso'}).status_code,400)
        user=get_user_model().objects.create_user('sem-aprovacao')
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.modal,{'acao':'prever'}).status_code,403)

    def test_status_sem_n_mais_um_2_vs_30(self):
        self.segunda()
        def medir():
            self.client.get(self.modal)
            with CaptureQueriesContext(connection) as queries:
                p=self.client.get(self.modal)
            self.assertEqual(p.status_code,200)
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
            return len(queries)
        pequeno=medir()
        for _ in range(28):self.segunda()
        self.assertLessEqual(medir(),pequeno+2)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AjustesModalConcorrenciaTests(Fixture,TransactionTestCase):
    def test_duas_confirmacoes_mesma_previa_uma_aplicacao(self):
        _,dados=self.prever();barreira=Barrier(2)
        clientes=[Client(),Client()]
        for c in clientes:c.force_login(self.usuario)
        def aplicar(c):
            try:
                barreira.wait(timeout=15)
                r=c.post(self.modal,dados)
                return r.status_code,r.json()
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:rs=list(pool.map(aplicar,clientes))
        self.assertEqual([s for s,p in rs],[200,200])
        self.assertEqual(rs[0][1]['aplicacoes'],rs[1][1]['aplicacoes'])
        self.assertEqual(AplicacaoAjuste.objects.count(),1)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,8)


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AjustesModalBrowserTests(Fixture,StaticLiveServerTestCase):
    def abrir(self,tab,width=1366):
        tab.call('Network.enable')
        tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
        tab.call('Page.navigate',{'url':self.live_server_url+self.detalhe})
        tab.wait('window.inventarioAjustesModalPronto===true')
    def previa_browser(self,tab):
        tab.evaluate("document.querySelector('[data-revisar-estoque]').click();true")
        tab.wait("!!document.querySelector('#modal-previa-estoque [data-confirmar-ajustes-modal]')")
    def capturar(self,tab,nome):
        pasta=Path("diagnostics/ajustes-modal")
        pasta.mkdir(parents=True,exist_ok=True)
        (pasta/(nome+".png")).write_bytes(base64.b64decode(tab.call("Page.captureScreenshot",{"format":"png"})["data"]))
    def sucesso(self,tab):
        tab.wait("!!document.querySelector('#modal-sucesso-estoque')?.open")
        self.assertIn('1 produto',tab.evaluate("document.querySelector('[data-quantidade-sucesso]').textContent"))

    def test_desktop_mobile_previa_somente_diferencas_bloqueado_sucesso_e_f5(self):
        self.bloqueada()
        with TemporaryDirectory(prefix='ajustes-modal-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();self.abrir(tab)
                for width in [1366,390,320]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                    self.previa_browser(tab)
                    self.assertTrue(tab.evaluate("document.querySelector('#modal-previa-estoque').matches(':modal')"))
                    self.assertEqual(tab.evaluate("document.querySelectorAll('#modal-previa-estoque .previa-lote li').length"),1)
                    self.assertIn('Estabilizante',tab.evaluate("document.querySelector('[data-bloqueados-modal]').textContent"))
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertTrue(tab.evaluate("(()=>{const r=document.querySelector('#modal-previa-estoque').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})()"))
                    self.capturar(tab,"previa-"+str(width))
                    tab.evaluate("document.querySelector('#modal-previa-estoque [data-cancelar-ajustes]').click();true")
                self.previa_browser(tab)
                tab.evaluate("document.querySelector('#confirmar-atualizacao-modal button[type=submit]').click();true")
                self.sucesso(tab)
                self.capturar(tab,"sucesso-320")
                self.assertIn('1 produto(s) bloqueado',tab.evaluate("document.querySelector('[data-pendencias-sucesso]').textContent"))
                self.assertEqual(AplicacaoAjuste.objects.count(),1)
                tab.call('Page.reload');tab.wait('window.inventarioAjustesModalPronto===true')
                self.assertFalse(tab.evaluate("document.querySelector('#modal-sucesso-estoque').open"))
                self.assertTrue(tab.evaluate("document.querySelector('[data-revisar-estoque]').hidden"))
                self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('[data-revisar-estoque]')).display"),"none")
                self.assertEqual(AplicacaoAjuste.objects.count(),1)
            finally:chrome.stop()

    def test_finalizada_duplo_clique_resposta_perdida_confirma_por_recibo(self):
        self.finalizar()
        with TemporaryDirectory(prefix='ajustes-modal-recibo-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();self.abrir(tab,390);self.previa_browser(tab)
                tab.evaluate("window.fetchReal=fetch;window.perdeu=false;window.fetch=async(...args)=>{if(args[1]?.method==='POST'&&args[1]?.body?.get('acao')==='atualizar'){sessionStorage.setItem('teste-posts',String(Number(sessionStorage.getItem('teste-posts')||0)+1));const r=await fetchReal(...args);if(!perdeu){perdeu=true;throw new TypeError('Resposta perdida sintetica')}return r}return fetchReal(...args)};const b=document.querySelector('#confirmar-atualizacao-modal button[type=submit]');b.click();b.click();true")
                self.sucesso(tab)
                self.assertEqual(tab.evaluate("sessionStorage.getItem('teste-posts')"),'1')
                self.assertEqual(AplicacaoAjuste.objects.count(),1)
                self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
                tab.evaluate("document.querySelector('#modal-sucesso-estoque a.secondary').click();true")
                tab.wait('window.inventarioAjustesModalPronto===true')
                self.assertFalse(tab.evaluate("document.querySelector('#modal-sucesso-estoque').open"))
            finally:chrome.stop()

    def test_erro_sem_recibo_nao_mostra_sucesso(self):
        with TemporaryDirectory(prefix='ajustes-modal-erro-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();self.abrir(tab);self.previa_browser(tab)
                tab.evaluate("window.fetchReal=fetch;window.fetch=(...args)=>args[1]?.method==='POST'&&args[1]?.body?.get('acao')==='atualizar'?Promise.resolve(new Response('{}',{status:500,headers:{'content-type':'application/json'}})):fetchReal(...args);document.querySelector('#confirmar-atualizacao-modal button[type=submit]').click();true")
                tab.wait("document.querySelector('[data-erro-ajustes]').getAttribute('role')==='alert'")
                self.assertFalse(tab.evaluate("document.querySelector('#modal-sucesso-estoque').open"))
                self.assertTrue(tab.evaluate("document.querySelector('#modal-previa-estoque').open"))
                self.assertFalse(AplicacaoAjuste.objects.exists())
            finally:chrome.stop()
