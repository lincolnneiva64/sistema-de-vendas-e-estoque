from estoque.services import estoque_manual_lote
"""Final POST and response handling, using fictitious stock on disposable PostgreSQL."""
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.messages.storage.base import Message
from django.contrib import messages
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from estoque.models import MovimentacaoEstoqueManual
from offline.browser_support import Chrome
from . import acesso_ajustes, ajustes, services
from .models import AplicacaoAjuste, EventoAjuste, ConfirmacaoContagem
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME

HEADERS = {'HTTP_X_INVENTARIO_AJUSTE': '1', 'HTTP_ACCEPT': 'application/json'}


class Fluxo:
    def preparar(self, finalizada=False):
        self.painel = reverse('inventario:ajustes', args=[self.inv.pk])
        self.detalhe = reverse('inventario:detalhe', args=[self.inv.pk])
        self.historico = ''
        if finalizada:
            for acesso in self.inv.acessos.all():
                self.inv.refresh_from_db()
                services.finalizar_conferencia(acesso.pk, self.inv.versao)
            self.inv.refresh_from_db()
            self.historico = acesso_ajustes.iniciar(self.usuario, self.inv)
        pagina = self.client.get(self.painel, {'confirmar': 'todos', 'revisao_historica': self.historico})
        self.dados = dict(acao='atualizar', previa=pagina.context['token_aplicacao'],
            confirmacao='sim', revisao_historica=self.historico)
        return pagina


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConfirmacaoFinalTests(Fluxo, AjustesFixture, TestCase):
    def test_sete_ajustes_aplicados_uma_vez_e_recibo_somente_leitura(self):
        for _ in range(6):
            self.segunda()
        self.preparar(finalizada=True)
        oks = list(ConfirmacaoContagem.objects.values())
        resposta = self.client.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 200)
        recibo = resposta.json()
        self.assertEqual(recibo['mensagem'], 'Estoque atualizado com sucesso')
        self.assertEqual(len(recibo['aplicacoes']), 7)
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 7)
        self.assertEqual(oks, list(ConfirmacaoContagem.objects.values()))
        self.assertEqual(self.client.post(self.painel, self.dados, **HEADERS).json()['aplicacoes'], recibo['aplicacoes'])
        eventos = EventoAjuste.objects.count()
        leitura = self.client.get(self.painel, {'resultado_atualizacao': self.dados['previa']}, **HEADERS)
        self.assertEqual(leitura.json()['aplicacoes'], recibo['aplicacoes'])
        self.assertEqual(EventoAjuste.objects.count(), eventos)
        self.assertEqual(AplicacaoAjuste.objects.count(), 7)

    def test_acesso_historico_expirado_retorna_erro_real_e_nao_grava(self):
        self.preparar(finalizada=True)
        with patch('django.core.signing.time.time', return_value=time.time()+1201):
            resposta = self.client.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()['estado'], 'rejeitado')
        self.assertIn('Acesse a revisão', resposta.json()['erro'])
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_erro_de_validacao_e_rollback_sem_sucesso(self):
        self.segunda()
        self.preparar()
        original = estoque_manual_lote.conferir_movimento
        chamadas = []
        def falhar(*args, **kwargs):
            chamadas.append(args)
            if len(chamadas) == 2:
                raise ValueError('Falha fictícia no segundo produto')
            return original(*args, **kwargs)
        with patch.object(estoque_manual_lote, 'conferir_movimento', side_effect=falhar):
            resposta = self.client.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.json()['erro'], 'Falha fictícia no segundo produto')
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(EventoAjuste.objects.exists())
        self.assertEqual(self.fresca().item.produto.quantidade, 10)
        pagina = self.client.get(self.painel)
        self.assertNotContains(pagina, 'Estoque atualizado com sucesso')

    def test_servico_sem_registro_nao_pode_anunciar_sucesso(self):
        self.preparar()
        with patch.object(ajustes, 'atualizar', return_value=[]):
            resposta = self.client.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 400)
        self.assertIn('Nenhuma aplicação', resposta.json()['erro'])

    def test_sem_javascript_sucesso_apenas_apos_gravacao(self):
        self.preparar()
        resposta = self.client.post(self.painel, self.dados, follow=True)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Estoque atualizado com sucesso', count=1)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertNotContains(resposta, 'data-ajustes-pendentes')

    def test_post_integrado_json_e_recibo(self):
        self.preparar()
        resposta = self.client.post(self.detalhe, self.dados, **HEADERS)
        self.assertTrue(resposta.json()['ok'])
        leitura = self.client.get(self.detalhe, {'resultado_atualizacao': self.dados['previa']}, **HEADERS)
        self.assertEqual(leitura.json()['aplicacoes'], resposta.json()['aplicacoes'])

    def test_csrf_e_permissao_recusados_sem_operacao(self):
        self.preparar()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.usuario)
        resposta = csrf.post(self.painel, self.dados, **HEADERS)
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()['estado'], 'rejeitado')
        self.assertIn('segurança', resposta.json()['erro'])
        anonimo = Client().post(self.painel, self.dados, **HEADERS)
        self.assertEqual(anonimo.status_code, 403)
        self.assertIn('usuário autorizado', anonimo.json()['erro'])
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_recibo_adulterado_ou_de_outra_conferencia_nao_confirma(self):
        self.preparar()
        self.assertEqual(self.client.get(self.painel, {'resultado_atualizacao':'inválido'}, **HEADERS).status_code, 400)
        outro = services.criar_inventario(self.usuario, criterio='manual', selecionados=[self.produto.pk])
        leitura = self.client.get(reverse('inventario:ajustes', args=[outro.pk]),
            {'resultado_atualizacao':self.dados['previa']}, **HEADERS)
        self.assertEqual(leitura.status_code, 400)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_flash_antigo_consumido_e_sete_pendentes_explicitos(self):
        for _ in range(6):
            self.segunda()
        self.preparar()
        from django.contrib.messages.storage.cookie import MessageEncoder
        sessao = self.client.session
        sessao['_messages'] = json.dumps([
            Message(messages.SUCCESS, 'Operação concluída. Confira o resultado abaixo.'),
            Message(messages.SUCCESS, 'Operação de ajuste concluída. Confira os resultados e a auditoria abaixo.')], cls=MessageEncoder)
        sessao.save()
        # FallbackStorage consults session messages only when its signed cookie
        # says the cookie payload overflowed. Reproduce that real storage path.
        from django.contrib.messages.storage.cookie import CookieStorage
        from django.test import RequestFactory
        storage = CookieStorage(RequestFactory().get(self.painel))
        self.client.cookies['messages'] = storage._encode([storage.not_finished])
        pagina = self.client.get(self.painel)
        self.assertContains(pagina, '7 ajustes pendentes de aplicação')
        self.assertNotContains(pagina, 'Operação concluída')
        self.assertNotContains(pagina, 'Operação de ajuste concluída')
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(self.client.session.get('_messages'))


