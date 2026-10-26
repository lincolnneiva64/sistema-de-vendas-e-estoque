"""Apresentação de fornecedores: leitura, procedência e consultas constantes."""
from datetime import date

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from estoque.models import Compra, Fornecedor, ItemCompra, Produto, ProdutoFornecedor
from estoque.services.fornecedores_cards import preparar_fornecedores_cards


class FornecedoresCardsTests(TestCase):
    def produto(self, **campos):
        dados = dict(nome="Produto fictício", fornecedor=None, unidade_compra="KG",
                     quantidade=20, estoque_minimo=3, preco_compra=10, preco_vista=12, preco_prazo=14)
        return Produto.objects.create(**{**dados, **campos})

    def fornecedor(self, nome="Distribuidora fictícia", **campos):
        return Fornecedor.objects.create(nome=nome, **campos)

    def compra(self, produto, fornecedor, **campos):
        dados = dict(fornecedor=fornecedor, data_compra=date(2026, 1, 1),
                     status=Compra.STATUS_FINALIZADA, total=10)
        compra = Compra.objects.create(**{**dados, **campos})
        ItemCompra.objects.create(compra=compra, produto=produto, quantidade=1,
                                  unidade="KG", preco_unitario=10, valor_total=10)
        return compra

    def resolver(self, produto):
        return preparar_fornecedores_cards([produto])[0].fornecedores_card

    def test_ausencia_e_texto_legado_sem_converter_em_vinculo(self):
        p = self.produto()
        self.assertEqual(self.resolver(p)["resumo"], "Fornecedor não informado")
        p.fornecedor = "Distribuidor antigo"
        dados = self.resolver(p)
        self.assertIn("Distribuidor antigo", dados["resumo"])
        self.assertIn("Cadastro", dados["resumo"])
        self.assertEqual(dados["itens"], [])
        self.assertFalse(ProdutoFornecedor.objects.exists())

    def test_um_vinculo_ativo_e_varios_com_prioridade_e_sem_duplicidade(self):
        p = self.produto()
        ativo = self.fornecedor("Z fornecedor ativo")
        ProdutoFornecedor.objects.create(produto=p, fornecedor=ativo)
        self.assertEqual(self.resolver(p)["resumo"], f"Fornecedor: {ativo.nome}")
        self.compra(p, ativo)
        inativo = self.fornecedor("A vínculo desativado")
        ProdutoFornecedor.objects.create(produto=p, fornecedor=inativo, ativo=False)
        self.compra(p, inativo)
        historico = self.fornecedor("B histórico")
        self.compra(p, historico)
        dados = self.resolver(p)
        self.assertEqual(dados["resumo"], "3 fornecedores")
        self.assertEqual([i["id"] for i in dados["itens"]], [ativo.pk, inativo.pk, historico.pk])
        self.assertEqual([i["origem"] for i in dados["itens"]], ["Vinculado", "Vínculo inativo", "Histórico"])

    def test_varios_vinculos_ativos_e_legado_nao_aumenta_contagem(self):
        p = self.produto(fornecedor="Texto antigo sem identidade segura")
        for nome in ("Fornecedor B", "Fornecedor A"):
            ProdutoFornecedor.objects.create(produto=p, fornecedor=self.fornecedor(nome))
        dados = self.resolver(p)
        self.assertEqual(dados["resumo"], "2 fornecedores")
        self.assertEqual(dados["legado"], p.fornecedor)
        self.assertEqual([i["nome"] for i in dados["itens"]], ["Fornecedor A", "Fornecedor B"])
        p.fornecedor = " fornecedor a "
        self.assertEqual(self.resolver(p)["legado"], "")

    def test_historico_completo_e_exclusao_de_compras_nao_elegiveis(self):
        p = self.produto()
        for i in range(5):
            self.compra(p, self.fornecedor(f"Histórico {i}"))
        repetido = self.fornecedor("Compras repetidas")
        for _ in range(4):
            self.compra(p, repetido)
        # Compras abertas são mantidas, como no histórico existente, por compatibilidade.
        self.compra(p, self.fornecedor("Aberta legada"), status=Compra.STATUS_ABERTA)
        for status in (Compra.STATUS_CANCELADA, Compra.STATUS_RASCUNHO, Compra.STATUS_FINALIZACAO_INICIADA):
            self.compra(p, self.fornecedor(status), status=status)
        self.compra(p, self.fornecedor("Cancelada por indicador"), cancelada=True)
        self.compra(p, None)
        self.assertEqual(len(self.resolver(p)["itens"]), 7)
        self.assertTrue(all(i["origem"] == "Histórico" for i in self.resolver(p)["itens"]))

    def test_origens_unicas_e_cadastro_inativo_nao_sao_apresentados_como_ativos(self):
        p = self.produto()
        f = self.fornecedor(ativo=False)
        self.compra(p, f)
        self.assertIn("Histórico", self.resolver(p)["resumo"])
        self.assertIn("Cadastro inativo", self.resolver(p)["resumo"])
        ProdutoFornecedor.objects.create(produto=p, fornecedor=f, ativo=False)
        self.assertIn("Vínculo inativo", self.resolver(p)["resumo"])
        self.assertNotIn("Histórico", self.resolver(p)["resumo"])

    def test_dois_ids_com_mesmo_nome_nao_sao_fundidos(self):
        p = self.produto()
        for _ in range(2):
            ProdutoFornecedor.objects.create(produto=p, fornecedor=self.fornecedor("Mesmo nome"))
        self.assertEqual(self.resolver(p)["resumo"], "2 fornecedores")

    def test_consulta_real_escape_filtros_nomes_longos_e_leitura_sem_escrita(self):
        p = self.produto(nome="Produto com nome longo " * 4, fornecedor="Texto legado <b>antigo</b>")
        f = self.fornecedor("Distribuidora <script>teste</script> " + "nome longo " * 5)
        ProdutoFornecedor.objects.create(produto=p, fornecedor=f)
        self.compra(p, f)
        modelos = (Produto, Fornecedor, ProdutoFornecedor, Compra, ItemCompra)
        antes = [list(m.objects.order_by("pk").values()) for m in modelos]
        self.client.get(reverse("estoque:home"))  # Inicialização de controles existentes.
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(reverse("estoque:home"), {"q": p.nome[:15]})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "&lt;script&gt;teste&lt;/script&gt;")
        self.assertContains(resposta, escape(p.fornecedor))
        self.assertContains(resposta, '<details class="produto-fornecedores produto-fornecedor">')
        self.assertContains(resposta, "KG")
        self.assertContains(resposta, reverse("estoque:produto_editar", args=[p.pk]))
        self.assertEqual(antes, [list(m.objects.order_by("pk").values()) for m in modelos])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for q in consultas))
        self.assertNotContains(self.client.get(reverse("estoque:home"), {"q": "inexistente"}), f'id="produto-{p.pk}"')

    def test_duas_consultas_para_um_ou_muitos_produtos(self):
        produtos = [self.produto()]
        f = self.fornecedor()
        ProdutoFornecedor.objects.create(produto=produtos[0], fornecedor=f)
        self.compra(produtos[0], f)
        with self.assertNumQueries(2):
            preparar_fornecedores_cards(produtos)
        self.client.get(reverse("estoque:home"))
        with CaptureQueriesContext(connection) as poucas:
            self.client.get(reverse("estoque:home"))
        for i in range(25):
            p = self.produto(nome=f"Produto {i}")
            ProdutoFornecedor.objects.create(produto=p, fornecedor=f)
            self.compra(p, f)
            produtos.append(p)
        with self.assertNumQueries(2):
            preparar_fornecedores_cards(produtos)
        with CaptureQueriesContext(connection) as muitas:
            self.client.get(reverse("estoque:home"))
        self.assertEqual(len(poucas), len(muitas))
        with self.assertNumQueries(0):
            self.assertEqual(preparar_fornecedores_cards([]), [])
