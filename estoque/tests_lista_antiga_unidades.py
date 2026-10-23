"""Legacy blank units require an explicit choice, never a catalogue inference."""
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from estoque import views
from estoque.models import Compra, ContaPagar, ItemCompra, MovimentoFinanceiro, Produto
from estoque.tests_compra_unidades import UnidadesFixture
from offline.browser_support import Chrome


class ListaAntigaFixture(UnidadesFixture):
    def setUp(self):
        super().setUp()
        self.compra.delete()
        self.lista.itens.update(unidade="")
        self.original_list = list(self.lista.itens.order_by('pk').values())
        self.original_products = list(Produto.objects.order_by('pk').values())

    def generate(self):
        response = self.client.post(reverse('estoque:compras_lista_fornecedor_gerar_compra', args=[self.lista.pk]))
        self.assertEqual(response.status_code, 302)
        self.compra = Compra.objects.get(lista_fornecedor=self.lista)
        self.assertEqual(self.compra.itens.filter(unidade='').count(), 15)
        return response

    def explicit_payload(self):
        payload = self.payload()
        payload['unidade[]'] = [p.unidade_venda_1 or p.unidade_compra for p in self.produtos]
        payload.pop('atualizar_custo_produto_ids[]')
        return payload

    def assert_list_unchanged(self):
        self.assertEqual(list(self.lista.itens.order_by('pk').values()), self.original_list)


