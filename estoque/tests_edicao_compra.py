from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
import os

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .models import (
    Compra, ContaPagar, ContaFinanceira, EdicaoCompra, Fornecedor, ItemCompra,
    ListaCompraFornecedor, MovimentoFinanceiro, PagamentoContaPagar, ParcelaCompra,
    ParcelaNotaListaCompraFornecedor, Produto, CartaoCredito, LancamentoCartao,
    GrupoProdutoVinculado, MembroGrupoProduto,
)
from .services.edicao_compra import estado_compra


class EdicaoFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("edicao-compra", "teste@example.test", "teste")
        self.client.force_login(self.user)
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor editavel")
        self.produto = Produto.objects.create(nome="Produto historico", quantidade=20, preco_compra=15,
            preco_vista=25, preco_prazo=30, unidade_compra="UN")
        self.compra = Compra.objects.create(fornecedor=self.fornecedor, data_compra=date(2026, 10, 8),
            data_vencimento=date(2026, 11, 8), tipo_pagamento="aprazo", total=100, total_produtos=100,
            status=Compra.STATUS_FINALIZADA, estoque_entrada_realizada=True, observacao="Nota original")
        self.item = ItemCompra.objects.create(compra=self.compra, produto=self.produto, quantidade=10,
            preco_unitario=10, valor_total=100, unidade="UN", observacao="Observacao do item")
        self.conta = ContaPagar.objects.create(compra=self.compra, fornecedor=self.fornecedor,
            data_emissao=self.compra.data_compra, data_vencimento=self.compra.data_vencimento,
            valor_original=100, valor_em_aberto=100)
        self.url = reverse("estoque:compra_editar", args=[self.compra.pk])

    def payload(self, **extra):
        pagina = self.client.get(self.url)
        dados = {"acao_compra": "salvar_edicao_finalizada", "edicao_token": pagina.context["edicao_token"],
            "motivo_edicao": "Conferencia da nota", "fornecedor_id": str(self.fornecedor.pk),
            "data_compra": "2026-10-08", "tipo_pagamento": "aprazo", "data_vencimento": "2026-11-08",
            "produto_id[]": [str(self.produto.pk)], "item_edicao_id[]": [str(self.item.pk)],
            "quantidade[]": ["10"], "preco_unitario[]": ["10"], "unidade[]": ["UN"],
            "observacao_item[]": ["Observacao do item"], "observacao": "Nota original",
            "valor_cobrado": "100", "compra_forma_cobranca_nota": "boleto_unico",
            "compra_quantidade_boletos": "1", "compra_valor_nota_boleto": "100",
            "compra_parcela_valor_1": "100", "compra_parcela_vencimento_1": "2026-11-08",
            "compra_conta_id_1": str(self.conta.pk)}
        dados.update(extra)
        return dados

    def assert_bloqueado(self, dados, texto):
        self.compra.refresh_from_db()
        antes = estado_compra(self.compra)
        estoque = Produto.objects.get(pk=self.produto.pk).quantidade
        resposta = self.client.post(self.url, dados)
        self.assertEqual(resposta.status_code, 302)
        self.assertIn(texto, " ".join(str(m) for m in get_messages(resposta.wsgi_request)))
        self.compra.refresh_from_db()
        self.assertEqual(estado_compra(self.compra), antes)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, estoque)
        self.assertFalse(EdicaoCompra.objects.exists())

    def movimento_automatico_compra_avista(self, **extra):
        from .views import _descricao_compra_a_vista
        caixa = ContaFinanceira.objects.create(nome="Caixa compra teste", tipo="caixa")
        return MovimentoFinanceiro.objects.create(
            compra=self.compra, conta=caixa, tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=self.compra.total, data=self.compra.data_compra,
            descricao=_descricao_compra_a_vista(self.compra, caixa),
            operador=self.compra.operador or "", origem="compra_a_vista", **extra,
        )


