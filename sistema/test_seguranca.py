"""Fictitious identities and data on the guarded disposable PostgreSQL runner."""
import io
import json
import secrets
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import Client, TestCase, override_settings, RequestFactory
from django.utils import timezone

from estoque.models import (Cliente, Produto, Venda, ContaReceber, DespesaDiaria, MovimentoFinanceiro,
    ItemVenda, ItemVendaRemovido, SeparacaoVenda, SeparacaoVendaItem, EntregaRota, EntregaRotaItem,
    MovimentacaoEstoqueManual, PagamentoContaPagar, RecebimentoContaReceber)
from estoque.services.vendas import criar_ou_atualizar_venda
from sistema.acesso_funcionarios import proteger_path, SALT


class SegurancaBackendTests(TestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Execute no runner PostgreSQL descartável.')
        self.senha = secrets.token_urlsafe(24)
        self.lincoln = get_user_model().objects.create_superuser('lincoln-ficticio', '', self.senha)
        self.roseli = get_user_model().objects.create_superuser('roseli-ficticia', '', secrets.token_urlsafe(24))
        self.comum = get_user_model().objects.create_user('operador-ficticio')
        self.cliente = Cliente.objects.create(nome='Cliente Segurança')
        self.produto = Produto.objects.create(nome='Produto Segurança', quantidade=10,
            unidade_compra='UN', unidade_venda_1='UN', preco_compra=5, preco_vista=10, preco_prazo=10)
        self.dados = dict(cliente_id=self.cliente.pk, data_venda=timezone.localdate().isoformat(),
            data_vencimento=timezone.localdate().isoformat(), tipo_pagamento='A prazo', operador='Teste',
            itens=[dict(produto_id=self.produto.pk, produto_nome=self.produto.nome,
                quantidade='2', unidade='UN', preco_unitario='10')])
        self.venda = criar_ou_atualizar_venda(usuario=self.lincoln, dados=self.dados).venda
        self.conta = ContaReceber.objects.get(venda=self.venda)
        self.anon = self.browser()

    def browser(self, user=None):
        c = Client(enforce_csrf_checks=True)
        if user:
            c.force_login(user)
        c.get('/entrar/', secure=True)
        return c

    def post(self, c, url, data=None, json_body=False):
        kw = dict(secure=True, HTTP_ORIGIN='https://testserver', HTTP_X_CSRFTOKEN=c.cookies['csrftoken'].value)
        if json_body:
            kw['content_type'] = 'application/json'
            data = json.dumps(data or {})
        return c.post(url, data or {}, **kw)

    def test_gets_comerciais_e_aliases_exigem_login(self):
        for path in ['/', '/estoque/', '/vendas/', '/estoque/vendas/', '/vendas/consultar/',
                f'/vendas/{self.venda.pk}/', f'/vendas/{self.venda.pk}/editar/',
                f'/produto/{self.produto.pk}/', '/clientes/', '/compras/', '/compras/nova/',
                '/fornecedores/', '/contas-a-pagar/', '/contas-a-receber/', '/caixa-banco/',
                '/despesas-diarias/', '/painel-financeiro/', '/locacoes/', '/entregas/', '/separacao-vendas/']:
            r = self.anon.get(path, secure=True)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r['Location'].startswith('/entrar/?next='))
            self.assertNotIn('Produto Segurança', r.content.decode())
        for path in ['/clientes/autocomplete/?q=Segurança', '/estoque/clientes/autocomplete/?q=Segurança']:
            r = self.anon.get(path, secure=True)
            self.assertEqual(r.status_code, 401)
            self.assertIn('erro', r.json())

    def test_posts_anonimos_nao_alteram_qualquer_registro(self):
        models = [Produto, Venda, ItemVenda, ItemVendaRemovido, ContaReceber, DespesaDiaria,
            MovimentoFinanceiro, MovimentacaoEstoqueManual, PagamentoContaPagar, RecebimentoContaReceber]
        before = {m: list(m.objects.order_by('pk').values()) for m in models}
        for path, data in [('/vendas/gravar/', self.dados),
                ('/estoque/vendas/gravar/', {**self.dados, 'venda_id': self.venda.pk}),
                (f'/vendas/{self.venda.pk}/cancelar/', {}),
                (f'/contas-a-receber/{self.conta.pk}/receber/', {'valor': '5'}),
                ('/contas-pagar/1/baixar/', {'valor_pago': '5'}),
                ('/despesas-diarias/', {'acao': 'salvar_despesa', 'valor': '5'}),
                ('/', {'acao': 'excluir', 'excluir_id': self.produto.pk}),
                (f'/produto/{self.produto.pk}/alternar-ativo/', {'acao': 'desativar'}),
                ('/api/vendas/online/', {}), ('/compras/nova/', {})]:
            r = self.post(self.anon, path, data, 'gravar/' in path or path.startswith('/api/'))
            self.assertEqual(r.status_code, 401, (path, r.content))
            self.assertIn('erro', r.json())
        for model in models:
            self.assertEqual(before[model], list(model.objects.order_by('pk').values()), model.__name__)

    def test_usuario_comum_e_staff_nao_recebem_acesso_total(self):
        for staff in [False, True]:
            self.comum.is_staff = staff
            self.comum.save()
            c = self.browser(self.comum)
            for path in ['/', '/vendas/', '/caixa-banco/', '/compras/', '/locacoes/']:
                self.assertEqual(c.get(path, secure=True).status_code, 403, path)
            self.assertEqual(self.post(c, '/vendas/gravar/', self.dados, True).status_code, 403)
            self.assertEqual(self.post(c, '/api/vendas/online/', {}, True).status_code, 403)

    def test_ambos_administradores_acesso_equivalente(self):
        for user in [self.lincoln, self.roseli]:
            c = self.browser(user)
            for path in ['/', '/vendas/', '/clientes/', '/compras/', '/fornecedores/',
                    '/contas-a-receber/', '/caixa-banco/', '/despesas-diarias/', '/contas-a-pagar/', '/locacoes/', '/inventario/']:
                self.assertEqual(c.get(path, secure=True).status_code, 200, (user.username, path))
            self.assertEqual(self.post(c, '/vendas/gravar/', self.dados, True).status_code, 200)

    def test_permissoes_vendedor_nao_autorizam_edicao_financeiro_ou_admin(self):
        self.comum.user_permissions.add(*Permission.objects.filter(content_type__app_label='estoque',
            codename__in=['view_venda', 'add_venda', 'view_cliente', 'view_produto']))
        c = self.browser(self.comum)
        self.assertEqual(c.get('/vendas/', secure=True).status_code, 200)
        self.assertEqual(self.post(c, '/vendas/gravar/', self.dados, True).status_code, 200)
        self.assertEqual(self.post(c, '/vendas/gravar/', {**self.dados, 'venda_id': self.venda.pk}, True).status_code, 403)
        self.assertEqual(c.get('/caixa-banco/', secure=True).status_code, 403)
        self.assertEqual(c.get('/admin/', secure=True).status_code, 302)

    def test_login_atalho_sessao_persistente_logout_e_csrf(self):
        r = self.anon.get('/vendas/?editar=' + str(self.venda.pk), secure=True)
        next_path = parse_qs(urlsplit(r['Location']).query)['next'][0]
        r = self.post(self.anon, '/entrar/', dict(username=self.lincoln.username, password=self.senha, next=next_path))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], next_path)
        self.assertGreater(int(self.anon.cookies['sessionid']['max-age']), 0)
        self.assertNotIn(self.senha, self.anon.cookies['sessionid'].value)
        self.assertEqual(self.anon.get(next_path, secure=True).status_code, 200)
        self.assertEqual(self.anon.get('/sair/', secure=True).status_code, 405)
        r = self.post(self.anon, '/sair/')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('_auth_user_id', self.anon.session)
        self.assertEqual(self.anon.get('/vendas/', secure=True).status_code, 302)
        r = Client(enforce_csrf_checks=True).post('/vendas/gravar/', '{}', content_type='application/json', secure=True)
        self.assertEqual(r.status_code, 403)

    def test_login_nao_redireciona_para_site_externo(self):
        r = self.post(self.anon, '/entrar/', dict(username=self.lincoln.username, password=self.senha, next='https://externo.invalid'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/')

    def test_publicos_neutros_e_offline_continuam_sem_dados(self):
        for path in ['/offline/', '/offline/vendas-shell/', '/offline/revisao/',
                '/api/offline/health/', '/api/offline/session/', '/service-worker.js', '/offline/assets/2-8f/core.js']:
            r = self.anon.get(path, secure=True)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn('Cliente Segurança', r.content.decode())
            self.assertNotIn('Produto Segurança', r.content.decode())
        self.assertEqual(self.anon.get('/api/offline/snapshot/', secure=True).status_code, 401)
        self.assertEqual(self.anon.get('/inventario/', secure=True).status_code, 302)

    def test_cache_privado_e_usuario_inativo(self):
        c = self.browser(self.lincoln)
        r = c.get('/vendas/', secure=True)
        self.assertIn('no-store', r['Cache-Control'])
        self.lincoln.is_active = False
        self.lincoln.save()
        self.assertEqual(c.get('/vendas/', secure=True).status_code, 302)

    def request_admin(self):
        request = RequestFactory().get('/')
        request.user = self.lincoln
        request.session = {}
        return request

    def separacao(self):
        sep = SeparacaoVenda.objects.create(venda=self.venda)
        item = SeparacaoVendaItem.objects.create(separacao=sep, item_venda=self.venda.itens.get(),
            produto_nome_snapshot=self.produto.nome, unidade_snapshot='UN', quantidade_solicitada=2)
        return sep, item

    def test_link_separacao_restrito_a_tarefa_e_sem_login_admin(self):
        sep, item = self.separacao()
        path = f'/separacao-vendas/{sep.pk}/'
        link = proteger_path(self.request_admin(), path)
        self.assertIn('acesso_operacional=', link)
        self.assertEqual(self.anon.get(path, secure=True).status_code, 302)
        self.assertEqual(self.anon.get(link, secure=True).status_code, 200)
        self.assertNotIn('_auth_user_id', self.anon.session)
        self.assertEqual(self.anon.get(path, secure=True).status_code, 200)
        self.assertEqual(self.anon.get('/vendas/', secure=True).status_code, 302)
        self.assertEqual(self.anon.get('/separacao-vendas/99999/', secure=True).status_code, 302)
        self.assertEqual(self.post(self.anon, '/vendas/gravar/', self.dados, True).status_code, 401)
        saved = self.post(self.anon, f'{path}itens/{item.pk}/salvar/',
            {f'status_{item.pk}': SeparacaoVendaItem.STATUS_CONFERIDO})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['sucesso'])
        item.refresh_from_db()
        self.assertEqual(item.status, SeparacaoVendaItem.STATUS_CONFERIDO)
        self.assertEqual(item.quantidade_separada, 2)
        finished = self.post(self.anon, path)
        self.assertEqual(finished.status_code, 302)
        self.assertEqual(finished.url, path)
        self.assertNotIn('_auth_user_id', self.anon.session)

    def test_token_adulterado_expirado_e_emissor_revogado(self):
        sep, _ = self.separacao()
        link = proteger_path(self.request_admin(), f'/separacao-vendas/{sep.pk}/')
        self.assertEqual(self.browser().get(link + 'x', secure=True).status_code, 302)
        with override_settings(OPERACIONAL_LINK_MAX_AGE=-1):
            self.assertEqual(self.browser().get(link, secure=True).status_code, 302)
        self.lincoln.set_password(secrets.token_urlsafe(24)); self.lincoln.save()
        self.assertEqual(self.browser().get(link, secure=True).status_code, 302)

    def test_token_de_cliente_nao_libera_rota_inteira(self):
        rota = EntregaRota.objects.create(tipo='unitaria')
        item = EntregaRotaItem.objects.create(rota=rota, venda=self.venda)
        path = f'/entregas/{rota.pk}/checklist/item/{item.pk}/'
        link = proteger_path(self.request_admin(), path)
        self.assertEqual(self.anon.get(link, secure=True).status_code, 200)
        self.assertEqual(self.anon.get(f'/entregas/{rota.pk}/checklist/', secure=True).status_code, 302)
        self.assertEqual(self.post(self.anon, path).status_code, 401)

    def test_link_entrega_permite_salvar_apenas_a_rota_autorizada(self):
        from estoque.models import EntregaChecklistItem
        rota = EntregaRota.objects.create(tipo='unitaria')
        item = EntregaRotaItem.objects.create(rota=rota, venda=self.venda)
        path = f'/entregas/{rota.pk}/checklist/'
        link = proteger_path(self.request_admin(), path)
        self.assertEqual(self.anon.get(path, secure=True).status_code, 302)
        self.assertEqual(self.anon.get(link, secure=True).status_code, 200)
        checklist = EntregaChecklistItem.objects.get(rota_item=item)
        response = self.post(self.anon, path, {
            'salvar_bloco': f'{item.pk}:entrega', f'entregue_{checklist.pk}': 'on',
            f'conferido_{item.pk}': 'on', f'concluida_{item.pk}': 'on'})
        self.assertEqual(response.status_code, 302)
        checklist.refresh_from_db()
        self.assertTrue(checklist.entregue)
        self.assertEqual(self.anon.get('/entregas/99999/checklist/', secure=True).status_code, 302)
        self.assertEqual(self.anon.get('/caixa-banco/', secure=True).status_code, 302)
        self.assertNotIn('_auth_user_id', self.anon.session)

    def test_link_entrega_locacao_preserva_registro_e_comprovante_restritos(self):
        from locacoes.tests import LocacoesChecklistOperacionalTests
        from locacoes.models import FaixaPrecoLocacao, ConferenciaEntregaLocacao
        from locacoes.services import obter_ou_criar_tarefa_operacional
        from django.urls import reverse
        FaixaPrecoLocacao.objects.get_or_create(codigo='centro_perto',
            defaults={'nome': 'Ficticio', 'preco_jogo_diaria': '8.00'})
        helper = LocacoesChecklistOperacionalTests()
        helper.setUp()
        locacao = helper.criar_locacao()
        task = obter_ou_criar_tarefa_operacional(locacao, 'entrega')
        path = reverse('locacoes:conferencia_entrega', args=[task.pk])
        link = proteger_path(self.request_admin(), path)
        self.assertEqual(self.anon.get(path, secure=True).status_code, 302)
        self.assertEqual(self.anon.get(link, secure=True).status_code, 200)
        response = self.post(self.anon, path, helper.dados_conferencia_entrega())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ConferenciaEntregaLocacao.objects.filter(tarefa=task).count(), 1)
        self.assertEqual(self.anon.get(response.url, secure=True).status_code, 200)
        self.assertEqual(self.anon.get('/locacoes/', secure=True).status_code, 302)
        self.assertNotIn('_auth_user_id', self.anon.session)

    def test_preparacao_de_conta_exige_terminal_e_nao_expoe_senha(self):
        out = io.StringIO()
        call_command('preparar_administrador', username=self.lincoln.username, stdout=out)
        self.assertIn('Conta preparada', out.getvalue())
        with self.assertRaises(CommandError), patch('sys.stdin.isatty', return_value=False):
            call_command('preparar_administrador', username='nao-criada', ativar=True, stdout=out)
        self.assertFalse(get_user_model().objects.filter(username='nao-criada').exists())

    def test_preparacao_interativa_gera_privilegios_equivalentes(self):
        for username in ['titular-a-ficticio', 'titular-b-ficticio']:
            senha = secrets.token_urlsafe(24)
            out = io.StringIO()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=username), \
                    patch('getpass.getpass', side_effect=[senha, senha]):
                call_command('preparar_administrador', username=username, ativar=True, stdout=out)
            user = get_user_model().objects.get(username=username)
            self.assertTrue(user.is_active and user.is_superuser and user.is_staff)
            self.assertTrue(user.check_password(senha))
            self.assertNotIn(senha, out.getvalue())

    def test_preparacao_recusa_senha_visivel_fraca_e_divergente(self):
        import getpass
        for values in [[getpass.GetPassWarning('sem terminal seguro')], ['fraca', 'fraca'], ['uma', 'outra']]:
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='conta-nao-criada'), \
                    patch('getpass.getpass', side_effect=values), self.assertRaises(CommandError):
                call_command('preparar_administrador', username='conta-nao-criada', ativar=True, stdout=io.StringIO())
            self.assertFalse(get_user_model().objects.filter(username='conta-nao-criada').exists())

    def test_header_local_nao_burla_permissao_publica(self):
        self.assertEqual(self.anon.post('/api/vendas/online/', '{}', content_type='application/json',
            secure=True, HTTP_ORIGIN='https://testserver', HTTP_X_CSRFTOKEN=self.anon.cookies['csrftoken'].value,
            HTTP_X_LOCAL_SALES='1').status_code, 403)

    @override_settings(SESSION_COOKIE_AGE=3600)
    def test_duracao_configuravel_e_troca_senha_revoga_sessao(self):
        r = self.post(self.anon, '/entrar/', dict(username=self.lincoln.username, password=self.senha))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.anon.cookies['sessionid']['max-age']), 3600)
        self.lincoln.set_password(secrets.token_urlsafe(24)); self.lincoln.save()
        self.assertEqual(self.anon.get('/', secure=True).status_code, 302)
