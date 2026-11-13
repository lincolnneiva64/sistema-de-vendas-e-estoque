"""Regressões sintéticas do incidente; nunca consulta compras reais."""
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from estoque import views
from estoque.models import ContaPagar, ItemCompra, MovimentoFinanceiro
from estoque.tests_compra_pre_revisao import CompraPreRevisaoFixture


class CompraRascunhoUnidadeTests(CompraPreRevisaoFixture, TestCase):
    def test_recusa_valor_monetario_sem_alterar_rascunho(self):
        item = self.compra.itens.get()
        response = self.client.post(self.salvar_url, self.payload(**{'unidade[]': ['2,95']}))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unidade', response.json()['erro'])
        item.refresh_from_db()
        self.assertEqual(item.unidade, 'UN')
        self.assert_sem_efeitos()

    def test_corrige_legado_normaliza_e_reabre_valor_persistido(self):
        self.compra.itens.update(unidade='2,95')
        page = self.client.get(self.editar_url)
        self.assertEqual(page.context['item_linhas'][0]['unidade'], '2,95')
        self.assertTrue(page.context['item_linhas'][0]['unidade_divergente'])
        response = self.client.post(self.salvar_url, self.payload(**{
            'unidade[]': [' un '], 'quantidade[]': ['10'], 'preco_unitario[]': ['2,95']}))
        self.assertEqual(response.status_code, 200)
        item = self.compra.itens.get()
        self.assertEqual((item.unidade, item.quantidade, item.preco_unitario), ('UN', Decimal('10'), Decimal('2.95')))
        page = self.client.get(self.editar_url)
        self.assertFalse(page.context['item_linhas'][0]['unidade_divergente'])
        self.assertEqual(page.context['item_linhas'][0]['unidade'], item.unidade)

    def test_codigo_legitimo_com_numero_e_unidade_vazia(self):
        self.produto.unidade_compra = 'CX12'
        self.produto.unidade_venda_1 = 'CX12'
        self.produto.save()
        for unidade in (' cx12 ', ''):
            response = self.client.post(self.salvar_url, self.payload(**{'unidade[]': [unidade]}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.compra.itens.get().unidade, 'CX12')

    def test_falha_financeira_reenvia_mesma_revisao_sem_duplicar(self):
        # Read the preview after saving, exactly as the browser does.
        payload = self.payload(acao_compra='confirmar_financeiro')
        response = self.client.post(self.salvar_url, payload)
        payload['previa_decisao_precos'] = response.json()['revisao_precos']['previa']
        criar = views._criar_contas_pagar_compra
        def falhar(compra):
            criar(compra)
            raise RuntimeError('falha sintética após financeiro')
        headers = {'HTTP_X_COMPRA_DECISAO': '1'}
        with patch.object(views, '_criar_contas_pagar_compra', side_effect=falhar):
            response = self.client.post(self.finalizar_url, payload, **headers)
        self.assertEqual(response.status_code, 500)
        self.assertIn('Estoque', response.json()['erro'])
        self.assertNotIn('revisar_precos', response.json())
        self.assert_sem_efeitos()
        from inventario.models import MovimentoObservado
        self.assertFalse(MovimentoObservado.objects.filter(produto=self.produto, origem='compra').exists())
        response = self.client.post(self.finalizar_url, payload, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal('19'))
        contas = list(ContaPagar.objects.filter(compra=self.compra).values_list('pk', flat=True))
        self.assertEqual(len(contas), 3)
        response = self.client.post(self.finalizar_url, payload, **headers)
        self.assertTrue(response.json()['ok'])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal('19'))
        self.assertEqual(list(ContaPagar.objects.filter(compra=self.compra).values_list('pk', flat=True)), contas)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
    def test_reproduz_aceitacao_na_versao_de_producao(self):
        import ast
        import subprocess
        from django.test import RequestFactory
        source = subprocess.check_output(['git', 'show', 'e52bd9fb53bb6c10d66af6ead89983f01ea9f22a:estoque/views.py']).decode('utf-8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_dados_compra_post')
        namespace = dict(vars(views))
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<baseline-compra>', 'exec'), namespace)
        dados = namespace['_dados_compra_post'](RequestFactory().post('/', self.payload(**{'unidade[]': ['2,95']})))
        self.assertEqual(dados['itens'][0]['unidade'], '2,95')
    def test_lista_valida_antes_de_gerar_e_preserva_origem(self):
        from types import SimpleNamespace
        from estoque.models import Compra, ItemListaCompraFornecedor, ListaCompraFornecedor
        self.lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,
            data_lista='2026-10-08', data_inicio_periodo='2026-10-01', data_fim_periodo='2026-10-08')
        item = ItemListaCompraFornecedor.objects.create(lista=self.lista, produto=self.produto,
            quantidade_final=10, unidade='2,95', preco_compra='2.95', total='29.50')
        antes = Compra.objects.count()
        with self.assertRaisesMessage(ValueError, 'Unidade'):
            views._criar_compra_rascunho_da_lista_fornecedor(self.lista, SimpleNamespace(user=self.user))
        self.assertEqual(Compra.objects.count(), antes)
        item.unidade = 'UN'
        item.save(update_fields=['unidade'])
        compra = views._criar_compra_rascunho_da_lista_fornecedor(self.lista, SimpleNamespace(user=self.user))
        self.assertEqual(compra.lista_fornecedor_id, self.lista.pk)
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        linha = compra.itens.get()
        self.assertEqual((linha.quantidade, linha.unidade, linha.preco_unitario), (10, 'UN', Decimal('2.95')))
        self.assertFalse(compra.estoque_entrada_realizada)