class ListaAntigaUnidadesTests(ListaAntigaFixture, TestCase):
    def test_open_generate_and_open_draft_preserve_unknown_units_and_values(self):
        response = self.client.get(reverse('estoque:compras_lista_fornecedor_editar', args=[self.lista.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([line['unidade'] for line in response.context['linhas']], [''] * 15)
        for item in self.lista.itens.select_related('produto'):
            self.assertEqual(views._lista_fornecedor_produto_payload(item.produto, item=item)['unidade'], '')
        response = self.generate()
        edit = self.client.get(response.url)
        self.assertEqual([line['unidade'] for line in edit.context['item_linhas']], [''] * 15)
        for source, target in zip(self.lista.itens.order_by('pk'), self.compra.itens.order_by('pk')):
            self.assertEqual((target.produto_id, target.quantidade, target.preco_unitario, target.valor_total),
                             (source.produto_id, source.quantidade_final, source.preco_compra, source.total))
        self.assertEqual(self.compra.total, Decimal('2180'))
        self.assertEqual(list(Produto.objects.order_by('pk').values()), self.original_products)
        self.assert_sem_efeitos()
        self.assert_list_unchanged()

    def test_saving_old_list_does_not_replace_missing_units(self):
        payload = {'fornecedorId': str(self.fornecedor.pk), 'dataInicio': '2026-10-01',
                   'dataFim': '2026-10-22', 'linhas': [
                       {'produtoId': str(i.produto_id), 'sugestao': str(i.quantidade_final),
                        'precoCompra': str(i.preco_compra), 'precoUnitario': str(i.preco_unitario),
                        'total': str(i.total), 'unidade': ''} for i in self.lista.itens.order_by('pk')]}
        response = self.client.post(reverse('estoque:compras_lista_fornecedor_editar', args=[self.lista.pk]),
                                    {'lista_payload': json.dumps(payload), 'gerar_compra': '1'})
        self.assertEqual(response.status_code, 302)
        self.compra = Compra.objects.get(lista_fornecedor=self.lista)
        self.assertEqual(self.lista.itens.filter(unidade='').count(), 15)
        self.assertEqual(self.compra.itens.filter(unidade='').count(), 15)
        self.assert_sem_efeitos()

    def test_partial_draft_survives_failed_finalization_and_names_every_missing_product(self):
        self.generate()
        payload = self.explicit_payload()
        payload['unidade[]'][-2:] = ['', '   ']
        payload['acao_compra'] = 'salvar_rascunho'
        edit_url = reverse('estoque:compra_editar', args=[self.compra.pk])
        self.assertEqual(self.client.post(edit_url, payload).status_code, 302)
        snapshot = list(self.compra.itens.order_by('pk').values())
        payload['acao_compra'] = 'confirmar_financeiro'
        for url in [edit_url, reverse('estoque:compra_finalizar', args=[self.compra.pk])]:
            response = self.client.post(url, payload)
            text = ' '.join(str(m) for m in response.wsgi_request._messages)
            for product in self.produtos[-2:]:
                self.assertIn(product.nome, text)
            self.assertIn('rascunho foi preservado', text)
            self.assertEqual(list(self.compra.itens.order_by('pk').values()), snapshot)
            self.assert_sem_efeitos()
        edit = self.client.get(edit_url)
        self.assertEqual([line['unidade'] for line in edit.context['item_linhas']], payload['unidade[]'][:-2] + ['', ''])
        self.assert_list_unchanged()
        response = self.client.post(reverse('estoque:compra_finalizar', args=[self.compra.pk]), self.explicit_payload())
        self.assertEqual(response.status_code, 302)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(ContaPagar.objects.get(compra=self.compra).valor_original, Decimal('2153.11'))
        self.assertEqual(self.compra.itens.filter(unidade='').count(), 0)
        self.assert_list_unchanged()

    def test_service_lists_all_missing_units_and_rolls_back(self):
        self.generate()
        before = list(self.compra.itens.values())
        with self.assertRaises(ValueError) as error:
            views._finalizar_compra_com_financeiro(self.compra)
        for product in self.produtos:
            self.assertIn(product.nome, str(error.exception))
        self.assertEqual(list(self.compra.itens.values()), before)
        self.assert_sem_efeitos()

    def test_explicit_units_finalize_once_preserving_prices_conversion_and_finance(self):
        self.generate()
        payload = self.explicit_payload()
        url = reverse('estoque:compra_finalizar', args=[self.compra.pk])
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertTrue(self.compra.estoque_entrada_realizada)
        self.assertEqual((self.compra.total_produtos, self.compra.total, self.compra.ajuste_total),
                         (Decimal('2180'), Decimal('2153.11'), Decimal('-26.89')))
        self.assertEqual(ContaPagar.objects.get(compra=self.compra).valor_original, Decimal('2153.11'))
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        for before, product, item in zip(self.original_products, self.produtos, self.compra.itens.order_by('pk')):
            product.refresh_from_db()
            after = Produto.objects.filter(pk=product.pk).values().get()
            for field in ['fator_conversao', 'preco_compra', 'preco_compra_fracionado',
                          'preco_vista', 'preco_prazo', 'preco_vista_fracionado', 'preco_prazo_fracionado']:
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(product.quantidade, Decimal('10') + item.quantidade)
            self.assertEqual(item.unidade, product.unidade_venda_1 or product.unidade_compra)
        stock = list(Produto.objects.values_list('pk', 'quantidade'))
        self.client.post(url, payload)
        self.assertEqual(list(Produto.objects.values_list('pk', 'quantidade')), stock)
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 1)
        self.assert_list_unchanged()

    def test_explicit_incompatible_unit_still_blocks_without_conversion(self):
        self.generate()
        payload = self.explicit_payload()
        payload['unidade[]'][0] = 'UN'
        response = self.client.post(reverse('estoque:compra_finalizar', args=[self.compra.pk]), payload)
        self.assertTrue(any('diverge do estoque' in str(m) for m in response.wsgi_request._messages))
        self.assert_sem_efeitos()
        self.assert_list_unchanged()


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(), 'Chrome unavailable')
class ListaAntigaUnidadesBrowserTests(ListaAntigaFixture, StaticLiveServerTestCase):
    def test_missing_units_remain_editable_on_desktop_and_mobile(self):
        self.generate()
        with TemporaryDirectory(prefix='legacy-list-units-') as profile:
            browser = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = browser.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value,
                                              'url': self.live_server_url})
                for width in [1280, 320, 360, 390]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 860))
                    tab.call('Page.navigate', {'url': self.live_server_url + reverse('estoque:compra_editar', args=[self.compra.pk])})
                    tab.wait('document.readyState === "complete" && !!document.getElementById("formCompra")')
                    self.assertEqual(tab.evaluate('[...document.querySelectorAll(\'#tabelaItensCompra [name="unidade[]"]\')].map(i=>i.value)'), [''] * 15)
                    self.assertEqual(tab.evaluate('''(() => {
                        const input = document.querySelector('#tabelaItensCompra [name="unidade[]"]');
                        input.value = 'PCT'; input.dispatchEvent(new Event('input', {bubbles:true}));
                        input.dispatchEvent(new Event('change', {bubbles:true}));
                        return [input.value, input.disabled, input.readOnly];
                    })()'''), ['PCT', False, False])
                # Send the actual editor form, including its CSRF token, to save a partial draft.
                self.assertEqual(tab.evaluate('''(async () => {
                    const form = document.getElementById('formCompra');
                    const data = new FormData(form);
                    data.set('acao_compra', 'salvar_rascunho');
                    const response = await fetch(form.action || location.href, {
                        method:'POST', body:data, credentials:'same-origin'
                    });
                    return response.status;
                })()'''), 200)
            finally:
                browser.stop()
        self.assertEqual(self.compra.itens.get(produto=self.produtos[0]).unidade, 'PCT')
        self.assertEqual(self.compra.itens.filter(unidade='').count(), 14)
        self.assert_sem_efeitos()
        self.assert_list_unchanged()
