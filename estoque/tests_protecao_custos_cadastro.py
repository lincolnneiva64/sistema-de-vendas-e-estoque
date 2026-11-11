"""Product and purchase price approvals, exclusively synthetic data."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .admin import ProdutoAdminForm
from .forms import ProdutoForm
from .models import Categoria, Unidade, Produto, Compra, ItemCompra, MovimentoFinanceiro
from .services.precos_compra import aplicar_precos_compra
from .services import decisoes_precos_compra as decisoes
from .tests_decisoes_precos_compra import DecisoesFixture
from .tests_compra_pre_revisao import CompraPreRevisaoFixture


class CadastroCustosTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser(username='custos-ficticios',password='teste')
        self.client.force_login(self.user)
        Categoria.objects.create(nome='Mercearia')
        for sigla in ('PCT','UN'):
            Unidade.objects.create(nome=sigla,sigla=sigla)

    def dados(self, **extra):
        data=dict(nome='Isqueiro fictício',categoria='Mercearia',unidade_compra='PCT',
            preco_compra='62.54',preco_vista='70',preco_prazo='75',quantidade='10',estoque_minimo='0',
            vende_fracionado='on',fator_conversao='12',unidade_venda_2='UN',preco_compra_fracionado='5.21',
            preco_vista_fracionado='6',preco_prazo_fracionado='6.50',
            percentual_vista_fracionado='20',percentual_prazo_fracionado='25')
        data.update(extra)
        return data

    def produto(self):
        form=ProdutoForm(self.dados())
        self.assertTrue(form.is_valid(),form.errors)
        return form.save()

    def test_legacy_invalid_product_can_correct_cost_or_prices(self):
        for changes in ({'preco_compra':'60'}, {'preco_vista':'85','preco_prazo':'90'},
                        {'preco_compra':'65','preco_vista':'85','preco_prazo':'90'}):
            with self.subTest(changes=changes):
                product=self.produto()
                Produto.objects.filter(pk=product.pk).update(preco_compra=Decimal('80'))
                response=self.client.post(reverse('estoque:produto_editar',args=[product.pk]),self.dados(**changes))
                self.assertEqual(response.status_code,302,
                    response.context['form'].errors if response.status_code==200 else response.content)
                product.refresh_from_db()
                product.delete()

    def test_new_product_blocks_each_main_price_even_with_loss_option(self):
        for field in ('preco_vista','preco_prazo'):
            with self.subTest(field=field):
                form=ProdutoForm(self.dados(**{field:'58'}))
                self.assertFalse(form.is_valid())
                self.assertIn('compra',str(form.errors))
                product=Produto(nome='Exceção não autorizada',preco_compra=Decimal('62.54'),
                    preco_vista=Decimal('70'),preco_prazo=Decimal('75'),permitir_prejuizo=True,motivo_prejuizo='Não libera preço inválido')
                setattr(product,field,Decimal('58'))
                with self.assertRaises(ValidationError):product.full_clean()
        self.assertEqual(Produto.objects.count(),0)

    def test_new_product_http_blocks_both_fractional_prices(self):
        for field in ('preco_vista_fracionado','preco_prazo_fracionado'):
            response=self.client.post(reverse('estoque:cadastrar_produto'),self.dados(**{field:'5.20'}))
            self.assertEqual(response.status_code,200)
            self.assertIn('custo individual',str(response.context['form'].errors))
            self.assertEqual(Produto.objects.count(),0)

    def test_edit_product_rejects_invalid_main_and_fractional_prices_without_writes(self):
        product=self.produto()
        before=Produto.objects.values().get(pk=product.pk)
        for field in ('preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'):
            value='58' if field in ('preco_vista','preco_prazo') else '5.20'
            response=self.client.post(reverse('estoque:produto_editar',args=[product.pk]),self.dados(**{field:value}))
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.context['form'].errors)
            self.assertEqual(Produto.objects.values().get(pk=product.pk),before)

    def test_cost_increase_requires_review_of_all_sale_prices(self):
        product=self.produto()
        before=Produto.objects.values().get(pk=product.pk)
        for changes in ({'preco_compra':'80'},{'preco_compra_fracionado':'7'}):
            form=ProdutoForm(self.dados(**changes),instance=Produto.objects.get(pk=product.pk))
            self.assertFalse(form.is_valid())
            self.assertEqual(Produto.objects.values().get(pk=product.pk),before)
        form=ProdutoForm(self.dados(preco_compra='80',preco_vista='85',preco_prazo='90',
            preco_compra_fracionado='6.67',preco_vista_fracionado='7',preco_prazo_fracionado='8'),instance=product)
        self.assertTrue(form.is_valid(),form.errors)

    def test_fractional_fallback_rounding_and_equal_cost(self):
        form=ProdutoForm(self.dados(preco_compra='10.05',preco_compra_fracionado='',fator_conversao='2',
            preco_vista_fracionado='5.02',preco_prazo_fracionado='5.02'))
        self.assertTrue(form.is_valid(),form.errors)
        form=ProdutoForm(self.dados(preco_compra='10.05',preco_compra_fracionado='',fator_conversao='2',
            preco_vista_fracionado='5.01'))
        self.assertFalse(form.is_valid())

    def test_price_guards_and_sale_use_same_cost_for_each_unit(self):
        from .views import _custo_produto_para_unidade_venda
        from .services.precos_vinculados import custo_fracionado_produto
        product=self.produto()
        for explicit,expected in ((Decimal('5.21'),Decimal('5.21')),(Decimal('0'),Decimal('5.21'))):
            product.preco_compra_fracionado=explicit
            self.assertEqual(_custo_produto_para_unidade_venda(product,'PCT'),Decimal('62.54'))
            self.assertEqual(_custo_produto_para_unidade_venda(product,' un '),expected)
            self.assertEqual(custo_fracionado_produto(product),expected)

    def test_admin_reuses_the_same_fractional_protection(self):
        product=self.produto()
        data=ProdutoAdminForm(instance=product).initial
        valid=ProdutoAdminForm(data,instance=Produto.objects.get(pk=product.pk))
        self.assertTrue(valid.is_valid(),valid.errors)
        data['preco_prazo_fracionado']='5.20'
        invalid=ProdutoAdminForm(data,instance=Produto.objects.get(pk=product.pk))
        self.assertFalse(invalid.is_valid())
        self.assertIn('custo individual',str(invalid.errors))


class CompraCustosTests(DecisoesFixture,TestCase):
    def snapshot(self):
        return [list(m.objects.order_by('pk').values()) for m in (Produto,Compra,ItemCompra,MovimentoFinanceiro)]

    def test_deferred_purchase_can_receive_legacy_invalid_product(self):
        Produto.objects.filter(pk=self.produto.pk).update(preco_compra=Decimal('48'))
        self.defer()
        self.assertEqual(self.compra.status,Compra.STATUS_FINALIZADA)
        self.assertTrue(self.compra.estoque_entrada_realizada)
        self.assertEqual(self.produto.quantidade,Decimal('25'))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)

    def test_purchase_price_service_cannot_write_independent_prices_below_cost(self):
        item=self.compra.itens.get()
        before=self.snapshot()
        for field in ('preco_vista','preco_prazo'):
            with self.assertRaises(ValidationError):
                aplicar_precos_compra(item,{field:Decimal('35')},operador=self.usuario)
            self.assertEqual(self.snapshot(),before)

    def test_reviewed_purchase_cannot_keep_prices_below_new_cost(self):
        data=self.decisao(payload=self.payload(**{'preco_unitario[]':['50'],'origem_banco':'750'}))
        before=self.snapshot()
        response=self.client.post(self.finalizar_url,data)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.snapshot(),before)

    def test_independent_fractional_prices_use_the_existing_unit_cost_guard(self):
        self.produto.vende_fracionado=True
        self.produto.fator_conversao=10
        self.produto.unidade_venda_2='UN'
        self.produto.preco_compra_fracionado=Decimal('3.60')
        self.produto.preco_vista_fracionado=Decimal('4.50')
        self.produto.preco_prazo_fracionado=Decimal('4.60')
        self.produto.save()
        before=self.snapshot()
        for field in ('preco_vista_fracionado','preco_prazo_fracionado'):
            with self.assertRaises(ValidationError):
                aplicar_precos_compra(self.compra.itens.get(),{field:Decimal('3.59')},operador=self.usuario)
            self.assertEqual(self.snapshot(),before)

    def test_cost_and_sale_prices_can_be_reviewed_in_the_same_purchase(self):
        data=self.decisao('alterar',payload=self.payload(**{'preco_unitario[]':['50'],'origem_banco':'750'}))
        data.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)]*2,
            'atualizar_preco_venda_nomes[]':['preco_vista','preco_prazo'],
            'atualizar_preco_venda_valores[]':['55','56']})
        self.assertEqual(self.client.post(self.finalizar_url,data).status_code,302)
        self.compra.refresh_from_db();self.produto.refresh_from_db()
        self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertEqual((self.produto.preco_compra,self.produto.preco_vista,self.produto.preco_prazo),
                         (Decimal('50'),Decimal('55'),Decimal('56')))

    def defer(self):
        data=self.payload(**{'preco_unitario[]':['50'],'origem_banco':'750'})
        data.update(previa_decisao_precos=self.previa(data),decisao_precos_compra='adiar')
        self.assertEqual(self.client.post(self.finalizar_url,data).status_code,302)
        self.compra.refresh_from_db();self.produto.refresh_from_db()
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertEqual(self.produto.preco_compra,Decimal('50'))
        self.assertFalse(self.produto.permitir_prejuizo)
        self.item=self.compra.itens.get()

    def posterior(self, changes=None):
        data=dict(compra_id=self.compra.pk,item_id=self.item.pk,produto_id=self.produto.pk,
            previa_decisao_precos=decisoes.preparar_posterior(self.compra,self.usuario)[f'p:{self.produto.pk}'],
            decisao_precos='alterar' if changes else 'manter')
        if changes:
            data.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)]*len(changes),
                'atualizar_preco_venda_nomes[]':list(changes), 'atualizar_preco_venda_valores[]':list(changes.values())})
        return self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),data)

    def test_deferred_purchase_keeps_pending_review_and_cannot_confirm_invalid_prices(self):
        self.defer()
        before=self.snapshot()
        self.assertEqual(self.posterior().status_code,400)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.posterior({'preco_vista':'55'}).status_code,400)
        self.assertEqual(self.snapshot(),before)
        quantities=list(Produto.objects.values_list('pk','quantidade'))
        finance=list(MovimentoFinanceiro.objects.values())
        self.assertEqual(self.posterior({'preco_vista':'55','preco_prazo':'56'}).status_code,200)
        self.compra.refresh_from_db();self.item.refresh_from_db()
        self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertTrue(self.item.revisao_preco_concluida)
        self.assertEqual(list(Produto.objects.values_list('pk','quantidade')),quantities)
        self.assertEqual(list(MovimentoFinanceiro.objects.values()),finance)

    def test_posterior_cannot_close_while_fractional_prices_are_still_below_cost(self):
        self.produto.vende_fracionado=True
        self.produto.fator_conversao=10
        self.produto.unidade_venda_2='UN'
        self.produto.preco_compra_fracionado=Decimal('3.60')
        self.produto.preco_vista_fracionado=Decimal('4.50')
        self.produto.preco_prazo_fracionado=Decimal('4.60')
        self.produto.save()
        self.defer()
        before=self.snapshot()
        self.assertEqual(self.posterior({'preco_vista':'55','preco_prazo':'56'}).status_code,400)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.posterior({'preco_vista':'55','preco_prazo':'56',
            'preco_vista_fracionado':'5.50','preco_prazo_fracionado':'5.60'}).status_code,200)


class CompraPendenteBoletosCustosTests(CompraPreRevisaoFixture,TestCase):
    def test_cost_increase_deferred_launches_stock_and_payables_once(self):
        self.check_deferred_payables(Decimal('5'))

    def test_legacy_invalid_deferred_launches_stock_and_payables_once(self):
        self.check_deferred_payables(Decimal('18'))

    def check_deferred_payables(self, old_cost):
        Produto.objects.filter(pk=self.produto.pk).update(preco_compra=old_cost)
        data=self.payload(acao_compra='confirmar_financeiro',revisao_precos_pendente='1',
            **{'preco_unitario[]':['20'],'atualizar_custo_produto_ids[]':[str(self.produto.pk)],
               'compra_valor_nota_boleto':'180','compra_parcela_valor_1':'60',
               'compra_parcela_valor_2':'60','compra_parcela_valor_3':'60'})
        data['previa_decisao_precos']=self.client.post(self.salvar_url,data).json()['revisao_precos']['previa']
        for _ in range(2):
            self.assertEqual(self.client.post(self.finalizar_url,data).status_code,302)
        self.compra.refresh_from_db();self.produto.refresh_from_db()
        self.assertEqual(self.compra.status,Compra.STATUS_FINALIZADA)
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertTrue(self.compra.estoque_entrada_realizada)
        self.assertEqual(self.produto.quantidade,Decimal('19'))
        self.assertEqual(self.produto.preco_compra,Decimal('20'))
        self.assertEqual(self.produto.preco_vista,Decimal('15'))
        self.assertEqual(list(self.compra.contas_pagar.order_by('numero_parcela').values_list('valor_original',flat=True)),[Decimal('60')]*3)
        self.assertFalse(self.compra.itens.filter(revisao_preco_concluida=True).exists())
