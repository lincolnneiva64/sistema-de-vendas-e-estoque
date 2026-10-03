from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Compra, ContaPagar, ItemCompra, MovimentoFinanceiro, Produto


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class RevisaoPrecosPosteriorTests(TestCase):
    def setUp(self):
        self.produto = Produto.objects.create(
            nome="Produto de teste", preco_compra=Decimal("9.00"),
            preco_vista=Decimal("12.00"), preco_venda=Decimal("12.00"),
            preco_prazo=Decimal("14.00"), quantidade=Decimal("25.000"),
            vende_fracionado=True, fator_conversao=Decimal("2"),
            preco_compra_fracionado=Decimal("4.50"),
            preco_vista_fracionado=Decimal("6.00"),
            preco_prazo_fracionado=Decimal("7.00"),
        )
        self.compra = Compra.objects.create(
            data_compra=timezone.localdate(), status=Compra.STATUS_FINALIZADA,
            revisao_precos_pendente=True, estoque_entrada_realizada=True,
        )
        self.item = self.novo_item()
        self.url = reverse("estoque:revisao_precos_posterior_salvar")
        self.consulta = reverse("estoque:revisao_precos_posterior_pendentes")

    def novo_item(self, **extra):
        dados = dict(compra=self.compra, produto=self.produto, quantidade=1,
                     preco_unitario=9, preco_compra_anterior=8, valor_total=9)
        dados.update(extra)
        return ItemCompra.objects.create(**dados)

    def payload(self, **extra):
        dados = dict(compra_id=self.compra.pk, item_id=self.item.pk, produto_id=self.produto.pk)
        dados.update(extra)
        return dados

    def precos(self, campos, valores, ids=None):
        return {
            "atualizar_preco_venda_produto_ids[]": ids or [str(self.produto.pk)] * len(campos),
            "atualizar_preco_venda_nomes[]": campos,
            "atualizar_preco_venda_valores[]": valores,
        }

    def test_salva_apenas_selecionados_sem_efeitos_colaterais(self):
        produto_antes = Produto.objects.values().get(pk=self.produto.pk)
        compra_antes = Compra.objects.values().get(pk=self.compra.pk)
        financeiro_antes = (ContaPagar.objects.count(), MovimentoFinanceiro.objects.count())
        with patch("estoque.views._atualizar_custos_produtos_compra") as custos, patch("estoque.views._finalizar_compra_com_financeiro") as finalizar:
            resposta = self.client.post(self.url, self.payload(**self.precos(
                ["preco_vista", "preco_prazo_fracionado"], ["13,50", "7,90"])))
        self.assertEqual(resposta.status_code, 200)
        custos.assert_not_called()
        finalizar.assert_not_called()
        self.item.refresh_from_db()
        self.assertTrue(self.item.revisao_preco_concluida)
        produto_depois = Produto.objects.values().get(pk=self.produto.pk)
        for campo in produto_antes.keys() - {"preco_vista", "preco_venda", "preco_prazo_fracionado", "atualizado_em", "autoria_precos"}:
            self.assertEqual(produto_depois[campo], produto_antes[campo], campo)
        self.assertEqual(set(produto_depois["autoria_precos"]), {"preco_vista", "preco_venda", "preco_prazo_fracionado"})
        self.assertEqual(set(self.item.alteracoes_precos), {"preco_vista", "preco_venda", "preco_prazo_fracionado"})
        for campo, registro in self.item.alteracoes_precos.items():
            self.assertEqual(registro["alteracao_id"], produto_depois["autoria_precos"][campo])
        self.assertEqual(produto_depois["preco_vista"], Decimal("13.50"))
        self.assertEqual(produto_depois["preco_venda"], Decimal("13.50"))
        self.assertEqual(produto_depois["preco_prazo_fracionado"], Decimal("7.90"))
        compra_depois = Compra.objects.values().get(pk=self.compra.pk)
        for campo in compra_antes.keys() - {"revisao_precos_pendente"}:
            self.assertEqual(compra_depois[campo], compra_antes[campo], campo)
        self.assertFalse(compra_depois["revisao_precos_pendente"])
        self.assertEqual(financeiro_antes, (ContaPagar.objects.count(), MovimentoFinanceiro.objects.count()))
        self.assertEqual(self.client.get(self.consulta).json()["produtos"], 0)
        pagina = self.client.get(reverse("estoque:vendas"))
        self.assertEqual(pagina.context["revisao_precos_pendentes_qtd"], 0)
        self.assertNotContains(pagina, 'id="vendas-revisao-precos"')

    def test_manter_precos_persiste_por_item(self):
        outro = self.novo_item()
        self.novo_item(preco_compra_anterior=None)
        antes = Produto.objects.values().get(pk=self.produto.pk)
        resposta = self.client.post(self.url, self.payload())
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["produtos"], 1)
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)
        self.compra.refresh_from_db()
        self.assertTrue(self.compra.revisao_precos_pendente)
        dados = self.client.get(self.consulta).json()
        self.assertEqual([i["item_id"] for i in dados["itens"]], [outro.pk])
        self.assertEqual(dados["compras"], 1)
        self.assertEqual(self.client.post(self.url, self.payload(item_id=outro.pk)).status_code, 200)
        self.compra.refresh_from_db()
        self.assertFalse(self.compra.revisao_precos_pendente)

    def test_consulta_usa_custos_do_item_e_precos_atuais(self):
        dados = self.client.get(self.consulta).json()
        self.assertEqual((dados["produtos"], dados["compras"]), (1, 1))
        item = dados["itens"][0]
        self.assertEqual(item["custo_anterior"], "8.00")
        self.assertEqual(item["custo_novo"], "9.00")
        self.assertEqual(item["produto"]["preco_vista"], "12.00")
        self.assertEqual(Decimal(item["produto"]["preco_compra_fracionado"]), Decimal("4"))
        self.client.post(self.url, self.payload(**self.precos(["preco_vista"], ["13,50"])))
        self.novo_item()
        Compra.objects.filter(pk=self.compra.pk).update(revisao_precos_pendente=True)
        self.assertEqual(self.client.get(self.consulta).json()["itens"][0]["produto"]["preco_vista"], "13.50")

    def test_rejeita_produtos_campos_e_valores_invalidos_sem_concluir(self):
        invalido = [
            self.payload(produto_id=self.produto.pk + 1000),
            self.payload(**self.precos(["preco_compra"], ["20"])),
            self.payload(**self.precos(["quantidade"], ["20"])),
            self.payload(**self.precos(["preco_vista"], ["20"], [str(self.produto.pk + 1000)])),
            self.payload(**self.precos(["preco_vista"], ["-1"])),
            self.payload(**self.precos(["preco_vista"], ["NaN"])),
            self.payload(**self.precos(["preco_vista"], ["Infinity"])),
            self.payload(**self.precos(["preco_vista"], ["10000000000"])),
            self.payload(**self.precos(["preco_vista"], [""])),
            self.payload(**self.precos(["preco_vista", "preco_prazo"], ["20"])),
            self.payload(**{"atualizar_preco_venda_campos[]": [f"{self.produto.pk}:preco_compra"]}),
            self.payload(**{f"novo_preco_venda_produto_{self.produto.pk + 1000}_preco_vista": "20"}),
        ]
        antes = Produto.objects.values().get(pk=self.produto.pk)
        for dados in invalido:
            with self.subTest(dados=dados):
                self.assertEqual(self.client.post(self.url, dados).status_code, 400)
                self.item.refresh_from_db()
                self.assertFalse(self.item.revisao_preco_concluida)
                self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)

    def test_validacao_da_compra_e_do_item(self):
        outra = Compra.objects.create(data_compra=timezone.localdate(), status=Compra.STATUS_FINALIZADA, revisao_precos_pendente=True)
        self.assertEqual(self.client.post(self.url, self.payload(compra_id=outra.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.payload(compra_id=999999)).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.payload(item_id=999999)).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.payload(compra_id="abc")).status_code, 400)
        for status, pendente in [(Compra.STATUS_ABERTA, True), (Compra.STATUS_FINALIZADA, False)]:
            Compra.objects.filter(pk=self.compra.pk).update(status=status, revisao_precos_pendente=pendente)
            self.assertEqual(self.client.post(self.url, self.payload()).status_code, 409)
        Compra.objects.filter(pk=self.compra.pk).update(status=Compra.STATUS_FINALIZADA, revisao_precos_pendente=True)
        ItemCompra.objects.filter(pk=self.item.pk).update(preco_compra_anterior=None)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 409)
        ItemCompra.objects.filter(pk=self.item.pk).update(preco_compra_anterior=8, revisao_preco_concluida=True)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 409)

    def test_transacao_reverte_tudo_se_gravacao_falhar(self):
        antes = Produto.objects.values().get(pk=self.produto.pk)
        with patch.object(ItemCompra, "save", side_effect=ValueError("Falha simulada")):
            self.assertEqual(self.client.post(self.url, self.payload(**self.precos(["preco_vista"], ["20"]))).status_code, 400)
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk), antes)
        self.item.refresh_from_db()
        self.assertFalse(self.item.revisao_preco_concluida)

    def test_csrf_metodos_e_reenvio(self):
        from django.test import Client
        self.assertEqual(Client(enforce_csrf_checks=True).post(self.url, self.payload()).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.consulta).status_code, 405)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 200)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 409)

    def test_formato_alternativo_existente_e_produto_indisponivel(self):
        Produto.objects.filter(pk=self.produto.pk).update(excluido=True)
        self.assertFalse(self.client.get(self.consulta).json()["itens"][0]["disponivel"])
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 400)
        self.item.refresh_from_db()
        self.assertFalse(self.item.revisao_preco_concluida)
        Produto.objects.filter(pk=self.produto.pk).update(excluido=False)
        self.assertEqual(self.client.post(self.url, self.payload(**{
            "atualizar_preco_venda_campos[]": [f"{self.produto.pk}:preco_vista"],
            f"novo_preco_venda_produto_{self.produto.pk}_preco_vista": "13,50",
        })).status_code, 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_venda, Decimal("13.50"))


class SimuladorRevisaoPrecosCompatibilidadeTests(SimpleTestCase):
    def test_funcoes_isoladas_preservam_o_simulador_aprovado(self):
        from pathlib import Path
        import re

        templates = Path(__file__).parent / "templates" / "estoque"
        compra = (templates / "compras_nova.html").read_text(encoding="utf-8")
        compra = compra[compra.index("const revisaoPrecosPendenteCompra"):]
        posterior = (templates / "includes" / "revisao_precos_posterior_simulador.js.html").read_text(encoding="utf-8")
        nomes = re.findall(r"  function (\w+)\(", posterior)
        self.assertIn("montarPrecosProduto", nomes)
        self.assertIn("enriquecerPreco", nomes)
        self.assertIn("atualizarPrecoPelaMargem", nomes)
        for nome in nomes:
            with self.subTest(funcao=nome):
                inicio = compra.index("  function " + nome + "(")
                original = compra[inicio:compra.index("\n  }", inicio) + 4]
                inicio = posterior.index("  function " + nome + "(")
                adaptada = posterior[inicio:posterior.index("\n  }", inicio) + 4]
                self.assertEqual(original, adaptada.replace("rp-compras-preco-", "compras-preco-"))
