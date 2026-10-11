"""Boletos independentes da origem, sem banco operacional."""
from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from . import views
from .models import Compra, ContaPagar, MovimentoFinanceiro, ParcelaCompra
from . import tests_compra_pre_revisao as pre_revisao


class CompraDiretaBoletosTests(pre_revisao.CompraPreRevisaoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.compra.lista_fornecedor = None
        self.compra.save(update_fields=["lista_fornecedor"])

    def assert_plano(self, quantidade):
        self.assertEqual(self.compra.parcelas_nota.count(), quantidade)
        self.assertFalse(self.lista.parcelas_nota.exists())
        pagina = self.client.get(self.editar_url)
        self.assertContains(pagina, 'id="painelBoletosCompra"')
        self.assertIsNone(pagina.context["lista_origem_compra"])
        self.assertEqual(len(pagina.context["pagamento_nota_compra"]["linhas"]), quantidade)

    def test_compra_direta_com_dois_boletos(self):
        dados = self.payload(compra_quantidade_boletos="2", compra_parcela_valor_1="40", compra_parcela_valor_2="50")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.assert_plano(2)
        self.assert_sem_efeitos()
        self.client.post(self.finalizar_url, {**dados, "acao_compra": "confirmar_financeiro"})
        contas = list(self.compra.contas_pagar.order_by("numero_parcela"))
        self.assertEqual([c.valor_original for c in contas], [Decimal("40"), Decimal("50")])
        self.assertEqual([c.data_vencimento for c in contas], [date(2026, 11, 8), date(2026, 11, 18)])
        self.assertEqual([c.numero_parcela for c in contas], [1, 2])
        self.assertTrue(all(c.total_parcelas == 2 and c.valor_em_aberto == c.valor_original for c in contas))
        self.assertIn("Documento 2", contas[1].observacao)
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_compra_nova_direta_com_tres_boletos_e_reenvio(self):
        url = reverse("estoque:compras_nova")
        dados = self.payload(fechamento_token="b" * 32)
        for _ in range(2):
            self.assertEqual(self.client.post(url, dados).status_code, 302)
        compra = Compra.objects.get(fechamento_token="b" * 32)
        self.assertIsNone(compra.lista_fornecedor_id)
        self.assertEqual(compra.parcelas_nota.count(), 3)
        self.assertEqual(compra.contas_pagar.count(), 3)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_salvar_rascunho_normal_novo_e_reabrir(self):
        url = reverse("estoque:compras_nova")
        dados = self.payload(acao_compra="salvar_rascunho", fechamento_token="c" * 32)
        self.client.post(url, dados)
        self.client.post(url, dados)
        compra = Compra.objects.get(fechamento_token="c" * 32)
        self.assertEqual(compra.parcelas_nota.count(), 3)
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertFalse(compra.contas_pagar.exists())
        pagina = self.client.get(reverse("estoque:compra_editar", args=[compra.pk]))
        self.assertEqual(pagina.context["pagamento_nota_compra"]["linhas"][2]["observacao"], "Documento 3")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("10"))

    def test_rascunho_editado_e_retorno_da_revisao_preservam_boletos(self):
        dados = self.payload(acao_compra="salvar_rascunho")
        self.client.post(self.editar_url, dados)
        self.assert_plano(3)
        self.assert_sem_efeitos()
        self.assertEqual(self.client.post(self.salvar_url, self.payload()).status_code, 200)
        ids = list(self.compra.parcelas_nota.values_list("pk", flat=True))
        self.client.post(self.finalizar_url, {"acao_compra": "voltar_itens"})
        self.assertEqual(list(self.compra.parcelas_nota.values_list("pk", flat=True)), ids)
        self.assert_plano(3)
        self.assert_sem_efeitos()

    def test_finalizacao_repetida_preserva_itens_e_estoque(self):
        self.client.post(self.salvar_url, self.payload())
        self.client.post(self.editar_url, self.payload())
        itens = list(self.compra.itens.values())
        plano = list(self.compra.parcelas_nota.values())
        for url in (self.editar_url, self.finalizar_url):
            self.client.post(url, self.payload(acao_compra="confirmar_financeiro"))
        self.assertEqual(self.compra.contas_pagar.count(), 3)
        self.assertEqual(list(self.compra.itens.values()), itens)
        self.assertEqual(list(self.compra.parcelas_nota.values()), plano)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(self.client.post(self.salvar_url, self.payload()).status_code, 409)

    def test_finalizacao_intercalada_revalida_compra_apos_lock(self):
        original = views._dados_compra_post

        def finalizar_outro_envio(request, exigir_itens=True):
            dados = original(request, exigir_itens)
            with patch("estoque.views._dados_compra_post", original):
                self.client.post(self.editar_url, self.payload())
            return dados

        with patch("estoque.views._dados_compra_post", side_effect=finalizar_outro_envio):
            self.client.post(self.finalizar_url, self.payload(acao_compra="confirmar_financeiro"))
        self.assertEqual(self.compra.contas_pagar.count(), 3)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))

    def test_falha_na_segunda_conta_reverte_toda_finalizacao(self):
        self.client.post(self.salvar_url, self.payload())
        ids = list(self.compra.parcelas_nota.values_list("pk", flat=True))
        criar = ContaPagar.objects.create
        chamadas = []

        def falhar(**kwargs):
            chamadas.append(kwargs)
            if len(chamadas) == 2:
                raise RuntimeError("Falha ao criar segunda parcela")
            return criar(**kwargs)

        with patch("estoque.views.logger"), patch("estoque.views.ContaPagar.objects.create", side_effect=falhar):
            self.client.post(self.finalizar_url, self.payload(acao_compra="confirmar_financeiro"))
        self.assertEqual(len(chamadas), 2)
        self.assert_sem_efeitos()
        self.assertEqual(list(self.compra.parcelas_nota.values_list("pk", flat=True)), ids)

    def test_falha_ao_salvar_plano_reverte_itens_e_cabecalho(self):
        antes = list(self.compra.itens.values())
        with patch("estoque.views.logger"), patch("estoque.views.ParcelaCompra.objects.bulk_create", side_effect=RuntimeError("Falha")):
            self.assertEqual(self.client.post(self.salvar_url, self.payload()).status_code, 500)
        self.assertEqual(list(self.compra.itens.values()), antes)
        self.assertFalse(self.compra.parcelas_nota.exists())
        self.assert_sem_efeitos()

    def test_validacoes_preservam_rascunho_anterior(self):
        self.client.post(self.salvar_url, self.payload())
        antes = list(self.compra.parcelas_nota.values())
        for extra in (
            {"compra_parcela_valor_2": "31"}, {"compra_parcela_valor_2": "0"},
            {"compra_parcela_valor_2": "-1"}, {"compra_parcela_valor_2": "NaN"},
            {"compra_parcela_valor_2": "Infinity"}, {"compra_parcela_vencimento_2": ""},
            {"compra_parcela_vencimento_2": "2026-02-30"}, {"compra_quantidade_boletos": "13"},
            {"compra_quantidade_boletos": "0"}, {"compra_quantidade_boletos": "abc"},
            {"compra_quantidade_boletos": "1"}, {"compra_forma_cobranca_nota": "invalida"},
            {"compra_parcela_observacao_1": "x" * 121},
        ):
            with self.subTest(extra=extra):
                resposta = self.client.post(self.salvar_url, self.payload(observacao="Nao persistir", **extra))
                self.assertEqual(resposta.status_code, 400)
                self.assertEqual(list(self.compra.parcelas_nota.values()), antes)
                self.compra.refresh_from_db()
                self.assertEqual(self.compra.observacao, "Rascunho teste")
        self.assert_sem_efeitos()

    def test_aceita_limite_de_doze_boletos(self):
        dados = self.payload(compra_quantidade_boletos="12")
        for n in range(1, 13):
            dados[f"compra_parcela_valor_{n}"] = "7,50"
            dados[f"compra_parcela_vencimento_{n}"] = "2026-11-08"
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.client.post(self.editar_url, dados)
        self.assertEqual(self.compra.contas_pagar.count(), 12)

    def test_boleto_unico_e_vencimento_derivado(self):
        dados = self.payload(compra_forma_cobranca_nota="boleto_unico", compra_quantidade_boletos="1",
                             compra_parcela_valor_1="90", data_vencimento="")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.data_vencimento, date(2026, 11, 8))
        self.client.post(self.editar_url, dados)
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_compatibilidade_post_antigo_sem_painel(self):
        dados = {k: v for k, v in self.payload().items() if not k.startswith("compra_")}
        self.client.post(self.editar_url, dados)
        self.assertEqual(self.compra.contas_pagar.count(), 1)
        self.assertEqual(self.compra.contas_pagar.get().valor_original, Decimal("90"))

    def test_valor_financeiro_independente_preservado_como_na_lista(self):
        dados = self.payload(compra_valor_nota_boleto="96", compra_parcela_valor_1="32",
                             compra_parcela_valor_2="32", compra_parcela_valor_3="32")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.assertEqual(self.client.get(self.editar_url).context["pagamento_nota_compra"]["valor_nota"], Decimal("96"))
        self.client.post(self.editar_url, dados)
        self.assertEqual(sum(self.compra.contas_pagar.values_list("valor_original", flat=True)), Decimal("96"))

    def test_a_vista_limpa_plano_sem_contas_indevidas(self):
        self.client.post(self.salvar_url, self.payload())
        dados = self.payload(tipo_pagamento="avista", acao_compra="confirmar_financeiro", origem_caixa="90")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.assertFalse(self.compra.parcelas_nota.exists())
        self.compra.refresh_from_db()
        self.assertIsNone(self.compra.valor_nota_boleto)
        self.assert_sem_efeitos()
        for _ in range(2):
            self.client.post(self.finalizar_url, dados)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(), 1)

    def test_criacao_idempotente_e_divergencia_bloqueada(self):
        self.client.post(self.salvar_url, self.payload())
        views._criar_contas_pagar_compra(self.compra)
        ids = list(self.compra.contas_pagar.values_list("pk", flat=True))
        views._criar_contas_pagar_compra(self.compra)
        self.assertEqual(list(self.compra.contas_pagar.values_list("pk", flat=True)), ids)
        self.compra.contas_pagar.order_by("id").last().delete()
        with self.assertRaisesMessage(ValueError, "divergem"):
            views._criar_contas_pagar_compra(self.compra)
        self.assertEqual(self.compra.contas_pagar.count(), 2)

    def test_numeros_duplicados_bloqueados_no_banco(self):
        self.client.post(self.salvar_url, self.payload())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ParcelaCompra.objects.create(compra=self.compra, numero=1, valor=30, data_vencimento=date(2026, 11, 8))


