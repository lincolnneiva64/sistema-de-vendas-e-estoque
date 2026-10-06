import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from offline.browser_support import Chrome
from . import views
from .models import (
    CartaoCredito, Compra, ContaFinanceira, ContaPagar, FaturaCartao, Fornecedor, ItemCompra, LancamentoCartao,
    ListaCompraFornecedor, MovimentoFinanceiro, MovimentacaoEstoqueManual,
    PagamentoContaPagar, ParcelaNotaListaCompraFornecedor, Produto,
)


class CorrecaoCompraSemMovimentoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="auditor-compras")
        self.client.force_login(self.user)
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor da nota")
        self.produto = Produto.objects.create(
            nome="Produto da nota", quantidade=Decimal("20.000"),
            preco_compra=Decimal("100.00"), preco_vista=Decimal("110.00"),
            preco_prazo=Decimal("120.00"), unidade_compra="UN",
        )
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor, data_compra=date(2026, 9, 16),
            data_vencimento=date(2026, 9, 22), tipo_pagamento="aprazo",
            total_produtos=Decimal("1044.10"), ajuste_total=Decimal("-118.00"),
            total=Decimal("926.10"), status=Compra.STATUS_FINALIZADA,
            estoque_entrada_realizada=True,
        )
        self.item = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto, quantidade=Decimal("2.000"),
            preco_unitario=Decimal("522.05"), valor_total=Decimal("1044.10"), unidade="UN",
        )
        self.conta = ContaPagar.objects.create(
            compra=self.compra, fornecedor=self.fornecedor, data_emissao=self.compra.data_compra,
            data_vencimento=self.compra.data_vencimento,
            valor_original=Decimal("926.10"), valor_em_aberto=Decimal("926.10"),
            status=ContaPagar.STATUS_ABERTA,
        )
        self.url = reverse("estoque:compra_corrigir_itens", kwargs={"pk": self.compra.pk})

    def dados(self, **alteracoes):
        dados = {
            "confirmar": "1", "item_id[]": [str(self.item.pk)],
            "quantidade[]": ["2"], "preco_unitario[]": ["522,05"],
            "modo_total_correcao": "ajuste", "ajuste_total_correcao": "-118,00",
            "conta_pagar_id_1": str(self.conta.pk), "parcela_valor_1": "926,10",
            "parcela_vencimento_1": "2026-09-22", "motivo_correcao": "Conferencia da nota",
        }
        dados.update(alteracoes)
        return dados

    def assert_totais(self, produtos, ajuste, total):
        self.compra.refresh_from_db(); self.conta.refresh_from_db()
        self.assertEqual(self.compra.total_produtos, Decimal(produtos))
        self.assertEqual(self.compra.ajuste_total, Decimal(ajuste))
        self.assertEqual(self.compra.total, Decimal(total))
        self.assertEqual(self.conta.valor_original, Decimal(total))
        self.assertEqual(self.conta.valor_em_aberto, Decimal(total))

    def assert_sem_recriacao(self):
        self.produto.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("20.000"))
        self.assertTrue(self.compra.estoque_entrada_realizada)
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 1)
        self.assertEqual(self.compra.itens.count(), 1)
        self.assertEqual(PagamentoContaPagar.objects.count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)

    def test_compra_direta_sem_pagamento_tem_valores_editaveis(self):
        resposta = self.client.get(self.url, secure=True)
        self.assertTrue(resposta.context["valores_editaveis"])
        self.assertTrue(resposta.context["correcao_parcelas"]["linhas"][0]["valor_editavel"])

    def test_correcao_explicita_ajuste_sincroniza_sem_recriar_financeiro_ou_estoque(self):
        dados = self.dados(ajuste_total_correcao="0,00", parcela_valor_1="1044,10")
        for _ in range(2):
            resposta = self.client.post(self.url, dados, secure=True)
            self.assertRedirects(resposta, reverse("estoque:compras_detalhe", kwargs={"pk": self.compra.pk}), fetch_redirect_response=False)
        self.assert_totais("1044.10", "0.00", "1044.10")
        self.assertEqual(self.conta.data_vencimento, date(2026, 9, 22))
        self.assertEqual(self.compra.data_vencimento, date(2026, 9, 22))
        self.assertIn("auditor-compras", self.compra.observacao)
        self.assertIn("Conferencia da nota", self.compra.observacao)
        self.assertIn("Ajuste anterior R$ -118,00", self.compra.observacao)
        self.assertIn("ajuste novo R$ 0,00", self.compra.observacao)
        self.assertIn("Total anterior R$ 926,10", self.compra.observacao)
        self.assertIn("novo total R$ 1044,10", self.compra.observacao)
        self.assert_sem_recriacao()

    def test_ajuste_legitimo_preservado_quando_preco_muda(self):
        self.client.post(self.url, self.dados(**{
            "preco_unitario[]": ["600,00"], "parcela_valor_1": "1082,00",
        }), secure=True)
        self.assert_totais("1200.00", "-118.00", "1082.00")
        self.assert_sem_recriacao()

    def test_post_antigo_sem_campo_de_ajuste_preserva_desconto(self):
        dados = self.dados(**{"preco_unitario[]": ["600,00"], "parcela_valor_1": "1082,00"})
        del dados["ajuste_total_correcao"]
        del dados["modo_total_correcao"]
        self.client.post(self.url, dados, secure=True)
        self.assert_totais("1200.00", "-118.00", "1082.00")

    def test_valor_cobrado_explicito_recalcula_ajuste_e_sincroniza_vencimento(self):
        self.client.post(self.url, self.dados(
            modo_total_correcao="valor_cobrado", valor_cobrado_correcao="1044,10",
            parcela_valor_1="1044,10", parcela_vencimento_1="2026-10-01",
        ), secure=True)
        self.assert_totais("1044.10", "0.00", "1044.10")
        self.assertEqual(self.conta.data_vencimento, date(2026, 10, 1))
        self.assertEqual(self.compra.data_vencimento, date(2026, 10, 1))

    def test_pagamento_inclusive_cancelado_bloqueia_get_e_post(self):
        for cancelado in (False, True):
            with self.subTest(cancelado=cancelado):
                pagamento = PagamentoContaPagar.objects.create(
                    conta=self.conta, valor=Decimal("10.00"), data_pagamento=date(2026, 9, 20), cancelado=cancelado,
                )
                resposta = self.client.get(self.url, secure=True)
                self.assertFalse(resposta.context["valores_editaveis"])
                self.assertFalse(resposta.context["correcao_parcelas"]["linhas"][0]["valor_editavel"])
                self.client.post(self.url, self.dados(ajuste_total_correcao="0", parcela_valor_1="1044,10"), secure=True)
                self.assert_totais("1044.10", "-118.00", "926.10")
                self.assertEqual(PagamentoContaPagar.objects.get(pk=pagamento.pk).cancelado, cancelado)
                pagamento.delete()

    def test_saldo_ou_status_movimentado_bloqueia(self):
        for status, saldo in ((ContaPagar.STATUS_PARCIAL, "926.10"), (ContaPagar.STATUS_ABERTA, "900.00"), (ContaPagar.STATUS_CANCELADA, "926.10")):
            with self.subTest(status=status, saldo=saldo):
                self.conta.status = status; self.conta.valor_em_aberto = Decimal(saldo)
                self.conta.save()
                self.assertFalse(views._compra_valores_editaveis(self.compra)[0])
                self.client.post(self.url, self.dados(ajuste_total_correcao="0", parcela_valor_1="1044,10"), secure=True)
                self.compra.refresh_from_db(); self.conta.refresh_from_db()
                self.assertEqual(self.compra.total, Decimal("926.10"))
                self.assertEqual(self.conta.valor_em_aberto, Decimal(saldo))

    def test_somente_vencimento_preserva_pagamento_e_movimento_existentes(self):
        pagamento = PagamentoContaPagar.objects.create(
            conta=self.conta, valor=Decimal("10.00"), data_pagamento=date(2026, 9, 20),
        )
        self.conta.status = ContaPagar.STATUS_PARCIAL
        self.conta.valor_em_aberto = Decimal("916.10")
        self.conta.save()
        movimento = MovimentoFinanceiro.objects.create(
            conta=ContaFinanceira.objects.create(nome="Banco auditoria", tipo="banco"),
            pagamento_conta_pagar=pagamento, tipo="saida", valor=Decimal("10.00"),
            data=pagamento.data_pagamento, origem="pagamento_conta_pagar",
        )
        self.client.post(self.url, self.dados(parcela_vencimento_1="2026-10-01"), secure=True)
        self.conta.refresh_from_db(); pagamento.refresh_from_db(); movimento.refresh_from_db()
        self.assertEqual(self.conta.data_vencimento, date(2026, 10, 1))
        self.assertEqual(self.conta.valor_original, Decimal("926.10"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("916.10"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PARCIAL)
        self.assertEqual(pagamento.valor, Decimal("10.00"))
        self.assertEqual(movimento.valor, Decimal("10.00"))
        self.assertEqual(MovimentoFinanceiro.objects.count(), 1)
        self.assertEqual(PagamentoContaPagar.objects.count(), 1)

    def test_fatura_cartao_continua_protegida(self):
        cartao = CartaoCredito.objects.create(nome="Cartao auditoria", titular="Comprador")
        fatura = FaturaCartao.objects.create(cartao=cartao, data_vencimento=date(2026, 10, 1))
        lancamento = LancamentoCartao.objects.create(
            cartao=cartao, fatura=fatura, compra=self.compra,
            data=self.compra.data_compra, descricao="Nota", valor=self.compra.total,
        )
        self.assertFalse(views._compra_valores_editaveis(self.compra)[0])
        self.client.post(self.url, self.dados(ajuste_total_correcao="0", parcela_valor_1="1044,10"), secure=True)
        self.assert_totais("1044.10", "-118.00", "926.10")
        lancamento.refresh_from_db()
        self.assertEqual(lancamento.valor, Decimal("926.10"))

    def test_movimentos_diretos_e_legados_bloqueiam_valores(self):
        caixa = ContaFinanceira.objects.create(nome="Caixa auditoria", tipo="caixa")
        for fk, origem, descricao in (
            (self.compra, "ajuste", "Ajuste da nota"),
            (None, "compra_a_vista", f"Pagamento da Compra #{self.compra.pk}"),
            (None, "compra_correcao_origem", f"Compra {self.compra.pk}"),
            (None, "compra_a_vista", f"Pagamento fornecedor {self.fornecedor.nome}"),
        ):
            with self.subTest(origem=origem, descricao=descricao):
                movimento = MovimentoFinanceiro.objects.create(
                    conta=caixa, compra=fk, tipo="saida", valor=Decimal("926.10"),
                    data=self.compra.data_compra, origem=origem, descricao=descricao,
                )
                self.assertFalse(views._compra_valores_editaveis(self.compra)[0])
                self.client.post(self.url, self.dados(ajuste_total_correcao="0", parcela_valor_1="1044,10"), secure=True)
                self.assert_totais("1044.10", "-118.00", "926.10")
                self.assertEqual(MovimentoFinanceiro.objects.get(pk=movimento.pk).valor, Decimal("926.10"))
                movimento.delete()

    def test_inconsistencia_de_parcela_desfaz_toda_correcao(self):
        self.client.post(self.url, self.dados(**{
            "quantidade[]": ["3"], "ajuste_total_correcao": "0",
        }), secure=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantidade, Decimal("2.000"))
        self.assert_totais("1044.10", "-118.00", "926.10")
        self.assertIsNone(self.compra.observacao)
        self.assert_sem_recriacao()

    def test_quantidade_corrige_apenas_delta_sem_repetir_entrada(self):
        dados = self.dados(**{"quantidade[]": ["3"], "parcela_valor_1": "1448,15"})
        for _ in range(2):
            self.client.post(self.url, dados, secure=True)
        self.assert_totais("1566.15", "-118.00", "1448.15")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("21.000"))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_lista_boleto_sincroniza_ajuste_explicitamente(self):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor, data_lista=self.compra.data_compra,
            data_inicio_periodo=self.compra.data_compra, data_fim_periodo=self.compra.data_compra,
            forma_cobranca_nota=ListaCompraFornecedor.FORMA_COBRANCA_BOLETO_UNICO,
            valor_nota_boleto=Decimal("926.10"),
        )
        parcela = ParcelaNotaListaCompraFornecedor.objects.create(
            lista=lista, numero=1, valor=Decimal("926.10"), data_vencimento=self.compra.data_vencimento,
        )
        self.compra.lista_fornecedor = lista; self.compra.save()
        self.conta.numero_parcela = 1; self.conta.total_parcelas = 1; self.conta.save()
        self.client.post(self.url, self.dados(
            ajuste_total_correcao="0", parcela_valor_1="1044,10", parcela_nota_id_1=str(parcela.pk),
        ), secure=True)
        self.assert_totais("1044.10", "0.00", "1044.10")
        lista.refresh_from_db(); parcela.refresh_from_db()
        self.assertEqual(lista.valor_nota_boleto, Decimal("1044.10"))
        self.assertEqual(parcela.valor, Decimal("1044.10"))
        self.assert_sem_recriacao()


