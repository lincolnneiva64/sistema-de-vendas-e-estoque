"""Regressão de renderização das pendências, sem reclassificar registros."""
from datetime import timedelta
import base64
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse, resolve
from django.utils import timezone

from estoque import views
from estoque.models import (Cliente, Produto, Venda, ItemVenda, EventoVenda,
    EntregaRota, EntregaRotaItem, EntregaChecklistItem, ContaReceber, MovimentoFinanceiro)
from offline.browser_support import Chrome, DevTools


class PendenciasEntregaFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('entrega-ficticia', 'teste@example.test', 'teste-isolado')
        self.client.force_login(self.user)
        self.cliente = Cliente.objects.create(nome='Cliente fictício das pendências')

    def criar(self, status='parcial', sem_checklist=False, entregue=False, dias=0, venda_cancelada=False):
        venda = Venda.objects.create(cliente=self.cliente, data_venda=timezone.localdate(),
            total=10, cancelada=venda_cancelada)
        produto = Produto.objects.create(nome=f'Produto fictício {venda.pk}', quantidade=20,
            preco_compra=5, preco_vista=10, preco_prazo=10, unidade_compra='UN', unidade_venda_1='UN')
        linha = ItemVenda.objects.create(venda=venda, produto=produto, quantidade=1,
            unidade='UN', preco_unitario=10, valor_total=10)
        rota = EntregaRota.objects.create(data=timezone.localdate()-timedelta(days=dias), tipo='unitaria')
        bloco = EntregaRotaItem.objects.create(rota=rota, venda=venda, status=status,
            observacao='[checklist_entrega_salva]')
        checklist = None if sem_checklist else EntregaChecklistItem.objects.create(
            rota_item=bloco, item_venda=linha, carregado=True, entregue=entregue)
        return bloco, checklist

    def retrato(self):
        return [list(m.objects.order_by('pk').values()) for m in (Cliente, Produto, Venda,
            ItemVenda, EventoVenda, EntregaRota, EntregaRotaItem, EntregaChecklistItem,
            ContaReceber, MovimentoFinanceiro)]

    def abrir(self, **params):
        antes = self.retrato()
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(reverse('estoque:pendencias_entrega'), params)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(antes, self.retrato())
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in consultas))
        return resposta


