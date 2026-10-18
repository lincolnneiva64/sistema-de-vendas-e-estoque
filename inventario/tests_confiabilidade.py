from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services, sessoes
from .models import ConfirmacaoContagem
from .tests import produto
from .tests_browser import CHROME


class Preparar:
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-confiabilidade', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.produto = produto(nome='Trigopan fictício', quantidade=Decimal('68'),
                               unidade_compra='UN', vende_fracionado=False)
        self.inv = services.criar_inventario(self.gestor, 'Conferência fictícia', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, [self.rodada.pk])
        self.mobile = reverse('inventario:mobile')
        self.contar = reverse('inventario:contar', args=[self.rodada.pk])
        self.convite = reverse('inventario:convite', args=[self.token])

    def preservar(self, esperado='36'):
        self.rodada.refresh_from_db()
        self.produto.refresh_from_db()
        self.inv.refresh_from_db()
        self.assertEqual(self.rodada.contado_base, Decimal(esperado))
        self.assertEqual(self.produto.quantidade, Decimal('68'))
        self.assertIsNone(self.inv.finalizada_em)


class ConfiabilidadeHTTPTests(Preparar, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.get(self.convite)
        csrf = self.client.cookies['csrftoken'].value
        self.client.post(self.convite, {'csrfmiddlewaretoken': csrf})
        self.csrf = csrf
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '36', '', True)

    def enviar(self, versao=1, quantidade='66', csrf=True):
        dados = {'versao': versao, 'embalagens': quantidade, 'acao': 'ok'}
        if csrf:
            dados['csrfmiddlewaretoken'] = self.csrf
        return self.client.post(self.contar, dados, HTTP_ACCEPT='application/json')

    def test_recontagem_historico_repeticao_e_painel(self):
        resposta = self.enviar()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()['produto']['ultimo_ok']['embalagens'], '66')
        self.preservar('66')
        self.assertEqual(list(self.rodada.confirmacoes.order_by('versao_entrada').values_list('embalagens', flat=True)),
                         [Decimal('36'), Decimal('66')])
        self.assertEqual(self.enviar().status_code, 200)  # Retry after lost response.
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        from .views import linhas_revisao
        self.assertEqual(linhas_revisao(self.inv)[0]['diferenca'], Decimal('-2'))

    def test_csrf_rejeita_sem_gravar_e_consulta_recupera_token(self):
        resposta = self.enviar(csrf=False)
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()['codigo'], 'csrf_invalido')
        self.assertIn('no-store', resposta['Cache-Control'])
        self.preservar()
        consulta = self.client.get(self.mobile, HTTP_ACCEPT='application/json').json()
        self.assertEqual(consulta['produtos'][0]['ultimo_ok']['embalagens'], '36')
        self.assertNotIn('esperado', consulta['produtos'][0])
        self.csrf = consulta['csrf']
        self.assertEqual(self.enviar().status_code, 200)

    def test_login_em_outra_aba_rotaciona_csrf_sem_perder_sessao_contagem(self):
        cookie = self.client.cookies[sessoes.COOKIE].value
        resposta = self.client.post(reverse('inventario:login'), {
            'username': 'gestor-confiabilidade', 'password': 'teste', 'csrfmiddlewaretoken': self.csrf,
        })
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self.client.cookies[sessoes.COOKIE].value, cookie)
        self.assertNotEqual(self.client.cookies['csrftoken'].value, self.csrf)
        self.assertEqual(self.enviar().json()['codigo'], 'csrf_invalido')
        self.preservar()
        consulta = self.client.get(self.mobile, HTTP_ACCEPT='application/json').json()
        self.csrf = consulta['csrf']
        self.assertEqual(self.enviar().status_code, 200)
        self.preservar('66')

    def test_renovacao_recusa_pagina_antiga_e_retomada_nao_grava(self):
        _, token = services.renovar_acesso(self.gestor, self.acesso.pk)
        resposta = self.enviar()
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()['codigo'], 'credencial_renovada')
        self.preservar()
        self.client.post(reverse('inventario:convite', args=[token]), {'csrfmiddlewaretoken': self.csrf})
        consulta = self.client.get(self.mobile, HTTP_ACCEPT='application/json')
        self.assertEqual(consulta.status_code, 200)
        self.assertEqual(consulta.json()['produtos'][0]['ultimo_ok']['embalagens'], '36')
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        self.assertEqual(self.enviar().status_code, 200)
        self.preservar('66')

    def test_erros_distintos_sessao_expiracao_revogacao(self):
        for campo, valor, codigo in [('expira_em', timezone.now()-timedelta(seconds=1), 'acesso_expirado'),
                                     ('revogado_em', timezone.now(), 'acesso_revogado')]:
            original = getattr(self.acesso, campo)
            setattr(self.acesso, campo, valor)
            self.acesso.save(update_fields=[campo])
            resposta = self.enviar()
            self.assertEqual(resposta.status_code, 403)
            self.assertEqual(resposta.json()['codigo'], codigo)
            setattr(self.acesso, campo, original)
            self.acesso.save(update_fields=[campo])
        del self.client.cookies[sessoes.COOKIE]
        self.assertEqual(self.enviar().json()['codigo'], 'sessao_ausente')
        self.preservar()

    def test_versao_antiga_nao_sobrescreve_ok_mais_recente(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, '40', '', True)
        resposta = self.enviar(versao=1)
        self.assertEqual(resposta.status_code, 409)
        self.preservar('40')
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)

    def test_payload_nao_mistura_rodada_lida_antes_com_confirmacao_posterior(self):
        from .models import RodadaContagem
        from .views import payload_cego
        antiga = RodadaContagem.objects.select_related('item').get(pk=self.rodada.pk)
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, '66', '', True)
        resposta_antiga = payload_cego(antiga)
        self.assertEqual(resposta_antiga['versao'], 1)
        self.assertEqual(resposta_antiga['embalagens'], '36')
        self.assertEqual(resposta_antiga['ultimo_ok']['embalagens'], '36')
        atual = self.client.get(self.mobile, HTTP_ACCEPT='application/json').json()['produtos'][0]
        self.assertEqual(atual['versao'], 2)
        self.assertEqual(atual['ultimo_ok']['embalagens'], '66')

    def test_ultimo_ok_independe_de_rascunho_e_consulta_nao_muda_versoes(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, '66', '', False)
        self.rodada.refresh_from_db()
        versao = self.rodada.versao
        resultado = self.client.get(self.mobile, HTTP_ACCEPT='application/json').json()['produtos'][0]
        self.assertFalse(resultado['conferido'])
        self.assertEqual(resultado['embalagens'], '66')
        self.assertEqual(resultado['ultimo_ok']['embalagens'], '36')
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.versao, versao)

    def test_recusas_nao_expoem_dados_e_csrf_fora_do_inventario_mantem_comportamento(self):
        del self.client.cookies[sessoes.COOKIE]
        for url in (self.mobile, self.contar):
            resposta = (self.client.get(url, HTTP_ACCEPT='application/json') if url == self.mobile
                        else self.enviar())
            self.assertEqual(resposta.status_code, 403)
            self.assertNotIn('produtos', resposta.json())
            self.assertNotIn(self.token, resposta.content.decode())
        resposta = self.client.post('/vendas/', HTTP_ACCEPT='application/json')
        self.assertEqual(resposta.status_code, 403)
        self.assertNotIn('application/json', resposta['Content-Type'])