CHROME = os.environ.get("COMPRA_TEST_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class CorrecaoCompraPreviewTests(TestCase):
    setUp = CorrecaoCompraSemMovimentoTests.setUp

    @skipUnless(Path(CHROME).is_file(), "Chrome indisponivel")
    def test_previa_real_javascript_corresponde_ao_backend(self):
        resposta = self.client.get(self.url, secure=True)
        html = resposta.content.decode()
        corpo = html[html.index('<div class="compra-correcao-wrap">'):html.index('</template>') + len('</template>')]
        script = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "function totaisCorrecaoCompra" in s)
        with TemporaryDirectory(prefix="compra-preview-") as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Page.setDocumentContent", {"frameId": tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"], "html": corpo + "<script>" + script + "</script>"})
                for modo, ajuste, cobrado, preco, esperado in (
                    ("ajuste", "-118,00", "926,10", "522,05", "926.10"),
                    ("ajuste", "-118,00", "926,10", "600,00", "1082.00"),
                    ("ajuste", "0,00", "1044,10", "522,05", "1044.10"),
                    ("valor_cobrado", "-118,00", "1044,10", "522,05", "1044.10"),
                ):
                    with self.subTest(modo=modo, ajuste=ajuste, preco=preco):
                        tab.evaluate("""const valores = """ + json.dumps([modo, ajuste, cobrado, preco]) + """;
                            document.getElementById('modoTotalCorrecao').value=valores[0];
                            document.getElementById('ajusteTotalCorrecao').value=valores[1];
                            document.getElementById('valorCobradoCorrecao').value=valores[2];
                            document.querySelector('[name="preco_unitario[]"]').value=valores[3];
                            document.getElementById('modoTotalCorrecao').dispatchEvent(new Event('change',{bubbles:true})); true""")
                        dados = tab.evaluate("Array.from(new FormData(document.getElementById('formCorrecaoItensCompra')).entries())")
                        post = {}
                        for chave, valor in dados:
                            post.setdefault(chave, []).append(valor)
                        self.client.post(self.url, post, secure=True)
                        self.compra.refresh_from_db(); self.conta.refresh_from_db()
                        self.assertEqual(self.compra.total, Decimal(esperado))
                        self.assertEqual(self.compra.total, self.conta.valor_original)
                        texto = tab.evaluate("document.getElementById('novoTotalCorrecaoCompra').textContent")
                        self.assertEqual(Decimal(texto.replace('R$', '').replace('\u00a0', '').replace(' ', '').replace('.', '').replace(',', '.')), self.compra.total)
                # Editing the single obligation explicitly changes the effective total/adjustment.
                tab.evaluate("""const parcela=document.querySelector('[name="parcela_valor_1"]');
                    parcela.focus();parcela.value='1050,00';
                    parcela.dispatchEvent(new Event('input',{bubbles:true}));true""")
                self.assertEqual(tab.evaluate("document.getElementById('modoTotalCorrecao').value"), "valor_cobrado")
                self.assertEqual(tab.evaluate("document.getElementById('ajusteTotalCorrecao').value"), "5,90")
                dados = tab.evaluate("Array.from(new FormData(document.getElementById('formCorrecaoItensCompra')).entries())")
                post = {}
                for chave, valor in dados:
                    post.setdefault(chave, []).append(valor)
                self.client.post(self.url, post, secure=True)
                self.compra.refresh_from_db(); self.conta.refresh_from_db()
                self.assertEqual(self.compra.total, Decimal("1050.00"))
                self.assertEqual(self.compra.ajuste_total, Decimal("5.90"))
                self.assertEqual(self.conta.valor_original, Decimal("1050.00"))
                # Decimal's half-even rounding is applied to every item, including fractional quantities.
                for qtd, preco in (("0,005", "1,00"), ("0,015", "1,00"), ("1,2345", "2,345")):
                    js = tab.evaluate("""const r=totaisCorrecaoCompra(""" + json.dumps([{"quantidade": qtd, "preco": preco}]) + """,'ajuste','0','0');Number(r.total)""")
                    python = (views._decimal_compra(qtd, casas=3) * views._decimal_compra(preco, casas=2)).quantize(Decimal("0.01"))
                    self.assertEqual(Decimal(js) / 100, python)
            finally:
                chrome.stop()