class PendenciasEntrega500Tests(PendenciasEntregaFixtures, TestCase):
    def test_rota_e_lista_vazia(self):
        self.assertEqual(resolve('/entregas/pendencias/').func, views.pendencias_entrega)
        self.assertContains(self.abrir(), 'Nenhuma pendencia de entrega encontrada.')

    def test_poucas_pendencias_preservam_acoes_validas(self):
        for _ in range(2):
            bloco, checklist = self.criar()
            r = self.abrir()
            self.assertContains(r, reverse('estoque:revisar_remocao_pendencia_da_nota', args=[checklist.pk]))
            self.assertContains(r, reverse('estoque:entrega_rota_checklist', args=[bloco.rota_id]))

    def test_cancelada_sem_id_de_checklist_renderiza_sem_link_de_remocao(self):
        bloco, _ = self.criar(status='cancelada')
        pendencias = views.listar_pendencias_entrega()
        self.assertEqual(len(pendencias), 1)  # Regra existente, não alterada nesta correção.
        self.assertEqual(pendencias[0]['id'], '')
        r = self.abrir()
        self.assertContains(r, 'Cancelada')
        self.assertNotContains(r, 'Remover da nota')
        self.assertContains(r, reverse('estoque:entrega_rota_checklist', args=[bloco.rota_id]))

    def test_parcial_sem_checklist_renderiza(self):
        self.criar(sem_checklist=True)
        r = self.abrir()
        self.assertContains(r, 'Parcial')
        self.assertNotContains(r, 'Remover da nota')

    def test_parcial_com_todos_entregues_preserva_registro_existente(self):
        self.criar(entregue=True)
        self.assertEqual(views.listar_pendencias_entrega()[0]['id'], '')
        self.assertContains(self.abrir(), 'Parcial')

    def test_muitas_pendencias_misturadas_sem_ocultar_linhas(self):
        for _ in range(32):
            self.criar()
        self.criar(status='cancelada')
        self.criar(sem_checklist=True)
        self.criar(entregue=True)
        pendencias = views.listar_pendencias_entrega()
        self.assertEqual(len(pendencias), 35)
        r = self.abrir()
        self.assertContains(r, '35 registro(s)')
        self.assertContains(r, 'Remover da nota', count=32)

    def test_antigas_mantem_ordenacao_e_regra_sem_prazo_de_expiracao(self):
        antiga, _ = self.criar(dias=400)
        recente, _ = self.criar()
        self.assertEqual([p['item_rota_id'] for p in views.listar_pendencias_entrega()], [recente.pk, antiga.pk])
        self.assertContains(self.abrir(), antiga.rota.data.strftime('%d/%m/%Y'))

    def test_produto_e_cliente_ausentes_nao_quebram_renderizacao(self):
        bloco, checklist = self.criar()
        ItemVenda.objects.filter(pk=checklist.item_venda_id).update(produto=None)
        Venda.objects.filter(pk=bloco.venda_id).update(cliente=None)
        r = self.abrir()
        self.assertContains(r, 'Produto nao identificado')
        self.assertContains(r, 'Consumidor')

    def test_resolvidas_sem_rota_existente_ou_descricao_completa(self):
        bloco, _ = self.criar()
        EventoVenda.objects.create(venda=bloco.venda, tipo_evento='pendencia_removida_da_nota',
            descricao='Pendencia da rota #999999 resolvida por remocao da nota. Item removido: Produto antigo - 1 UN (R$ 10.00).')
        EventoVenda.objects.create(venda=bloco.venda, tipo_evento='pendencia_removida_da_nota', descricao='Registro legado')
        r = self.abrir(status='resolvidas')
        self.assertContains(r, '2 registro(s)')
        self.assertContains(r, 'Rota #999999')
        self.assertNotContains(r, 'Remover da nota')
        self.assertEqual(len(views.listar_pendencias_entrega()), 1)  # Evento isolado não encerra o checklist.

    def test_quantidade_de_consultas_nao_cresce_com_lista_comum(self):
        self.criar()
        with CaptureQueriesContext(connection) as poucas:
            views.listar_pendencias_entrega()
        for _ in range(34):
            self.criar()
        with CaptureQueriesContext(connection) as muitas:
            views.listar_pendencias_entrega()
        self.assertEqual(len(poucas), len(muitas))
        self.assertLessEqual(len(muitas), 6)  # Uma leitura em lote da auditoria administrativa.

    def test_botao_ver_todas_as_pendencias_aponta_para_rota(self):
        self.criar(status='cancelada')
        antes = self.retrato()
        r = self.client.get(reverse('estoque:entregas_dia'))
        self.assertContains(r, 'Ver todas as pendências')
        self.assertContains(r, 'href="/entregas/pendencias/"')
        self.assertEqual(antes, self.retrato())
        self.abrir()

    def test_venda_cancelada_parcial_nao_e_reclassificada_pela_correcao(self):
        self.criar(venda_cancelada=True)
        self.assertEqual(views.listar_pendencias_entrega()[0]['id'], '')
        self.assertContains(self.abrir(), 'Parcial')