class EdicaoCompraTests(EdicaoFixture, TestCase):
    def test_original_preenchida_e_fluxo_simples_sem_formularios_adicionais(self):
        pagina = self.client.get(self.url)
        self.assertTemplateUsed(pagina, "estoque/compras_nova.html")
        self.assertTrue(pagina.context["edicao_finalizada"])
        self.assertContains(pagina, "Salvar alterações")
        self.assertContains(pagina, f'value="{self.item.pk}"')
        self.assertNotContains(pagina, reverse("estoque:compra_corrigir_itens", args=[self.compra.pk]))
        self.assertNotContains(pagina, 'name="tipo_correcao"')
        self.assertNotContains(pagina, 'name="valor_pago_real"')
        self.assertContains(self.client.get(reverse("estoque:compras_lista")), self.url)

    def test_edicao_finalizada_com_varios_itens_nao_consulta_grupos_por_produto(self):
        grupo = GrupoProdutoVinculado.objects.create(nome="Grupo para teste de edicao")
        produtos = [self.produto]
        for indice in (2, 3):
            produtos.append(Produto.objects.create(
                nome=f"Produto da compra {indice}", quantidade=10, preco_compra=15,
                preco_vista=25, preco_prazo=30, unidade_compra="UN",
            ))
        for produto in produtos:
            MembroGrupoProduto.objects.create(produto=produto, grupo=grupo)
        for indice, produto in enumerate(produtos[1:], start=2):
            ItemCompra.objects.create(
                compra=self.compra, produto=produto, quantidade=indice,
                preco_unitario=10, valor_total=indice * 10, unidade="UN",
            )

        with CaptureQueriesContext(connection) as queries:
            pagina = self.client.get(self.url)

        self.assertEqual(pagina.status_code, 200)
        consultas_grupo = [
            consulta["sql"] for consulta in queries.captured_queries
            if "estoque_membrogrupoproduto" in consulta["sql"].lower()
        ]
        self.assertLessEqual(len(consultas_grupo), 2, consultas_grupo)
        linhas = pagina.context["item_linhas"]
        self.assertEqual(len(linhas), 3)
        self.assertEqual({linha["produto_nome"] for linha in linhas}, {
            "Produto Historico", "Produto Da Compra 2", "Produto Da Compra 3",
        })

    def test_delta_de_estoque_preserva_ids_e_precos_catalogo(self):
        dados = self.payload(**{"quantidade[]": ["12"], "preco_unitario[]": ["11"],
            "valor_cobrado": "132", "compra_valor_nota_boleto": "132", "compra_parcela_valor_1": "132"})
        self.client.post(self.url, dados)
        self.produto.refresh_from_db(); self.item.refresh_from_db(); self.conta.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("22"))
        self.assertEqual(self.item.quantidade, Decimal("12"))
        self.assertEqual(self.item.preco_unitario, Decimal("11"))
        self.assertEqual(self.conta.valor_original, Decimal("132"))
        self.assertEqual(self.produto.preco_compra, Decimal("15"))
        self.assertEqual(self.produto.preco_vista, Decimal("25"))
        self.assertEqual(self.produto.preco_prazo, Decimal("30"))
        self.assertEqual(self.compra.itens.count(), 1)
        self.assertEqual(self.compra.contas_pagar.count(), 1)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(PagamentoContaPagar.objects.exists())
        self.assertEqual(EdicaoCompra.objects.get().antes["itens"][0]["id"], self.item.pk)

    def test_adicao_remocao_e_troca_aplicam_apenas_diferencas(self):
        novo = Produto.objects.create(nome="Outro produto", quantidade=5, preco_compra=10, preco_vista=20, preco_prazo=25)
        dados = self.payload(**{"produto_id[]": [str(novo.pk)], "quantidade[]": ["3"],
            "valor_cobrado": "30", "compra_valor_nota_boleto": "30", "compra_parcela_valor_1": "30"})
        self.client.post(self.url, dados)
        self.produto.refresh_from_db(); novo.refresh_from_db(); self.item.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("10"))
        self.assertEqual(novo.quantidade, Decimal("8"))
        self.assertEqual(self.item.produto_id, novo.pk)

    def test_novo_item_e_remocao_com_auditoria(self):
        novo = Produto.objects.create(nome="Novo", quantidade=5, preco_compra=10, preco_vista=20, preco_prazo=25)
        dados = self.payload(**{"produto_id[]": [str(novo.pk)], "item_edicao_id[]": [""], "quantidade[]": ["10"]})
        self.client.post(self.url, dados)
        self.assertFalse(ItemCompra.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(EdicaoCompra.objects.get().antes["itens"][0]["id"], self.item.pk)
        self.produto.refresh_from_db(); novo.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("10"))
        self.assertEqual(novo.quantidade, Decimal("15"))

    def test_fornecedor_data_e_observacoes_sincronizam_contas(self):
        outro = Fornecedor.objects.create(nome="Fornecedor correto")
        dados = self.payload(fornecedor_id=str(outro.pk), data_compra="2026-10-09", observacao="Nota corrigida")
        self.client.post(self.url, dados)
        self.compra.refresh_from_db(); self.conta.refresh_from_db()
        self.assertEqual(self.compra.fornecedor_id, outro.pk)
        self.assertEqual(self.conta.fornecedor_id, outro.pk)
        self.assertEqual(self.conta.data_emissao, date(2026, 10, 9))
        self.assertIn("Nota original", self.compra.observacao)
        self.assertIn("Nota corrigida", self.compra.observacao)
        self.assertIn("edicao-compra", self.compra.observacao)

    def test_reenvio_identico_nao_duplica_estoque_contas_ou_auditoria(self):
        dados = self.payload(**{"quantidade[]": ["11"], "valor_cobrado": "110",
            "compra_valor_nota_boleto": "110", "compra_parcela_valor_1": "110"})
        for _ in range(2): self.client.post(self.url, dados)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("21"))
        self.assertEqual(EdicaoCompra.objects.count(), 1)
        self.assertEqual(self.compra.contas_pagar.count(), 1)

    def test_pagina_antiga_e_token_reutilizado_com_payload_diferente(self):
        dados = self.payload()
        self.client.post(self.url, dados)
        resposta = self.client.post(self.url, {**dados, "motivo_edicao": "Outro"})
        self.assertIn("ja utilizada", " ".join(str(m) for m in get_messages(resposta.wsgi_request)))
        self.assertEqual(EdicaoCompra.objects.count(), 1)

    def test_estado_alterado_apos_leitura_bloqueia(self):
        dados = self.payload()
        ContaPagar.objects.filter(pk=self.conta.pk).update(data_vencimento=date(2026, 11, 10))
        self.assert_bloqueado(dados, "mudaram")

    def test_duas_edicoes_da_mesma_versao_so_aplicam_uma(self):
        primeira = self.payload(**{"quantidade[]": ["11"]})
        segunda = self.payload(**{"quantidade[]": ["12"]})
        self.client.post(self.url, primeira)
        resposta = self.client.post(self.url, segunda)
        self.assertIn("mudaram", " ".join(str(m) for m in get_messages(resposta.wsgi_request)))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("21"))
        self.assertEqual(EdicaoCompra.objects.count(), 1)

    def test_pagamento_aparecendo_apos_leitura_invalida_edicao(self):
        dados = self.payload(**{"preco_unitario[]": ["11"]})
        PagamentoContaPagar.objects.create(conta=self.conta, valor=10, data_pagamento=date(2026, 10, 9))
        self.assert_bloqueado(dados, "mudaram")

    def test_valores_nao_finitos_e_parcelas_invalidas(self):
        for extra in ({"quantidade[]": ["NaN"]}, {"preco_unitario[]": ["NaN"]},
                      {"compra_quantidade_boletos": "13"}, {"compra_parcela_valor_1": "0"},
                      {"compra_parcela_vencimento_1": "invalido"}, {"compra_parcela_valor_1": "99"}):
            with self.subTest(extra=extra):
                dados = self.payload(**extra)
                antes = estado_compra(self.compra)
                resposta = self.client.post(self.url, dados)
                self.assertEqual(resposta.status_code, 302)
                self.assertEqual(estado_compra(self.compra), antes)
                self.assertFalse(EdicaoCompra.objects.exists())

    def test_rollback_da_auditoria_reverte_toda_operacao(self):
        dados = self.payload(**{"quantidade[]": ["12"]})
        with patch("estoque.views.logger"), patch("estoque.services.edicao_compra.EdicaoCompra.objects.create", side_effect=RuntimeError("Falha da auditoria")):
            self.assert_bloqueado(dados, "Nenhuma alteracao foi aplicada")

    def test_vinculo_legado_por_texto_e_conta_divergente_bloqueiam(self):
        lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor, data_lista=self.compra.data_compra,
            data_inicio_periodo=self.compra.data_compra, data_fim_periodo=self.compra.data_compra)
        self.compra.observacao = f"Gerada a partir da Lista de Compras #{lista.pk}"; self.compra.save()
        self.assert_bloqueado(self.payload(), "Vinculo legado")
        self.compra.observacao = "Nota original"; self.compra.save()
        outro = Fornecedor.objects.create(nome="Fornecedor divergente")
        self.conta.fornecedor = outro; self.conta.save()
        self.assert_bloqueado(self.payload(), "divergem")

    def test_estoque_negativo_bloqueia_toda_correcao(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=1)
        self.assert_bloqueado(self.payload(**{"quantidade[]": ["1"]}), "estoque negativo")

    def test_snapshot_de_preco_impede_remocao_e_troca(self):
        self.item.alteracoes_precos = {"preco_compra": {"anterior": "5", "novo": "15"}}
        self.item.save()
        outro = Produto.objects.create(nome="Outra mercadoria", quantidade=20, preco_compra=10, preco_vista=20, preco_prazo=25)
        self.assert_bloqueado(self.payload(**{"produto_id[]": [str(outro.pk)]}), "historico de precos")

    def test_pagamento_inclusive_cancelado_bloqueia_valores(self):
        PagamentoContaPagar.objects.create(conta=self.conta, valor=10, data_pagamento=date(2026, 10, 9), cancelado=True)
        self.assert_bloqueado(self.payload(**{"preco_unitario[]": ["11"]}), "pagamento registrado")

    def test_pagamento_permite_vencimento_sem_tocar_historico(self):
        pagamento = PagamentoContaPagar.objects.create(conta=self.conta, valor=20, juros_bancarios=2, data_pagamento=date(2026, 10, 9))
        self.conta.valor_em_aberto = 80; self.conta.status = ContaPagar.STATUS_PARCIAL; self.conta.save()
        dados = self.payload(compra_parcela_vencimento_1="2026-12-01")
        antes = dict(PagamentoContaPagar.objects.values().get())
        self.client.post(self.url, dados)
        self.conta.refresh_from_db(); self.produto.refresh_from_db()
        self.assertEqual(self.conta.data_vencimento, date(2026, 12, 1))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("80"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PARCIAL)
        self.assertEqual(dict(PagamentoContaPagar.objects.values().get()), antes)
        self.assertEqual(self.produto.quantidade, Decimal("20"))
        self.assertEqual(pagamento.pk, self.conta.pagamentos.get().pk)

    def test_movimento_ou_estorno_bloqueia_conversao_sem_supor_dinheiro_real(self):
        self.conta.delete()
        self.compra.tipo_pagamento = "avista"; self.compra.save()
        banco = ContaFinanceira.objects.create(nome="Banco teste", tipo="banco")
        MovimentoFinanceiro.objects.create(compra=self.compra, conta=banco, tipo="saida", valor=100,
            data=self.compra.data_compra, origem="compra_a_vista")
        dados = self.payload(compra_conta_id_1="")
        self.assert_bloqueado(dados, "Registro nao comprova pagamento")

    def test_conversao_vista_para_prazo_sem_movimento_cria_dois_boletos(self):
        self.conta.delete(); self.compra.tipo_pagamento = "avista"; self.compra.save()
        dados = self.payload(compra_conta_id_1="", compra_forma_cobranca_nota="varios_boletos",
            compra_quantidade_boletos="2", compra_parcela_valor_1="40", compra_parcela_valor_2="60",
            compra_parcela_vencimento_2="2026-12-08")
        for _ in range(2): self.client.post(self.url, dados)
        self.assertEqual(self.compra.contas_pagar.count(), 2)
        self.assertEqual(self.compra.parcelas_nota.count(), 2)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("20"))
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_conversao_reverte_saida_automatica_com_auditoria_e_sem_repetir_estoque(self):
        self.conta.delete()
        self.compra.tipo_pagamento = "avista"
        self.compra.save(update_fields=["tipo_pagamento"])
        movimento = self.movimento_automatico_compra_avista()
        estoque_antes = self.produto.quantidade
        item_id = self.item.pk
        self.assertFalse(self.client.get(self.url).context["bloqueio_edicao"])
        dados = self.payload(
            compra_conta_id_1="", compra_forma_cobranca_nota="varios_boletos",
            compra_quantidade_boletos="2", compra_valor_nota_boleto="100",
            compra_parcela_valor_1="40", compra_parcela_vencimento_1="2026-11-08",
            compra_parcela_valor_2="60", compra_parcela_vencimento_2="2026-12-08",
        )

        resposta = self.client.post(self.url, dados)

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(MovimentoFinanceiro.objects.filter(pk=movimento.pk).exists())
        self.assertEqual(self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).count(), 2)
        self.assertEqual(list(self.compra.contas_pagar.order_by("numero_parcela").values_list("valor_original", flat=True)),
            [Decimal("40"), Decimal("60")])
        self.assertEqual(self.compra.parcelas_nota.count(), 2)
        self.assertFalse(PagamentoContaPagar.objects.exists())
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(self.compra.itens.get().pk, item_id)

        auditoria = EdicaoCompra.objects.get()
        self.assertEqual(auditoria.usuario, self.user)
        self.assertEqual(auditoria.motivo, "Conferencia da nota")
        self.assertIsNotNone(auditoria.criado_em)
        self.assertEqual(auditoria.antes["movimentos"][0]["id"], movimento.pk)
        self.assertEqual(auditoria.antes["movimentos"][0]["valor"], "100.00")
        self.assertEqual(auditoria.depois["movimentos"], [])

    def test_conversao_com_saida_automatica_e_idempotente(self):
        self.conta.delete()
        self.compra.tipo_pagamento = "avista"
        self.compra.save(update_fields=["tipo_pagamento"])
        movimento = self.movimento_automatico_compra_avista()
        dados = self.payload(
            compra_conta_id_1="", compra_forma_cobranca_nota="varios_boletos",
            compra_quantidade_boletos="2", compra_valor_nota_boleto="100",
            compra_parcela_valor_1="40", compra_parcela_valor_2="60",
            compra_parcela_vencimento_2="2026-12-08",
        )

        self.client.post(self.url, dados)
        estado_apos_primeiro_envio = estado_compra(self.compra)
        self.client.post(self.url, dados)

        self.assertEqual(EdicaoCompra.objects.count(), 1)
        self.assertEqual(estado_compra(self.compra), estado_apos_primeiro_envio)
        self.assertFalse(MovimentoFinanceiro.objects.filter(pk=movimento.pk).exists())
        self.assertEqual(self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).count(), 2)

        contas = list(self.compra.contas_pagar.order_by("numero_parcela"))
        reenvio = self.payload(
            compra_conta_id_1=str(contas[0].pk), compra_conta_id_2=str(contas[1].pk),
            compra_forma_cobranca_nota="varios_boletos", compra_quantidade_boletos="2",
            compra_valor_nota_boleto="100", compra_parcela_valor_1="40",
            compra_parcela_vencimento_1="2026-11-08", compra_parcela_valor_2="60",
            compra_parcela_vencimento_2="2026-12-08",
        )
        self.client.post(self.url, reenvio)

        self.assertEqual(self.compra.contas_pagar.count(), 2)
        self.assertEqual(self.compra.parcelas_nota.count(), 2)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertEqual(EdicaoCompra.objects.count(), 2)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("20"))

    def test_conversao_bloqueia_saida_automatica_com_vinculo_incompativel(self):
        self.conta.delete()
        self.compra.tipo_pagamento = "avista"
        self.compra.save(update_fields=["tipo_pagamento"])
        outra_conta = ContaFinanceira.objects.create(nome="Banco destino", tipo="banco")
        self.movimento_automatico_compra_avista(conta_destino=outra_conta)
        dados = self.payload(compra_conta_id_1="", compra_quantidade_boletos="2",
            compra_forma_cobranca_nota="varios_boletos", compra_parcela_valor_1="40",
            compra_parcela_valor_2="60", compra_parcela_vencimento_2="2026-12-08")
        self.assert_bloqueado(dados, "Registro nao comprova pagamento")

    def test_conversao_bloqueia_pagamento_real_mesmo_com_saida_automatica(self):
        self.compra.tipo_pagamento = "avista"
        self.compra.save(update_fields=["tipo_pagamento"])
        self.movimento_automatico_compra_avista()
        PagamentoContaPagar.objects.create(
            conta=self.conta, valor=20, data_pagamento=date(2026, 10, 9),
        )
        self.conta.valor_em_aberto = 80
        self.conta.status = ContaPagar.STATUS_PARCIAL
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        dados = self.payload(compra_forma_cobranca_nota="varios_boletos",
            compra_quantidade_boletos="2", compra_parcela_valor_1="40",
            compra_parcela_valor_2="60", compra_parcela_vencimento_2="2026-12-08")
        self.assert_bloqueado(dados, "Existe pagamento registrado")

    def test_conversion_prazo_para_vista_preserva_conta(self):
        self.assert_bloqueado(self.payload(tipo_pagamento="avista"), "Conversao para pagamento imediato")

    def test_reparcelamento_preserva_conta_mantida_e_cancela_retirada(self):
        dados = self.payload(compra_forma_cobranca_nota="varios_boletos", compra_quantidade_boletos="2",
            compra_parcela_valor_1="40", compra_parcela_valor_2="60", compra_parcela_vencimento_2="2026-12-08")
        self.client.post(self.url, dados)
        mantida = self.compra.contas_pagar.get(pk=self.conta.pk)
        retirada = self.compra.contas_pagar.exclude(pk=self.conta.pk).get()
        self.assertEqual(mantida.valor_original, Decimal("40"))
        dados = self.payload()
        self.client.post(self.url, dados)
        retirada.refresh_from_db()
        self.assertEqual(retirada.status, ContaPagar.STATUS_CANCELADA)
        self.assertEqual(self.compra.contas_pagar.count(), 2)
        self.assertEqual(self.compra.contas_pagar.exclude(status=ContaPagar.STATUS_CANCELADA).count(), 1)
        self.assertEqual(self.compra.parcelas_nota.count(), 1)

    def test_compra_por_lista_sincroniza_plano_preservando_parcela(self):
        lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor, data_lista=self.compra.data_compra,
            data_inicio_periodo=self.compra.data_compra, data_fim_periodo=self.compra.data_compra,
            forma_cobranca_nota="boleto_unico", valor_nota_boleto=100)
        parcela = ParcelaNotaListaCompraFornecedor.objects.create(lista=lista, numero=1, valor=100, data_vencimento=self.compra.data_vencimento)
        self.compra.lista_fornecedor = lista; self.compra.save()
        dados = self.payload(compra_parcela_vencimento_1="2026-12-01")
        self.client.post(self.url, dados)
        parcela.refresh_from_db(); self.conta.refresh_from_db()
        self.assertEqual(parcela.data_vencimento, self.conta.data_vencimento)
        self.assertEqual(parcela.pk, lista.parcelas_nota.get().pk)
        self.assertFalse(self.compra.parcelas_nota.exists())

    def test_rollback_de_parcela_reverte_itens_estoque_e_contas(self):
        dados = self.payload(**{"quantidade[]": ["12"]})
        antes = estado_compra(self.compra)
        with patch("estoque.services.edicao_compra.ParcelaCompra.objects.create", side_effect=ValueError("Falha de parcela")):
            self.assert_bloqueado(dados, "Falha de parcela")
        self.assertEqual(estado_compra(self.compra), antes)

    def test_token_invalido_e_motivo_obrigatorio(self):
        self.assert_bloqueado(self.payload(edicao_token="invalido"), "identificacao invalida")
        self.assert_bloqueado(self.payload(motivo_edicao=""), "motivo")

    def test_preco_de_venda_e_conta_financeira_nao_sao_alterados(self):
        self.assert_bloqueado(self.payload(atualizar_preco_venda_produto_1="25"), "nao atualiza")
        self.assert_bloqueado(self.payload(origem_banco="100"), "Troca de conta financeira")

    def test_cartao_bloqueia_alteracoes(self):
        cartao = CartaoCredito.objects.create(nome="Cartao", titular="Teste")
        self.compra.cartao = cartao; self.compra.save()
        self.assert_bloqueado(self.payload(**{"preco_unitario[]": ["11"]}), "cartao/fatura")

    def test_conta_inconsistente_e_identificadores_adulterados(self):
        self.assert_bloqueado(self.payload(compra_conta_id_1="99999"), "Conta a Pagar invalida")
        self.assert_bloqueado(self.payload(**{"item_edicao_id[]": ["99999"]}), "Identificacao de item")
        self.conta.valor_em_aberto = 90; self.conta.save()
        self.assert_bloqueado(self.payload(**{"preco_unitario[]": ["11"]}), "saldo movimentado")

    def test_rotulo_legado_carregado_sem_converter_financeiro(self):
        self.compra.tipo_pagamento = "A prazo"; self.compra.save()
        self.assertEqual(self.client.get(self.url).context["tipo_pagamento_form"], "aprazo")
        self.client.post(self.url, self.payload(compra_parcela_vencimento_1="2026-12-08"))
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.tipo_pagamento, "A prazo")

    def test_contas_ambiguas_e_produto_sem_vinculo_bloqueados(self):
        extra = ContaPagar.objects.create(compra=self.compra, fornecedor=self.fornecedor, data_emissao=self.compra.data_compra,
            valor_original=100, valor_em_aberto=100, data_vencimento=self.compra.data_vencimento)
        self.assert_bloqueado(self.payload(), "ambigua")
        extra.delete()
        self.item.produto = None; self.item.save()
        self.assert_bloqueado(self.payload(), "Produto removido")

    def test_permissao_e_cancelamento(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.user)
        dados = self.payload()
        self.compra.cancelada = True; self.compra.save()
        self.assert_bloqueado(dados, "cancelada")


