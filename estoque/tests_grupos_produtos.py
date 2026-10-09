from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model

from .grupos_produtos import criar_grupo, sugerir_nome
from .models import GrupoProdutoVinculado, MembroGrupoProduto, Produto
from .services.precos_vinculados import diagnosticar_grupo, regularizar_grupo
from .tests_precos_vinculados import criar_operador_precos


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class GruposProdutosTests(TestCase):
    def setUp(self):
        self.operador=criar_operador_precos('grupos-test')
        self.client.force_login(self.operador)
        self.produtos = [Produto.objects.create(
            nome=nome, preco_compra=Decimal('2'), preco_vista=Decimal('3'),
            preco_prazo=Decimal('4'), quantidade=Decimal('10'), unidade_compra='UN',
        ) for nome in ['Micos Cola 6/2,5L', 'Micos Guaraná 6/2,5L', 'Micos Laranja 6/2,5L']]
        self.url = reverse('estoque:grupos_produtos_criar')

    def criar(self):
        resposta = self.client.post(self.url, self.criacao_confirmada('Micos 6/2,5L'))
        self.assertEqual(resposta.status_code, 201)
        grupo = GrupoProdutoVinculado.objects.get(pk=resposta.json()['id'])
        return grupo, reverse('estoque:grupos_produtos_detalhe', args=[grupo.pk])

    def criacao_confirmada(self, nome):
        dados = {'acao': 'previa', 'produto_ids': [p.pk for p in self.produtos], 'referencia': self.produtos[0].pk}
        previa = self.client.post(self.url, dados).json()['precos_vinculados']
        return {**dados, 'acao': 'criar', 'nome': nome, 'confirmar': '1', 'versao_observada': previa['versao_observada']}

    def test_criar_grupo_com_varios_produtos_e_visualizar(self):
        grupo, url = self.criar()
        self.assertEqual(grupo.produtos.count(), 3)
        self.assertEqual(len(self.client.get(url).json()['produtos']), 3)

    def test_renomear(self):
        grupo, url = self.criar()
        resposta = self.client.post(url, {'acao': 'renomear', 'nome': 'Família Micos'})
        self.assertEqual(resposta.status_code, 200)
        grupo.refresh_from_db()
        self.assertEqual(grupo.nome, 'Família Micos')

    def test_remover_membro_e_manter_grupo_unitario(self):
        grupo, url = self.criar()
        for p in self.produtos[:2]:
            self.assertEqual(self.client.post(url, {'acao': 'remover', 'produto_id': p.pk}).status_code, 200)
        self.assertEqual(grupo.produtos.count(), 1)
        self.assertFalse(MembroGrupoProduto.objects.filter(produto=self.produtos[0]).exists())
        self.assertEqual(Produto.objects.count(), 3)

    def test_excluir_grupo_vazio(self):
        grupo, url = self.criar()
        for p in self.produtos:
            resposta = self.client.post(url, {'acao': 'remover', 'produto_id': p.pk})
            self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()['excluido'])
        self.assertFalse(GrupoProdutoVinculado.objects.filter(pk=grupo.pk).exists())

    def test_nao_permitir_dois_grupos_por_produto(self):
        grupo, _ = self.criar()
        with self.assertRaises(ValidationError):
            criar_grupo([p.pk for p in self.produtos[:2]], 'Outro grupo')
        outro = GrupoProdutoVinculado.objects.create(nome='Outro')
        with self.assertRaises(IntegrityError), transaction.atomic():
            MembroGrupoProduto.objects.create(grupo=outro, produto=self.produtos[0])
        self.produtos[0].refresh_from_db()
        self.assertEqual(self.produtos[0].vinculo_grupo.grupo_id, grupo.pk)

    def test_produto_sem_grupo_e_campos_comerciais_preservados(self):
        p = self.produtos[0]
        antes = Produto.objects.filter(pk=p.pk).values().get()
        self.assertFalse(p.grupos_vinculados.exists())
        p.refresh_from_db()
        self.assertEqual(p.preco_vista, Decimal('3'))
        grupo, url = self.criar()
        self.client.post(url, {'acao': 'renomear', 'nome': 'Novo nome'})
        self.client.post(url, {'acao': 'remover', 'produto_id': p.pk})
        self.assertEqual(Produto.objects.filter(pk=p.pk).values().get(), antes)

    def test_nome_sugerido_editavel_antes_de_salvar(self):
        ids = [p.pk for p in self.produtos]
        resposta = self.client.post(self.url, {'acao': 'sugerir', 'produto_ids': ids})
        self.assertEqual(resposta.json()['nome'].casefold(), 'micos 6/2,5l')
        self.assertFalse(GrupoProdutoVinculado.objects.exists())
        resposta = self.client.post(self.url, self.criacao_confirmada('Nome escolhido pelo usuário'))
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(GrupoProdutoVinculado.objects.get().nome, 'Nome escolhido pelo usuário')

    def test_heuristica_exemplos_e_ambiguidade(self):
        self.assertEqual(sugerir_nome(['Fanta Laranja 2L', 'Fanta Uva 2L']), 'Fanta 2L')
        self.assertEqual(sugerir_nome(['Rosca Chocolate Micos 30/300G', 'Rosca Coco Micos 30/300G', 'Rosca Leite Micos 30/300G']), 'Rosca Micos 30/300G')
        self.assertEqual(sugerir_nome(['ABC', 'XYZ']), 'Grupo de produtos')

    def test_validacao_sem_grupo_parcial(self):
        for dados in [
            {'nome': 'Teste', 'produto_ids': [self.produtos[0].pk]},
            {'nome': '', 'produto_ids': [p.pk for p in self.produtos]},
            {'nome': 'Teste', 'produto_ids': ['inválido']},
            {'nome': 'Teste', 'produto_ids': [self.produtos[0].pk, 999999]},
        ]:
            self.assertEqual(self.client.post(self.url, dados).status_code, 400)
        self.assertFalse(GrupoProdutoVinculado.objects.exists())

    def test_exclusao_definitiva_limpa_grupo_vazio(self):
        grupo, _ = self.criar()
        Produto.objects.filter(pk__in=[p.pk for p in self.produtos]).delete()
        self.assertFalse(GrupoProdutoVinculado.objects.filter(pk=grupo.pk).exists())

    def test_listagem_com_e_sem_vinculo(self):
        resposta = self.client.get(reverse('estoque:home'))
        self.assertContains(resposta, 'Vincular produtos')
        self.assertNotContains(resposta, 'Vinculado · Micos')
        self.criar()
        resposta = self.client.get(reverse('estoque:home'))
        self.assertContains(resposta, 'Vinculado · Micos 6/2,5L · 3 produtos', count=3)

    def test_listar_grupos_com_contagens_e_urls(self):
        grupo, url = self.criar()
        resposta = self.client.get(reverse('estoque:grupos_produtos_lista'))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()['grupos'], [{'id': grupo.pk, 'nome': grupo.nome, 'quantidade': 3, 'url': url}])
        self.assertEqual(self.client.get(url).json()['quantidade'], 3)

    def test_listagem_de_grupos_vazia(self):
        resposta = self.client.get(reverse('estoque:grupos_produtos_lista'))
        self.assertEqual(resposta.json()['grupos'], [])

    def test_adicionar_varios_membros_sem_alterar_produtos(self):
        grupo = criar_grupo([p.pk for p in self.produtos[:2]], 'Micos')
        novo = Produto.objects.create(nome='Micos Uva 6/2,5L', preco_compra=2, preco_vista=3, preco_prazo=4, unidade_compra='UN')
        ids = [self.produtos[2].pk, novo.pk]
        antes = list(Produto.objects.order_by('pk').values())
        url = reverse('estoque:grupos_produtos_detalhe', args=[grupo.pk])
        regularizar_grupo(grupo.pk, diagnosticar_grupo(list(grupo.produtos.all()))['versao_observada'], confirmar=True, operador=self.operador)
        previa = self.client.post(url, {'acao': 'previa_adicao', 'produto_ids': ids}).json()['precos_vinculados']
        resposta = self.client.post(url, {'acao': 'adicionar', 'produto_ids': ids, 'confirmar': '1', 'versao_observada': previa['versao_observada']})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()['grupo']['quantidade'], 4)
        self.assertEqual(resposta.json()['mensagem'], 'Produtos adicionados ao grupo.')
        self.assertEqual(list(Produto.objects.order_by('pk').values()), antes)

    def test_bloquear_outro_grupo_sem_adicao_parcial(self):
        grupo = criar_grupo([p.pk for p in self.produtos[:2]], 'Grupo original')
        outro = GrupoProdutoVinculado.objects.create(nome='Outro grupo')
        MembroGrupoProduto.objects.create(grupo=outro, produto=self.produtos[2])
        novo = Produto.objects.create(nome='Livre', preco_compra=2, preco_vista=3, preco_prazo=4)
        url = reverse('estoque:grupos_produtos_detalhe', args=[grupo.pk])
        resposta = self.client.post(url, {'acao': 'adicionar', 'produto_ids': [novo.pk, self.produtos[2].pk]})
        self.assertEqual(resposta.status_code, 400)
        self.assertIn('outro grupo', resposta.json()['erro'])
        self.assertEqual(grupo.produtos.count(), 2)
        self.assertFalse(MembroGrupoProduto.objects.filter(produto=novo).exists())
        self.assertEqual(MembroGrupoProduto.objects.get(produto=self.produtos[2]).grupo_id, outro.pk)

    def test_selecao_apenas_ativos_elegiveis(self):
        grupo = criar_grupo([p.pk for p in self.produtos[:2]], 'Grupo')
        outro = GrupoProdutoVinculado.objects.create(nome='Outro')
        membro_outro = Produto.objects.create(nome='Outro membro', preco_compra=2, preco_vista=3, preco_prazo=4)
        MembroGrupoProduto.objects.create(grupo=outro, produto=membro_outro)
        inativo = Produto.objects.create(nome='Inativo', ativo=False, preco_compra=2, preco_vista=3, preco_prazo=4)
        excluido = Produto.objects.create(nome='Excluído', excluido=True, preco_compra=2, preco_vista=3, preco_prazo=4)
        url = reverse('estoque:grupos_produtos_detalhe', args=[grupo.pk])
        resposta = self.client.get(url, {'elegiveis': '1'})
        self.assertEqual([p['id'] for p in resposta.json()['produtos']], [self.produtos[2].pk])
        for p in [inativo, excluido]:
            resposta = self.client.post(url, {'acao': 'adicionar', 'produto_ids': [p.pk]})
            self.assertEqual(resposta.status_code, 400)
        self.assertEqual(grupo.produtos.count(), 2)

    def test_adicao_repetida_nao_duplica_membros(self):
        grupo, url = self.criar()
        resposta = self.client.post(url, {'acao': 'adicionar', 'produto_ids': [self.produtos[0].pk] * 2, 'confirmar': '1'})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(grupo.produtos.count(), 3)

    def test_excluir_grupo_preserva_todos_os_campos_dos_produtos(self):
        grupo, url = self.criar()
        antes = list(Produto.objects.order_by('pk').values())
        resposta = self.client.post(url, {'acao': 'excluir', 'confirmar': '1'})
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()['excluido'])
        self.assertFalse(GrupoProdutoVinculado.objects.filter(pk=grupo.pk).exists())
        self.assertFalse(MembroGrupoProduto.objects.exists())
        self.assertEqual(list(Produto.objects.order_by('pk').values()), antes)

    def test_exclusao_exige_confirmacao(self):
        grupo, url = self.criar()
        resposta = self.client.post(url, {'acao': 'excluir'})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(grupo.produtos.count(), 3)

    def test_contagem_apos_remover_e_renomear_preserva_vinculos(self):
        grupo, url = self.criar()
        resposta = self.client.post(url, {'acao': 'remover', 'produto_id': self.produtos[0].pk})
        self.assertEqual(resposta.json()['grupo']['quantidade'], 2)
        self.assertEqual(resposta.json()['mensagem'], 'Produto removido do grupo.')
        ids = list(grupo.produtos.values_list('pk', flat=True))
        resposta = self.client.post(url, {'acao': 'renomear', 'nome': 'Nome novo'})
        self.assertEqual(resposta.json()['mensagem'], 'Grupo atualizado.')
        self.assertEqual(list(grupo.produtos.values_list('pk', flat=True)), ids)
        self.assertEqual(self.client.get(reverse('estoque:grupos_produtos_lista')).json()['grupos'][0]['quantidade'], 2)
        self.assertContains(self.client.get(reverse('estoque:home')), 'Vinculado · Nome novo · 2 produtos', count=2)

    def test_adicao_invalida_e_grupo_inexistente(self):
        grupo, url = self.criar()
        for ids in [[], ['abc'], [999999]]:
            resposta = self.client.post(url, {'acao': 'adicionar', 'produto_ids': ids})
            self.assertEqual(resposta.status_code, 400)
        self.assertEqual(grupo.produtos.count(), 3)
        self.assertEqual(self.client.get(reverse('estoque:grupos_produtos_detalhe', args=[999999])).status_code, 404)

    def test_csrf_obrigatorio(self):
        from django.test import Client
        resposta = Client(enforce_csrf_checks=True).post(self.url, {'nome': 'Teste', 'produto_ids': [p.pk for p in self.produtos]})
        self.assertEqual(resposta.status_code, 403)
