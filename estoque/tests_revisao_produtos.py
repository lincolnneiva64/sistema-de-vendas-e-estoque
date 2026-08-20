from datetime import datetime
from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Fornecedor, Produto, ProdutoFornecedor


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class ProdutoRevisaoTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("estoque:revisao_produtos")
        self.data_importacao = timezone.make_aware(datetime(2026, 7, 27, 10, 0))
        self.data_fora_importacao = timezone.make_aware(datetime(2026, 7, 28, 10, 0))

        self.produto1 = self._produto("Produto A", "001", "LEG001", "Bebidas")
        self.produto2 = self._produto("Produto B", "", "LEG002", "Alimentos")
        self.produto3 = self._produto(
            "Produto C Revisado",
            "003",
            "LEG003",
            "Bebidas",
            revisado_importacao=True,
            revisado_importacao_em=timezone.now(),
        )
        self.produto_novo = self._produto("Produto Novo", "999", None, "Bebidas")
        self.produto_fora_data = self._produto("Produto Fora Data", "997", "LEG997", "Bebidas")
        Produto.objects.filter(pk=self.produto_fora_data.pk).update(criado_em=self.data_fora_importacao)
        self.produto_excluido = self._produto("Produto Excluido", "998", "LEG998", "Bebidas", excluido=True)
        self.fornecedor1 = Fornecedor.objects.create(nome="Fornecedor Um")
        self.fornecedor2 = Fornecedor.objects.create(nome="Fornecedor Dois")

    def _produto(self, nome, codigo, codigo_legado, categoria, **extras):
        dados = {
            "nome": nome,
            "codigo": codigo,
            "codigo_legado": codigo_legado,
            "categoria": categoria,
            "preco_compra": Decimal("1.00"),
            "preco_venda": Decimal("3.00"),
            "preco_vista": Decimal("3.00"),
            "preco_prazo": Decimal("4.00"),
        }
        dados.update(extras)
        produto = Produto.objects.create(**dados)
        Produto.objects.filter(pk=produto.pk).update(criado_em=self.data_importacao)
        produto.refresh_from_db()
        return produto

    def test_view_revisao_existe(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "estoque/revisao_produtos.html")

    def test_filtro_pendentes(self):
        response = self.client.get(self.url, {"filtro": "pendentes"})
        self.assertContains(response, "Produto A")
        self.assertContains(response, "Produto B")
        self.assertNotContains(response, "Produto C Revisado")
        self.assertNotContains(response, "Produto Novo")

    def test_filtro_revisados(self):
        response = self.client.get(self.url, {"filtro": "revisados"})
        self.assertContains(response, "Produto C Revisado")
        self.assertNotContains(response, "Produto A")
        self.assertNotContains(response, "Produto B")

    def test_filtro_todos(self):
        response = self.client.get(self.url, {"filtro": "todos"})
        self.assertContains(response, "Produto A")
        self.assertContains(response, "Produto B")
        self.assertContains(response, "Produto C Revisado")
        self.assertNotContains(response, "Produto Novo")
        self.assertNotContains(response, "Produto Fora Data")

    def test_busca_por_nome(self):
        response = self.client.get(self.url, {"busca": "Produto A"})
        self.assertContains(response, "Produto A")
        self.assertNotContains(response, "Produto B")

    def test_editar_nome_codigo_categoria_e_marcar_revisado(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A Atualizado",
            f"codigo_{self.produto1.id}": "A-001",
            f"categoria_{self.produto1.id}": "Alimentos",
            f"revisado_{self.produto1.id}": "1",
        }

        response = self.client.post(self.url, data, follow=True)

        self.assertRedirects(response, f"{self.url}?filtro=pendentes")
        self.produto1.refresh_from_db()
        self.assertEqual(self.produto1.nome, "Produto A Atualizado")
        self.assertEqual(self.produto1.codigo, "A-001")
        self.assertEqual(self.produto1.categoria, "Alimentos")
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_marcar_revisado_preenche_data(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto2.id)],
            f"nome_{self.produto2.id}": "Produto B",
            f"codigo_{self.produto2.id}": "",
            f"categoria_{self.produto2.id}": "Alimentos",
            f"revisado_{self.produto2.id}": "1",
        }

        self.client.post(self.url, data, follow=True)

        self.produto2.refresh_from_db()
        self.assertTrue(self.produto2.revisado_importacao)
        self.assertIsNotNone(self.produto2.revisado_importacao_em)

    def test_desmarcar_revisao_limpa_data(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto3.id)],
            f"nome_{self.produto3.id}": "Produto C Revisado",
            f"codigo_{self.produto3.id}": "003",
            f"categoria_{self.produto3.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto3.refresh_from_db()
        self.assertFalse(self.produto3.revisado_importacao)
        self.assertIsNone(self.produto3.revisado_importacao_em)

    def test_impede_alteracao_de_produto_fora_do_conjunto(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto_novo.id)],
            f"nome_{self.produto_novo.id}": "Tentativa Indevida",
            f"codigo_{self.produto_novo.id}": "999",
            f"categoria_{self.produto_novo.id}": "Bebidas",
            f"revisado_{self.produto_novo.id}": "1",
        }

        self.client.post(self.url, data, follow=True)

        self.produto_novo.refresh_from_db()
        self.assertEqual(self.produto_novo.nome, "Produto Novo")
        self.assertFalse(self.produto_novo.revisado_importacao)

    def test_paginacao_50_itens(self):
        for i in range(56):
            self._produto(f"Produto Teste {i}", f"{1000 + i}", f"LEG_{i:04d}", "Testes")

        response = self.client.get(self.url)

        self.assertEqual(response.context["page"].paginator.per_page, 50)
        self.assertGreater(response.context["page"].paginator.num_pages, 1)

    def test_estatisticas_corretas(self):
        response = self.client.get(self.url)

        self.assertEqual(response.context["total_importados"], 3)
        self.assertEqual(response.context["total_pendentes"], 2)
        self.assertEqual(response.context["total_revisados"], 1)

    def test_alterar_nome_marca_revisado_automaticamente(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A Alterado",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_alterar_codigo_marca_revisado_automaticamente(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "",
            f"categoria_{self.produto1.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertEqual(self.produto1.codigo, "")
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_alterar_categoria_marca_revisado_automaticamente(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Alimentos",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertEqual(self.produto1.categoria, "Alimentos")
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_adicionar_fornecedor_marca_revisado_automaticamente(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
            f"fornecedores_{self.produto1.id}": [str(self.fornecedor1.id)],
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_remover_fornecedor_marca_revisado_automaticamente(self):
        ProdutoFornecedor.objects.create(produto=self.produto1, fornecedor=self.fornecedor1, ativo=True)
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_nao_alterar_nada_continua_pendente(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertFalse(self.produto1.revisado_importacao)
        self.assertIsNone(self.produto1.revisado_importacao_em)

    def test_marcar_checkbox_sem_alterar_marca_revisado(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
            f"revisado_{self.produto1.id}": "1",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_excluir_logicamente_produto_da_revisao(self):
        data = {
            "salvar": "1",
            "acao": f"excluir:{self.produto1.id}",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.excluido)
        self.assertIsNotNone(self.produto1.excluido_em)

    def test_produto_excluido_deixa_de_aparecer(self):
        Produto.objects.filter(pk=self.produto1.pk).update(excluido=True, excluido_em=timezone.now())

        response = self.client.get(self.url, {"filtro": "todos"})

        self.assertNotContains(response, "Produto A")

    def test_vincular_fornecedor(self):
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
            f"fornecedores_{self.produto1.id}": [str(self.fornecedor1.id), str(self.fornecedor2.id)],
        }

        self.client.post(self.url, data, follow=True)

        fornecedores = set(
            ProdutoFornecedor.objects.filter(produto=self.produto1, ativo=True)
            .values_list("fornecedor_id", flat=True)
        )
        self.assertEqual(fornecedores, {self.fornecedor1.id, self.fornecedor2.id})
        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)

    def test_remover_vinculo_fornecedor(self):
        ProdutoFornecedor.objects.create(produto=self.produto1, fornecedor=self.fornecedor1, ativo=True)
        ProdutoFornecedor.objects.create(produto=self.produto1, fornecedor=self.fornecedor2, ativo=True)
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
            f"fornecedores_{self.produto1.id}": [str(self.fornecedor1.id)],
        }

        self.client.post(self.url, data, follow=True)

        self.assertTrue(ProdutoFornecedor.objects.get(produto=self.produto1, fornecedor=self.fornecedor1).ativo)
        self.assertFalse(ProdutoFornecedor.objects.get(produto=self.produto1, fornecedor=self.fornecedor2).ativo)
        self.produto1.refresh_from_db()
        self.assertTrue(self.produto1.revisado_importacao)

    def test_nao_duplica_produto_fornecedor(self):
        ProdutoFornecedor.objects.create(produto=self.produto1, fornecedor=self.fornecedor1, ativo=False)
        data = {
            "salvar": "1",
            "produto_id": [str(self.produto1.id)],
            f"nome_{self.produto1.id}": "Produto A",
            f"codigo_{self.produto1.id}": "001",
            f"categoria_{self.produto1.id}": "Bebidas",
            f"fornecedores_{self.produto1.id}": [str(self.fornecedor1.id)],
        }

        self.client.post(self.url, data, follow=True)

        self.assertEqual(
            ProdutoFornecedor.objects.filter(produto=self.produto1, fornecedor=self.fornecedor1).count(),
            1,
        )
        self.assertTrue(ProdutoFornecedor.objects.get(produto=self.produto1, fornecedor=self.fornecedor1).ativo)

    def test_impede_exclusao_de_produto_fora_do_conjunto(self):
        data = {
            "salvar": "1",
            "acao": f"excluir:{self.produto_novo.id}",
            "produto_id": [str(self.produto_novo.id)],
            f"nome_{self.produto_novo.id}": "Produto Novo",
            f"codigo_{self.produto_novo.id}": "999",
            f"categoria_{self.produto_novo.id}": "Bebidas",
        }

        self.client.post(self.url, data, follow=True)

        self.produto_novo.refresh_from_db()
        self.assertFalse(self.produto_novo.excluido)