class CompraDiretaBoletosBrowserTests(pre_revisao.CompraPreRevisaoBrowserTests):
    def setUp(self):
        super().setUp()
        self.compra.lista_fornecedor = None
        self.compra.save(update_fields=["lista_fornecedor"])
        self.lista = self.compra  # Mesmo contrato de parcelas na verificacao de navegador existente.

    def test_reload_preserva_valores_personalizados_e_total_financeiro(self):
        dados = self.payload(compra_quantidade_boletos="2", compra_parcela_valor_1="40",
                             compra_parcela_valor_2="56", compra_valor_nota_boleto="96")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        with TemporaryDirectory(prefix="compra-direta-boletos-", ignore_cleanup_errors=True) as profile:
            chrome = pre_revisao.Chrome(pre_revisao.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + self.editar_url})
                tab.wait("document.readyState === 'complete' && !!document.querySelector('[name=compra_parcela_valor_2]')")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_valor_1]').value"), "40,00")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_valor_2]').value"), "56,00")
                self.assertEqual(tab.evaluate("document.getElementById('compraValorNotaBoleto').value"), "96,00")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_observacao_2]').value"), "Documento 2")
                self.assertFalse(tab.evaluate("document.getElementById('painelBoletosCompra').classList.contains('oculto')"))
                tab.evaluate("document.getElementById('tipoPagamentoCompra').value='avista';document.getElementById('tipoPagamentoCompra').dispatchEvent(new Event('change'));true")
                self.assertTrue(tab.evaluate("document.getElementById('painelBoletosCompra').classList.contains('oculto')"))
                self.assertTrue(tab.evaluate("document.querySelector('[name=compra_parcela_valor_1]').disabled"))
            finally:
                chrome.stop()