class PendenciasEntrega500BrowserTests(PendenciasEntregaFixtures, StaticLiveServerTestCase):
    def test_navegacao_lista_e_painel_desktop_mobile(self):
        out = Path(__file__).resolve().parents[1] / 'amostras/pendencias-entrega-500'
        out.mkdir(exist_ok=True)
        capturas = []
        with TemporaryDirectory(prefix='pendencias-entrega-browser-', ignore_cleanup_errors=True) as profile:
            browser = Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', profile).start()
            try:
                time.sleep(1)
                with urlopen(f'http://127.0.0.1:{browser.port}/json/list') as response:
                    targets = json.load(response)
                tab = DevTools(next(t['webSocketDebuggerUrl'] for t in targets if t['type']=='page'))
                tab.call('Page.enable')
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                for estado, total in (('vazia',0), ('poucas',2), ('muitas',35), ('resolvidas',1)):
                    if estado == 'poucas':
                        self.criar()
                        self.criar()
                    elif estado == 'muitas':
                        for _ in range(30):
                            self.criar()
                        bloco, _ = self.criar(status='cancelada')
                        self.criar(sem_checklist=True)
                        self.criar(entregue=True)
                    elif estado == 'resolvidas':
                        EventoVenda.objects.create(venda=bloco.venda, tipo_evento='pendencia_removida_da_nota',
                            descricao='Pendencia da rota #999999 resolvida por remocao da nota. Item removido: Produto histórico - 1 UN (R$ 10.00).')
                    antes = self.retrato()
                    for width in (1440,390,320):
                        tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':950,'deviceScaleFactor':1,'mobile':width<600})
                        url = self.live_server_url + reverse('estoque:pendencias_entrega')
                        if estado == 'muitas':
                            tab.call('Page.navigate', {'url':self.live_server_url+reverse('estoque:entregas_dia')})
                            tab.wait('document.readyState==="complete" && [...document.querySelectorAll("a")].some(a=>a.textContent.trim()==="Ver todas as pendências")')
                            tab.evaluate('window.link=[...document.querySelectorAll("a")].find(a=>a.textContent.trim()==="Ver todas as pendências");link.scrollIntoView({block:"center",behavior:"instant"});true')
                            point = tab.evaluate('(()=>{const r=link.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()')
                            for evento in ('mousePressed','mouseReleased'):
                                tab.call('Input.dispatchMouseEvent', {'type':evento,**point,'button':'left','clickCount':1})
                            tab.wait('location.pathname==="/entregas/pendencias/" && document.readyState==="complete" && !!document.querySelector(".pend-card")')
                        else:
                            tab.call('Page.navigate', {'url':url+('?status=resolvidas' if estado=='resolvidas' else '')})
                            tab.wait('document.readyState==="complete" && !!document.querySelector(".pend-card")')
                        self.assertLessEqual(tab.evaluate('document.documentElement.scrollWidth'), width)
                        self.assertIn(f'{total} registro(s)', tab.evaluate('document.querySelector(".pend-card").textContent'))
                        if estado == 'muitas':
                            self.assertEqual(tab.evaluate("""document.querySelectorAll('a[href*="/remover-da-nota/"]').length"""),32)
                            tab.evaluate("""window.panel=[...document.querySelectorAll('.pend-detail-row')].find(e=>!e.querySelector('a[href*="/remover-da-nota/"]'));panel.previousElementSibling.querySelector('.pend-tratar-toggle').click();true""")
                            self.assertFalse(tab.evaluate('panel.hidden'))
                            tab.evaluate('panel.previousElementSibling.querySelector(".pend-tratar-toggle").click();window.scrollTo(0,0);true')
                        if width<600 and total:
                            self.assertGreater(tab.evaluate('document.querySelector(".pend-table-wrap").scrollWidth'),tab.evaluate('document.querySelector(".pend-table-wrap").clientWidth'))
                        shot=tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':False})['data']
                        arquivo=f'{estado}-{width}.png'
                        (out/arquivo).write_bytes(base64.b64decode(shot))
                        capturas.append(arquivo)
                    self.assertEqual(antes, self.retrato())
            finally:
                browser.stop()
        (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Pendências de entrega — teste isolado</title><style>body{font:16px system-ui;margin:24px}img{max-width:100%;border:1px solid #ccc}figure{margin:30px 0}</style><h1>Pendências de entrega — dados fictícios</h1>'+''.join(f'<figure><figcaption>{n}</figcaption><a href="{n}"><img loading="lazy" src="{n}"></a></figure>' for n in capturas),encoding='utf-8')