@skipUnless(Path(CHROME).is_file(), 'Chrome obrigatório para validar o interpretador de resposta')
@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConfirmacaoFinalBrowserTests(Fluxo, AjustesFixture, StaticLiveServerTestCase):
    def navegador(self, largura, executar):
        with TemporaryDirectory(prefix='confirmacao-final-chrome-') as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls':['https://*']})
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<768))
                tab.call('Page.navigate', dict(url=self.live_server_url+self.painel+'?confirmar=todos&revisao_historica='+self.historico))
                tab.wait('window.inventarioFeedbackPronto && !!document.querySelector("#confirmar-aplicacao button:not(:disabled)")')
                executar(tab)
            finally:
                chrome.stop()

    def test_perda_resposta_recupera_auditoria_sem_segundo_post(self):
        self.preparar(finalizada=True)
        def executar(tab):
            tab.evaluate("window.realFetch=fetch; window.posts=0; window.fetch=async(...args)=>{if(args[1]?.method==='POST'){window.posts++;await realFetch(...args);throw new TypeError('Resposta perdida ficticia');}return realFetch(...args);};document.querySelector('#confirmar-aplicacao button').click();true")
            tab.wait("document.querySelector('[data-mensagem-servidor]')?.textContent.includes('Estoque atualizado com sucesso')")
            self.assertEqual(AplicacaoAjuste.objects.count(), 1)
            self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
            self.assertFalse(tab.evaluate('!!document.querySelector("#confirmar-aplicacao")'))
        self.navegador(1440, executar)

    def test_rejeicao_definitiva_mobile_sem_mensagem_ambigua(self):
        self.preparar()
        def executar(tab):
            tab.evaluate("document.querySelector('[name=previa]').value='adulterada';document.querySelector('#confirmar-aplicacao button').click();true")
            tab.wait("document.querySelector('[data-feedback][role=alert]')?.textContent.includes('inválida')")
            texto = tab.evaluate("document.querySelector('[data-feedback]').textContent")
            self.assertNotIn('pode ter sido registrada', texto)
            self.assertFalse(AplicacaoAjuste.objects.exists())
            self.assertFalse(tab.evaluate('document.querySelector("#confirmar-aplicacao button").disabled'))
        self.navegador(390, executar)

    def test_sessionstorage_legado_nao_fabrica_conclusao(self):
        self.preparar()
        def executar(tab):
            tab.evaluate("sessionStorage.setItem('inventario-feedback',JSON.stringify({path:location.pathname,text:'Operação concluída. Confira o resultado abaixo.'}));location.reload();true")
            tab.wait("window.inventarioFeedbackPronto && sessionStorage.getItem('inventario-feedback') === null")
            self.assertNotIn('Operação concluída', tab.evaluate('document.body.innerText'))
            self.assertIn('1 ajuste pendente', tab.evaluate('document.body.innerText'))
            self.assertFalse(AplicacaoAjuste.objects.exists())
        self.navegador(320, executar)

    def test_acesso_historico_recusado_nao_vira_resposta_incerta(self):
        self.preparar(finalizada=True)
        def executar(tab):
            tab.evaluate("document.querySelector('[name=revisao_historica]').value='expirado';document.querySelector('#confirmar-aplicacao button').click();true")
            tab.wait("document.querySelector('[data-feedback][role=alert]')?.textContent.includes('Acesse a revisão')")
            texto = tab.evaluate("document.querySelector('[data-feedback]').textContent")
            self.assertNotIn('pode ter sido registrada', texto)
            self.assertNotIn('Estoque atualizado com sucesso', tab.evaluate('document.body.innerText'))
            self.assertFalse(AplicacaoAjuste.objects.exists())
        self.navegador(1440, executar)

    def test_entrada_administrativa_nao_fabrica_operacao_concluida(self):
        self.preparar(finalizada=True)
        def executar(tab):
            tab.call('Page.navigate', dict(url=self.live_server_url+self.detalhe))
            tab.wait('window.inventarioFeedbackPronto && !!document.querySelector("#acessar-revisao-historica button")')
            tab.evaluate("document.querySelector('#acoes-administrativas').open=true;document.querySelector('#acessar-revisao-historica button').click();true")
            tab.wait("window.inventarioFeedbackPronto && !!document.querySelector('[data-ajustes-pendentes]')")
            texto = tab.evaluate('document.body.innerText')
            self.assertIn('1 ajuste pendente de aplicação', texto)
            self.assertNotIn('Operação concluída', texto)
            self.assertNotIn('Estoque atualizado com sucesso', texto)
            self.assertFalse(AplicacaoAjuste.objects.exists())
        self.navegador(1440, executar)