@skipUnless(Path(CHROME).is_file(), 'Chrome não disponível')
class ConfiabilidadeBrowserTests(Preparar, StaticLiveServerTestCase):
    def abrir(self, tab):
        tab.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
        tab.call('Page.navigate', {'url': self.live_server_url + self.convite})
        tab.wait("!!document.querySelector('form button')")
        tab.evaluate("document.querySelector('form button').click();true")
        tab.wait('window.inventarioContagemPronta === true')

    def corrigir(self, tab, quantidade='66'):
        tab.evaluate("document.querySelector('[data-editar-contagem]').click();const campo=document.querySelector('[name=embalagens]');campo.value='"+quantidade+"';campo.dispatchEvent(new Event('input',{bubbles:true}));true")

    def verificar(self, tab):
        tab.evaluate("document.querySelector('#verificar-acesso').click();true")
        tab.wait("!document.querySelector('#verificar-acesso').disabled && document.querySelector('#mensagem-acesso').textContent.includes('consultados no servidor')")

    def test_403_apos_login_em_outra_aba_preserva_66_e_sessao_comercial(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '36', '', True)
        client = Client()
        client.force_login(self.gestor)
        sessao = client.session
        sessao['rascunho_comercial'] = {'operacao_pendente': 'preservar'}
        sessao.save()
        comercial_id = sessao.session_key
        with TemporaryDirectory(prefix='inventario-confiabilidade-login-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                comercial = chrome.tab()
                comercial.call('Network.enable')
                comercial.call('Network.setCookie', {'name': 'sessionid', 'value': comercial_id, 'url': self.live_server_url})
                comercial.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                comercial.wait("location.pathname === '/vendas/' && !!document.querySelector('body')")
                comercial.evaluate("localStorage.setItem('rascunho-comercial-teste','preservar');sessionStorage.setItem('operacao-comercial-teste','preservar');true")
                contagem = chrome.tab()
                self.abrir(contagem)
                self.corrigir(contagem)

                def cookies():
                    return {c['name']: c['value'] for c in comercial.call('Network.getCookies', {
                        'urls': [self.live_server_url + self.mobile],
                    })['cookies']}

                antes = cookies()
                comercial.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:login')})
                comercial.wait("!!document.querySelector('[name=password]')")
                comercial.evaluate("document.querySelector('[name=username]').value='gestor-confiabilidade';document.querySelector('[name=password]').value='teste';document.querySelector('form button').click();true")
                comercial.wait("location.pathname === '/inventario/'")
                apos_login = cookies()
                self.assertNotEqual(antes['csrftoken'], apos_login['csrftoken'])
                self.assertEqual(antes['sessionid'], apos_login['sessionid'])
                self.assertEqual(antes[sessoes.COOKIE], apos_login[sessoes.COOKIE])
                contagem.evaluate("document.querySelector('[value=ok]').click();true")
                contagem.wait("document.querySelector('.mensagem').textContent.includes('CSRF')")
                self.preservar()
                self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
                self.assertEqual(contagem.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                self.verificar(contagem)
                self.preservar()
                self.assertEqual(contagem.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                self.assertIn('36 UN', contagem.evaluate("document.querySelector('.quantidade-confirmada').textContent"))
                self.assertEqual(cookies(), apos_login)
                contagem.evaluate("document.querySelector('[value=ok]').click();true")
                contagem.wait("document.querySelector('form.contagem').dataset.confirmado === 'true'")
                self.preservar('66')
                self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
                self.assertEqual(cookies(), apos_login)
                estado = SessionStore(session_key=comercial_id)
                self.assertEqual(estado['_auth_user_id'], str(self.gestor.pk))
                self.assertEqual(estado['rascunho_comercial'], {'operacao_pendente': 'preservar'})
                self.assertEqual(comercial.evaluate("localStorage.getItem('rascunho-comercial-teste')"), 'preservar')
                self.assertEqual(comercial.evaluate("sessionStorage.getItem('operacao-comercial-teste')"), 'preservar')
                self.assertEqual(comercial.evaluate("(async()=>{return (await fetch('/vendas/')).status})()"), 200)
            finally:
                chrome.stop()

    def test_csrf_renovacao_resposta_perdida_e_retomada_sem_perder_66(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '36', '', True)
        with TemporaryDirectory(prefix='inventario-confiabilidade-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                self.corrigir(tab)
                self.assertIn('66 UN não confirmados; último OK conhecido: 36 UN', tab.evaluate("document.querySelector('.estado-produto').textContent"))
                tab.evaluate("document.querySelector('form.contagem [name=csrfmiddlewaretoken]').value='invalido';document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('CSRF')")
                self.preservar()
                self.verificar(tab)
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                self.assertFalse(tab.evaluate("document.querySelector('[value=ok]').disabled"))
                _, token = services.renovar_acesso(self.gestor, self.acesso.pk)
                tab.evaluate("document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('renovado')")
                self.preservar()
                # Accept the renewed invitation in another tab of the same isolated profile.
                nova = chrome.tab()
                nova.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[token])})
                nova.wait("!!document.querySelector('form button')")
                nova.evaluate("document.querySelector('form button').click();true")
                nova.wait('window.inventarioContagemPronta === true')
                self.verificar(tab)
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                self.preservar()
                tab.evaluate("window.realFetch=fetch;window.fetch=async(...args)=>{await realFetch(...args);throw new TypeError('Resposta perdida')};document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('Sem confirmação')")
                self.preservar('66')
                self.assertIn('último OK conhecido: 36 UN', tab.evaluate("document.querySelector('.mensagem').textContent"))
                self.assertEqual(tab.evaluate("document.querySelector('.quantidade-confirmada').textContent"), 'Último OK conhecido: 36 UN')
                self.assertTrue(tab.evaluate("document.querySelector('[value=ok]').disabled"))
                tab.evaluate('window.fetch=window.realFetch;true')
                self.verificar(tab)
                self.assertIn('Último OK salvo: 66 UN', tab.evaluate("document.querySelector('.quantidade-confirmada').textContent"))
                self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
                self.assertTrue(tab.evaluate("document.querySelector('form.contagem').dataset.confirmado === 'true'"))
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            finally:
                chrome.stop()

    def test_versao_antiga_resposta_html_e_outro_acesso_preservam_digitacao(self):
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, '36', '', True)
        with TemporaryDirectory(prefix='inventario-confiabilidade-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                self.abrir(tab)
                self.corrigir(tab)
                services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, '40', '', True)
                tab.evaluate("document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('outros números')")
                self.preservar('40')
                self.verificar(tab)
                self.assertIn('último OK conhecido: 40 UN', tab.evaluate("document.querySelector('.estado-produto').textContent"))
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                # A non-JSON proxy error must not be misdiagnosed as lost authentication.
                tab.evaluate("window.realFetch=fetch;window.fetch=async()=>new Response('<h1>Erro</h1>',{status:502,headers:{'Content-Type':'text/html'}});document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('.mensagem').textContent.includes('HTTP 502')")
                self.preservar('40')
                tab.evaluate('window.fetch=window.realFetch;true')
                self.verificar(tab)
                # Another assignment in the same browser must never adopt this draft.
                outro = produto(nome='Outro produto fictício')
                inv = services.criar_inventario(self.gestor, 'Outra conferência fictícia', 'manual', [outro.pk])
                _, token = services.gerar_acesso(self.gestor, inv.pk, self.funcionario.pk, [inv.itens.get().rodadas.get().pk])
                nova = chrome.tab()
                nova.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[token])})
                nova.wait("!!document.querySelector('form button')")
                nova.evaluate("document.querySelector('form button').click();true")
                nova.wait('window.inventarioContagemPronta === true')
                tab.evaluate("document.querySelector('#verificar-acesso').click();true")
                tab.wait("document.querySelector('#mensagem-acesso').textContent.includes('outra atribuição')")
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '66')
                self.preservar('40')
                self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
            finally:
                chrome.stop()
