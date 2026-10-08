import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from estoque.models import Cliente, ContaReceber, Funcionario, MovimentoFinanceiro, Produto, Venda
from .models import OperacaoSincronizacao


def commercial_fixtures():
    user = get_user_model().objects.create_user(username='commercial-reader', password='test-only')
    for app, code in [('offline', 'registrar_observacao'), ('estoque', 'view_cliente'), ('estoque', 'view_produto')]:
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
    client = Cliente.objects.create(nome='Cliente Comercial', apelido_nome_conhecido='Mercadinho',
        whatsapp='(91) 99999-1234', telefone_alternativo='123', prazo_padrao_dias=14,
        cpf_cnpj='12345678901', email='excluded@example.test', chave_pix='segredo-pix',
        limite_credito=9000, observacao_financeira='segredo-financeiro', bairro='Rota confidencial')
    inactive_client = Cliente.objects.create(nome='Cliente Inativo', ativo=False)
    product = Produto.objects.create(nome='Produto Fracionado', codigo='P-01',
        preco_compra=12, preco_vista=24, preco_prazo=26, unidade_compra='CX',
        vende_fracionado=True, unidade_venda_2='UN', fator_conversao=12,
        preco_vista_fracionado=3, preco_compra_fracionado=1, quantidade='2.500',
        estoque_conferido=True, preco_conferido=True, fornecedor='Fornecedor excluido', estoque_minimo=5)
    inactive_product = Produto.objects.create(nome='Produto Inativo', ativo=False, preco_compra=1, preco_vista=2, preco_prazo=3)
    excluded = Produto.objects.create(nome='Produto Excluido', excluido=True, preco_compra=1, preco_vista=2, preco_prazo=3)
    incomplete = Produto.objects.create(nome='Produto incompleto ativo', cadastro_incompleto=True, preco_compra=1, preco_vista=2, preco_prazo=3)
    operator = Funcionario.objects.create(nome='Operador Comercial', pode_operar_sistema=True, observacoes='segredo-operador')
    Funcionario.objects.create(nome='Operador Inativo', ativo=False, pode_operar_sistema=True)
    Funcionario.objects.create(nome='Nao Operador', pode_operar_sistema=False)
    return user, client, product, operator, inactive_client, inactive_product, excluded, incomplete


class CommercialSnapshotAPITests(TestCase):
    def setUp(self):
        self.user, self.customer, self.product, self.operator, *self.others = commercial_fixtures()
        self.client.force_login(self.user)
        self.url = '/api/offline/snapshot/comercial/'

    def test_complete_readonly_snapshot_and_explicit_projection(self):
        device = str(uuid4())
        before = {model.__name__: list(model.objects.order_by('pk').values()) for model in
                  [Cliente, Produto, Funcionario, Venda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]}
        response = self.client.get(self.url, {'device_id': device})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        data = response.json()
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['tipo'], 'comercial_vendas')
        self.assertEqual(data['device_id'], device)
        self.assertEqual(data['actor']['id'], str(self.user.pk))
        self.assertEqual(data['environment_id'], 'offline-isolated-tests')
        self.assertTrue(data['snapshot_id'] and data['gerado_em'])
        self.assertTrue(data['somente_referencia'])
        self.assertIn('credito', data['revalidar_no_servidor'])
        self.assertEqual(data['contagens'], {'clientes': 1, 'produtos': 2, 'operadores': 1})
        self.assertEqual(data['clientes'], [{'id': str(self.customer.pk), 'nome': self.customer.nome,
            'ativo': True, 'apelido_nome_conhecido': 'Mercadinho', 'telefone': '(91) 99999-1234', 'prazo_padrao_dias': 14}])
        produto = next(p for p in data['produtos'] if p['id'] == str(self.product.pk))
        self.assertEqual(produto['preco_venda'], '24.00')
        self.assertEqual(produto['preco_venda_fracionado'], '3.00')
        self.assertEqual(produto['custo_referencia'], '12.00')
        self.assertEqual(produto['estoque_referencia'], '2.500')
        self.assertEqual(produto['fator_conversao'], '12.00')
        self.assertEqual(produto['unidade_base'], 'CX')
        self.assertEqual(produto['unidade_venda_2'], 'UN')
        self.assertEqual(data['operadores'], [{'id': str(self.operator.pk), 'nome': self.operator.nome}])
        self.assertEqual([p['valor'] for p in data['formas_pagamento']], ['À vista', 'A prazo', 'consumo_proprio'])
        for field in ['limite_credito', 'status_credito', 'observacao_financeira', 'cpf_cnpj', 'chave_pix',
                      'email', 'bairro', 'contas', 'saldo', 'csrf_token', 'pedidos', 'fornecedor', 'estoque_minimo']:
            self.assertNotIn('"' + field + '"', json.dumps(data))
        after = {model.__name__: list(model.objects.order_by('pk').values()) for model in
                 [Cliente, Produto, Funcionario, Venda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao]}
        self.assertEqual(after, before)
        self.assertNotEqual(self.client.get(self.url).json()['snapshot_id'], data['snapshot_id'])

    def test_auth_permissions_method_and_device(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.user)
        self.user.user_permissions.remove(Permission.objects.get(content_type__app_label='estoque', codename='view_cliente'))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='estoque', codename='view_cliente'))
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(self.client.get(self.url, {'device_id': 'invalid'}).status_code, 400)

    def test_sales_screen_retains_catalog_and_commercial_action_is_gated(self):
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context['produtos'].values_list('pk', flat=True)), {self.product.pk, self.others[-1].pk})
        self.assertContains(response, '/offline/assets/2-8ab/commercial-ui.js')
        self.assertContains(response, 'id="clienteBusca"')
        self.assertContains(response, 'id="tipoVenda"')
        self.assertContains(response, 'id="operadorVenda"')
        self.client.logout()
        response = self.client.get('/vendas/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '/static/offline/commercial-ui.js')