class MigrationParcelasCompraTests(TransactionTestCase):
    def test_migration_aditiva_preserva_compra_finalizada_e_lista(self):
        anterior = [("estoque", "0132_adocao_precos_por_produto")]
        atual = [("estoque", "0133_parcelas_compra_direta")]
        executor = MigrationExecutor(connection)
        executor.migrate(anterior)
        try:
            apps = executor.loader.project_state(anterior).apps
            compra = apps.get_model("estoque", "Compra").objects.create(
                data_compra=date(2026, 10, 8), total=90, status="finalizada", estoque_entrada_realizada=True,
            )
            item = apps.get_model("estoque", "ItemCompra").objects.create(
                compra=compra, quantidade=9, preco_unitario=10, valor_total=90,
                alteracoes_precos={"anterior": "5.00"},
            )
            conta = apps.get_model("estoque", "ContaPagar").objects.create(
                compra=compra, data_emissao=date(2026, 10, 8), valor_original=90, valor_em_aberto=90,
            )
            lista = apps.get_model("estoque", "ListaCompraFornecedor").objects.create(
                data_lista=date(2026, 10, 8), data_inicio_periodo=date(2026, 10, 1),
                data_fim_periodo=date(2026, 10, 8), forma_cobranca_nota="boleto_unico", valor_nota_boleto=90,
            )
            parcela = apps.get_model("estoque", "ParcelaNotaListaCompraFornecedor").objects.create(
                lista=lista, numero=1, valor=90, data_vencimento=date(2026, 11, 8),
            )
            MigrationExecutor(connection).migrate(atual)
            compra_atual = Compra.objects.get(pk=compra.pk)
            self.assertEqual(compra_atual.total, Decimal("90"))
            self.assertEqual(compra_atual.status, "finalizada")
            self.assertTrue(compra_atual.estoque_entrada_realizada)
            self.assertIsNone(compra_atual.valor_nota_boleto)
            self.assertFalse(compra_atual.parcelas_nota.exists())
            self.assertEqual(compra_atual.itens.get().pk, item.pk)
            self.assertEqual(compra_atual.itens.get().alteracoes_precos, {"anterior": "5.00"})
            self.assertEqual(compra_atual.contas_pagar.get().pk, conta.pk)
            self.assertEqual(compra_atual.contas_pagar.get().valor_em_aberto, Decimal("90"))
            self.assertTrue(lista.parcelas_nota.filter(pk=parcela.pk, valor=90).exists())
        finally:
            MigrationExecutor(connection).migrate(atual)
