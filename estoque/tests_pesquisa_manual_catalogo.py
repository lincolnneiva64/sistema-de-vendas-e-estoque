"""Catálogo manual completo com dados fictícios e consultas em lote."""
from time import perf_counter

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque import tests_compra_mobile_total_browser as existing
from estoque.models import Fornecedor, Produto, ProdutoFornecedor


class PesquisaManualCatalogoTests(TestCase):
    setUp = existing.CompraMobileTotalBrowserTests.setUp

    def consultar(self, fornecedor):
        return self.client.get(reverse('estoque:sugestao_compra_fornecedor'), {
            'fornecedor': fornecedor.pk, 'periodo': '7', 'consulta_manual': '1',
        })

    def test_catalogo_independe_do_fornecedor_e_preserva_geracao(self):
        self.products[0].nome = 'Skol ficticia vinculada'
        self.products[0].save(update_fields=['nome'])
        amstel = self.products[-1]
        amstel.nome = 'Amstel ficticia sem vinculo'
        amstel.fornecedor = 'Fornecedor habitual ficticio'
        amstel.save(update_fields=['nome', 'fornecedor'])
        fornecedor_habitual = amstel.fornecedor
        ProdutoFornecedor.objects.filter(produto=amstel).delete()
        Produto.objects.create(nome='Amstel inativa ficticia', ativo=False, preco_compra=10, preco_vista=20, preco_prazo=21)
        Produto.objects.create(nome='Skol excluida ficticia', excluido=True, preco_compra=10, preco_vista=20, preco_prazo=21)
        outro = Fornecedor.objects.create(nome='Outro atacadista ficticio')
        vinculos = list(ProdutoFornecedor.objects.values())
        for fornecedor in (self.supplier, outro):
            resposta = self.consultar(fornecedor)
            self.assertEqual(resposta.status_code, 200)
            produtos = resposta.json()['produtos']
            self.assertEqual({p['id'] for p in produtos}, {p.pk for p in self.products})
            self.assertEqual(len(produtos), len(self.products))
            for termo, produto in [('ams', amstel), ('sko', self.products[0])]:
                self.assertEqual([p['id'] for p in produtos if termo in p['nome'].lower()], [produto.pk])
            self.assertEqual(next(p for p in produtos if p['id'] == amstel.pk)['quantidade_vendida'], '1.000')
            pagina = self.client.get(reverse('estoque:sugestao_compra_fornecedor'), {
                'fornecedor': fornecedor.pk, 'periodo': '7',
            })
            self.assertNotIn(amstel.pk, [l['produto_id'] for l in pagina.context['linhas']])
            self.assertEqual(produtos, pagina.context['produtos_manual_payload'])
        self.assertEqual(list(ProdutoFornecedor.objects.values()), vinculos)
        amstel.refresh_from_db()
        self.assertEqual(amstel.fornecedor, fornecedor_habitual)

    def test_catalogo_completo_mantem_consultas_em_lote(self):
        with CaptureQueriesContext(connection) as pequena:
            self.consultar(self.supplier)
        Produto.objects.bulk_create([
            Produto(nome=f'Produto ficticio adicional {n:04}', quantidade=0, estoque_minimo=1)
            for n in range(1000)
        ])
        inicio = perf_counter()
        with CaptureQueriesContext(connection) as grande:
            resposta = self.consultar(self.supplier)
        duracao = perf_counter() - inicio
        self.assertEqual(len(resposta.json()['produtos']), 1004)
        self.assertEqual(len(grande), len(pequena))
        print(f'Catalogo ficticio: 4/1004 produtos, {len(pequena)}/{len(grande)} consultas; '
              f'1004 produtos em {duracao:.3f}s; JSON {len(resposta.content)} bytes')
