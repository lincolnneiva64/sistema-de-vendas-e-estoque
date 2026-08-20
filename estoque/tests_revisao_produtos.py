"""
Testes para a funcionalidade de revisão de produtos importados
"""
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from datetime import datetime
from estoque.models import Produto


class ProdutoRevisaoTestCase(TestCase):
    """Testes para a view de revisão de produtos"""

    def setUp(self):
        """Preparar dados de teste"""
        self.client = Client()
        
        # Criar produtos com codigo_legado (importados)
        self.produto1 = Produto.objects.create(
            nome="Produto A",
            codigo="001",
            codigo_legado="LEG001",
            categoria="Bebidas",
            preco_venda=10.00,
            preco_vista=10.00,
            preco_prazo=11.00,
        )
        
        self.produto2 = Produto.objects.create(
            nome="Produto B",
            codigo="",
            codigo_legado="LEG002",
            categoria="Alimentos",
            preco_venda=5.00,
            preco_vista=5.00,
            preco_prazo=5.50,
        )
        
        self.produto3 = Produto.objects.create(
            nome="Produto C (Revisado)",
            codigo="003",
            codigo_legado="LEG003",
            categoria="Bebidas",
            preco_venda=15.00,
            preco_vista=15.00,
            preco_prazo=16.00,
            revisado_importacao=True,
            revisado_importacao_em=timezone.now(),
        )
        
        # Produto sem codigo_legado (novo, não entra na revisão)
        self.produto_novo = Produto.objects.create(
            nome="Produto Novo",
            codigo="999",
            codigo_legado=None,
            categoria="Bebidas",
            preco_venda=20.00,
            preco_vista=20.00,
            preco_prazo=21.00,
        )
        
        # Produto excluído (não entra na revisão)
        self.produto_excluido = Produto.objects.create(
            nome="Produto Excluído",
            codigo="998",
            codigo_legado="LEG_EXC",
            categoria="Bebidas",
            preco_venda=8.00,
            preco_vista=8.00,
            preco_prazo=8.50,
            excluido=True,
        )

    def test_view_revisao_existe(self):
        """Teste: A view de revisão está acessível"""
        response = self.client.get(reverse('estoque:revisao_produtos'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'estoque/revisao_produtos.html')

    def test_filtro_pendentes(self):
        """Teste: Filtro de pendentes mostra apenas produtos não revisados"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'filtro': 'pendentes'})
        
        # Produtos pendentes: produto1, produto2
        self.assertContains(response, 'Produto A')
        self.assertContains(response, 'Produto B')
        
        # Produtos revisados não devem aparecer
        self.assertNotContains(response, 'Produto C (Revisado)')
        
        # Produtos novos não devem aparecer
        self.assertNotContains(response, 'Produto Novo')

    def test_filtro_revisados(self):
        """Teste: Filtro de revisados mostra apenas produtos revisados"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'filtro': 'revisados'})
        
        # Produtos revisados: produto3
        self.assertContains(response, 'Produto C (Revisado)')
        
        # Produtos pendentes não devem aparecer
        self.assertNotContains(response, 'Produto A')
        self.assertNotContains(response, 'Produto B')

    def test_filtro_todos(self):
        """Teste: Filtro 'todos' mostra todos os produtos importados"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'filtro': 'todos'})
        
        # Todos os produtos com codigo_legado devem aparecer
        self.assertContains(response, 'Produto A')
        self.assertContains(response, 'Produto B')
        self.assertContains(response, 'Produto C (Revisado)')
        
        # Produtos novos não devem aparecer
        self.assertNotContains(response, 'Produto Novo')

    def test_busca_por_nome(self):
        """Teste: Busca por nome filtra corretamente"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'busca': 'Produto A'})
        
        # Apenas Produto A deve aparecer
        self.assertContains(response, 'Produto A')
        self.assertNotContains(response, 'Produto B')

    def test_busca_por_codigo_legado(self):
        """Teste: Busca por código legado funciona"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'busca': 'LEG001'})
        
        # Apenas Produto A (que tem LEG001) deve aparecer
        self.assertContains(response, 'Produto A')
        self.assertNotContains(response, 'Produto B')

    def test_editar_nome_e_marcar_revisado(self):
        """Teste: Editar nome e marcar como revisado"""
        data = {
            'salvar': '1',
            'produto_id': [str(self.produto1.id)],
            f'nome_{self.produto1.id}': 'Produto A Atualizado',
            f'codigo_{self.produto1.id}': '001',
            f'categoria_{self.produto1.id}': 'Bebidas',
            f'revisar_{self.produto1.id}': '1',
        }
        
        response = self.client.post(reverse('estoque:revisao_produtos'), data, follow=True)
        
        # Verificar que o produto foi atualizado
        self.produto1.refresh_from_db()
        self.assertEqual(self.produto1.nome, 'Produto A Atualizado')
        self.assertTrue(self.produto1.revisado_importacao)
        self.assertIsNotNone(self.produto1.revisado_importacao_em)

    def test_marcar_revisado_sem_alteracoes(self):
        """Teste: Marcar como revisado sem fazer alterações"""
        data = {
            'salvar': '1',
            'produto_id': [str(self.produto2.id)],
            f'nome_{self.produto2.id}': 'Produto B',
            f'codigo_{self.produto2.id}': '',
            f'categoria_{self.produto2.id}': 'Alimentos',
            f'revisar_{self.produto2.id}': '1',
        }
        
        response = self.client.post(reverse('estoque:revisao_produtos'), data, follow=True)
        
        # Verificar que o produto foi marcado como revisado
        self.produto2.refresh_from_db()
        self.assertEqual(self.produto2.nome, 'Produto B')  # Sem alterações
        self.assertTrue(self.produto2.revisado_importacao)
        self.assertIsNotNone(self.produto2.revisado_importacao_em)

    def test_desmarcar_revisado(self):
        """Teste: Desmarcar um produto já revisado"""
        # Confirmar que produto3 está revisado
        self.assertTrue(self.produto3.revisado_importacao)
        
        data = {
            'salvar': '1',
            'produto_id': [str(self.produto3.id)],
            f'nome_{self.produto3.id}': 'Produto C (Revisado)',
            f'codigo_{self.produto3.id}': '003',
            f'categoria_{self.produto3.id}': 'Bebidas',
            # Não incluir revisar_{produto3.id} = sem check
        }
        
        response = self.client.post(reverse('estoque:revisao_produtos'), data, follow=True)
        
        # Verificar que foi desmarcado
        self.produto3.refresh_from_db()
        self.assertFalse(self.produto3.revisado_importacao)
        self.assertIsNone(self.produto3.revisado_importacao_em)

    def test_editar_categoria(self):
        """Teste: Editar categoria de um produto"""
        data = {
            'salvar': '1',
            'produto_id': [str(self.produto1.id)],
            f'nome_{self.produto1.id}': 'Produto A',
            f'codigo_{self.produto1.id}': '001',
            f'categoria_{self.produto1.id}': 'Alimentos',
            f'revisar_{self.produto1.id}': '1',
        }
        
        response = self.client.post(reverse('estoque:revisao_produtos'), data, follow=True)
        
        # Verificar que a categoria foi alterada
        self.produto1.refresh_from_db()
        self.assertEqual(self.produto1.categoria, 'Alimentos')

    def test_nao_editar_produto_novo(self):
        """Teste: Produtos novos (sem codigo_legado) não aparecem na revisão"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'filtro': 'todos'})
        
        # Produto novo não deve aparecer
        self.assertNotContains(response, 'Produto Novo')

    def test_nao_editar_produto_excluido(self):
        """Teste: Produtos excluídos não aparecem na revisão"""
        response = self.client.get(reverse('estoque:revisao_produtos'), {'filtro': 'todos'})
        
        # Produto excluído não deve aparecer
        self.assertNotContains(response, 'Produto Excluído')

    def test_seguranca_id_nao_permitido(self):
        """Teste: Não permitir edição de produto fora do conjunto autorizado"""
        # Tentar editar produto novo (não tem codigo_legado)
        data = {
            'salvar': '1',
            'produto_id': [str(self.produto_novo.id)],
            f'nome_{self.produto_novo.id}': 'Tentativa de Hack',
            f'codigo_{self.produto_novo.id}': '999',
            f'categoria_{self.produto_novo.id}': 'Bebidas',
        }
        
        response = self.client.post(reverse('estoque:revisao_produtos'), data, follow=True)
        
        # Verificar que o produto NÃO foi alterado
        self.produto_novo.refresh_from_db()
        self.assertEqual(self.produto_novo.nome, 'Produto Novo')

    def test_paginacao_50_items(self):
        """Teste: Paginação padrão de 50 itens por página"""
        # Criar 60 produtos importados
        for i in range(56):  # Já existem 3 (1, 2, 3)
            Produto.objects.create(
                nome=f"Produto Teste {i}",
                codigo=f"{1000+i}",
                codigo_legado=f"LEG_{i:04d}",
                categoria="Testes",
                preco_venda=1.00,
                preco_vista=1.00,
                preco_prazo=1.10,
            )
        
        response = self.client.get(reverse('estoque:revisao_produtos'))
        
        # Verificar que há paginação
        self.assertIn('page', response.context)
        self.assertTrue(response.context['page'].paginator.num_pages > 1)

    def test_estatisticas_corretas(self):
        """Teste: Estatísticas mostram números corretos"""
        response = self.client.get(reverse('estoque:revisao_produtos'))
        
        # Deve haver 3 produtos importados (com codigo_legado)
        self.assertEqual(response.context['total_importados'], 3)
        # Deve haver 2 pendentes
        self.assertEqual(response.context['total_pendentes'], 2)
        # Deve haver 1 revisado
        self.assertEqual(response.context['total_revisados'], 1)