CHROME = os.environ.get("COMPRA_TEST_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


@skipUnless(Path(CHROME).is_file(), "Navegador indisponivel")
class EdicaoCompraBrowserTests(EdicaoFixture, StaticLiveServerTestCase):
    def verificar(self, mobile):
        with TemporaryDirectory(prefix="edicao-compra-", ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                if mobile:
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + self.url})
                tab.wait("document.readyState === 'complete' && !!document.getElementById('motivoEdicaoCompra')")
                self.assertEqual(tab.evaluate("document.getElementById('btnSalvarCompra').textContent"), "Salvar alterações")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_conta_id_1]').value"), str(self.conta.pk))
                tab.evaluate("""document.getElementById('motivoEdicaoCompra').value='Teste de navegador';
                    const linha=document.querySelector('#tabelaItensCompra .linha-item');
                    if (window.innerWidth < 820) linha.querySelector('.btn-editar-item-mobile').click();
                    linha.querySelector('[name="quantidade[]"]').value='12';
                    linha.querySelector('[name="quantidade[]"]').dispatchEvent(new Event('input',{bubbles:true}));
                    linha.querySelector('.campo-preco-unitario-compra').value='11,00';
                    linha.querySelector('.campo-preco-unitario-compra').dispatchEvent(new Event('input',{bubbles:true}));
                    if (window.innerWidth < 820) linha.querySelector('.btn-salvar-edicao-item-mobile').click();
                    document.getElementById('valorCobradoCompra').value='132,00';
                    document.getElementById('valorCobradoCompra').dispatchEvent(new Event('input',{bubbles:true}));
                    document.getElementById('compraValorNotaBoleto').value='132,00';
                    document.querySelector('[name=compra_parcela_valor_1]').value='132,00';
                    document.querySelector('[name=compra_parcela_vencimento_1]').value='2026-12-08';
                    document.querySelector('[name=compra_parcela_vencimento_1]').dispatchEvent(new Event('input',{bubbles:true}));
                    document.getElementById('btnSalvarCompra').click();true""")
                tab.wait("location.pathname === '" + reverse("estoque:compras_detalhe", args=[self.compra.pk]) + "'")
                self.conta.refresh_from_db(); self.produto.refresh_from_db(); self.item.refresh_from_db()
                self.assertEqual(self.conta.data_vencimento, date(2026, 12, 8))
                self.assertEqual(self.produto.quantidade, Decimal("22"))
                self.assertEqual(self.item.quantidade, Decimal("12"))
                self.assertEqual(self.item.preco_unitario, Decimal("11"))
                self.assertEqual(self.produto.preco_compra, Decimal("15"))
                self.assertEqual(self.produto.preco_vista, Decimal("25"))
                self.assertEqual(EdicaoCompra.objects.count(), 1)
                self.assertFalse(MovimentoFinanceiro.objects.exists())
                self.assertFalse(tab.evaluate("!!document.querySelector('#modalConferenciaPrecosCompra.visivel')"))
            finally:
                chrome.stop()

    def test_desktop_edita_no_formulario_original(self): self.verificar(False)
    def test_mobile_edita_no_formulario_original(self): self.verificar(True)


class MigrationEdicaoCompraTests(TransactionTestCase):
    def test_migration_aditiva_preserva_primeira_etapa(self):
        anterior = [("estoque", "0133_parcelas_compra_direta")]
        atual = [("estoque", "0134_edicao_compra")]
        MigrationExecutor(connection).migrate(anterior)
        try:
            compra = Compra.objects.create(data_compra=date(2026, 10, 8), total=100, forma_cobranca_nota="boleto_unico", valor_nota_boleto=100)
            parcela = ParcelaCompra.objects.create(compra=compra, numero=1, valor=100, data_vencimento=date(2026, 11, 8))
            MigrationExecutor(connection).migrate(atual)
            compra.refresh_from_db(); parcela.refresh_from_db()
            self.assertEqual(compra.total, Decimal("100"))
            self.assertEqual(compra.valor_nota_boleto, Decimal("100"))
            self.assertEqual(compra.parcelas_nota.get().pk, parcela.pk)
            self.assertFalse(EdicaoCompra.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(atual)
