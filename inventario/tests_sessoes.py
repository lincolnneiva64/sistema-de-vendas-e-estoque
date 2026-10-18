from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario
from . import services, sessoes
from .models import AcessoInventario
from .tests import produto


class SessoesSeparadasTests(TestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-sessoes', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.produto = produto()
        self.inv = services.criar_inventario(self.gestor, 'Sessão fictícia', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, [self.rodada.pk])
        self.convite = reverse('inventario:convite', args=[self.token])
        self.mobile = reverse('inventario:mobile')

    def legado(self, autenticar=False):
        if autenticar:
            self.client.force_login(self.gestor)
        sessao = self.client.session
        sessao['inventario_acesso'] = self.acesso.pk
        sessao['inventario_token_hash'] = self.acesso.token_hash
        sessao['rascunho_comercial'] = {'operacao_pendente': 'preservar'}
        sessao.save()
        return sessao.session_key

    def test_convite_preserva_autenticacao_cookie_e_dados_comerciais(self):
        self.client.force_login(self.gestor)
        sessao = self.client.session
        sessao['rascunho_comercial'] = {'operacao_pendente': 'preservar'}
        sessao.save()
        antes = dict(self.client.session)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.client.cookies['local_sales_owner'] = 'identidade-intacta'
        self.client.get(self.convite)
        csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        resposta = self.client.post(self.convite, secure=True)
        self.assertEqual(resposta.status_code, 302)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, resposta.cookies)
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, cookie)
        self.assertEqual(self.client.cookies[settings.CSRF_COOKIE_NAME].value, csrf)
        self.assertEqual(dict(self.client.session), antes)
        self.assertEqual(self.client.cookies['local_sales_owner'].value, 'identidade-intacta')
        contagem = resposta.cookies[sessoes.COOKIE]
        self.assertEqual(contagem['path'], '/inventario/')
        self.assertTrue(contagem['httponly'])
        self.assertTrue(contagem['secure'])
        self.assertEqual(contagem['samesite'], 'Lax')
        self.assertLessEqual(int(contagem['max-age']), 86400)
        self.assertNotEqual(contagem.value, cookie)
        self.assertNotIn(self.token, contagem.value)
        self.assertNotIn(self.acesso.token_hash, contagem.value)
        estado = SessionStore(session_key=contagem.value)
        self.assertEqual(estado['inventario_acesso'], self.acesso.pk)
        self.assertNotIn('_auth_user_id', estado)
        for url in ('/vendas/', '/compras/', '/', '/locacoes/'):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(self.client.get('/api/offline/session/').json()['authenticated'])

    def test_convite_anonimo_nao_concede_autenticacao_ou_permissoes(self):
        self.client.post(self.convite)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(self.client.get('/api/offline/session/').json()['authenticated'])
        self.assertEqual(self.client.get(self.mobile).status_code, 200)
        self.assertEqual(self.client.get(reverse('inventario:lista')).status_code, 403)
        self.assertEqual(self.client.get(reverse('estoque:conferencia_estoque_produto', args=[self.produto.pk])).status_code, 403)
        self.assertEqual(self.client.get('/vendas/').status_code, 200)

    def test_apenas_cookie_comercial_nao_autoriza_contagem(self):
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.get(self.mobile).status_code, 403)
        self.assertEqual(self.client.get(reverse('inventario:lista')).status_code, 200)

    def test_legado_migra_apenas_marcas_preservando_id_e_autenticacao(self):
        chave = self.legado(autenticar=True)
        self.client.cookies['local_sales_owner'] = 'nao-limpar'
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(sessoes.COOKIE, response.cookies)
        self.assertEqual(self.client.session.session_key, chave)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.gestor.pk)
        self.assertEqual(self.client.session['rascunho_comercial'], {'operacao_pendente': 'preservar'})
        self.assertFalse(any(k in self.client.session for k in sessoes.CHAVES_LEGADAS))
        self.assertEqual(self.client.cookies['local_sales_owner'].value, 'nao-limpar')
        self.assertEqual(self.client.get(self.mobile).status_code, 200)

    def test_legado_sem_login_nao_reconstroi_autenticacao_perdida(self):
        chave = self.legado()
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session.session_key, chave)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.get(self.mobile).status_code, 200)

    def test_legado_com_apenas_marcas_nao_apaga_cookie_comercial(self):
        sessao = self.client.session
        sessao['inventario_acesso'] = self.acesso.pk
        sessao['inventario_token_hash'] = self.acesso.token_hash
        sessao.save()
        chave = sessao.session_key
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session.session_key, chave)
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, chave)
        self.assertNotEqual(response.cookies[settings.SESSION_COOKIE_NAME]['max-age'], 0)
        self.assertEqual(self.client.get(self.mobile).status_code, 200)

    def test_legado_expirado_libera_vendas_sem_reativar_acesso(self):
        self.legado(autenticar=True)
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(expira_em=timezone.now() - timedelta(seconds=1))
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(sessoes.COOKIE, response.cookies)
        self.assertEqual(self.client.session['rascunho_comercial'], {'operacao_pendente': 'preservar'})
        self.assertEqual(self.client.get(self.mobile).status_code, 403)

    def test_legado_nao_sobrescreve_sessao_de_contagem_ja_separada(self):
        self.client.post(self.convite)
        cookie = self.client.cookies[sessoes.COOKIE].value
        self.legado()
        self.client.get(self.mobile)
        self.assertEqual(self.client.cookies[sessoes.COOKIE].value, cookie)
        self.assertFalse(any(k in self.client.session for k in sessoes.CHAVES_LEGADAS))

    def test_cookie_adulterado_nao_autoriza_contagem(self):
        self.client.post(self.convite)
        self.client.cookies[sessoes.COOKIE] = '0' * 32
        self.assertEqual(self.client.get(self.mobile).status_code, 403)
        self.assertEqual(self.client.get('/vendas/').status_code, 200)

    def test_expiracao_revogacao_inativacao_e_renovacao_sao_respeitadas(self):
        for bloqueio in ('expiracao', 'revogacao', 'inativacao', 'renovacao'):
            with self.subTest(bloqueio=bloqueio):
                self.funcionario.ativo = True
                self.funcionario.save(update_fields=['ativo'])
                self.acesso, token = services.renovar_acesso(self.gestor, self.acesso.pk)
                client = Client()
                client.post(reverse('inventario:convite', args=[token]))
                if bloqueio == 'expiracao':
                    AcessoInventario.objects.filter(pk=self.acesso.pk).update(expira_em=timezone.now() - timedelta(seconds=1))
                elif bloqueio == 'revogacao':
                    services.revogar_acesso(self.gestor, self.acesso.pk)
                elif bloqueio == 'inativacao':
                    self.funcionario.ativo = False
                    self.funcionario.save(update_fields=['ativo'])
                else:
                    services.renovar_acesso(self.gestor, self.acesso.pk)
                self.assertEqual(client.get(self.mobile).status_code, 403)
                self.assertEqual(client.get('/vendas/').status_code, 200)

    def test_produto_fora_da_atribuicao_e_recusado(self):
        outro = produto(nome='Outro produto fictício')
        inv = services.criar_inventario(self.gestor, 'Outra conferência', 'manual', [outro.pk])
        rodada = inv.itens.get().rodadas.get()
        self.client.post(self.convite)
        response = self.client.post(reverse('inventario:contar', args=[rodada.pk]),
                                   {'acao': 'ok', 'embalagens': '8', 'avulsos': '0', 'versao': '0'})
        self.assertEqual(response.status_code, 403)
        rodada.refresh_from_db()
        self.assertEqual(rodada.versao, 0)

    def test_csrf_continua_obrigatorio_na_sessao_separada(self):
        client = Client(enforce_csrf_checks=True)
        client.get(self.convite)
        self.assertEqual(client.post(self.convite).status_code, 403)
        csrf = client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(client.post(self.convite, {'csrfmiddlewaretoken': csrf}).status_code, 302)
        url = reverse('inventario:contar', args=[self.rodada.pk])
        data = {'acao': 'salvar', 'embalagens': '6', 'avulsos': '0', 'versao': '0'}
        self.assertEqual(client.post(url, data).status_code, 403)
        self.assertEqual(client.post(url, dict(data, csrfmiddlewaretoken=csrf)).status_code, 200)
