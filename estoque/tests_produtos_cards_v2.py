"""Cards reais de produtos: dados, controles existentes e custo de consultas."""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Categoria, Produto


class ProdutosCardsV2Tests(TestCase):
    def produto(self, nome="Produto ilustrativo", **campos):
        dados = dict(nome=nome, categoria="Bebidas", fornecedor="Fornecedor de teste",
                     quantidade=20, estoque_minimo=3, unidade_compra="CX",
                     preco_compra=48, preco_vista=72, preco_prazo=78)
        dados.update(campos)
        return Produto.objects.create(**dados)

    def test_precos_fracionamento_saldos_e_acoes_da_pagina_real(self):
        p = self.produto(vende_fracionado=True, unidade_venda_2="UN", fator_conversao=12,
                         preco_compra_fracionado=4, preco_vista_fracionado=6,
                         preco_prazo_fracionado=Decimal("6.50"))
        antes = Produto.objects.filter(pk=p.pk).values().get()
        resposta = self.client.get(reverse("estoque:home"))
        self.assertEqual(resposta.status_code, 200)
        for texto in (p.fornecedor, "Compra", "Venda à vista", "Venda a prazo",
                      "48,00", "4,00", "72,00", "6,00", "78,00", "6,50", "1 CX = 12 UN"):
            self.assertContains(resposta, texto)
        self.assertContains(resposta, reverse("estoque:produto_editar", args=[p.pk]))
        self.assertContains(resposta, reverse("estoque:produto_alternar_ativo", args=[p.pk]))
        self.assertContains(resposta, f'id="produto-{p.pk}"')
        self.assertContains(resposta, f'data-produto-id="{p.pk}"')
        self.assertContains(resposta, 'name="csrfmiddlewaretoken"')
        self.assertEqual(Produto.objects.filter(pk=p.pk).values().get(), antes)

    def test_nome_longo_dados_ausentes_e_produto_sem_fracionamento(self):
        nome = "Produto com identificação extensa " * 4
        p = self.produto(nome=nome[:100], categoria=None, fornecedor=None, unidade_compra="KG")
        # Representa cadastro legado incompleto sem alterar a validação do model.
        Produto.objects.filter(pk=p.pk).update(preco_compra=None)
        resposta = self.client.get(reverse("estoque:home"))
        self.assertContains(resposta, p.nome)
        for texto in ("Sem categoria", "Fornecedor não informado", "Sem fracionamento", 'aria-label="Não informado"'):
            self.assertContains(resposta, texto)
        self.assertNotContains(resposta, "produto-preco-fracionado")

    def test_estoque_baixo_zerado_e_busca_filtros_existentes(self):
        baixo = self.produto("Arroz baixo", quantidade=2, estoque_minimo=5, unidade_compra="PCT")
        self.produto("Produto normal", quantidade=50)
        zerado = self.produto("Produto zerado", quantidade=0)
        Categoria.objects.create(nome="Bebidas")
        resposta = self.client.get(reverse("estoque:home"), {"f": "critico"})
        self.assertContains(resposta, f'id="produto-{baixo.pk}"')
        self.assertContains(resposta, "Estoque baixo")
        self.assertNotContains(resposta, 'data-produto-search="Produto normal"')
        self.assertNotContains(resposta, f'id="produto-{zerado.pk}"')
        resposta = self.client.get(reverse("estoque:home"), {"q": "Arroz"})
        self.assertContains(resposta, 'data-categoria="bebidas"')
        self.assertContains(resposta, 'id="buscarProduto"')
        self.assertContains(resposta, 'id="filtroCategoriaProduto"')
        resposta = self.client.get(reverse("estoque:home"), {"f": "zerado"})
        self.assertContains(resposta, f'id="produto-{zerado.pk}"')
        self.assertContains(resposta, "Zerado")

    def test_inativos_preservam_acao_reativar(self):
        p = self.produto(ativo=False)
        self.assertNotContains(self.client.get(reverse("estoque:home")), f'id="produto-{p.pk}"')
        resposta = self.client.get(reverse("estoque:home"), {"ativos": "0"})
        self.assertContains(resposta, f'id="produto-{p.pk}"')
        self.assertContains(resposta, "Reativar")
        self.assertNotContains(resposta, 'class="btn btn-outline-danger btn-sm btn-status-produto btn-desativar-produto"')

    def test_consultas_nao_crescem_por_produto(self):
        self.produto()
        self.client.get(reverse("estoque:home"))  # Inicialização existente dos controles de preços.
        with CaptureQueriesContext(connection) as poucas:
            self.client.get(reverse("estoque:home"))
        for i in range(25):
            self.produto(f"Produto adicional {i}")
        with CaptureQueriesContext(connection) as muitas:
            resposta = self.client.get(reverse("estoque:home"))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(len(muitas), len(poucas), [q["sql"] for q in muitas])
