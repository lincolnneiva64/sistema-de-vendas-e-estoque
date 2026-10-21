"""Only synthetic counts; the PostgreSQL launcher guards every connection."""
import json
import base64
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase, Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .models import ConfirmacaoContagem, EventoInventario, AplicacaoAjuste
from .tests import produto
from .tests_browser import CHROME
from .tests_ajustes import AjustesFixture


class Dados:
    def setUp(self):
        super().setUp()
        self.usuario = get_user_model().objects.create_superuser('gestor-usabilidade', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Funcionário fictício')
        self.produtos = [produto(nome=nome, unidade_compra='PCT', unidade_venda_2='KG', fator_conversao=fator)
            for nome, fator in [('Salsicha fictícia', '3'), ('Linguiça fictícia', '2.5')]]
        self.inv = services.criar_inventario(self.usuario, 'Teste fictício', 'manual', [p.pk for p in self.produtos])
        self.rodadas = list(self.inv.itens.order_by('pk').values_list('rodadas__pk', flat=True))
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, self.rodadas)


class ConversaoVisualTests(Dados, TestCase):
    def setUp(self):
        super().setUp()
        self.client.post(reverse('inventario:convite', args=[self.token]))

    def test_exemplos_consulta_somente_leitura_e_snapshot_original(self):
        for pk, p, av, total in zip(self.rodadas, ['2', '1'], ['1,899', '1,560'], ['2,633', '1,624']):
            with CaptureQueriesContext(connection) as queries:
                r = self.client.get(reverse('inventario:previa_conversao', args=[pk]), {'embalagens':p, 'avulsos':av})
            self.assertEqual(r.json(), {'total':total})
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.produtos[0].fator_conversao = 99
        self.produtos[0].save(update_fields=['fator_conversao'])
        r = self.client.get(reverse('inventario:previa_conversao', args=[self.rodadas[0]]), {'embalagens':2, 'avulsos':'1.899'})
        self.assertEqual(r.json()['total'], '2,633')
        self.assertFalse(ConfirmacaoContagem.objects.exists())

    def test_restricao_da_atribuicao_expiracao_e_entradas_invalidas(self):
        url = reverse('inventario:previa_conversao', args=[self.rodadas[0]])
        self.assertEqual(Client().get(url, HTTP_ACCEPT='application/json').status_code, 403)
        self.assertEqual(self.client.get(url, {'embalagens':'-1', 'avulsos':0}).status_code, 400)
        outro = services.criar_inventario(self.usuario, '', 'manual', [self.produtos[0].pk])
        pk = outro.itens.get().rodadas.get().pk
        self.assertEqual(self.client.get(reverse('inventario:previa_conversao', args=[pk])).status_code, 404)
        services.revogar_acesso(self.usuario, self.acesso.pk)
        self.assertEqual(self.client.get(url, HTTP_ACCEPT='application/json').status_code, 403)

    def test_total_persistente_apos_ok_envio_sem_navegacao_administrativa(self):
        for pk, p, av in zip(self.rodadas, ['2', '1'], ['1.899', '1.560']):
            services.salvar_contagem(self.acesso.pk, pk, 0, p, av, True)
        antes = list(ConfirmacaoContagem.objects.values())
        self.inv.refresh_from_db()
        self.client.post(reverse('inventario:finalizar'), {'versao':self.inv.versao})
        r = self.client.get(reverse('inventario:mobile'))
        self.assertContains(r, '2,633 PCT')
        self.assertContains(r, '1,624 PCT')
        self.assertNotContains(r, 'navegacao-contextual"')
        self.assertNotContains(r, 'Todas as conferências')
        self.assertEqual(antes, list(ConfirmacaoContagem.objects.values()))
        self.assertEqual(self.client.get(reverse('inventario:ajustes', args=[self.inv.pk])).status_code, 403)
        for p in self.produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)

    def test_arredondamento_oficial_nos_empates(self):
        p = produto(nome='Empates fictícios', unidade_compra='PCT', unidade_venda_2='KG', fator_conversao=2)
        inv = services.criar_inventario(self.usuario, '', 'manual', [p.pk])
        pk = inv.itens.get().rodadas.get().pk
        _, token = services.gerar_acesso(self.usuario, inv.pk, self.funcionario.pk, [pk])
        self.client.post(reverse('inventario:convite', args=[token]))
        for avulsos, total in [('0.001', '0,000'), ('0.003', '0,002')]:
            response = self.client.get(reverse('inventario:previa_conversao', args=[pk]), {'embalagens':0, 'avulsos':avulsos})
            self.assertEqual(response.json()['total'], total)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class ContagemVisualBrowserTests(Dados, StaticLiveServerTestCase):
    @contextmanager
    def navegador(self, largura):
        with TemporaryDirectory(prefix='inventario-usabilidade-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=844, deviceScaleFactor=1, mobile=largura<768))
                tab.call('Page.navigate', {'url':self.live_server_url + reverse('inventario:convite', args=[self.token])})
                tab.wait("!!document.querySelector('form button')")
                tab.evaluate("document.querySelector('form button').click();true")
                tab.wait('window.inventarioContagemPronta === true')
                yield tab
            finally:
                chrome.stop()

    def fluxo(self, largura):
        with self.navegador(largura) as tab:
            self.assertFalse(tab.evaluate("!!document.querySelector('.navegacao-contextual')"))
            self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            tab.evaluate("""document.querySelector('a[href="#produtos-contagem"]').click();true""")
            self.assertEqual(tab.evaluate('location.hash'), '#produtos-contagem')
            for index, p, av, total in [(0,'2','1.899','2,633 PCT'), (1,'1','1.560','1,624 PCT')]:
                selector = f'document.querySelectorAll("form.contagem")[{index}]'
                tab.evaluate(f"{{const f={selector}; f.elements.embalagens.value='{p}';f.elements.avulsos.value='{av}';f.elements.avulsos.dispatchEvent(new Event('input',{{bubbles:true}}));}};true")
                tab.wait(f'{selector}.querySelector("output").textContent === '+json.dumps(total))
                # Hold the response to inspect instantaneous feedback and duplicate prevention.
                tab.evaluate("window.realFetch=fetch;window.posts=0;window.fetch=(...args)=>{if(args[1]?.method==='POST'){window.posts++;return new Promise((resolve,reject)=>{window.release=()=>realFetch(...args).then(resolve,reject)})}return realFetch(...args)};true")
                tab.evaluate(f"{{const b={selector}.querySelector('[value=salvar]');b.click();b.click();}};true")
                self.assertEqual(tab.evaluate('window.posts'), 1)
                self.assertEqual(tab.evaluate(f"{selector}.querySelector('[value=salvar]').textContent"), 'Salvando...')
                tab.evaluate('window.release();window.fetch=window.realFetch;true')
                tab.wait(f'{selector}.querySelector(".mensagem").textContent.includes("Contagem guardada")')
                tab.evaluate(f"{selector}.querySelector('[value=ok]').click();true")
                tab.wait(f'{selector}.dataset.confirmado === "true"')
                self.assertEqual(tab.evaluate(f'{selector}.querySelector("output").textContent'), total)
                if index == 0:
                    tab.evaluate(f"{selector}.querySelector('[data-editar-contagem]').click();true")
                    self.assertEqual(tab.evaluate(f"{selector}.querySelector('[value=ok]').textContent"), 'Atualizar OK')
                    tab.evaluate("window.realFetch=fetch;window.fetch=(...args)=>args[1]?.method==='POST'?new Promise(resolve=>{window.release=()=>realFetch(...args).then(resolve)}):realFetch(...args);true")
                    tab.evaluate(f"{selector}.querySelector('[value=ok]').click();true")
                    self.assertEqual(tab.evaluate(f"{selector}.querySelector('[value=ok]').textContent"), 'Confirmando...')
                    tab.evaluate('window.release();window.fetch=window.realFetch;true')
                    tab.wait(f'{selector}.dataset.confirmado === "true"')
            tab.evaluate("window.realFetch=fetch;window.posts=0;window.fetch=(...args)=>{if(args[1]?.method==='POST'){window.posts++;return new Promise(resolve=>{window.release=()=>realFetch(...args).then(resolve)})}return realFetch(...args)};document.querySelector('#finalizar-conferencia button').click();document.querySelector('#finalizar-conferencia button').click();true")
            self.assertEqual(tab.evaluate('window.posts'), 1)
            self.assertEqual(tab.evaluate("document.querySelector('#finalizar-conferencia button').textContent"), 'Confirmando envio...')
            tab.evaluate('window.release();window.fetch=window.realFetch;true')
            tab.wait('document.querySelector("#estado-conferencia").textContent.includes("finalizada e enviada")')
            tab.call('Page.reload')
            tab.wait('window.inventarioContagemPronta === true')
            self.assertEqual(tab.evaluate('[...document.querySelectorAll("output")].map(o=>o.textContent)'), ['2,633 PCT','1,624 PCT'])
            self.assertEqual(ConfirmacaoContagem.objects.count(), 3)
            tab.evaluate("""document.querySelector('a[href="#inicio-contagem"]').click();true""")
            self.assertEqual(tab.evaluate('location.hash'), '#inicio-contagem')
            destino = Path('artifacts/usabilidade')
            destino.mkdir(parents=True, exist_ok=True)
            (destino / f'contagem-enviada-{largura}.png').write_bytes(base64.b64decode(
                tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']))

    def test_mobile(self): self.fluxo(390)
    def test_desktop(self): self.fluxo(1366)

    def test_falha_contagem_preserva_valores_e_recupera_apos_verificar(self):
        with self.navegador(390) as tab:
            tab.evaluate("const f=document.querySelector('form.contagem');f.elements.embalagens.value='2';f.elements.avulsos.value='1.899';window.realFetch=fetch;window.fetch=()=>Promise.reject(new TypeError('Falha simulada'));f.querySelector('[value=ok]').click();true")
            tab.wait('document.querySelector(".mensagem").textContent.includes("Sem confirmação")')
            self.assertEqual(tab.evaluate('document.querySelector("[name=avulsos]").value'), '1.899')
            tab.evaluate('window.fetch=window.realFetch;document.querySelector("#verificar-acesso").click();true')
            tab.wait('document.querySelector("#mensagem-acesso").textContent.includes("Valores digitados preservados")')
            self.assertFalse(tab.evaluate('document.querySelector("[value=ok]").disabled'))
            self.assertFalse(ConfirmacaoContagem.objects.exists())

    def test_previa_descarta_resposta_antiga_e_nao_mostra_total_desatualizado(self):
        with self.navegador(390) as tab:
            tab.evaluate("window.realFetch=fetch;window.respostas=[];window.fetch=()=>new Promise(resolve=>respostas.push(resolve));const f=document.querySelector('form.contagem');f.elements.embalagens.value='2';f.elements.avulsos.value='1.899';f.elements.avulsos.dispatchEvent(new Event('input',{bubbles:true}));f.elements.embalagens.value='3';f.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));true")
            tab.evaluate("respostas[1](new Response(JSON.stringify({total:'3,633'}),{headers:{'Content-Type':'application/json'}}));true")
            tab.wait('document.querySelector("output").textContent === "3,633 PCT"')
            tab.evaluate("respostas[0](new Response(JSON.stringify({total:'2,633'}),{headers:{'Content-Type':'application/json'}}));true")
            self.assertEqual(tab.evaluate('document.querySelector("output").textContent'), '3,633 PCT')
            tab.evaluate("window.fetch=()=>Promise.reject(new TypeError('Sem rede'));document.querySelector('[name=embalagens]').value='4';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));true")
            tab.wait('document.querySelector("[data-erro-conversao]").textContent.includes("Sem conexão")')
            self.assertEqual(tab.evaluate('document.querySelector("output").textContent'), '—')
            self.assertFalse(tab.evaluate('document.querySelector("[value=salvar]").disabled'))
            tab.evaluate('window.fetch=window.realFetch;true')

    def test_recontagem_administrativa_feedback_e_preservacao_do_ok(self):
        services.salvar_contagem(self.acesso.pk, self.rodadas[0], 0, '2', '1.899', True)
        historico = list(ConfirmacaoContagem.objects.values())
        client = Client()
        client.force_login(self.usuario)
        with self.navegador(390) as tab:
            tab.call('Network.enable')
            tab.call('Network.setCookie', dict(name='sessionid', value=client.cookies['sessionid'].value, url=self.live_server_url))
            tab.call('Page.navigate', {'url':self.live_server_url + reverse('inventario:detalhe', args=[self.inv.pk])})
            tab.wait('window.inventarioFeedbackPronto === true && window.inventarioRevisaoPronta === true')
            tab.evaluate("window.realFetch=fetch;window.posts=0;window.fetch=(...args)=>{window.posts++;return new Promise(resolve=>{window.release=()=>realFetch(...args).then(resolve)})};const f=document.querySelector('[name=acao][value=recontar]').form;f.elements.motivo.value='Recontagem fictícia autorizada para validar a interface.';f.querySelector('button').click();f.querySelector('button').click();true")
            self.assertEqual(tab.evaluate('window.posts'), 1)
            self.assertEqual(tab.evaluate("document.querySelector('[value=recontar]').form.querySelector('button').textContent"), 'Confirmando recontagem...')
            tab.evaluate('window.release();true')
            tab.wait('document.querySelector("main > .status")?.textContent.includes("sucesso")')
            self.assertEqual(historico, list(ConfirmacaoContagem.objects.values()))
            self.assertEqual(self.inv.itens.first().rodadas.count(), 2)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class AjustesFeedbackBrowserTests(AjustesFixture, StaticLiveServerTestCase):
    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_ajuste_feedback_falha_recuperacao_navegacao_e_unica_aplicacao(self):
        with TemporaryDirectory(prefix='inventario-feedback-ajustes-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                for width in [1366, 390]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=844,deviceScaleFactor=1,mobile=width<768))
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url+'?confirmar=todos'})
                    tab.wait('window.inventarioFeedbackPronto === true && !!document.querySelector("#confirmar-aplicacao button:not(:disabled)")')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".navegacao-contextual a").length'), 3)
                    tab.evaluate("window.realFetch=fetch;window.fetch=()=>Promise.reject(new TypeError('Falha simulada'));document.querySelector('#confirmar-aplicacao button').click();true")
                    tab.wait('document.querySelector("[data-feedback]")?.textContent.includes("Consulte o histórico")')
                    self.assertFalse(tab.evaluate('document.querySelector("#confirmar-aplicacao button").disabled'))
                    self.assertFalse(AplicacaoAjuste.objects.exists())
                    tab.evaluate('window.fetch=window.realFetch;true')
                tab.evaluate("window.posts=0;window.fetch=(...args)=>{window.posts++;return new Promise(resolve=>{window.release=()=>realFetch(...args).then(resolve)})};const b=document.querySelector('#confirmar-aplicacao button');b.click();b.click();true")
                self.assertEqual(tab.evaluate('window.posts'), 1)
                self.assertEqual(tab.evaluate('document.querySelector("#confirmar-aplicacao button").textContent'), 'Atualizando...')
                self.assertFalse(tab.evaluate('document.querySelector(".navegacao-contextual a").hasAttribute("disabled")'))
                tab.evaluate('window.release();true')
                tab.wait('!!document.querySelector("[data-estado=Aplicado]")')
                self.assertTrue(tab.evaluate('document.querySelector(".status").textContent.includes("Estoque atualizado com sucesso")'))
                self.assertEqual(AplicacaoAjuste.objects.count(), 1)
                tab.evaluate('document.querySelectorAll(".navegacao-contextual a")[2].click();true')
                tab.wait('window.inventarioRevisaoPronta === true')
                tab.evaluate('document.querySelectorAll(".navegacao-contextual a")[1].click();true')
                tab.wait('!!document.querySelector("#resumo-conferencias")')
            finally:
                chrome.stop()
