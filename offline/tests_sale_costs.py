"""Cost checks only read fictional products, never create sale operations."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings

from estoque.models import Produto, Venda, ContaReceber, MovimentoFinanceiro
from .models import OperacaoSincronizacao
from .tests_commercial import commercial_fixtures


class SaleCostTests(TestCase):
    url = '/api/vendas/online/custos/'

    def setUp(self):
        self.user, self.customer, self.product, *_ = commercial_fixtures()
        self.client.force_login(self.user)

    def check(self, items=None):
        return self.client.post(self.url, json.dumps({'itens': items or [self.item()]}), content_type='application/json')

    def item(self, price='24', unit='CX', product=None):
        return {'produto_id':str((product or self.product).pk), 'preco_unitario':price, 'unidade':unit}

    def test_multiple_products_equal_price_and_cost_and_readonly(self):
        other = Produto.objects.create(nome='Produto custo alto', preco_compra=Decimal('62.54'), preco_vista=Decimal('70'), preco_prazo=Decimal('70'), unidade_venda_1='PCT', quantidade=10)
        before = list(Produto.objects.order_by('pk').values())
        with self.assertNumQueries(1):
            from .sale_costs import inspect_costs
            rows = inspect_costs([self.item('12'), self.item('58','PCT',other)])
        self.assertEqual([r['estado'] for r in rows], ['ok','abaixo'])
        self.assertEqual(rows[1]['custo'], '62.54')
        self.assertEqual(rows[1]['nome'], other.nome)
        response = self.check([self.item('12'), self.item('58','PCT',other)])
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response['Cache-Control'])
        self.assertEqual(list(Produto.objects.order_by('pk').values()),before)
        for model in (Venda, ContaReceber, MovimentoFinanceiro, OperacaoSincronizacao):
            self.assertEqual(model.objects.count(),0)

    def test_fractional_cost_override_conversion_and_half_even(self):
        self.assertEqual(self.check([self.item('1','un')]).json()['itens'][0]['estado'],'ok')
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=Decimal('10.05'), preco_compra_fracionado=None, fator_conversao=2)
        row = self.check([self.item('5.02','UN')]).json()['itens'][0]
        self.assertEqual((row['custo'],row['estado']),('5.02','ok'))
        self.assertEqual(self.check([self.item('5.01','UN')]).json()['itens'][0]['estado'],'abaixo')

    def test_cost_change_is_read_again_not_cached(self):
        self.assertEqual(self.check().json()['itens'][0]['estado'],'ok')
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=30)
        self.assertEqual(self.check().json()['itens'][0]['estado'],'abaixo')
        Produto.objects.filter(pk=self.product.pk).update(preco_compra=12)
        self.assertEqual(self.check().json()['itens'][0]['estado'],'ok')

    def test_existing_official_sale_stock_finance_and_history_stay_intact(self):
        from .tests_sale_creation import fixtures, SaleCreationTests
        from .services import process_operation
        user, customer, product, command = fixtures()
        command['payload']['operador'] = 'Operador Comercial'
        for index in range(5):
            extra = Produto.objects.create(nome=f'Produto oficial fictício {index}', quantidade=10,
                preco_compra=Decimal('1'), preco_vista=Decimal('2'), preco_prazo=Decimal('2'), unidade_venda_1='UN')
            command['payload']['itens'].append({'produto_id':str(extra.pk),'quantidade':'1','unidade':'UN','preco_unitario':'2'})
        self.assertEqual(process_operation(command,user)[1],200)
        before=SaleCreationTests.effects(self)
        operations=list(OperacaoSincronizacao.objects.values())
        self.check([self.item('1')])
        self.assertEqual(SaleCreationTests.effects(self),before)
        self.assertEqual(list(OperacaoSincronizacao.objects.values()),operations)

    def test_unknown_cost_unit_and_inactive_product_are_not_assumed_safe(self):
        self.assertEqual(self.check([self.item(unit='KG')]).json()['itens'][0]['estado'],'indisponivel')
        for changes in ({'preco_compra':None}, {'preco_compra':0}, {'ativo':False}):
            Produto.objects.filter(pk=self.product.pk).update(**changes)
            self.assertEqual(self.check().json()['itens'][0]['estado'],'indisponivel')

    def test_auth_permission_csrf_and_bad_input(self):
        self.assertEqual(Client().post(self.url,'{}',content_type='application/json').status_code,401)
        stranger=get_user_model().objects.create_user(username='no-cost-access')
        client=Client();client.force_login(stranger)
        self.assertEqual(client.post(self.url,'{}',content_type='application/json').status_code,403)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url,'{}',content_type='application/json').status_code,403)
        for payload in ({'itens':[]}, {'itens':[self.item('NaN')]}, {'itens':[self.item('1e1000')]}, {'itens':[{'produto_id':'1;DROP'}]}, []):
            self.assertEqual(self.client.post(self.url,json.dumps(payload),content_type='application/json').status_code,400)

    @override_settings(LOCAL_SALES_LOOPBACK=True)
    def test_local_access_requires_existing_trusted_workstation(self):
        client=Client(HTTP_HOST='localhost')
        client.get('/vendas/')
        response=client.post(self.url,json.dumps({'itens':[self.item()]}),content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(client.post(self.url,'{}',content_type='application/json',HTTP_X_FORWARDED_FOR='127.0.0.1').status_code,401)
