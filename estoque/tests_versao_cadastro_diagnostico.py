"""Characterize existing concurrency tokens without changing their mechanism."""
import copy
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from .forms import ProdutoForm
from .models import Produto
from .services.precos_vinculados import versao_cadastro_produto
from .tests_precos_fracionados_diagnostico import FracionadosFixture
from . import tests_precos_fracionados_diagnostico as fracionados_tests
from .tests_precos_vinculados import criar_operador_precos


class VersaoCadastroDiagnosticoTests(FracionadosFixture, TestCase):
    dados_editor = fracionados_tests.FracionadosDiagnosticoTests.dados_editor

    def test_new_form_refreshes_decimal_representation_from_database(self):
        p = Produto.objects.get(pk=self.produtos[1].pk)
        original = versao_cadastro_produto(p)
        p.preco_compra = Decimal('24.0000')
        self.assertNotEqual(versao_cadastro_produto(p), original)
        form = ProdutoForm(instance=p, integrar_precos_vinculados=True)
        self.assertEqual(form.initial['versao_cadastro_precos'], original)

    def test_nested_dictionary_order_is_stable(self):
        p = Produto.objects.get(pk=self.produtos[1].pk)
        p.autoria_precos = {'a': {'x': 1, 'y': 2}, 'b': 3}
        token = versao_cadastro_produto(p)
        p.autoria_precos = {'b': 3, 'a': {'y': 2, 'x': 1}}
        self.assertEqual(versao_cadastro_produto(p), token)

    def test_null_and_empty_are_distinct_and_observable(self):
        p = Produto.objects.get(pk=self.produtos[1].pk)
        for campo, vazio in [('codigo', ''), ('preco_compra_fracionado', Decimal(0))]:
            with self.subTest(campo=campo):
                anterior = copy.copy(p)
                setattr(anterior, campo, None)
                novo = copy.copy(anterior)
                setattr(novo, campo, vazio)
                self.assertNotEqual(versao_cadastro_produto(anterior), versao_cadastro_produto(novo))

    def test_derived_price_and_metadata_are_part_of_token(self):
        p = Produto.objects.get(pk=self.produtos[1].pk)
        for campo, valor in [('preco_venda', Decimal('99')), ('cadastro_incompleto', not p.cadastro_incompleto)]:
            with self.subTest(campo=campo):
                novo = copy.copy(p)
                setattr(novo, campo, valor)
                self.assertNotEqual(versao_cadastro_produto(p), versao_cadastro_produto(novo))

    def test_save_without_business_changes_updates_timestamp_and_token(self):
        p = Produto.objects.get(pk=self.produtos[1].pk)
        antes = Produto.objects.filter(pk=p.pk).values().get()
        token = versao_cadastro_produto(p)
        p.save()
        p.refresh_from_db()
        depois = Produto.objects.filter(pk=p.pk).values().get()
        self.assertEqual({campo for campo in antes if antes[campo] != depois[campo]}, {'atualizado_em'})
        self.assertNotEqual(versao_cadastro_produto(p), token)

    def verificar_duas_sessoes(self, outro_operador):
        self.compatibilizar_fixture()
        segundo = Client()
        operador = criar_operador_precos('second-price-operator') if outro_operador else self.operador
        segundo.force_login(operador)
        url = reverse('estoque:produto_editar', args=[self.produtos[1].pk])
        original = self.dados_editor(1)
        primeira = dict(original, preco_compra='25.00', quantidade='9.000')
        self.assertEqual(segundo.post(url, primeira).status_code, 302)
        antes = list(Produto.objects.order_by('pk').values())
        resposta = self.client.post(url, dict(original, nome='Stale editor'))
        self.assertEqual(resposta.status_code, 200)
        self.assertIn('Cadastro, custo ou estoque alterado', str(resposta.context['form'].errors))
        self.assertEqual(list(Produto.objects.order_by('pk').values()), antes)
        atual = self.dados_editor(1)
        self.assertEqual(self.client.post(url, dict(atual, nome='Fresh editor')).status_code, 302)
        p = Produto.objects.get(pk=self.produtos[1].pk)
        self.assertEqual(p.quantidade, Decimal('9.000'))
        self.assertEqual(p.preco_compra, Decimal('25.00'))

    def test_other_operator_change_refuses_old_form(self):
        self.verificar_duas_sessoes(True)

    def test_same_operator_other_tab_refuses_old_form(self):
        self.verificar_duas_sessoes(False)
