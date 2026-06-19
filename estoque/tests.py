import io
import json
import os
import tempfile
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import FuncionarioForm, PixRecebidoForm
from .models import AjusteItemVendaQuitada, Cliente, Compra, ContaPagar, ContaReceber, CreditoCliente, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, Fornecedor, Funcionario, ItemCompra, ItemVenda, ItemVendaRemovido, MovimentoFinanceiro, PixRecebido, Produto, RecebimentoContaReceber, Venda
from .utils_pix import analisar_comprovante_pix, analisar_comprovante_pix_google_vision, _preparar_recortes_ocr
from . import views


class FechamentoCompraFinanceiroTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Teste")
        self.produto = Produto.objects.create(
            nome="Produto Compra",
            preco_compra=Decimal("100.00"),
            preco_vista=Decimal("150.00"),
            preco_prazo=Decimal("160.00"),
            quantidade=Decimal("10.000"),
        )
        self.url = "/estoque/compras/nova/"

    def dados(self, **alteracoes):
        dados = {
            "fechamento_token": "a" * 32,
            "fornecedor_id": str(self.fornecedor.id),
            "data_compra": "2026-06-19",
            "tipo_pagamento": "pix",
            "produto_id[]": [str(self.produto.id)],
            "quantidade[]": ["1"],
            "unidade[]": ["UN"],
            "preco_unitario[]": ["1.000,00"],
            "observacao_item[]": [""],
            "origem_caixa": "600,00",
            "origem_reserva": "200,00",
            "origem_banco": "200,00",
        }
        dados.update(alteracoes)
        return dados

    def test_nova_compra_exibe_apenas_tipos_pagamento_simplificados(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, '<option value="">Selecione...</option>')
        self.assertContains(resposta, '<option value="avista">À vista (Dinheiro / Pix)</option>')
        self.assertContains(resposta, '<option value="aprazo" selected>A prazo</option>')
        self.assertContains(resposta, '<option value="cartao_credito">Cartão crédito</option>')
        self.assertContains(resposta, '<option value="cartao_debito">Cartão débito</option>')
        for valor_antigo in ["pix", "dinheiro", "banco", "boleto", "cartao"]:
            self.assertNotContains(resposta, f'<option value="{valor_antigo}">')

    def test_rotulo_pagamento_compra_preserva_valores_antigos(self):
        self.assertEqual(Compra(tipo_pagamento="pix").tipo_pagamento_texto, "Pix")
        self.assertEqual(Compra(tipo_pagamento="cartao").tipo_pagamento_texto, "Cartão")
        self.assertEqual(
            Compra(tipo_pagamento="A vista").tipo_pagamento_texto,
            "À vista (Dinheiro / Pix)",
        )
        self.assertEqual(
            Compra(tipo_pagamento="cartao_debito").tipo_pagamento_texto,
            "Cartão débito",
        )
        self.assertTrue(views._compra_pagamento_imediato("cartao_credito"))
        self.assertTrue(views._compra_pagamento_imediato("cartao_debito"))
        self.assertTrue(views._compra_pagamento_a_prazo("aprazo"))

    def test_modal_de_fechamento_tem_resumo_compacto_e_sem_usar_restante(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Total da compra")
        self.assertContains(resposta, "Falta distribuir")
        self.assertContains(resposta, "Tudo distribuído")
        self.assertContains(resposta, 'campo.addEventListener("input"')
        self.assertNotContains(resposta, "distribuirRestanteApos")
        self.assertNotContains(resposta, 'id="valorOrigemDistribuidoCompra"')
        self.assertContains(resposta, "Distribua o total da compra entre Caixa, Sangria e Banco/Pix.")
        self.assertContains(resposta, 'id="origemCaixaCompra"')
        self.assertContains(resposta, 'id="origemReservaCompra"')
        self.assertContains(resposta, 'id="origemBancoCompra"')
        self.assertNotContains(resposta, "Total informado")
        self.assertNotContains(resposta, "Usar restante")
        self.assertContains(resposta, "Fechando compra...")
        self.assertContains(resposta, "definirEnvioModalEmAndamento(true)")
        self.assertContains(resposta, "Saldo atual: R$ 0,00", count=3)

    def test_modal_exibe_saldos_financeiros_calculados_na_renderizacao(self):
        saldos = {
            "caixa": Decimal("476.85"),
            "reserva": Decimal("1800.00"),
            "banco": Decimal("985.35"),
        }
        for chave, valor in saldos.items():
            MovimentoFinanceiro.objects.create(
                conta=views._conta_financeira_padrao(chave),
                tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                valor=valor,
                data=timezone.localdate(),
                origem="teste_saldo_modal_compra",
            )

        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Saldo atual: R$ 476,85")
        self.assertContains(resposta, "Saldo atual: R$ 1800,00")
        self.assertContains(resposta, "Saldo atual: R$ 985,35")

    def test_fecha_compra_e_cria_tres_saidas(self):
        resposta = self.client.post(self.url, self.dados(), follow=True, secure=True)

        self.assertEqual(Compra.objects.count(), 1)
        compra = Compra.objects.get()
        movimentos = list(compra.movimentos_financeiros.order_by("valor"))
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual([movimento.valor for movimento in movimentos], [Decimal("200.00"), Decimal("200.00"), Decimal("600.00")])
        self.assertTrue(all(movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA for movimento in movimentos))
        self.assertTrue(all(f"Pagamento da compra #{compra.id}" in movimento.descricao for movimento in movimentos))
        self.assertContains(resposta, "Compra fechada e valores lançados no financeiro com sucesso.")

    def test_detalhe_exibe_resumo_financeiro_compacto_e_lancamentos_fechados(self):
        self.client.post(self.url, self.dados(), secure=True)
        compra = Compra.objects.get()

        resposta = self.client.get(f"/estoque/compras/{compra.id}/", secure=True)

        self.assertContains(resposta, "Resumo do pagamento")
        self.assertContains(resposta, "Compra à vista paga no fechamento. Nenhuma conta a pagar foi gerada.")
        self.assertContains(resposta, "Total pago")
        self.assertEqual(resposta.context["compra"].total, Decimal("1000.00"))
        self.assertContains(resposta, "Mostrar lançamentos financeiros (3)")
        self.assertContains(resposta, '<details class="nota-compra-lancamentos">')
        self.assertNotContains(resposta, "Detalhamento do pagamento")
        self.assertContains(resposta, "☰ Ações da compra")
        self.assertContains(resposta, 'id="painelAcoesCompraDetalhe"')
        self.assertContains(resposta, "Corrigir itens")
        self.assertContains(resposta, "Corrigir origem")
        self.assertContains(resposta, ">Excluir</button>")

    def test_fecha_com_os_rateios_visiveis_de_1173_83(self):
        casos = [
            {
                "token": "b" * 32,
                "caixa": "173,83",
                "reserva": "1.000,00",
                "banco": "0,00",
                "esperado": [Decimal("173.83"), Decimal("1000.00")],
            },
            {
                "token": "c" * 32,
                "caixa": "173,83",
                "reserva": "600,00",
                "banco": "400,00",
                "esperado": [Decimal("173.83"), Decimal("400.00"), Decimal("600.00")],
            },
        ]

        for caso in casos:
            with self.subTest(caso=caso):
                resposta = self.client.post(
                    self.url,
                    self.dados(
                        fechamento_token=caso["token"],
                        origem_caixa=caso["caixa"],
                        origem_reserva=caso["reserva"],
                        origem_banco=caso["banco"],
                        **{"preco_unitario[]": ["1.173,83"]},
                    ),
                    secure=True,
                )

                self.assertEqual(resposta.status_code, 302)
                compra = Compra.objects.get(fechamento_token=caso["token"])
                valores = list(compra.movimentos_financeiros.order_by("valor").values_list("valor", flat=True))
                self.assertEqual(valores, caso["esperado"])

    def test_bloqueia_soma_menor_maior_e_valor_invalido(self):
        casos = [
            {"origem_banco": "199,99"},
            {"origem_banco": "200,01"},
            {"origem_banco": "valor-invalido"},
            {"origem_caixa": "-600,00"},
        ]
        for indice, alteracoes in enumerate(casos):
            with self.subTest(alteracoes=alteracoes):
                alteracoes["fechamento_token"] = str(indice).zfill(32)
                self.client.post(self.url, self.dados(**alteracoes), secure=True)
                self.assertEqual(Compra.objects.count(), 0)
                self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_reenvio_do_mesmo_token_nao_duplica_compra_nem_movimentos(self):
        dados = self.dados()
        self.client.post(self.url, dados, secure=True)
        resposta = self.client.post(self.url, dados, follow=True, secure=True)

        self.assertEqual(Compra.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="compra_a_vista").count(), 3)
        self.assertContains(resposta, "Esta compra ja foi fechada e lancada no financeiro.")

    def test_falha_financeira_desfaz_compra_itens_e_estoque(self):
        with patch("estoque.views.MovimentoFinanceiro.objects.create", side_effect=RuntimeError("falha simulada")):
            resposta = self.client.post(self.url, self.dados(), follow=True, secure=True)

        self.produto.refresh_from_db()
        self.assertEqual(Compra.objects.count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.assertEqual(self.produto.quantidade, Decimal("10.000"))
        self.assertContains(resposta, "Não foi possível fechar a compra. Nenhum valor foi lançado no financeiro.")


class CorrecaoItensCompraTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Correcao")
        self.produto_a = self.criar_produto("Produto A", "20.000")
        self.produto_b = self.criar_produto("Produto B", "12.000")
        self.produto_c = self.criar_produto("Produto C", "3.000")
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="avista",
            total=Decimal("110.00"),
            status=Compra.STATUS_FINALIZADA,
            estoque_entrada_realizada=True,
        )
        self.item_a = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto_a, quantidade=Decimal("10.000"),
            unidade="UN", preco_unitario=Decimal("10.00"), valor_total=Decimal("100.00"),
        )
        self.item_b = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto_b, quantidade=Decimal("2.000"),
            unidade="UN", preco_unitario=Decimal("5.00"), valor_total=Decimal("10.00"),
        )
        self.conta_pagar = ContaPagar.objects.create(
            compra=self.compra, fornecedor=self.fornecedor, data_emissao=timezone.localdate(),
            valor_original=Decimal("110.00"), valor_em_aberto=Decimal("110.00"),
        )
        self.movimento = MovimentoFinanceiro.objects.create(
            conta=views._conta_financeira_padrao("caixa"),
            tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=Decimal("110.00"),
            data=timezone.localdate(),
            origem="compra_a_vista",
            compra=self.compra,
        )
        self.url = f"/estoque/compras/{self.compra.id}/corrigir-itens/"

    def criar_produto(self, nome, quantidade):
        return Produto.objects.create(
            nome=nome, preco_compra=Decimal("2.00"), preco_vista=Decimal("3.00"),
            preco_prazo=Decimal("4.00"), quantidade=Decimal(quantidade), unidade_compra="UN",
        )

    def dados(self, **alteracoes):
        dados = {
            "item_id[]": [str(self.item_a.id), str(self.item_b.id)],
            "quantidade[]": ["10", "2"],
            "preco_unitario[]": ["10,00", "5,00"],
            "novo_produto_id[]": [""],
            "nova_quantidade[]": [""],
            "novo_preco_unitario[]": [""],
        }
        dados.update(alteracoes)
        return dados

    def assert_financeiro_inalterado(self):
        self.conta_pagar.refresh_from_db()
        self.movimento.refresh_from_db()
        self.assertEqual(self.conta_pagar.valor_original, Decimal("110.00"))
        self.assertEqual(self.conta_pagar.valor_em_aberto, Decimal("110.00"))
        self.assertEqual(self.movimento.valor, Decimal("110.00"))
        self.assertEqual(self.compra.movimentos_financeiros.count(), 1)

    def test_diminuir_quantidade_reduz_estoque_pela_diferenca(self):
        self.client.post(self.url, self.dados(**{"quantidade[]": ["7", "2"]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db(); self.item_a.refresh_from_db()
        self.assertEqual(self.produto_a.quantidade, Decimal("17.000"))
        self.assertEqual(self.item_a.quantidade, Decimal("7.000"))
        self.assertEqual(self.compra.total, Decimal("80.00"))
        self.assertIn("Total anterior R$ 110,00", self.compra.observacao)
        self.assertIn("Financeiro nao alterado", self.compra.observacao)
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        self.assertContains(detalhe, "Observações e histórico")
        self.assert_financeiro_inalterado()

    def test_aumentar_quantidade_aumenta_estoque_pela_diferenca(self):
        self.client.post(self.url, self.dados(**{"quantidade[]": ["15", "2"]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto_a.quantidade, Decimal("25.000"))
        self.assertEqual(self.compra.total, Decimal("160.00"))
        self.assert_financeiro_inalterado()

    def test_alterar_preco_recalcula_total_sem_alterar_estoque(self):
        self.client.post(self.url, self.dados(**{"preco_unitario[]": ["12,00", "5,00"]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto_a.quantidade, Decimal("20.000"))
        self.assertEqual(self.compra.total, Decimal("130.00"))
        self.assert_financeiro_inalterado()

    def test_remover_item_desfaz_sua_entrada_no_estoque(self):
        self.client.post(self.url, self.dados(**{"remover_item[]": [str(self.item_a.id)]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertFalse(ItemCompra.objects.filter(pk=self.item_a.id).exists())
        self.assertEqual(self.produto_a.quantidade, Decimal("10.000"))
        self.assertEqual(self.compra.total, Decimal("10.00"))
        self.assert_financeiro_inalterado()

    def test_adicionar_item_aumenta_estoque(self):
        self.client.post(self.url, self.dados(**{
            "novo_produto_id[]": [str(self.produto_c.id)],
            "nova_quantidade[]": ["5"],
            "novo_preco_unitario[]": ["4,00"],
        }), secure=True)
        self.produto_c.refresh_from_db(); self.compra.refresh_from_db()
        novo = self.compra.itens.get(produto=self.produto_c)
        self.assertEqual(self.produto_c.quantidade, Decimal("8.000"))
        self.assertEqual(novo.valor_total, Decimal("20.00"))
        self.assertEqual(self.compra.total, Decimal("130.00"))
        self.assert_financeiro_inalterado()

    def test_compra_antiga_e_tela_de_correcao_continuam_abrindo(self):
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        correcao = self.client.get(self.url, secure=True)
        self.assertEqual(detalhe.status_code, 200)
        self.assertContains(correcao, "Salvar correção dos itens")
        self.assertContains(correcao, "Novo total")
        self.assertContains(correcao, "Caixa/Banco e Conta a Pagar não serão alterados")


class FuncionarioTests(TestCase):
    def test_exige_whatsapp_para_receber_checklist(self):
        funcionario = Funcionario(
            nome="Joao Entregador",
            pode_receber_checklist=True,
        )

        with self.assertRaises(ValidationError):
            funcionario.full_clean()

    def test_habilitados_para_checklist_filtra_ativos_e_marcados(self):
        habilitado = Funcionario.objects.create(
            nome="Maria Entregadora",
            telefone_whatsapp="85999990000",
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            telefone_whatsapp="85999991111",
            ativo=False,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Checklist",
            telefone_whatsapp="85999992222",
            pode_receber_checklist=False,
        )

        self.assertEqual(list(Funcionario.habilitados_para_checklist()), [habilitado])

    def test_operadores_do_sistema_filtra_ativos_e_marcados(self):
        operador = Funcionario.objects.create(
            nome="Bruna Operadora",
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            ativo=False,
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Operador",
            pode_operar_sistema=False,
        )

        self.assertEqual(list(Funcionario.operadores_do_sistema()), [operador])

    def test_form_valida_whatsapp_quando_checklist_marcado(self):
        form = FuncionarioForm(data={
            "nome": "Carlos Entregador",
            "telefone_whatsapp": "",
            "pode_receber_checklist": "on",
            "ativo": "on",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("telefone_whatsapp", form.errors)

    def test_view_cria_busca_edita_e_alterna_status(self):
        url = reverse("estoque:funcionarios")

        resposta_criar = self.client.post(url, data={
            "nome": "Ana Entregadora",
            "telefone_whatsapp": "85999990000",
            "pode_receber_checklist": "on",
            "pode_operar_sistema": "on",
            "ativo": "on",
            "observacoes": "Rota centro",
        }, secure=True)
        self.assertEqual(resposta_criar.status_code, 302)

        funcionario = Funcionario.objects.get(nome="Ana Entregadora")
        self.assertTrue(funcionario.ativo)
        self.assertTrue(funcionario.pode_receber_checklist)
        self.assertTrue(funcionario.pode_operar_sistema)
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85999990000")

        resposta_busca = self.client.get(url, {"q": "99999"}, secure=True)
        self.assertContains(resposta_busca, "Ana Entregadora")

        resposta_editar = self.client.post(url, data={
            "funcionario_id": funcionario.id,
            "nome": "Ana Silva",
            "telefone_whatsapp": "85988887777",
            "pode_receber_checklist": "on",
            "pode_operar_sistema": "on",
            "ativo": "on",
        }, secure=True)
        self.assertEqual(resposta_editar.status_code, 302)
        funcionario.refresh_from_db()
        self.assertEqual(funcionario.nome, "Ana Silva")
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85988887777")

        resposta_status = self.client.post(url, data={
            "acao": "alternar_status",
            "funcionario_id": funcionario.id,
            "ativo": "0",
        }, secure=True)
        self.assertEqual(resposta_status.status_code, 302)
        funcionario.refresh_from_db()
        self.assertFalse(funcionario.ativo)
        self.assertFalse(funcionario.pode_receber_checklist)
        self.assertFalse(funcionario.pode_operar_sistema)


class PixRecebidoTests(TestCase):
    def _produto_teste(self, nome, quantidade=None):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("1.00"),
            preco_vista=Decimal("2.00"),
            preco_prazo=Decimal("3.00"),
            quantidade=quantidade,
            permitir_prejuizo=False,
        )

    def _post_cancelar_venda(self, venda, motivo="Pedido duplicado", destino_financeiro=""):
        dados = {
            "motivo_padrao": motivo,
            "observacao_cancelamento": "",
            "confirmacao_cancelamento": "CANCELAR",
            "ciencia_cancelamento": "1",
        }
        if destino_financeiro:
            dados["destino_financeiro"] = destino_financeiro
        return self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            dados,
            secure=True,
            follow=True,
        )

    def _post_gravar_venda(self, produto, quantidade="1"):
        return self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps({
                "cliente_id": "",
                "data_venda": timezone.localdate().isoformat(),
                "data_vencimento": "",
                "tipo_pagamento": "A vista",
                "operador": "Operador Teste",
                "itens": [
                    {
                        "produto_nome": produto.nome,
                        "quantidade": quantidade,
                        "unidade": "un",
                        "preco_unitario": "2.00",
                    }
                ],
            }),
            content_type="application/json",
            secure=True,
        )

    def test_cancelamento_manual_preserva_venda_itens_total_e_registra_historico(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelamento Manual", ativo=True)
        produto = self._produto_teste("Produto Cancelamento Manual")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("42.50"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("21.25"),
            valor_total=Decimal("42.50"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.motivo_cancelamento, "Pedido duplicado")
        self.assertIsNotNone(venda.cancelada_em)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("42.50"))
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="Motivo: Pedido duplicado",
                usuario="Operador Teste",
            ).exists()
        )

    def test_cancelamento_manual_exige_confirmacao_cancelar(self):
        cliente = Cliente.objects.create(nome="Cliente Confirmacao Errada", ativo=True)
        produto = self._produto_teste("Produto Confirmacao Errada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            {
                "motivo_padrao": "Pedido duplicado",
                "observacao_cancelamento": "",
                "confirmacao_cancelamento": "CANCELA",
                "ciencia_cancelamento": "1",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertIsNone(venda.cancelada_em)
        self.assertEqual(venda.motivo_cancelamento, "")
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").exists())
        self.assertContains(resposta, "Digite CANCELAR exatamente")

    def test_cancelamento_manual_exige_ciencia_de_preservacao_historica(self):
        cliente = Cliente.objects.create(nome="Cliente Ciencia Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Ciencia Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("18.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            {
                "motivo_padrao": "Pedido duplicado",
                "observacao_cancelamento": "",
                "confirmacao_cancelamento": "CANCELAR",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").exists())
        self.assertContains(resposta, "Marque a ciencia")

    def test_cancelamento_manual_de_venda_ja_cancelada_nao_registra_novo_cancelamento(self):
        cliente = Cliente.objects.create(nome="Cliente Ja Cancelada", ativo=True)
        produto = self._produto_teste("Produto Ja Cancelada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("32.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
            motivo_cancelamento="Cancelamento anterior",
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("32.00"),
            valor_total=Decimal("32.00"),
        )
        EventoVenda.objects.create(
            venda=venda,
            tipo_evento="venda_cancelada",
            descricao="Cancelamento anterior.",
            canal="sistema",
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.motivo_cancelamento, "Cancelamento anterior")
        self.assertEqual(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").count(), 1)

    def test_cancelamento_manual_cancela_conta_aberta_sem_excluir_venda_ou_itens(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aberta Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Aberta Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("75.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("75.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("75.00"),
            valor_em_aberto=Decimal("75.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("75.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("75.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertIn("Cancelada por venda nao realizada", conta.observacao)

    def test_cancelamento_manual_preserva_conta_parcial_e_recebimentos(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Parcial Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Parcial Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(recebimento.valor, Decimal("60.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("60.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)
        self.assertIn("cancelamento da venda", credito.observacao)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="virou credito do cliente",
            ).exists()
        )

    def test_cancelamento_manual_com_recebimento_sem_destino_financeiro_bloqueia_cancelamento(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Destino Financeiro", ativo=True)
        produto = self._produto_teste("Produto Sem Destino Financeiro")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("30.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("70.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)
        self.assertEqual(conta.valor_em_aberto, Decimal("30.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Escolha o destino financeiro")

    def test_cancelamento_manual_com_devolucao_manual_ou_pendencia_nao_cria_credito(self):
        cenarios = (
            ("devolucao_manual", "devolucao manual ao cliente"),
            ("pendencia_financeira", "pendencia financeira"),
        )
        for destino, texto_evento in cenarios:
            with self.subTest(destino=destino):
                cliente = Cliente.objects.create(nome=f"Cliente {destino}", ativo=True)
                produto = self._produto_teste(f"Produto {destino}")
                venda = Venda.objects.create(
                    cliente=cliente,
                    data_venda=timezone.localdate(),
                    tipo_pagamento="A prazo",
                    total=Decimal("80.00"),
                )
                ItemVenda.objects.create(
                    venda=venda,
                    produto=produto,
                    quantidade=Decimal("1.000"),
                    unidade="un",
                    preco_unitario=Decimal("80.00"),
                    valor_total=Decimal("80.00"),
                )
                conta = ContaReceber.objects.create(
                    venda=venda,
                    cliente=cliente,
                    data_emissao=timezone.localdate(),
                    valor_original=Decimal("80.00"),
                    valor_em_aberto=Decimal("20.00"),
                    status=ContaReceber.STATUS_PARCIAL,
                )
                recebimento = RecebimentoContaReceber.objects.create(
                    conta=conta,
                    data_recebimento=timezone.localdate(),
                    valor=Decimal("60.00"),
                    forma_pagamento="PIX",
                )

                resposta = self._post_cancelar_venda(venda, destino_financeiro=destino)

                self.assertEqual(resposta.status_code, 200)
                venda.refresh_from_db()
                conta.refresh_from_db()
                recebimento.refresh_from_db()
                self.assertTrue(venda.cancelada)
                self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
                self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
                self.assertEqual(recebimento.valor, Decimal("60.00"))
                self.assertFalse(CreditoCliente.objects.filter(cliente=cliente).exists())
                self.assertIn(texto_evento, conta.observacao)
                self.assertTrue(
                    EventoVenda.objects.filter(
                        venda=venda,
                        tipo_evento="venda_cancelada",
                        descricao__icontains=texto_evento,
                    ).exists()
                )

    def test_cancelamento_manual_com_credito_sem_cliente_bloqueia_cancelamento(self):
        produto = self._produto_teste("Produto Credito Sem Cliente")
        venda = Venda.objects.create(
            cliente=None,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=None,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("50.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("40.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)
        self.assertEqual(conta.valor_em_aberto, Decimal("10.00"))
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Nao e possivel gerar credito")

    def test_cancelamento_manual_preserva_conta_paga_e_recebimentos(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Paga Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Paga Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("90.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("90.00"),
            valor_total=Decimal("90.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("90.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("90.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("90.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(recebimento.valor, Decimal("90.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("90.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="virou credito do cliente",
            ).exists()
        )

    def test_cancelamento_manual_move_venda_para_consulta_de_canceladas(self):
        cliente = Cliente.objects.create(nome="Cliente Consulta Cancelada Manual", ativo=True)
        produto = self._produto_teste("Produto Consulta Cancelada Manual")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("30.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )

        self._post_cancelar_venda(venda)

        resposta_ativas = self.client.get(reverse("estoque:consultar_vendas"), secure=True)
        self.assertNotContains(resposta_ativas, "Cliente Consulta Cancelada Manual")
        resposta_canceladas = self.client.get(reverse("estoque:consultar_vendas_canceladas"), secure=True)
        self.assertContains(resposta_canceladas, "Cliente Consulta Cancelada Manual")

    def test_gravar_venda_baixa_estoque(self):
        produto = self._produto_teste("Produto Baixa Estoque Venda", quantidade=5)

        resposta = self._post_gravar_venda(produto, quantidade="2")

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        venda = Venda.objects.get(pk=resposta.json()["venda_id"])
        self.assertEqual(venda.itens.get().quantidade, Decimal("2.000"))
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_gravada",
                descricao__icontains="Estoque baixado",
            ).exists()
        )

    def test_gravar_venda_bloqueia_estoque_insuficiente(self):
        produto = self._produto_teste("Produto Estoque Insuficiente Venda", quantidade=1)

        resposta = self._post_gravar_venda(produto, quantidade="2")

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["sucesso"])
        self.assertIn("Estoque insuficiente", resposta.json()["mensagem"])
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 1)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)

    def test_adicionar_item_na_nota_baixa_estoque(self):
        cliente = Cliente.objects.create(nome="Cliente Adicao Estoque", ativo=True)
        produto = self._produto_teste("Produto Adicao Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("0.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            data={
                "produto_id": str(produto.id),
                "quantidade": "2",
                "preco_unitario": "2.00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        self.assertEqual(venda.total, Decimal("4.00"))
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto, quantidade=Decimal("2.000")).exists())

    def test_remover_item_da_nota_devolve_estoque_do_item(self):
        cliente = Cliente.objects.create(nome="Cliente Remove Estoque", ativo=True)
        produto = self._produto_teste("Produto Remove Estoque", quantidade=3)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        self.assertEqual(produto.quantidade, 5)
        self.assertTrue(remocao.estoque_devolvido)
        self.assertIsNotNone(remocao.estoque_devolvido_em)
        self.assertFalse(ItemVenda.objects.filter(pk=item.pk).exists())
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="item_removido_da_nota",
                descricao__icontains="Estoque devolvido",
            ).exists()
        )

    def test_desfazer_remocao_baixa_estoque_novamente(self):
        cliente = Cliente.objects.create(nome="Cliente Desfaz Estoque", ativo=True)
        produto = self._produto_teste("Produto Desfaz Estoque", quantidade=3)
        produto_extra = self._produto_teste("Produto Extra Desfaz Estoque", quantidade=4)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("6.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_extra,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("2.00"),
        )
        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item.id}),
            secure=True,
            follow=True,
        )
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)
        remocao = ItemVendaRemovido.objects.get(venda=venda)

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            data={"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        remocao.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertFalse(remocao.estoque_devolvido)
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto, quantidade=Decimal("2.000")).exists())

    def test_desfazer_remocao_bloqueia_estoque_insuficiente_sem_alterar_nota(self):
        cliente = Cliente.objects.create(nome="Cliente Desfaz Sem Estoque", ativo=True)
        produto = self._produto_teste("Produto Desfaz Sem Estoque", quantidade=0)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("0.00"),
        )
        remocao = ItemVendaRemovido.objects.create(
            venda=venda,
            produto=produto,
            produto_nome_snapshot=produto.nome,
            quantidade_snapshot=Decimal("2.000"),
            unidade_snapshot="un",
            preco_unitario_snapshot=Decimal("2.00"),
            valor_total_snapshot=Decimal("4.00"),
            status=ItemVendaRemovido.STATUS_REMOVIDO,
            estoque_devolvido=True,
            estoque_devolvido_em=timezone.now(),
        )

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            data={"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        remocao.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 0)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)
        self.assertTrue(remocao.estoque_devolvido)
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertContains(resposta, "Estoque insuficiente")

    def test_cancelamento_manual_devolve_estoque_dos_itens_ativos(self):
        cliente = Cliente.objects.create(nome="Cliente Cancela Estoque", ativo=True)
        produto = self._produto_teste("Produto Cancela Estoque", quantidade=2)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("6.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("6.00"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)
        self.assertTrue(venda.cancelada)
        self.assertTrue(venda.estoque_devolvido_cancelamento)
        self.assertIsNotNone(venda.estoque_devolvido_cancelamento_em)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="Estoque devolvido dos itens ativos",
            ).exists()
        )

    def test_cancelamento_manual_nao_devolve_item_ja_removido_duas_vezes(self):
        cliente = Cliente.objects.create(nome="Cliente Cancela Item Removido Estoque", ativo=True)
        produto_ativo = self._produto_teste("Produto Ativo Cancelamento Estoque", quantidade=8)
        produto_removido = self._produto_teste("Produto Removido Cancelamento Estoque", quantidade=7)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_ativo,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        ItemVendaRemovido.objects.create(
            venda=venda,
            produto=produto_removido,
            produto_nome_snapshot=produto_removido.nome,
            quantidade_snapshot=Decimal("3.000"),
            unidade_snapshot="un",
            preco_unitario_snapshot=Decimal("2.00"),
            valor_total_snapshot=Decimal("6.00"),
            status=ItemVendaRemovido.STATUS_REMOVIDO,
            estoque_devolvido=True,
            estoque_devolvido_em=timezone.now(),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto_ativo.refresh_from_db()
        produto_removido.refresh_from_db()
        self.assertEqual(produto_ativo.quantidade, 10)
        self.assertEqual(produto_removido.quantidade, 7)

    def test_cancelamento_manual_com_credito_cliente_cria_credito_e_devolve_estoque(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Estoque", ativo=True)
        produto = self._produto_teste("Produto Credito Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("4.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("4.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        conta.refresh_from_db()
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(produto.quantidade, 7)
        self.assertTrue(venda.cancelada)
        self.assertTrue(venda.estoque_devolvido_cancelamento)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(credito.valor, Decimal("4.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)

    def test_venda_ja_cancelada_nao_devolve_estoque_novamente(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelada Sem Dobrar Estoque", ativo=True)
        produto = self._produto_teste("Produto Cancelada Sem Dobrar Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
            motivo_cancelamento="Cancelamento anterior",
            estoque_devolvido_cancelamento=True,
            estoque_devolvido_cancelamento_em=timezone.now(),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)

    def test_historico_cliente_produto_retorna_ultima_venda_ativa(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Produto", ativo=True)
        produto = self._produto_teste("Produto Historico Ultima Compra", quantidade=10)
        venda_antiga = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=3),
            tipo_pagamento="A prazo",
            total=Decimal("8.00"),
        )
        ItemVenda.objects.create(
            venda=venda_antiga,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("8.00"),
            valor_total=Decimal("8.00"),
        )
        venda_recente = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=1),
            tipo_pagamento="A prazo",
            total=Decimal("30.00"),
        )
        ItemVenda.objects.create(
            venda=venda_recente,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("30.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertEqual(dados["historico"]["venda_id"], venda_recente.id)
        self.assertEqual(dados["historico"]["data_venda"], venda_recente.data_venda.isoformat())
        self.assertEqual(dados["historico"]["preco_unitario"], "10.00")
        self.assertEqual(dados["historico"]["quantidade"], "3.000")
        self.assertEqual(dados["historico"]["unidade"], "un")

    def test_historico_cliente_produto_ignora_venda_cancelada(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Ignora Cancelada", ativo=True)
        produto = self._produto_teste("Produto Historico Ignora Cancelada", quantidade=10)
        venda_ativa = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=5),
            tipo_pagamento="A prazo",
            total=Decimal("12.00"),
        )
        ItemVenda.objects.create(
            venda=venda_ativa,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("6.00"),
            valor_total=Decimal("12.00"),
        )
        venda_cancelada = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("40.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
        )
        ItemVenda.objects.create(
            venda=venda_cancelada,
            produto=produto,
            quantidade=Decimal("4.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("40.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["historico"]["venda_id"], venda_ativa.id)

    def test_historico_cliente_produto_ignora_outro_cliente(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Alvo", ativo=True)
        outro_cliente = Cliente.objects.create(nome="Cliente Historico Outro", ativo=True)
        produto = self._produto_teste("Produto Historico Outro Cliente", quantidade=10)
        Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=4),
            tipo_pagamento="A prazo",
            total=Decimal("5.00"),
        )
        venda_outro_cliente = Venda.objects.create(
            cliente=outro_cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("20.00"),
        )
        ItemVenda.objects.create(
            venda=venda_outro_cliente,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.json()["historico"])

    def test_historico_cliente_produto_sem_compra_retorna_vazio(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Historico Produto", ativo=True)
        produto = self._produto_teste("Produto Nunca Comprado", quantidade=10)

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        self.assertIsNone(resposta.json()["historico"])

    def test_tela_vendas_carrega_com_bloco_de_historico(self):
        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "historicoProdutoCliente")
        self.assertContains(resposta, reverse("estoque:vendas_cliente_produto_historico"))

    def test_venda_com_conta_paga_mostra_aviso_de_quitada_no_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente Aviso Conta Paga", ativo=True)
        produto = self._produto_teste("Produto Aviso Conta Paga")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("80.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("80.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "VENDA J&Aacute; QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertContains(resposta, "Esta nota j&aacute; possui pagamento registrado")
        self.assertContains(resposta, "Evite editar produtos ou valores")

    def test_acesso_direto_edicao_de_venda_quitada_e_bloqueado(self):
        cliente = Cliente.objects.create(nome="Cliente Aviso Recebimento", ativo=True)
        produto = self._produto_teste("Produto Aviso Recebimento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("120.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("120.00"),
            valor_total=Decimal("120.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("120.00"),
            valor_em_aberto=Decimal("70.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("50.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.get(
            reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        redirect_url = urlsplit(resposta.redirect_chain[0][0])
        self.assertEqual(
            f"{redirect_url.path}?{redirect_url.query}",
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?edicao_bloqueada=1",
        )
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertNotContains(resposta, "Editar nota - Venda")

    def test_venda_quitada_nao_mostra_botoes_de_edicao_comum_no_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente Botoes Quitada", ativo=True)
        produto = self._produto_teste("Produto Botoes Quitada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("85.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("85.00"),
            valor_total=Decimal("85.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("85.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertNotContains(resposta, reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}))
        self.assertNotContains(resposta, reverse("estoque:venda_editar_cabecalho", kwargs={"pk": venda.id}))
        self.assertNotContains(resposta, reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:preparar_whatsapp_venda", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_whatsapp_pdf", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_criar_entrega", kwargs={"pk": venda.id}))
        self.assertContains(resposta, 'id="btnImprimir"')
        self.assertEqual(
            self.client.get(reverse("estoque:preparar_whatsapp_venda", kwargs={"pk": venda.id}), secure=True).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("estoque:venda_whatsapp_pdf", kwargs={"pk": venda.id}), secure=True).status_code,
            200,
        )

    def test_acesso_direto_adicionar_produto_em_venda_quitada_e_bloqueado(self):
        cliente = Cliente.objects.create(nome="Cliente Add Quitada", ativo=True)
        produto = self._produto_teste("Produto Add Quitada")
        produto_novo = self._produto_teste("Produto Add Bloqueado")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("40.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("40.00"),
            valor_total=Decimal("40.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("40.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            {
                "produto_id": str(produto_novo.id),
                "quantidade": "1",
                "preco_unitario": "10.00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        redirect_url = urlsplit(resposta.redirect_chain[0][0])
        self.assertEqual(
            f"{redirect_url.path}?{redirect_url.query}",
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?edicao_bloqueada=1",
        )
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertFalse(ItemVenda.objects.filter(venda=venda, produto=produto_novo).exists())

    def test_venda_aberta_continua_permitindo_edicao_normal(self):
        cliente = Cliente.objects.create(nome="Cliente Aberta Editavel", ativo=True)
        produto = self._produto_teste("Produto Aberta Editavel")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("45.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("45.00"),
            valor_total=Decimal("45.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("45.00"),
            valor_em_aberto=Decimal("45.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        resposta_edicao = self.client.get(reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}))
        self.assertContains(resposta_detalhe, reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}))
        self.assertEqual(resposta_edicao.status_code, 200)
        self.assertContains(resposta_edicao, "Editar nota - Venda")

    def test_cancelamento_de_venda_paga_mostra_aviso_de_recebimentos_preservados(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelar Conta Paga Aviso", ativo=True)
        produto = self._produto_teste("Produto Cancelar Conta Paga Aviso")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("60.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("60.00"),
            valor_total=Decimal("60.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("60.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.get(reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "VENDA JA QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertContains(resposta, "Recebimentos nao serao apagados")
        self.assertContains(resposta, "historico financeiro sera preservado")
        self.assertContains(resposta, "Conta quitada / recebimentos registrados")

    def test_venda_com_conta_aberta_sem_recebimento_nao_mostra_aviso_de_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Aviso Quitada", ativo=True)
        produto = self._produto_teste("Produto Sem Aviso Quitada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("45.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("45.00"),
            valor_total=Decimal("45.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("45.00"),
            valor_em_aberto=Decimal("45.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "VENDA J&Aacute; QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertNotContains(resposta, "Esta nota j&aacute; possui pagamento registrado")

    def test_ajuste_item_venda_quitada_cria_snapshot_sem_alterar_dados_existentes(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Quitado", ativo=True)
        produto = self._produto_teste("Coca Cola Ajuste Quitado")
        produto.quantidade = 10
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Ajuste",
            total=Decimal("25.50"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("8.50"),
            valor_total=Decimal("25.50"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("25.50"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("25.50"),
            forma_pagamento="PIX",
        )

        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
            observacao="Cliente nao recebeu o item.",
        )

        venda.refresh_from_db()
        item.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("25.50"))
        self.assertEqual(conta.valor_original, Decimal("25.50"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 10)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertEqual(ajuste.venda, venda)
        self.assertEqual(ajuste.item_venda, item)
        self.assertEqual(ajuste.cliente, cliente)
        self.assertEqual(ajuste.produto, produto)
        self.assertEqual(ajuste.produto_nome_snapshot, "Coca Cola Ajuste Quitado")
        self.assertEqual(ajuste.quantidade_snapshot, Decimal("3.000"))
        self.assertEqual(ajuste.unidade_snapshot, "un")
        self.assertEqual(ajuste.preco_unitario_snapshot, Decimal("8.50"))
        self.assertEqual(ajuste.valor_total_snapshot, Decimal("25.50"))
        self.assertEqual(ajuste.diferenca_financeira, Decimal("25.50"))
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertEqual(ajuste.operador, "Operador Ajuste")

    def test_ajuste_item_venda_quitada_nao_permite_item_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Item Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Item Outra Venda")
        venda_quitada = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("10.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("10.00"),
        )
        item_outra_venda = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )

        with self.assertRaisesMessage(ValueError, "O item informado nao pertence a venda do ajuste."):
            views.criar_ajuste_item_venda_quitada(
                venda_quitada,
                item_outra_venda,
                AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
            )

        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_ajuste_item_venda_quitada_exige_venda_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Venda Aberta", ativo=True)
        produto = self._produto_teste("Produto Ajuste Venda Aberta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("10.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("10.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        with self.assertRaisesMessage(ValueError, "Ajuste de item quitado permitido apenas para venda quitada."):
            views.criar_ajuste_item_venda_quitada(
                venda,
                item,
                AjusteItemVendaQuitada.MOTIVO_PRODUTO_FALTOU,
            )

        venda.refresh_from_db()
        self.assertEqual(venda.total, Decimal("10.00"))
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_detalhe_venda_quitada_mostra_entrada_para_ajuste_de_item(self):
        cliente = Cliente.objects.create(nome="Cliente Entrada Ajuste", ativo=True)
        produto = self._produto_teste("Produto Entrada Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "Resolver item nao entregue/nao aceito")
        self.assertContains(resposta, reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}))

    def test_detalhe_venda_aberta_nao_mostra_entrada_para_ajuste_de_item(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Entrada Ajuste", ativo=True)
        produto = self._produto_teste("Produto Sem Entrada Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("18.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertNotContains(resposta, "Resolver item nao entregue/nao aceito")
        self.assertNotContains(resposta, reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}))

    def test_detalhe_venda_com_ajuste_pendente_mostra_bloco_sem_alterar_dados(self):
        cliente = Cliente.objects.create(nome="Cliente Bloco Ajuste", ativo=True)
        produto = self._produto_teste("Produto Bloco Ajuste")
        produto.quantidade = 9
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("24.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("24.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("24.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("24.00"),
            forma_pagamento="PIX",
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "AJUSTE PENDENTE EM VENDA QUITADA")
        self.assertContains(resposta, "Existe item registrado como n&atilde;o entregue/n&atilde;o aceito")
        self.assertContains(resposta, "Produto Bloco Ajuste")
        self.assertContains(resposta, "2.000 un")
        self.assertContains(resposta, "R$ 24.00")
        self.assertContains(resposta, "Motivo: Item nao entregue")
        self.assertContains(resposta, "Resolu&ccedil;&atilde;o: Nao definida")
        self.assertContains(resposta, "Status: Pendente")
        self.assertContains(resposta, "Resolver como cr&eacute;dito do cliente")
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertEqual(venda.total, Decimal("24.00"))
        self.assertEqual(conta.valor_original, Decimal("24.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 9)

    def test_detalhe_venda_sem_ajuste_pendente_nao_mostra_bloco(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Bloco Ajuste", ativo=True)
        produto = self._produto_teste("Produto Sem Bloco Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("24.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("24.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("24.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertNotContains(resposta, "AJUSTE PENDENTE EM VENDA QUITADA")
        self.assertNotContains(resposta, "Existe item registrado como n&atilde;o entregue/n&atilde;o aceito")

    def test_detalhe_separa_item_ajustado_da_tabela_principal_sem_alterar_dados(self):
        cliente = Cliente.objects.create(nome="Cliente Separa Ajuste", ativo=True)
        produto_normal = self._produto_teste("Produto Normal Separa")
        produto_ajustado = self._produto_teste("Produto Ajustado Separa")
        produto_ajustado.quantidade = 8
        produto_ajustado.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("70.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_normal,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("40.00"),
            valor_total=Decimal("40.00"),
        )
        item_ajustado = ItemVenda.objects.create(
            venda=venda,
            produto=produto_ajustado,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("70.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("70.00"),
            forma_pagamento="PIX",
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item_ajustado,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertIn("Produto Normal Separa", tabela_principal)
        self.assertNotIn("Produto Ajustado Separa", tabela_principal)
        self.assertContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertContains(resposta, "Produto Ajustado Separa")
        self.assertContains(resposta, "2.000")
        self.assertContains(resposta, "R$ 30.00")
        self.assertContains(resposta, "Resolu&ccedil;&atilde;o financeira pendente")
        self.assertContains(resposta, "Total original preservado")
        self.assertContains(resposta, "R$ 70.00")
        self.assertContains(resposta, "Itens n&atilde;o entregues/n&atilde;o aceitos")
        self.assertContains(resposta, "R$ 30.00")
        self.assertContains(resposta, "Total ajustado/entregue")
        self.assertContains(resposta, "R$ 40.00")
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto_ajustado.refresh_from_db()
        self.assertEqual(venda.total, Decimal("70.00"))
        self.assertEqual(conta.valor_original, Decimal("70.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto_ajustado.quantidade, 8)
        self.assertTrue(ItemVenda.objects.filter(pk=item_ajustado.pk, venda=venda).exists())

    def test_detalhe_item_resolvido_com_credito_mostra_credito_na_secao_ajustada(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Credito Secao", ativo=True)
        produto = self._produto_teste("Produto Ajuste Credito Secao")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("22.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("22.00"),
            valor_total=Decimal("22.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )
        ajuste.status = AjusteItemVendaQuitada.STATUS_RESOLVIDO
        ajuste.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE
        ajuste.save(update_fields=["status", "resolucao_financeira", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertNotIn("Produto Ajuste Credito Secao", tabela_principal)
        self.assertContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertContains(resposta, "Cr&eacute;dito gerado para o cliente: R$ 22.00")
        self.assertContains(resposta, "Total ajustado/entregue")
        self.assertContains(resposta, "R$ 0.00")

    def test_detalhe_venda_sem_ajuste_mantem_item_na_tabela_principal_e_sem_secao_ajustada(self):
        cliente = Cliente.objects.create(nome="Cliente Layout Sem Ajuste", ativo=True)
        produto = self._produto_teste("Produto Layout Sem Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertIn("Produto Layout Sem Ajuste", tabela_principal)
        self.assertNotContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertNotContains(resposta, "Total ajustado/entregue")

    def test_get_confirmacao_credito_mostra_dados_do_ajuste(self):
        cliente = Cliente.objects.create(nome="Cliente GET Credito", ativo=True)
        produto = self._produto_teste("Produto GET Credito")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("15.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("15.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )

        resposta = self.client.get(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Resolver ajuste como crédito do cliente")
        self.assertContains(resposta, "Venda #")
        self.assertContains(resposta, "Cliente Get Credito")
        self.assertContains(resposta, "Produto Get Credito")
        self.assertContains(resposta, "1.000 un")
        self.assertContains(resposta, "R$ 15.00")
        self.assertContains(resposta, "Item, venda, conta a receber e recebimentos não serão apagados")

    def test_post_credito_sem_confirmacao_forte_nao_gera_credito(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Sem Confirmacao", ativo=True)
        produto = self._produto_teste("Produto Credito Sem Confirmacao")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("15.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("15.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CRED", "ciencia_credito": "1"},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Digite CREDITO exatamente")
        ajuste.refresh_from_db()
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)

    def test_post_credito_resolve_ajuste_sem_alterar_venda_financeiro_estoque_ou_item(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Resolve", ativo=True)
        produto = self._produto_teste("Produto Credito Resolve")
        produto.quantidade = 11
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Credito",
            total=Decimal("28.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("14.00"),
            valor_total=Decimal("28.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("28.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("28.00"),
            forma_pagamento="PIX",
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_PRODUTO_FALTOU,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?credito_resolvido=1",
            fetch_redirect_response=False,
        )
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("28.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertIn(f"ajuste #{ajuste.id}", credito.observacao)
        ajuste.refresh_from_db()
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_RESOLVIDO)
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="ajuste_item_quitado_resolvido_credito",
                descricao__icontains="credito do cliente",
            ).exists()
        )
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("28.00"))
        self.assertEqual(conta.valor_original, Decimal("28.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 11)

    def test_post_credito_nao_permite_resolver_duas_vezes(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Duplo", ativo=True)
        produto = self._produto_teste("Produto Credito Duplo")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("19.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("19.00"),
            valor_total=Decimal("19.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        url = reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id})

        primeira = self.client.post(
            url,
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )
        segunda = self.client.post(
            url,
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertEqual(primeira.status_code, 302)
        self.assertRedirects(
            segunda,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?credito_bloqueado=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(CreditoCliente.objects.filter(cliente=cliente).count(), 1)

    def test_post_credito_nao_resolve_ajuste_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Credito Outra Venda")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("12.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("12.00"),
        )
        item = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("12.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            outra_venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        ajuste.refresh_from_db()
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)

    def test_tela_ajuste_item_quitado_so_permite_venda_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente GET Ajuste Aberto", ativo=True)
        produto = self._produto_teste("Produto GET Ajuste Aberto")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("18.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?ajuste_bloqueado=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_cria_auditoria_evento_sem_alterar_financeiro_estoque_ou_item(self):
        cliente = Cliente.objects.create(nome="Cliente Fluxo Ajuste", ativo=True)
        produto = self._produto_teste("Produto Fluxo Ajuste")
        produto.quantidade = 7
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Fluxo",
            total=Decimal("32.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("16.00"),
            valor_total=Decimal("32.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("32.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("32.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
                "observacao": "Nao saiu na entrega.",
            },
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?ajuste_registrado=1",
            fetch_redirect_response=False,
        )
        ajuste = AjusteItemVendaQuitada.objects.get(venda=venda, item_venda=item)
        self.assertEqual(ajuste.produto_nome_snapshot, "Produto Fluxo Ajuste")
        self.assertEqual(ajuste.quantidade_snapshot, Decimal("2.000"))
        self.assertEqual(ajuste.unidade_snapshot, "un")
        self.assertEqual(ajuste.preco_unitario_snapshot, Decimal("16.00"))
        self.assertEqual(ajuste.valor_total_snapshot, Decimal("32.00"))
        self.assertEqual(ajuste.diferenca_financeira, Decimal("32.00"))
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="ajuste_item_quitado_registrado",
                descricao__icontains="Resolucao financeira pendente",
            ).exists()
        )
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("32.00"))
        self.assertEqual(conta.valor_original, Decimal("32.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 7)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        remocao = ItemVendaRemovido.objects.get(venda=venda, ajuste_origem=ajuste)
        self.assertEqual(remocao.produto_nome_snapshot, "Produto Fluxo Ajuste")
        self.assertEqual(remocao.valor_total_snapshot, Decimal("32.00"))
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(detalhe, reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}))

    def test_post_ajuste_item_quitado_bloqueia_item_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Bloqueia Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Bloqueia Outra Venda")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item_outra_venda = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item_outra_venda.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Selecione um item valido desta venda.")
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_exige_observacao_para_motivo_outro(self):
        cliente = Cliente.objects.create(nome="Cliente Motivo Outro", ativo=True)
        produto = self._produto_teste("Produto Motivo Outro")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_OUTRO,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe uma observacao quando o motivo for outro.")
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_evita_duplicidade_pendente_do_mesmo_item(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Ajuste", ativo=True)
        produto = self._produto_teste("Produto Duplicidade Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda, item_venda=item).count(), 1)

    def test_post_ajuste_item_quitado_bloqueia_duplicidade_resolvida_com_credito(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Resolvida", ativo=True)
        produto = self._produto_teste("Produto Duplicidade Resolvida")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("67.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("67.00"),
            observacao="Credito do ajuste.",
        )
        ajuste.status = AjusteItemVendaQuitada.STATUS_RESOLVIDO
        ajuste.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE
        ajuste.save(update_fields=["status", "resolucao_financeira", "atualizado_em"])

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda, item_venda=item).count(), 1)

    def test_post_ajuste_item_quitado_bloqueia_mesmo_produto_da_mesma_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Produto", ativo=True)
        produto = self._produto_teste("Produto Mesmo Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("40.00"),
        )
        item_1 = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        item_2 = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item_1,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item_2.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda).count(), 1)

    def test_detalhe_ajuste_antigo_sem_snapshot_mostra_mensagem_sem_quebrar(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Antigo", ativo=True)
        produto = self._produto_teste("Produto Ajuste Antigo")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("25.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("25.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(
            resposta,
            "Este ajuste foi criado antes do controle de reversão automática. Não é possível desfazer automaticamente.",
        )
        self.assertNotContains(resposta, "Reverse for &#x27;venda_desfazer_remocao_item&#x27;")

    def test_detalhe_nao_duplica_total_de_ajustes_repetidos_do_mesmo_produto(self):
        cliente = Cliente.objects.create(nome="Cliente Total Duplicado", ativo=True)
        produto = self._produto_teste("Coca Cola Total Duplicado")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("290.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "Itens n&atilde;o entregues/n&atilde;o aceitos")
        self.assertContains(resposta, "R$ 67.00")
        self.assertNotContains(resposta, "R$ 134.00")

    def test_remover_item_da_nota_resolve_pendencia_de_entrega_do_item(self):
        cliente = Cliente.objects.create(nome="Cliente Entrega", ativo=True)
        produto_entregue = self._produto_teste("Agua Teste")
        produto_pendente = self._produto_teste("Coca Cola 2L Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("25.00"),
        )
        item_entregue = ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("5.000"),
            unidade="pct",
            preco_unitario=Decimal("3.00"),
            valor_total=Decimal("15.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_entregue,
            carregado=True,
            entregue=True,
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        pendencias_antes = views.listar_pendencias_entrega()
        self.assertTrue(any(pendencia["item_venda_id"] == item_pendente.id for pendencia in pendencias_antes))

        revisao_url = reverse(
            "estoque:revisar_remocao_pendencia_da_nota",
            kwargs={"checklist_id": checklist_pendente.id},
        )
        resposta_revisao = self.client.get(revisao_url, secure=True)
        self.assertContains(resposta_revisao, "Agua Teste")
        self.assertContains(resposta_revisao, "Coca Cola 2L Teste")
        self.assertContains(resposta_revisao, "Sera removido")
        self.assertContains(resposta_revisao, "Permanece na nota")

        resposta = self.client.post(
            revisao_url,
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Item removido da nota e pendencia resolvida com sucesso.")
        self.assertFalse(ItemVenda.objects.filter(pk=item_pendente.id).exists())
        item_rota.refresh_from_db()
        self.assertEqual(item_rota.status, EntregaRotaItem.STATUS_ENTREGUE)
        self.assertTrue(item_rota.entrega_concluida)
        evento_historico = EventoVenda.objects.get(
            venda=venda,
            tipo_evento="pendencia_removida_da_nota",
        )
        self.assertIn("Pendencia resolvida por resolucao de pendencia de entrega", evento_historico.descricao)
        self.assertIn("removido 5 pct de Coca Cola 2L Teste da nota", evento_historico.descricao)
        self.assertIn("motivo: item nao entregue", evento_historico.descricao)
        self.assertIn("Total alterado de R$ 25,00 para R$ 10,00", evento_historico.descricao)
        pendencias_depois = views.listar_pendencias_entrega()
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in pendencias_depois))

        resposta_abertas = self.client.get(reverse("estoque:pendencias_entrega"), secure=True)
        self.assertContains(resposta_abertas, "Ver pendencias resolvidas")
        self.assertNotContains(resposta_abertas, "Coca Cola 2L Teste")
        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Ver pendencias em aberto")
        self.assertContains(resposta_resolvidas, "Coca Cola 2L Teste")
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda continuou ativa")
        self.assertContains(resposta_resolvidas, "Abrir nota")
        self.assertContains(
            resposta_resolvidas,
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas&evento={evento_historico.id}',
        )

        resposta_nota_resolvida = self.client.get(
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas&evento={evento_historico.id}',
            secure=True,
        )
        self.assertContains(resposta_nota_resolvida, "Pendencia de entrega resolvida.")
        self.assertContains(resposta_nota_resolvida, "5 pct de Coca Cola 2L Teste")
        self.assertContains(resposta_nota_resolvida, "item nao entregue removido da nota")
        self.assertContains(resposta_nota_resolvida, "Total alterado de R$ 25,00 para R$ 10,00")
        self.assertContains(resposta_nota_resolvida, "A venda continuou ativa com os itens restantes.")
        self.assertContains(resposta_nota_resolvida, "Voltar para pendencias resolvidas")
        self.assertNotContains(resposta_nota_resolvida, "Editar nota")
        self.assertNotContains(resposta_nota_resolvida, "Cancelar venda")
        self.assertNotContains(resposta_nota_resolvida, "Imagem / WhatsApp")
        self.assertNotContains(resposta_nota_resolvida, ">PDF</a>")
        self.assertNotContains(resposta_nota_resolvida, "Imprimir</button>")
        self.assertNotContains(resposta_nota_resolvida, "Entrega / checklist")

    def test_remover_item_pela_edicao_da_nota_lista_pendencia_resolvida(self):
        cliente = Cliente.objects.create(nome="Cliente Edicao", ativo=True)
        produto_entregue = self._produto_teste("Agua Edicao Teste")
        produto_pendente = self._produto_teste("Guarana Pendente Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("30.00"),
        )
        item_entregue = ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("12.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("6.000"),
            unidade="un",
            preco_unitario=Decimal("3.00"),
            valor_total=Decimal("18.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_entregue,
            carregado=True,
            entregue=True,
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        pendencias_antes = views.listar_pendencias_entrega()
        self.assertTrue(any(pendencia["item_venda_id"] == item_pendente.id for pendencia in pendencias_antes))

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_pendente.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(ItemVenda.objects.filter(pk=item_pendente.id).exists())
        pendencias_depois = views.listar_pendencias_entrega()
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in pendencias_depois))

        resolvidas = views.listar_pendencias_resolvidas_entrega()
        resolvidas_item = [
            pendencia
            for pendencia in resolvidas
            if pendencia["venda"].id == venda.id and pendencia["produto"] == "Guarana Pendente Teste"
        ]
        self.assertEqual(len(resolvidas_item), 1)
        self.assertEqual(resolvidas_item[0]["resolucao"], "Resolvida removendo item da nota pela edicao da venda")

        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Guarana Pendente Teste")
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda continuou ativa")
        self.assertContains(
            resposta_resolvidas,
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas',
        )

    def test_remover_item_por_pendencia_atualiza_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Pendencia Conta Aberta", ativo=True)
        produto_entregue = self._produto_teste("Agua Pendencia Conta")
        produto_pendente = self._produto_teste("Refri Pendencia Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse(
                "estoque:revisar_remocao_pendencia_da_nota",
                kwargs={"checklist_id": checklist_pendente.id},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("70.00"))
        self.assertEqual(conta.valor_original, Decimal("70.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("70.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_remover_ultimo_item_por_pendencia_cancela_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Pendencia Conta Cancelada", ativo=True)
        produto_pendente = self._produto_teste("Produto Pendencia Cancela Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("50.00"),
            valor_em_aberto=Decimal("50.00"),
            status=ContaReceber.STATUS_ABERTA,
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse(
                "estoque:revisar_remocao_pendencia_da_nota",
                kwargs={"checklist_id": checklist_pendente.id},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertEqual(conta.valor_original, Decimal("50.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertIn("Cancelada por venda nao realizada", conta.observacao)

    def test_remover_ultimo_item_por_pendencia_anula_venda_sem_itens(self):
        cliente = Cliente.objects.create(nome="Cliente Venda Anulada Pendencia", ativo=True)
        produto_pendente = self._produto_teste("Skol 24/600ml Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("2.000"),
            unidade="CX",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("50.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_pendente.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(ItemVenda.objects.filter(venda=venda).exists())
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertTrue(venda.cancelada)
        self.assertIsNotNone(venda.cancelada_em)
        self.assertIn("Remocao de pendencia deixou a nota sem itens", venda.motivo_cancelamento)
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in views.listar_pendencias_entrega()))

        resolvidas = views.listar_pendencias_resolvidas_entrega()
        self.assertTrue(
            any(
                pendencia["venda"].id == venda.id
                and pendencia["produto"] == produto_pendente.nome
                and pendencia["resolucao"] == "Resolvida removendo item da nota pela edicao da venda"
                and pendencia["resumo_resolucao"] == "Item removido da nota - venda anulada porque ficou sem itens"
                for pendencia in resolvidas
            )
        )
        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda anulada porque ficou sem itens")
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_anulada_sem_itens_por_pendencia",
                descricao__icontains="Venda anulada porque a remocao da pendencia deixou a nota sem itens",
            ).exists()
        )

        resposta_ativas = self.client.get(reverse("estoque:consultar_vendas"), secure=True)
        self.assertNotContains(resposta_ativas, "Cliente Venda Anulada Pendencia")
        resposta_canceladas = self.client.get(reverse("estoque:consultar_vendas_canceladas"), secure=True)
        self.assertContains(resposta_canceladas, "Cliente Venda Anulada Pendencia")

    def test_filtros_de_pendencias_resolvidas(self):
        cliente_lincoln = Cliente.objects.create(nome="Lincoln Cliente", ativo=True)
        cliente_camila = Cliente.objects.create(nome="Camila Cliente", ativo=True)
        produto_aberto = self._produto_teste("Produto Aberto Teste")
        produto_coca = self._produto_teste("Coca Filtro Teste")
        produto_fanta = self._produto_teste("Fanta Filtro Teste")
        venda_lincoln = Venda.objects.create(
            cliente=cliente_lincoln,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("10.00"),
        )
        venda_camila = Venda.objects.create(
            cliente=cliente_camila,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("12.00"),
        )
        venda_aberta = Venda.objects.create(
            cliente=cliente_lincoln,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("5.00"),
        )
        item_aberto = ItemVenda.objects.create(
            venda=venda_aberta,
            produto=produto_aberto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("5.00"),
            valor_total=Decimal("5.00"),
        )
        rota_aberta = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota_aberto = EntregaRotaItem.objects.create(
            rota=rota_aberta,
            venda=venda_aberta,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota_aberto,
            item_venda=item_aberto,
            carregado=False,
            entregue=False,
        )

        rota_lincoln = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        rota_camila = EntregaRota.objects.create(data=timezone.localdate() - timedelta(days=2), tipo=EntregaRota.TIPO_UNITARIA)
        evento_lincoln = EventoVenda.objects.create(
            venda=venda_lincoln,
            tipo_evento="pendencia_removida_da_nota",
            descricao=(
                f"Pendencia da rota #{rota_lincoln.id} resolvida por remocao da nota. "
                "Item removido: Coca Filtro Teste - 2.000 un (R$ 10.00). Novo total: R$ 0.00."
            ),
            canal="sistema",
        )
        evento_camila = EventoVenda.objects.create(
            venda=venda_camila,
            tipo_evento="pendencia_removida_da_nota",
            descricao=(
                f"Pendencia da rota #{rota_camila.id} resolvida pela edicao da nota. "
                "Item removido: Fanta Filtro Teste - 3.000 un (R$ 12.00). Novo total: R$ 0.00."
            ),
            canal="sistema",
        )
        data_lincoln = timezone.make_aware(datetime(2026, 5, 10, 9, 0))
        data_camila = timezone.make_aware(datetime(2026, 5, 12, 9, 0))
        EventoVenda.objects.filter(pk=evento_lincoln.pk).update(criado_em=data_lincoln)
        EventoVenda.objects.filter(pk=evento_camila.pk).update(criado_em=data_camila)

        resposta_sem_filtro = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_sem_filtro, "Coca Filtro Teste")
        self.assertContains(resposta_sem_filtro, "Fanta Filtro Teste")
        self.assertContains(resposta_sem_filtro, "Limpar")

        resposta_venda = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "venda": str(venda_lincoln.id)},
            secure=True,
        )
        self.assertContains(resposta_venda, "Coca Filtro Teste")
        self.assertNotContains(resposta_venda, "Fanta Filtro Teste")

        resposta_cliente = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "cliente": "Lincoln"},
            secure=True,
        )
        self.assertContains(resposta_cliente, "Coca Filtro Teste")
        self.assertNotContains(resposta_cliente, "Fanta Filtro Teste")

        resposta_produto = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "produto": "Fanta"},
            secure=True,
        )
        self.assertContains(resposta_produto, "Fanta Filtro Teste")
        self.assertNotContains(resposta_produto, "Coca Filtro Teste")

        resposta_data = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "data_inicial": "2026-05-10", "data_final": "2026-05-10"},
            secure=True,
        )
        self.assertContains(resposta_data, "Coca Filtro Teste")
        self.assertNotContains(resposta_data, "Fanta Filtro Teste")

        resposta_abertas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"cliente": "Camila", "produto": "Fanta"},
            secure=True,
        )
        self.assertContains(resposta_abertas, "Produto Aberto Teste")
        self.assertContains(resposta_abertas, "Ver pendencias resolvidas")
        self.assertNotContains(resposta_abertas, "Coca Filtro Teste")

    def test_remover_item_da_nota_atualiza_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aberta", ativo=True)
        produto_base = self._produto_teste("Produto Base Conta")
        produto_removido = self._produto_teste("Produto Removido Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("75.00"),
            valor_total=Decimal("75.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("25.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("75.00"))
        self.assertEqual(conta.valor_original, Decimal("75.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("75.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_remover_item_da_nota_atualiza_conta_receber_parcial_preservando_recebido(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Parcial", ativo=True)
        produto_base = self._produto_teste("Produto Base Parcial")
        produto_removido = self._produto_teste("Produto Removido Parcial")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertEqual(venda.total, Decimal("80.00"))
        self.assertEqual(recebimento.valor, Decimal("60.00"))
        self.assertEqual(conta.valor_original, Decimal("80.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("20.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)

    def test_adicionar_item_na_nota_atualiza_conta_receber_parcial_preservando_recebido(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aumento", ativo=True)
        produto_base = self._produto_teste("Produto Base Aumento")
        produto_novo = self._produto_teste("Produto Novo Aumento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            {
                "produto_id": str(produto_novo.id),
                "quantidade": "1",
                "preco_unitario": "30.00",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("130.00"))
        self.assertEqual(conta.valor_original, Decimal("130.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("70.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)

    def test_remover_item_de_venda_a_vista_sem_conta_receber_nao_quebra(self):
        cliente = Cliente.objects.create(nome="Cliente Vista Sem Conta", ativo=True)
        produto_base = self._produto_teste("Produto Vista Base")
        produto_removido = self._produto_teste("Produto Vista Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("50.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        self.assertEqual(venda.total, Decimal("30.00"))
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_edicao_com_total_menor_que_recebido_mantem_conta_zerada(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Quitada", ativo=True)
        produto_base = self._produto_teste("Produto Quitado Base")
        produto_removido = self._produto_teste("Produto Quitado Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("100.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("80.00"))
        self.assertEqual(conta.valor_original, Decimal("80.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)

    def test_desfazer_remocao_sem_credito_recoloca_item_e_sincroniza_conta(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Simples", ativo=True)
        produto_base = self._produto_teste("Produto Desfazer Base")
        produto_removido = self._produto_teste("Produto Desfazer Removido")
        produto_removido.quantidade = 10
        produto_removido.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto_removido.refresh_from_db()
        remocao.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto_removido, valor_total=Decimal("30.00")).exists())
        self.assertEqual(venda.total, Decimal("100.00"))
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("100.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)
        self.assertEqual(produto_removido.quantidade, 10)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="remocao_item_desfeita").exists())

    def test_desfazer_remocao_com_credito_disponivel_cancela_credito_e_recoloca_item(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Credito", ativo=True)
        produto_base = self._produto_teste("Produto Credito Base")
        produto_removido = self._produto_teste("Produto Credito Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("100.00"),
            forma_pagamento="PIX",
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        credito = CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("30.00"),
            origem_conta_receber=conta,
            observacao=f"Credito gerado pela remocao #{remocao.id}.",
        )
        remocao.credito_gerado = credito
        remocao.save(update_fields=["credito_gerado"])

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        remocao.refresh_from_db()
        credito_total = CreditoCliente.objects.filter(cliente=cliente).aggregate(total=Sum("valor")).get("total")
        self.assertEqual(credito_total, Decimal("0.00"))
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto_removido, valor_total=Decimal("30.00")).exists())
        self.assertEqual(venda.total, Decimal("100.00"))
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)

    def test_desfazer_remocao_bloqueia_quando_credito_ja_foi_usado(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Usado", ativo=True)
        produto_base = self._produto_teste("Produto Usado Base")
        produto_removido = self._produto_teste("Produto Usado Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        credito = CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("30.00"),
            origem_conta_receber=conta,
            observacao=f"Credito gerado pela remocao #{remocao.id}.",
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("-30.00"),
            origem_conta_receber=conta,
            observacao="Credito usado em abatimento.",
        )
        remocao.credito_gerado = credito
        remocao.save(update_fields=["credito_gerado"])

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(detalhe, "Este crédito já foi usado e não pode ser desfeito automaticamente nesta etapa.")
        self.assertNotContains(detalhe, reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}))

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este crédito já foi usado e não pode ser desfeito automaticamente nesta etapa.")
        remocao.refresh_from_db()
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)
        self.assertFalse(ItemVenda.objects.filter(venda=venda, produto=produto_removido).exists())

    def test_desfazer_ajuste_novo_com_credito_cancela_credito_sem_duplicar_item(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Ajuste", ativo=True)
        produto = self._produto_teste("Produto Desfazer Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Ajuste",
            total=Decimal("67.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )

        self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
                "observacao": "Nao entregue.",
            },
            secure=True,
        )
        ajuste = AjusteItemVendaQuitada.objects.get(venda=venda, item_venda=item)
        remocao = ItemVendaRemovido.objects.get(venda=venda, ajuste_origem=ajuste)

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(
            detalhe,
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
        )

        self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )
        remocao.refresh_from_db()
        self.assertIsNotNone(remocao.credito_gerado_id)

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        ajuste.refresh_from_db()
        remocao.refresh_from_db()
        credito_total = CreditoCliente.objects.filter(cliente=cliente).aggregate(total=Sum("valor")).get("total")
        self.assertEqual(credito_total, Decimal("0.00"))
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_CANCELADO)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertEqual(venda.total, Decimal("67.00"))
        self.assertEqual(ItemVenda.objects.filter(venda=venda, produto=produto).count(), 1)
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="remocao_item_desfeita").exists())

    def test_consulta_vendas_por_numero_ignora_datas_preenchidas(self):
        cliente = Cliente.objects.create(nome="Lincoln Neiva", ativo=True)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=datetime(2026, 5, 11).date(),
            tipo_pagamento="A prazo",
            total=Decimal("1043.70"),
        )
        hoje = timezone.localdate().isoformat()

        resposta = self.client.get(
            reverse("estoque:consultar_vendas"),
            {
                "data_inicial": hoje,
                "data_final": hoje,
                "cliente": "",
                "numero": str(venda.id),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"#{venda.id}")
        self.assertContains(resposta, "Lincoln Neiva")
        self.assertContains(resposta, "11/05/2026")

    def test_form_permite_cliente_opcional_e_status_pendente(self):
        form = PixRecebidoForm(data={
            "cliente": "",
            "nome_pagador": "Maria Silva",
            "valor": "50.00",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "observacao": "Pix recebido no caixa",
            "status": PixRecebido.STATUS_PENDENTE,
        })

        self.assertTrue(form.is_valid(), form.errors)
        pix = form.save()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)

    def test_central_pix_registra_lista_e_nao_altera_fluxo_financeiro(self):
        cliente = Cliente.objects.create(nome="Cliente Pix", ativo=True)
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }

        resposta_get = self.client.get(url, secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, "Central de Pix")

        resposta_post = self.client.post(url, data={
            "cliente": cliente.id,
            "nome_pagador": "Filho do cliente",
            "valor": "123.45",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "instituicao_pix": "PicPay",
            "observacao": "Conferir manualmente depois",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta_post.status_code, 200)
        self.assertContains(resposta_post, "Filho do cliente")
        self.assertContains(resposta_post, "PicPay")
        pix = PixRecebido.objects.get(nome_pagador="Filho do cliente")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(pix.instituicao_pix, "PicPay")

        contagens_depois = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        self.assertEqual(contagens_depois, contagens_antes)

    def test_central_pix_pix_novo_aparece_com_linha_destacada(self):
        PixRecebido.objects.create(
            nome_pagador="Pix novo visual",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(reverse("estoque:central_pix"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix novo visual")
        self.assertContains(resposta, "pix-row-novo")
        self.assertNotContains(resposta, "pix-novo-badge")
        self.assertNotContains(resposta, ">Novo<")
        pix = PixRecebido.objects.get(nome_pagador="Pix novo visual")
        self.assertIsNone(pix.visualizado_em)

    def test_central_pix_lista_mostra_id_e_busca_por_numero_pix(self):
        alvo = PixRecebido.objects.create(
            nome_pagador="Pix alvo busca id",
            valor="25.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        PixRecebido.objects.create(
            nome_pagador="Pix fora da busca id",
            valor="35.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix"),
            {"q": str(alvo.id)},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Nº Pix")
        self.assertContains(resposta, f"#{alvo.id}")
        self.assertContains(resposta, "Buscar por nº do Pix, pagador, cliente, instituicao, status ou data...")
        self.assertContains(resposta, "Pix alvo busca id")
        self.assertContains(resposta, "pix-status baixado")
        self.assertNotContains(resposta, "Pix fora da busca id")

    def test_central_pix_detalhe_marca_pix_como_visualizado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix abre detalhe",
            valor="35.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Pix abre detalhe")
        self.assertNotContains(resposta_lista, '<tr class="pix-row pix-row-novo">', html=True)

    def test_central_pix_marcar_visualizado_nao_altera_status_financeiro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix status preservado",
            valor="45.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_detalhe_baixado_mostra_id_status_e_sem_botao_excluir(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix detalhe baixado",
            valor="99.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Detalhe do Pix baixado - Pix #{pix.id}")
        self.assertContains(resposta, "ID do Pix")
        self.assertContains(resposta, f"#{pix.id}")
        self.assertContains(resposta, "Este Pix ja foi baixado/usado financeiramente e nao pode ser excluido.")
        self.assertContains(resposta, "pix-detail-status-baixado")
        self.assertNotContains(resposta, ">Ignorar Pix sem baixa</button>")
        self.assertNotContains(resposta, "Excluir Pix enviado errado")
        self.assertNotContains(resposta, "Se voltar sem baixar, este Pix continuara pendente na Central de Pix.")

    def test_central_pix_nao_permite_ignorar_pix_baixado_por_post_direto(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix baixado protegido",
            valor="88.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {"acao": "ignorar"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertContains(resposta, "Pix baixado/usado financeiramente nao pode ser ignorado.")

    def test_central_pix_pendente_pode_ser_excluido_com_confirmacao_forte(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Excluir", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Arquivo enviado errado",
            valor="12.34",
            status=PixRecebido.STATUS_PENDENTE,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta_get = self.client.get(url, secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, f"#{pix.id}")
        self.assertContains(resposta_get, "Cliente Pix Excluir")
        self.assertContains(resposta_get, "Arquivo enviado errado")

        resposta_post = self.client.post(
            url,
            {"confirmacao": "EXCLUIR"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta_post.status_code, 200)
        self.assertFalse(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta_post, f"Pix #{pix.id} excluido com sucesso.")

    def test_central_pix_nao_exclui_se_confirmacao_estiver_errada(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Confirmacao errada",
            valor="22.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta = self.client.post(
            url,
            {"confirmacao": "excluir"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta, "Digite exatamente EXCLUIR para confirmar a exclusao do Pix.")

    def test_central_pix_baixado_nao_pode_ser_excluido(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix ja baixado",
            valor="33.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta_get = self.client.get(url, secure=True, follow=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta_get, "Nao e possivel excluir este Pix porque ele ja tem vinculo financeiro/baixa.")

        resposta_post = self.client.post(
            url,
            {"confirmacao": "EXCLUIR"},
            secure=True,
            follow=True,
        )
        self.assertEqual(resposta_post.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())

    def test_central_pix_bloqueia_salvar_sem_cliente_confirmado(self):
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        comprovante = SimpleUploadedFile(
            "mercado-pago.jpg",
            b"comprovante pix mercado pago",
            content_type="image/jpeg",
        )

        resposta = self.client.post(url, data={
            "cliente": "",
            "nome_pagador": "",
            "valor": "645.00",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "observacao": "",
            "status": PixRecebido.STATUS_PENDENTE,
            "comprovante": comprovante,
        }, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de salvar o Pix.")
        self.assertContains(resposta, "Confirme um cliente antes de salvar o Pix.")
        self.assertContains(resposta, 'placeholder="Sem cliente identificado"')
        self.assertEqual(PixRecebido.objects.count(), 0)

        contagens_depois = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        self.assertEqual(contagens_depois, contagens_antes)

    def test_central_pix_usa_next_interno_e_ignora_next_externo(self):
        url = reverse("estoque:central_pix")

        resposta_interna = self.client.get(f"{url}?next=/vendas/", secure=True)
        self.assertContains(resposta_interna, 'href="/vendas/"')

        resposta_externa = self.client.get(f"{url}?next=https://example.com/", secure=True)
        self.assertContains(resposta_externa, f'href="{reverse("estoque:contas_receber")}"')
        self.assertNotContains(resposta_externa, "example.com")

    def test_botoes_central_pix_alertam_somente_pix_em_atencao(self):
        cliente = Cliente.objects.create(nome="Cliente Alerta Pix", ativo=True)

        resposta_sem_pix = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertNotContains(resposta_sem_pix, "(pendente)")

        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pix baixado",
            valor="10.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        resposta_baixado = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertNotContains(resposta_baixado, "(pendente)")

        PixRecebido.objects.create(
            nome_pagador="Pix pendente",
            valor="20.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resposta_contas = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertContains(resposta_contas, "Central de Pix (pendente)")
        self.assertContains(resposta_contas, "contas-btn-pix-alerta")

        resposta_vendas = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertContains(resposta_vendas, "Central de Pix (pendente)")
        self.assertContains(resposta_vendas, "pix-alerta")

        PixRecebido.objects.filter(status=PixRecebido.STATUS_PENDENTE).update(
            status=PixRecebido.STATUS_BAIXADO
        )
        PixRecebido.objects.create(
            nome_pagador="Pix nao identificado",
            valor="30.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )
        resposta_receber = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )
        self.assertContains(resposta_receber, "Central de Pix (pendente)")
        self.assertContains(resposta_receber, "rc-btn-pix-alerta")

    def test_analisar_comprovante_pix_preenche_dados_sem_confirmar_cliente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Destino\n"
            "Nome: Loja Exemplo\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Cicero Cristiano Silva Souza")
        self.assertEqual(dados["valor"], "20.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(dados["mensagem_cliente"], "")
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_pagina_envio_comprovante_pix_inclui_manifest_pwa(self):
        resposta = self.client.get(reverse("estoque:central_pix_enviar_comprovante"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'rel="manifest"')
        self.assertContains(resposta, 'href="/static/manifest.json"')
        self.assertContains(resposta, 'name="theme-color" content="#16a34a"')

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_atalho_inteligente_pix_carrega_urls_configuradas(self):
        resposta = self.client.get(reverse("estoque:pix_enviar_inteligente"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Abrindo Enviar Pix")
        self.assertContains(resposta, "Tentando conexao local pelo Wi-Fi")
        self.assertContains(resposta, "http://10.0.0.154:8000/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "fetch(localUrl")
        self.assertContains(resposta, "window.location.replace(onlineUrl)")

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_local(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_online(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    @override_settings(ALLOWED_HOSTS=["example.com"])
    def test_pagina_envio_comprovante_pix_mostra_ambiente_nao_identificado(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="example.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "AMBIENTE NÃO IDENTIFICADO")

    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_local(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_online(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    def test_rota_antiga_envio_pix_post_sem_arquivo_mantem_selo_local(self):
        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"enviado_por": "Lincoln"},
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")
        self.assertContains(resposta, "Escolha uma imagem ou arquivo de comprovante Pix.")

    def test_rota_antiga_envio_pix_sucesso_mantem_selo_online(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        self.assertContains(resposta, "ONLINE / Render")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")

    def test_enviar_comprovante_pix_cria_registro_pendente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        arquivo = SimpleUploadedFile(
            "comprovante.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cicero Cristiano Silva Souza\n"
                "Valor R$ 20,00\n"
                "Data 16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        pix = PixRecebido.objects.get()
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
        self.assertContains(resposta, "Comprovante enviado")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(resposta, "Confira depois no computador antes de baixar qualquer conta.")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix_enviar_comprovante")}"')
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta, "Enviar outro comprovante")
        self.assertIsNone(pix.cliente)
        self.assertIsNone(pix.cliente_sugerido)
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(pix.enviado_por_nome, "Lincoln")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)
        self.assertTrue(pix.comprovante)
        self.assertContains(resposta, "OCR pendente / Conferencia pendente")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Ler comprovante (OCR)")

    def test_enviar_comprovante_pix_erro_storage_nao_derruba_pagina(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.PixRecebido.objects.create", side_effect=RuntimeError("falha storage")):
            with self.assertLogs("estoque.views", level="ERROR") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_enviar_comprovante"),
                    {"comprovante": arquivo, "enviado_por": "Lincoln"},
                    secure=True,
                )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta,
            "Erro ao salvar comprovante Pix. Verifique a configuração do armazenamento online.",
        )
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertIn("diagnostico_storage", "\n".join(logs.output))
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_enviar_comprovante_pix_sem_cliente_sugerido_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-cliente.txt",
            (
                "Mercado Pago\n"
                "Comprovante de Pix\n"
                "16/maio/2026 as 16:33:29\n"
                "R$ 600\n"
                "@ De\n"
                "Ivanildo Ferraz Patricio Junior\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

    def test_enviar_comprovante_pix_com_outro_salva_nome_informado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-outro.txt",
            (
                "Comprovante Pix\n"
                "Nome: Cliente Sem Cadastro\n"
                "Valor R$ 42,00\n"
                "Data 16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {
                "comprovante": arquivo,
                "enviado_por": "Outro",
                "enviado_por_outro": "  Ana Caixa  ",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
        self.assertEqual(pix.enviado_por_nome, "Ana Caixa")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Ana Caixa")
        self.assertContains(resposta, "Enviar outro comprovante")

    def test_enviar_comprovante_pix_sem_ocr_completo_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-ocr.txt",
            b"texto sem dados de pix",
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo, "enviado_por": "Roseli"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(
            resposta,
            "Confira depois no computador antes de baixar qualquer conta.",
        )
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")

    def test_enviar_comprovante_pix_com_erro_ocr_salva_diagnostico_sem_quebrar(self):
        arquivo = SimpleUploadedFile(
            "comprovante-render.jpg",
            b"imagem recebida pelo celular",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", side_effect=RuntimeError("tesseract nao encontrado")):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Roseli"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "OCR nao executado automaticamente no envio mobile")

    def test_analisar_comprovante_pix_com_ocr_vazio_salva_diagnostico(self):
        arquivo = SimpleUploadedFile(
            "comprovante-vazio.jpg",
            b"imagem sem texto",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", return_value=""):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

    def _imagem_pix_teste(self, tamanho=(900, 1600)):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", tamanho, "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_ocr_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in (80, 150, 220, 290, 360):
            desenho.rectangle((60, y, 430, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_nubank_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y, largura in zip((70, 135, 195, 260, 340), (80, 260, 320, 360, 340)):
            desenho.rectangle((70, y, 70 + largura, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_texto_denso_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (700, 1500), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in range(60, 1280, 28):
            desenho.rectangle((70, y, 610, y + 5), fill="black")
            desenho.rectangle((70, y + 10, 420, y + 14), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _modulo_pytesseract_fake(self, respostas_por_recorte):
        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            resposta = respostas_por_recorte.get(recorte, "")
            if callable(resposta):
                return resposta(imagem, kwargs)
            if isinstance(resposta, Exception):
                raise resposta
            return resposta

        return types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

    def _mock_google_vision_texto(self, texto):
        class ImagemFake:
            def __init__(self, content):
                self.content = content

        cliente = types.SimpleNamespace(
            document_text_detection=lambda image: types.SimpleNamespace(
                full_text_annotation=types.SimpleNamespace(text=texto),
                text_annotations=[],
                error=types.SimpleNamespace(message=""),
            )
        )
        vision = types.SimpleNamespace(Image=ImagemFake)
        return cliente, vision

    def test_analisar_comprovante_pix_google_vision_banpara_extrai_dados(self):
        texto = (
            "BANCO DO ESTADO DO PARÁ S.A. - BANPARÁ\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operação: 05/05/2026 16:45:50\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Instituição: NU PAGAMENTOS - IP\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
        )
        arquivo = SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["instituicao_pix"], "Banpará")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_valor_data(self):
        texto = "NU\nComprovante de transferência\n21 ABR 2026 - 13:05:01\nValor R$ 172,00\n"
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_pagador_origem_por_contexto(self):
        texto = (
            "NU\n"
            "Comprovante de transferencia\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Destino\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Juliana Cotrim Cardoso\n"
            "CPF ***.123.456-**\n"
            "Instituicao NU PAGAMENTOS - IP\n"
        )
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Juliana Cotrim Cardoso")
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_mercado_pago_extrai_pagador_de(self):
        texto = (
            "Comprovante de Pix\n"
            "23/maio/2026 as 18:55:32\n"
            "R$ 645\n"
            "De\n"
            "Joao de Almeida E Silva\n"
            "CPF: ***.105.902-**\n"
            "Mercado Pago\n"
            "Para\n"
            "Lincoln Albuquerque Neiva\n"
        )
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida E Silva")
        self.assertEqual(dados["valor"], "645.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-23T18:55")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_debug_texto_bruto_env(self):
        texto = "Comprovante de Pix\nDe\nJoao de Almeida E Silva\nPara\nLincoln Albuquerque Neiva\n"
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_OCR_DEBUG_TEXT": "True"}), patch(
            "estoque.utils_pix._criar_cliente_google_vision",
            return_value=self._mock_google_vision_texto(texto),
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            analisar_comprovante_pix_google_vision(arquivo)

        saida = "\n".join(logs.output)
        self.assertIn("[PIX OCR][Google Vision texto bruto]", saida)
        self.assertIn("--- INICIO GOOGLE VISION OCR ---", saida)
        self.assertIn("Joao de Almeida E Silva", saida)
        self.assertIn("--- FIM GOOGLE VISION OCR ---", saida)

    def test_analisar_comprovante_pix_google_vision_extrai_de_para_em_texto_achatado(self):
        casos = [
            (
                "pagbank.jpg",
                "Comprovante de envio de Pix 18/05/2026 as 07:08:52 Valor da transferencia R$ 5,00 De ROSELI DA COSTA GAMA CPF ***.115.912-** Instituicao PagBank Para Lincoln Albuquerque Neiva",
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "itau.jpg",
                "Comprovante de devolucao de Pix R$ 5,00 Realizado em 19/05/2026 as 17:18:36 De EUCLIDES CARNEIRO NEIVA NETO CPF/CNPJ: 787.484.883-72 Instituicao: ITAU UNIBANCO S.A Para Lincoln Albuquerque Neiva",
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
        ]

        for nome_arquivo, texto, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, b"imagem", content_type="image/jpeg")
                with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
                    dados = analisar_comprovante_pix_google_vision(arquivo)

                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_pix_google_vision_habilitado_le_variavel_de_ambiente_fora_dos_testes(self):
        with patch.dict(os.environ, {"PIX_USAR_GOOGLE_VISION": "True"}), patch("sys.argv", ["manage.py", "runserver"]):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_pix_google_vision_habilitado_usa_credencial_quando_flag_nao_definida(self):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "google-vision.json"}, clear=True), patch(
            "sys.argv",
            ["manage.py", "runserver"],
        ):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_analisar_comprovante_pix_google_vision_falha_sem_500(self):
        arquivo = SimpleUploadedFile("erro.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", side_effect=RuntimeError("sem credencial")):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("[Google Vision OCR erro]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_parcial_nao_derruba_extracao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-parcial.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 156,50\n16/05/2026 17:51\nNubank\n",
            "pagador": RuntimeError("timeout no recorte"),
            "inteira": "Origem\nNome: Joelson Ferreira dos Santos\nDestino\nNome: Lincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[OCR avisos]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_fallback_imagem_inteira_continua_funcionando(self):
        arquivo = SimpleUploadedFile(
            "comprovante-fallback.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": RuntimeError("falhou topo"),
            "pagador": RuntimeError("falhou pagador"),
            "inteira": (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Maria Fallback Silva\n"
                "Valor R$ 88,00\n"
                "16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Maria Fallback Silva")
        self.assertEqual(dados["valor"], "88.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["instituicao_pix"], "Banco do Brasil")

    def test_analisar_comprovante_pix_tenta_eng_quando_por_traineddata_falha(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-por.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )

        def texto_com_fallback(_imagem, kwargs):
            if kwargs.get("lang") == "por":
                raise RuntimeError("Error opening data file C:\\Program Files\\Tesseract-OCR/tessdata/por.traineddata")
            if kwargs.get("lang") == "eng":
                return "Pix enviado\nR$ 5,00\nData do pagamento\nSabado, 16/05/2026\nHorario\n23h41\n"
            return ""

        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": texto_com_fallback,
            "pagador": "",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertIn("recorte=topo tentativa OCR falhou=por:", "\n".join(logs.output))
        self.assertIn("recorte=topo idioma OCR usado=eng", "\n".join(logs.output))

    def test_analisar_comprovante_pix_recortes_nao_usam_recebedor_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-recebedor.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Valor R$ 600,00\n16/05/2026 16:33\nPara\nLincoln Albuquerque Neiva\n",
            "pagador": "@ De\nIvanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nMercado Pago\n",
            "inteira": "Para\nLA Neiva\nNu Pagamentos S.A.\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertNotIn(dados["pagador"], ["Lincoln Albuquerque Neiva", "LA Neiva"])
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_sem_banco_pagador_retorna_parcial_sem_erro(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-pagador.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 10,00\n21/05/2026 09:10\n",
            "pagador": "",
            "inteira": "Destino\nLincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertEqual(dados["valor"], "10.00")

    def test_analisar_comprovante_pix_todos_recortes_falham_sem_excecao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-falha.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        erro = RuntimeError("timeout")
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": erro,
            "pagador": erro,
            "inteira": erro,
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "")
        self.assertIn("preencha manualmente", dados["mensagem"].lower())

    def test_analisar_comprovante_pix_render_nao_usa_imagem_inteira_em_imagem_grande(self):
        arquivo = SimpleUploadedFile(
            "banpara-grande.png",
            self._imagem_pix_teste(tamanho=(900, 1800)),
            content_type="image/png",
        )
        recortes_chamados = []

        def falhar_recorte(imagem, _kwargs):
            recortes_chamados.append(imagem.info.get("ocr_recorte", "inteira"))
            raise RuntimeError("timeout render")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "faixa_valor_principal": falhar_recorte,
            "faixa_valor_alternativa": falhar_recorte,
            "faixa_data_principal": falhar_recorte,
            "faixa_data_alternativa": falhar_recorte,
            "rapido_superior": falhar_recorte,
            "rapido_meio_superior": falhar_recorte,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertNotIn("inteira", recortes_chamados)
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_render_bloqueia_imagem_densa_antes_do_tesseract(self):
        arquivo = SimpleUploadedFile(
            "banpara-denso.png",
            self._imagem_texto_denso_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
            True,
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("OCR bloqueado por seguran", dados["texto_ocr_bruto"])
        self.assertIn("OCR bloqueado por seguranca no Render antes do Tesseract", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_bloqueado_volta_sem_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara-denso.png", self._imagem_texto_denso_teste(), content_type="image/png"),
            )
            pytesseract_fake = self._modulo_pytesseract_fake({
                "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
            })

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
                True,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("OCR bloqueado por seguran", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_analisar_comprovante_pix_render_timeouts_sucessivos_retornam_falha_segura(self):
        arquivo = SimpleUploadedFile(
            "banpara-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def timeout(_imagem, _kwargs):
            raise RuntimeError("Tesseract process timeout")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "linha_01": timeout,
            "linha_02": timeout,
            "linha_03": timeout,
            "linha_04": timeout,
            "linha_05": timeout,
            "rapido_superior": timeout,
            "rapido_meio_superior": timeout,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_inter_enviado_preenche_valor_data_sem_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "ID da transacao\n"
                "E00416968202605170241GgMBKIN9FUL\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertNotIn("Lincoln Albuquerque Neiva", dados["pagador"])

    def test_analisar_comprovante_pix_inter_nao_usa_instituicao_do_recebedor_como_banco(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-recebedor-nubank.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Inter\n"
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "Quem pagou\n"
                "Nome\n"
                "RONISE DO SOCORRO DOS SANTOS FERREIRA\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RONISE DO SOCORRO DOS SANTOS FERREIRA")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "Inter")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")

    def test_analisar_comprovante_pix_inter_nao_usa_la_neiva_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-la-neiva.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Pix enviado\nRS 5,00\nData do pagamento Sabado, 16/05/2026 Horario 23h41\n",
            "pagador": "Quem recebeu\nLA Neiva\nInstituicao do recebedor\nNu Pagamentos\n",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")

    def test_enviar_mesmo_comprovante_pix_duas_vezes_nao_processa_duplicidade_no_mobile(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria Duplicada Silva\n"
            "Valor R$ 88,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
            "ID da transacao\n"
            "E00000000202605161730ABCDEF123456789\n"
        ).encode("utf-8")
        url = reverse("estoque:central_pix_enviar_comprovante")

        primeira = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-1.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )
        segunda = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-2.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertContains(segunda, "Comprovante enviado com sucesso para a Central de Pix.")
        pix_original, pix_duplicado = PixRecebido.objects.order_by("id")
        self.assertNotEqual(pix_original.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(pix_duplicado.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(pix_duplicado.pix_original)
        self.assertIsNone(pix_duplicado.cliente)

    def test_enviar_comprovante_mobile_nao_roda_duplicidade_por_ocr(self):
        PixRecebido.objects.create(
            nome_pagador="Jose Parecido da Silva",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "provavel-duplicado.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "Pagador: José Parecido da Silva\n"
                "Valor R$ 55,00\n"
                "16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(novo_pix.pix_original)
        self.assertIn("OCR pendente", novo_pix.observacao)

    def test_enviar_comprovante_pix_diferente_continua_pendente_normal(self):
        cliente = Cliente.objects.create(nome="Cliente Diferente Pix", ativo=True)
        PixRecebido.objects.create(
            nome_pagador="Outro Pagador",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "pix-diferente.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cliente Diferente Pix\n"
                "Valor R$ 75,00\n"
                "Data 16/05/2026 18:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertIsNone(novo_pix.cliente_sugerido)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(novo_pix.pix_original)

    def test_status_possivel_duplicado_aparece_na_central_e_no_detalhe(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
            observacao="Possivel Pix duplicado do registro #1.",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Possivel duplicado")
        self.assertContains(resposta_lista, "possivel_duplicado")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Possivel Pix duplicado")
        self.assertContains(resposta_detalhe, f"Ver Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Comparação com Pix parecido")
        self.assertContains(resposta_detalhe, f"Pix atual #{duplicado.id}")
        self.assertContains(resposta_detalhe, f"Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Compare os dados antes de decidir se este envio é duplicado.")

    def test_enviado_por_aparece_na_central_detalhe_e_comparacao(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original enviado",
            enviado_por_nome="Lincoln",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado enviado",
            enviado_por_nome="Roseli",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Enviado por")
        self.assertContains(resposta_lista, "Lincoln")
        self.assertContains(resposta_lista, "Roseli")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Enviado por")
        self.assertContains(resposta_detalhe, "Lincoln")
        self.assertContains(resposta_detalhe, "Roseli")

    def test_detalhe_pix_volta_para_resumo_de_envio_quando_recebe_next_seguro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix do resumo",
            valor="40.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resumo_url = f"{reverse('estoque:central_pix_enviar_comprovante')}?pix_recebido={pix.id}"
        detalhe_url = f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={resumo_url}"

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{resumo_url}"')

    def test_detalhe_pix_ignora_next_externo_e_volta_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix next externo",
            valor="15.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            "?next=https://example.com/roubo"
        )

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertNotContains(resposta, "example.com")

    def test_central_pix_lista_link_de_detalhe_do_pix_pendente(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix pendente detalhe",
            valor="35.50",
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="Texto tecnico do OCR",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Data do Pix")
        self.assertContains(resposta_lista, "Registrado em")
        self.assertNotContains(resposta_lista, "<th class=\"pix-col-observacao\">Observacao</th>")
        self.assertNotContains(resposta_lista, "Texto tecnico do OCR")
        self.assertContains(resposta_lista, "Pix pendente detalhe")
        self.assertContains(resposta_lista, 'class="pix-pagador" title="Pix pendente detalhe"')
        self.assertContains(resposta_lista, 'class="pix-sem-cliente">Sem cliente</span>')
        self.assertContains(resposta_lista, "Ver detalhe")
        self.assertContains(
            resposta_lista,
            f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})}?next={reverse("estoque:central_pix")}',
        )

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta_detalhe, "Enviar outro comprovante")
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Texto tecnico do OCR")

    def test_central_pix_ver_detalhe_retorna_para_central_mesmo_com_next_externo(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix com retorno central",
            valor="18.00",
            instituicao_pix="PagBank",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        contas_url = reverse("estoque:contas_receber")
        caminho_lista = f"{central_url}?next={contas_url}&q=pagbank"

        resposta_lista = self.client.get(caminho_lista, secure=True)

        detalhe_path = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_lista, f"{detalhe_path}?next=")
        self.assertContains(resposta_lista, "central-pix")
        self.assertContains(resposta_lista, "q%3Dpagbank")

    def test_receber_cliente_voltar_ao_pix_preserva_next_original_do_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente retorno pix", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente retorno pix",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            f"?{urlencode({'next': central_url})}"
        )
        receber_url = (
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}"
            f"?{urlencode({'pix_recebido': pix.id, 'next': detalhe_url})}"
        )

        resposta = self.client.get(receber_url, secure=True)

        self.assertContains(resposta, f'href="{detalhe_url}"')
        self.assertNotContains(resposta, f"{detalhe_url}?")
        self.assertNotContains(resposta, f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido=")

    def test_central_pix_busca_filtra_pix_registrados(self):
        cliente = Cliente.objects.create(nome="Jo\u00e3o De Almeida E Silva", ativo=True)
        pix_joao = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Joao de Almeida e Silva",
            valor="42.50",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 23, 18, 55)),
        )
        PixRecebido.objects.create(
            nome_pagador="Maria Ignorada",
            valor="12.00",
            instituicao_pix="Nubank",
            status=PixRecebido.STATUS_IGNORADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 22, 8, 30)),
        )

        for termo in ("joao", "almeida", "mercado", "pendente", "23/05/2026", "42,50"):
            resposta = self.client.get(f"{reverse('estoque:central_pix')}?q={termo}", secure=True)
            self.assertEqual(resposta.status_code, 200)
            self.assertContains(resposta, "Joao de Almeida e Silva")
            self.assertContains(resposta, "Mercado Pago")
            self.assertNotContains(resposta, "Maria Ignorada")
            self.assertContains(
                resposta,
                f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix_joao.id})}?next=',
            )
            self.assertContains(resposta, "q%3D")

        resposta_ignorado = self.client.get(f"{reverse('estoque:central_pix')}?q=ignorado", secure=True)
        self.assertContains(resposta_ignorado, "Maria Ignorada")
        self.assertNotContains(resposta_ignorado, "Joao de Almeida e Silva")
        self.assertContains(resposta_ignorado, "Limpar")

    def test_central_pix_filtro_por_data(self):
        PixRecebido.objects.create(
            nome_pagador="Pix dentro da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            nome_pagador="Pix fora da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 5, 10, 0)),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?data_inicio=2026-05-04&data_fim=2026-05-04",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix dentro da data")
        self.assertNotContains(resposta, "Pix fora da data")

    def test_central_pix_filtro_por_status(self):
        PixRecebido.objects.create(
            nome_pagador="Pix status pendente",
            valor="30.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        PixRecebido.objects.create(
            nome_pagador="Pix status baixado",
            valor="30.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?status={PixRecebido.STATUS_PENDENTE}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix status pendente")
        self.assertNotContains(resposta, "Pix status baixado")

    def test_central_pix_combina_texto_cliente_data_e_status(self):
        cliente_roseli = Cliente.objects.create(nome="Roseli Cliente", ativo=True)
        cliente_maria = Cliente.objects.create(nome="Maria Cliente", ativo=True)
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo certo",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo baixado",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_maria,
            nome_pagador="Pix combo maria",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )

        resposta = self.client.get(
            (
                f"{reverse('estoque:central_pix')}?q=Mercado&cliente=Roseli"
                f"&data_inicio=2026-05-04&data_fim=2026-05-04&status={PixRecebido.STATUS_PENDENTE}"
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix combo certo")
        self.assertNotContains(resposta, "Pix combo baixado")
        self.assertNotContains(resposta, "Pix combo maria")
        self.assertContains(resposta, "status%3Dpendente")

    def test_analisar_comprovante_pix_usa_google_vision_quando_habilitado(self):
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida e Silva",
            "valor": "42.50",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nJoao de Almeida e Silva\nR$ 42,50",
            "mensagem": "Dados lidos pelo Google Vision. Confira antes de salvar.",
        }
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with override_settings(PIX_USAR_GOOGLE_VISION=True):
            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision) as vision_mock:
                with patch("estoque.views.analisar_comprovante_pix") as local_mock:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_analisar_comprovante"),
                        {"comprovante": arquivo},
                        secure=True,
                    )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida e Silva")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        vision_mock.assert_called_once()
        local_mock.assert_not_called()
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nao_usa_ocr_local_sem_fallback_explicito(self):
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.views.analisar_comprovante_pix") as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertIn("Leitura automatica nao realizada", dados["mensagem"])
        self.assertIn("[Google Vision indisponivel]", dados["texto_ocr_bruto"])
        self.assertIn("fallback local desativado", "\n".join(logs.output))
        local_mock.assert_not_called()

    def test_analisar_comprovante_pix_usa_ocr_local_quando_fallback_explicito(self):
        dados_local = {
            "ok": True,
            "pagador": "Pagador Local",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "PagBank",
            "texto_ocr_bruto": "[OCR linhas detectadas]\nPagador Local",
            "mensagem": "OCR parcial concluido. Confira os dados antes de salvar.",
        }
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
            "estoque.views.analisar_comprovante_pix",
            return_value=dados_local,
        ) as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Pagador Local")
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("OCR local fallback permitido", "\n".join(logs.output))
        local_mock.assert_called_once()

    def test_detalhe_pix_processar_ocr_agora_atualiza_dados_do_comprovante(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain"),
            )

            resposta = self.client.post(
                reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Cicero Cristiano Silva Souza")
            self.assertEqual(str(pix.valor), "20.00")
            self.assertEqual(pix.instituicao_pix, "Banco do Brasil")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertIn("Comprovante Pix", pix.texto_ocr_bruto)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "Texto OCR bruto")
            self.assertContains(resposta, "Reler comprovante (OCR)")

    def test_detalhe_pix_processar_google_vision_preenche_sem_criar_baixa(self):
        cliente = Cliente.objects.create(nome="Rubem Arruda de Souza", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "RUBEM ARRUDA DE SOUZA",
            "valor": "847.70",
            "data_pagamento": "2026-05-05T16:45",
            "instituicao_pix": "Banpará",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Dados de Origem\n"
                "Titular: RUBEM ARRUDA DE SOUZA\n"
                "Valor: 847,70\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "RUBEM ARRUDA DE SOUZA")
            self.assertEqual(str(pix.valor), "847.70")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-05-05T16:45")
            self.assertEqual(pix.instituicao_pix, "Banpará")
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_confirma_cliente_exato_normalizado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida E Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Mercado Pago\n"
                "@ De\n"
                "Joao de Almeida E Silva\n"
                "Valor: R$ 5,00\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente, cliente)
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_cliente_inexistente_continua_sem_confirmacao(self):
        dados_vision = {
            "ok": True,
            "pagador": "Cliente Inexistente Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nCliente Inexistente Silva\nValor: R$ 5,00\n",
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_central_pix_cliente_confirmado_autocomplete_lista_ao_focar(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        Cliente.objects.create(nome="Maria Cliente Ativa", ativo=True)
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        nomes = [cliente["nome"] for cliente in resposta.json()["clientes"]]
        self.assertIn("João De Almeida E Silva", nomes)
        self.assertIn("Maria Cliente Ativa", nomes)
        self.assertNotIn("Cliente Inativo", nomes)
        self.assertLessEqual(len(nomes), 12)

    def test_central_pix_detalhe_carrega_clientes_para_autocomplete_local(self):
        cliente = Cliente.objects.create(
            nome="Jo\u00e3o De Almeida E Silva",
            apelido_nome_conhecido="Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)
        pix = PixRecebido.objects.create(
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "pixClientesAutocompleteData")
        clientes = resposta.context["clientes_pix_autocomplete"]
        item = next(cliente_local for cliente_local in clientes if cliente_local["id"] == cliente.id)
        self.assertEqual(item["nome"], "Jo\u00e3o De Almeida E Silva")
        self.assertIn("joao", item["busca"])
        self.assertIn("almeida", item["busca"])
        self.assertIn("12345678900", item["busca"])
        self.assertIn("85999990000", item["busca"])
        self.assertNotIn("Cliente Inativo", [cliente_local["nome"] for cliente_local in clientes])

    def test_central_pix_cliente_confirmado_autocomplete_busca_normalizada(self):
        cliente = Cliente.objects.create(
            nome="João De Almeida E Silva",
            apelido_nome_conhecido="Joao Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )

        for termo in ("alm", "joao", "123456", "999990000"):
            resposta = self.client.get(
                f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q={termo}",
                secure=True,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(resposta.status_code, 200)
            ids = [item["id"] for item in resposta.json()["clientes"]]
            self.assertIn(cliente.id, ids)

    def test_central_pix_cliente_confirmado_autocomplete_sem_resultado_nao_confirma_cliente(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q=clienteinexistente",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["clientes"], [])
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_central_pix_pendente_pode_remover_cliente_confirmado_sem_baixa(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        conteudo = b"comprovante pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                cliente=cliente,
                cliente_sugerido=cliente,
                nome_pagador="Joao de Almeida E Silva",
                valor=Decimal("5.00"),
                data_pagamento=timezone.now(),
                instituicao_pix="Mercado Pago",
                status=PixRecebido.STATUS_PENDENTE,
                texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", conteudo, content_type="image/jpeg"),
            )
            comprovante_nome = pix.comprovante.name

            resposta = self.client.post(
                reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(str(pix.valor), "5.00")
            self.assertEqual(pix.instituicao_pix, "Mercado Pago")
            self.assertEqual(pix.comprovante.name, comprovante_nome)
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
            self.assertEqual(CreditoCliente.objects.count(), 0)
            self.assertContains(resposta, "Cliente confirmado removido")

    def test_central_pix_baixado_nao_remove_cliente_confirmado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Este Pix ja foi baixado/inativado")

    def test_receber_cliente_com_pix_sem_contas_abertas_oferece_saida_segura(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Conta", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Sem Conta",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="[Google Vision OCR]\nCliente Sem Conta",
        )

        resposta = self.client.get(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este cliente nao tem contas abertas")
        self.assertContains(resposta, "Voltar ao Pix")
        self.assertContains(resposta, "Trocar cliente")
        self.assertContains(resposta, "foco_cliente=1")
        self.assertContains(resposta, "rc-btn-pix-voltar")
        self.assertContains(resposta, "rc-btn-pix-trocar")
        self.assertContains(resposta, "rc-btn-pix-remover")
        self.assertContains(resposta, "Remover cliente confirmado e manter pendente")
        self.assertContains(
            resposta,
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
        )
        conteudo = resposta.content.decode()
        self.assertLess(
            conteudo.rindex("Saldo atual em aberto"),
            conteudo.rindex("Este cliente nao tem contas abertas"),
        )
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_detalhe_pix_com_foco_cliente_foca_sem_abrir_autocomplete(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Cliente Teste",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta_sem_foco = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        resposta_com_foco = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?foco_cliente=1",
            secure=True,
        )

        self.assertEqual(resposta_sem_foco.status_code, 200)
        self.assertEqual(resposta_com_foco.status_code, 200)
        self.assertContains(resposta_sem_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "clienteConfirmadoBusca.select();")
        self.assertContains(resposta_com_foco, 'addEventListener("click"')
        self.assertContains(resposta_com_foco, 'addEventListener("input"')
        conteudo = resposta_com_foco.content.decode()
        bloco_foco = conteudo.split("if (focarClienteConfirmado && clienteConfirmadoBusca)", 1)[1]
        bloco_foco = bloco_foco.split('document.querySelectorAll("form")', 1)[0]
        self.assertNotIn("buscarClientes", bloco_foco)
        self.assertNotIn('addEventListener("focus"', conteudo)

    def test_detalhe_pix_com_cliente_confirmado_destaca_sem_focar_digitacao(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Confirmado", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Pix Confirmado",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "const focarClienteConfirmado = false;")
        self.assertContains(resposta, "pix-client-confirmed-visible")
        self.assertContains(resposta, "Cliente encontrado com seguranca")

    def test_detalhe_pix_com_dados_lidos_sem_cliente_orienta_vincular_pagador(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Lisandra De Oliveira Da Silva",
            valor=Decimal("548.30"),
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 14, 9, 15)),
            instituicao_pix="Stone",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este Pix j\u00e1 tem dados lidos")
        self.assertContains(resposta, "Vincule o pagador a um cliente para usar este Pix na baixa")
        self.assertContains(resposta, "const focarClienteConfirmado = true;")
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_processar_ocr_com_erro_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=RuntimeError("timeout render"),
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("RuntimeError", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("arquivo=comprovante_", "\n".join(logs.output))
            self.assertIn("timeout render", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_excecao_inesperada_nao_retorna_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=ValueError("falha banpara"),
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("ValueError", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_detalhe_pix_processar_ocr_com_timeout_retornado_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )
            dados_timeout = {
                "ok": False,
                "pagador": "",
                "valor": "",
                "data_pagamento": "",
                "instituicao_pix": "",
                "texto_ocr_bruto": "ERRO OCR: RuntimeError: Tesseract process timeout",
            }

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_timeout,
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("Tesseract process timeout", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_aproveita_texto_parcial_com_valor_e_data(self):
        conteudo = (
            "ERRO OCR: timeout parcial no Render\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Tipo de transferencia Pix\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-parcial.txt", conteudo, content_type="text/plain"),
            )

            with self.assertLogs("estoque.views", level="INFO") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(pix.nome_pagador, "")
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("Valor R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertContains(resposta, "Reler comprovante (OCR)")
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_para_apos_topo_com_valor_e_data(self):
        chamadas = []
        configs = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            configs.append(kwargs.get("config"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-topo.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(
                configs,
                [
                    "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789R$r$.,,",
                    "--oem 1 --psm 7",
                ],
            )
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))
            self.assertIn("config=--oem 1 --psm 7", "\n".join(logs.output))
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_faixas_evita_bloco_grande_com_timeout(self):
        chamadas = []
        timeouts = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            timeouts.append(kwargs.get("timeout"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            if recorte in {"rapido_superior", "rapido_meio_superior"}:
                raise RuntimeError("Tesseract process timeout")
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-render.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(timeouts, [4, 4])
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("recorte=faixa_valor_principal", "\n".join(logs.output))
            self.assertIn("recorte=faixa_data_principal", "\n".join(logs.output))
            self.assertIn("texto=R$ 172,00", "\n".join(logs.output))
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))

    def test_analisar_comprovante_pix_nubank_faixas_calculadas_na_imagem_700(self):
        with patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            _original, _reduzido, recortes = _preparar_recortes_ocr(self._imagem_pix_teste((485, 1600)))

        por_nome = {nome: (imagem, caixa) for nome, imagem, caixa in recortes}
        imagem_valor, caixa_valor = por_nome["faixa_valor_principal"]
        imagem_data, caixa_data = por_nome["faixa_data_principal"]

        self.assertEqual(imagem_valor.info["ocr_base_nome"], "nubank_700")
        self.assertEqual(imagem_valor.info["ocr_base_tamanho"][0], 700)
        self.assertEqual(imagem_valor.info["ocr_caixa_percentual"], (0.35, 0.38, 1.00, 0.47))
        self.assertEqual(imagem_data.info["ocr_caixa_percentual"], (0.05, 0.25, 0.95, 0.33))
        self.assertLess(caixa_data[1], int(imagem_data.info["ocr_base_tamanho"][1] * 0.34))
        self.assertLess(caixa_valor[1], int(imagem_valor.info["ocr_base_tamanho"][1] * 0.48))
        self.assertEqual(imagem_valor.info["ocr_tamanho_depois"][0], imagem_valor.info["ocr_tamanho_antes"][0] * 3)

    def test_analisar_comprovante_pix_ocr_por_linhas_extrai_valor_e_data(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas.png",
            self._imagem_linhas_nubank_teste(),
            content_type="image/png",
        )
        timeouts = {}

        respostas = {
            "linha_01": "NU",
            "linha_02": "Comprovante de",
            "linha_03": "transferencia",
            "linha_04": "21 ABR 2026 - 13:05:01",
            "linha_05": "Valor R$ 172,00",
        }

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte")
            timeouts[recorte] = kwargs.get("timeout")
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(timeouts["linha_04"], 4)
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("04 candidata_data: 21 ABR 2026 - 13:05:01", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_por_linhas_continua_ate_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-santander.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )
        chamadas = []
        respostas = {
            "linha_01": "RS 500,00",
            "linha_02": "18/05/2026 as 17:45:00",
            "linha_03": "Dados do pagador",
            "linha_04": "De",
            "linha_05": "lvanildo Ferraz Patricio Junior",
        }

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            chamadas.append(recorte)
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "500.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-18T17:45")
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertIn("linha_05", chamadas)
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_ocr_por_linhas_timeout_continua(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            if recorte == "linha_01":
                raise RuntimeError("Tesseract process timeout")
            if recorte == "linha_02":
                return "21 ABR 2026 - 13:05:01"
            if recorte == "linha_04":
                return "R$ 172,00"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")

    def test_analisar_comprovante_pix_ids_de_transacao_nao_viram_valor(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "ID da transacao 14605308174802dd\n"
            "E182361202604211305\n"
            "NU PAGAMENTOS - IP\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-id.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["valor"], "")
        self.assertEqual(dados["data_pagamento"], "")

    def test_detalhe_pix_processar_ocr_salva_debug_recortes_com_variavel(self):
        chamadas = []

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, DEBUG=False):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-debug.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {
                "PIX_OCR_DEBUG_CROPS": "True",
                "PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True",
            }), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIn("[OCR debug recortes]", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_valor_principal.jpg", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_data_principal.jpg", pix.texto_ocr_bruto)
            self.assertEqual(str(pix.valor), "172.00")

    def test_detalhe_pix_processar_ocr_sem_data_preserva_data_existente(self):
        data_original = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        dados_sem_data = {
            "ok": True,
            "pagador": "",
            "valor": "172.00",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "[OCR faixa_valor_principal]\nR$ 172,00",
        }

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                data_pagamento=data_original,
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-sem-data.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_sem_data,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(pix.data_pagamento, data_original)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")

    def test_detalhe_pix_usa_rota_propria_do_comprovante(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix_original = PixRecebido.objects.create(
                nome_pagador="Pix original com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("pix-original.jpg", b"imagem original", content_type="image/jpeg"),
            )
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
                pix_original=pix_original,
                comprovante=SimpleUploadedFile("pix-render.jpg", b"imagem pix", content_type="image/jpeg"),
            )
            comprovante_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id})
            comprovante_original_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix_original.id})

            resposta = self.client.get(
                reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertContains(resposta, f'href="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_original_url}"')
            self.assertNotContains(resposta, "/media/")

    def test_rota_comprovante_pix_retorna_arquivo_salvo(self):
        conteudo = b"imagem pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com comprovante",
                valor="20.00",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("comprovante-render.jpg", conteudo, content_type="image/jpeg"),
            )

            resposta = self.client.get(
                reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/jpeg")
            self.assertIn('inline; filename="comprovante-render', resposta["Content-Disposition"])
            self.assertEqual(b"".join(resposta.streaming_content), conteudo)

    def test_abrir_comprovante_pix_usa_fallback_media_root_para_arquivo_antigo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            caminho_antigo = Path(media_root) / "pix" / "comprovantes" / "antigo.txt"
            caminho_antigo.parent.mkdir(parents=True, exist_ok=True)
            caminho_antigo.write_bytes(b"arquivo antigo")
            pix = PixRecebido(valor=Decimal("0.00"), data_pagamento=timezone.now())
            pix.comprovante.name = "pix/comprovantes/antigo.txt"

            with patch.object(pix.comprovante, "open", side_effect=FileNotFoundError):
                arquivo = views._abrir_comprovante_pix(pix)

            try:
                self.assertEqual(arquivo.read(), b"arquivo antigo")
            finally:
                arquivo.close()

    def test_rota_comprovante_pix_retorna_404_sem_comprovante(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix sem comprovante",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)

    def test_detalhe_pix_pendente_permite_marcar_como_ignorado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix para ignorar",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {"acao": "ignorar"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertRedirects(resposta, reverse("estoque:central_pix"), fetch_redirect_response=False)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)

    def test_ignorar_pix_preserva_dados_conferidos(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Ignorado", ativo=True)
        pix_original = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45))
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            pix_original=pix_original,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=data_pagamento,
            instituicao_pix="Mercado Pago",
            observacao="Conferido antes de ignorar.",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "ignorar",
                "valor": "",
                "nome_pagador": "",
                "instituicao_pix": "",
                "cliente": "",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertRedirects(resposta, reverse("estoque:central_pix"), fetch_redirect_response=False)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)
        self.assertEqual(str(pix.valor), "500.00")
        self.assertEqual(pix.data_pagamento, data_pagamento)
        self.assertEqual(pix.instituicao_pix, "Mercado Pago")
        self.assertEqual(pix.nome_pagador, "Ivanildo Ferraz Patricio Junior")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.pix_original, pix_original)
        self.assertIn("Conferido antes de ignorar.", pix.observacao)
        self.assertIn("Pix ignorado sem baixa pelo operador", pix.observacao)

    def _criar_conta_receber_pix(self, cliente, valor="100.00"):
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=valor,
        )
        return ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=valor,
            valor_em_aberto=valor,
            status=ContaReceber.STATUS_ABERTA,
        )

    def test_receber_cliente_mostra_credito_disponivel_com_origem_e_saldo_resultante(self):
        cliente = Cliente.objects.create(nome="Cliente Com Credito", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "100.00")
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("35.00"),
            origem_conta_receber=conta,
            observacao="Ajuste por item nao entregue.",
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("-10.00"),
            origem_conta_receber=conta,
            observacao="Credito usado em recebimento posterior.",
        )

        resposta = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Créditos disponíveis do cliente")
        self.assertContains(resposta, "Total: R$ 25.00")
        self.assertContains(resposta, f"Venda #{conta.venda_id}")
        self.assertContains(resposta, f"Conta #{conta.id}")
        self.assertContains(resposta, "Motivo: Ajuste por item nao entregue.")
        self.assertContains(resposta, "Saldo resultante se o crédito for considerado")
        self.assertContains(resposta, "R$ 75.00")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_receber_cliente_sem_contas_mostra_mensagem_de_credito_disponivel(self):
        cliente = Cliente.objects.create(nome="Cliente Somente Credito", ativo=True)
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("42.50"),
            observacao="Credito manual de teste.",
        )

        resposta = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Cliente não possui contas em aberto. Crédito disponível: R$ 42.50")
        self.assertContains(resposta, "Motivo: Credito manual de teste.")
        self.assertNotContains(resposta, "Usar credito")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_clientes_autocomplete_por_id_retorna_financeiro_com_credito_atualizado(self):
        cliente = Cliente.objects.create(nome="Lisandra Credito", ativo=True)
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("67.00"),
            observacao="Credito disponivel para restauracao em vendas.",
        )

        resposta = self.client.get(
            reverse("estoque:clientes_autocomplete"),
            {"cliente_id": cliente.id, "q": "texto que nao precisa bater"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(len(dados["clientes"]), 1)
        self.assertEqual(dados["clientes"][0]["id"], cliente.id)
        self.assertEqual(dados["clientes"][0]["nome"], "Lisandra Credito")
        self.assertEqual(dados["clientes"][0]["financeiro"]["credito_disponivel"], "67.00")

    def test_baixa_com_pix_atual_bloqueia_quando_parecido_ja_foi_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Duplicado", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix_atual.id}",
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_manual_identifica_duplicado_ja_baixado_e_bloqueia_botao(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Jr",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_baixado)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Use apenas para conferencia")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "Abrir Pix parecido")
        self.assertNotContains(resposta, "Abrir imagem do Pix parecido")
        self.assertEqual(resposta.content.count(b">Ignorar Pix sem baixa</button>"), 1)

    def test_central_pix_prioriza_duplicado_baixado_sobre_pendente(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Junior",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        pix_novo = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(pix_novo.pix_original, pix_baixado)
        self.assertEqual(pix_novo.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")

    def test_reler_ocr_no_detalhe_nao_envia_next_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix OCR detalhe",
            valor="10.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={reverse('estoque:central_pix')}",
            secure=True,
        )

        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})
        self.assertContains(resposta, f'action="{action}"')
        self.assertNotContains(resposta, f'action="{action}?next=')

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": True,
            "pagador": "Pix OCR detalhe",
            "valor": "10.00",
            "data_pagamento": "2026-05-18T17:45",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "Comprovante Pix",
        }):
            resposta_ocr = self.client.post(action, secure=True, follow=True)

        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_ocr, "Comprovante original")
        self.assertContains(resposta_ocr, f'href="{detalhe_url}"')

    def test_reler_ocr_preserva_bloqueio_quando_duplicado_ja_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente OCR Duplicado", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )
        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "Comprovante Pix sem leitura completa",
        }):
            resposta = self.client.post(action, secure=True, follow=True)

        pix.refresh_from_db()
        self.assertEqual(pix.pix_original, pix_baixado)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertContains(resposta, "Comprovante original")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")

    def test_detalhe_pix_sem_valor_nao_mostra_acao_de_baixa(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente Pix Sem Valor",
            valor="0.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertContains(resposta, "Confira/digite o valor do Pix")
        self.assertContains(resposta, "Usar este Pix na baixa")

    def test_detalhe_pix_usar_na_baixa_salva_dados_e_abre_recebimento_com_data_do_pix(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Conferido", ativo=True)
        self._criar_conta_receber_pix(cliente, "172.00")
        data_pix = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        pix = PixRecebido.objects.create(
            nome_pagador="Nome OCR antigo",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 25, 17, 10)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador conferido",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.nome_pagador, "Pagador conferido")
        self.assertEqual(str(pix.valor), "172.00")
        self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
        self.assertEqual(pix.instituicao_pix, "Nubank")
        self.assertContains(resposta, 'name="valor" type="text" inputmode="decimal" autocomplete="off" value="172,00"')
        self.assertContains(resposta, 'name="data_recebimento" type="date" value="2026-04-21"')
        self.assertContains(resposta, '<option value="PIX" selected>PIX</option>', html=True)
        self.assertContains(resposta, "nenhuma baixa foi feita ainda")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertNotEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(timezone.localtime(pix.data_pagamento), data_pix)

    def test_detalhe_pix_usar_na_baixa_sem_cliente_nao_avanca(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pagador sem cliente",
            valor="172.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": "",
                "nome_pagador": "Pagador sem cliente",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de usar este Pix na baixa.")
        self.assertContains(resposta, "baixaSemClienteConfirmado")
        self.assertContains(resposta, "event.preventDefault();")
        self.assertContains(resposta, "pix-client-attention")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_usar_na_baixa_sem_valor_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem valor",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem valor",
                "valor": "",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe o valor do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_usar_na_baixa_sem_data_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Data Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem data",
            valor="172.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem data",
                "valor": "172,00",
                "data_pagamento": "",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe a data do pagamento do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_baixa_bloqueia_pix_igual_a_baixado_mesmo_sem_vinculo_original(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Vinculo", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "96.30")
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:conta_receber_receber", kwargs={"pk": conta.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "observacao": "",
                "destino_diferenca": "troco",
                "usar_credito": "0",
                "credito_utilizado": "0,00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Ja existe Pix igual baixado")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_baixa_marca_pix_baixado_quando_id_vem_na_url(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "500.00")
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            {
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "500,00",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertIn(f"conta(s): {conta.id}", pix.observacao)
        self.assertIn("valor: R$ 500,00", pix.observacao)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_atual_marca_parecido_pendente_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Atual", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_parecido.pix_original, pix_atual)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_parecido_marca_pix_atual_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Parecido", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_parecido.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_atual.pix_original, pix_parecido)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_analisar_comprovante_pix_nubank_usa_nome_da_origem(self):
        conteudo = (
            "Comprovante Pix\n"
            "Destino\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nubank_origem_nome_nao_usa_titulo_data(self):
        conteudo = (
            "Comprovante de transferência\n"
            "04 MAI 2026 - 10:49:17\n"
            "Valor R$ 400,00\n"
            "Tipo de transferência Pix\n"
            "Destino\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Origem\n"
            "Nome\n"
            "José Carlos da Silva Nascimento\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "CPF\n"
            "***.915.482-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "José Carlos da Silva Nascimento")
        self.assertNotIn("transferência", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "400.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-04T10:49")
        self.assertRegex(dados["instituicao_pix"], r"Nubank|NU PAGAMENTOS")

    def test_analisar_comprovante_pix_ton_stone_usa_dados_de_origem(self):
        conteudo = (
            "Comprovante de transfer\u00eancia\n"
            "14/05/2026 09:15\n"
            "Valor R$ 548,30\n"
            "Tipo Pix | Transfer\u00eancia\n"
            "\n"
            "DADOS DE DESTINO\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF ***.319.532-**\n"
            "Institui\u00e7\u00e3o\n"
            "NU PAGAMENTOS S.A. - INSTITUI\u00c7\u00c3O DE PAGAMENTO\n"
            "\n"
            "DADOS DE ORIGEM\n"
            "Nome\n"
            "Lisandra De Oliveira Da Silva\n"
            "CPF ***.157.092-**\n"
            "Institui\u00e7\u00e3o\n"
            "STONE INSTITUI\u00c7\u00c3O DE PAGAMENTO S.A.\n"
            "Ag\u00eancia 0001\n"
            "Conta 24279683-7\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("ton-stone.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "548.30")
        self.assertEqual(dados["data_pagamento"], "2026-05-14T09:15")
        self.assertEqual(dados["pagador"], "Lisandra De Oliveira Da Silva")
        self.assertEqual(dados["instituicao_pix"], "Stone")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotIn("DESTINO", dados["pagador"].upper())

    def test_analisar_comprovante_pix_caixa_tem_quem_vai_enviar_nao_usa_data(self):
        conteudo = (
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "Valor\n"
            "R$ 826,62\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "Dados da transa\u00e7\u00e3o\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertNotIn("maio de 2026", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertRegex(dados["instituicao_pix"], r"Caixa|Caixa Econ")

    def test_analisar_comprovante_pix_nubank_monta_pagador_origem_nome_quebrado(self):
        conteudo = (
            "Destino\n"
            "\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "\n"
            "Instituigaéo = NU PAGAMENTOS - IP\n"
            "\n"
            "Chave Pix +5591984111011\n"
            "\n"
            "Origem\n"
            "\n"
            "Maria Antonia Alves de\n"
            "\n"
            "Nome\n"
            "Paiva\n"
            "\n"
            "Instituigdo = NU PAGAMENTOS - IP\n"
            "\n"
            "CPF s+1.252.002-++\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["pagador"], "Maria Antonia Alves de Paiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_nubank_com_destino_banco_inter_nao_usa_regra_inter(self):
        conteudo = (
            "nu,\n"
            "\n"
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituigdo BANCO INTER\n"
            "\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "Nu Pagamentos S.A.\n"
            "nubank.com.br\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")
        saida = io.StringIO()

        with redirect_stdout(saida):
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotIn("[PIX OCR][Banco Inter][BRUTO]", saida.getvalue())
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_mercado_pago_usa_de_como_pagador(self):
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF: ***.319.532-**\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T16:33")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_pagador_por_contexto_sem_usar_recebedor(self):
        casos = [
            (
                "mercado-pago-de.txt",
                "Mercado Pago\nComprovante de Pix\n17/05/2026 11:09\nR$ 100\nDe Ivanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nPara Lincoln Albuquerque Neiva\n",
                "Ivanildo Ferraz Patricio Junior",
            ),
            (
                "quem-pagou.txt",
                "Comprovante Pix\nQuem pagou\nNome RONISE DO SOCORRO DOS SANTOS FERREIRA\nQuem recebeu Lincoln Albuquerque Neiva\nValor R$ 76,00\n27 ABR 2026 - 08:04:01\n",
                "RONISE DO SOCORRO DOS SANTOS FERREIRA",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador_esperado in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador_esperado)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_extrai_pagador_de_em_comprovantes_reais(self):
        casos = [
            (
                "pagbank-roceli.txt",
                (
                    "Comprovante de envio de Pix\n"
                    "18/05/2026 \u00e0s 07:08:52\n"
                    "Valor da transferencia R$ 5,00\n"
                    "De\n"
                    "ROSELI DA COSTA GAMA\n"
                    "CPF ***.115.912-**\n"
                    "Instituicao\n"
                    "PagBank (PagSeguro Internet Instituicao de Pagamento S.A.)\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "mercado-pago-joao.txt",
                (
                    "Comprovante de Pix\n"
                    "23/maio/2026 \u00e0s 18:55:32\n"
                    "R$ 645\n"
                    "De\n"
                    "Joao de Almeida E Silva\n"
                    "CPF: ***.105.902-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Joao de Almeida E Silva",
                "645.00",
                "2026-05-23T18:55",
                "Mercado Pago",
            ),
            (
                "itau-devolucao-euclides.txt",
                (
                    "Comprovante de devolucao de Pix\n"
                    "R$ 5,00\n"
                    "Realizado em 19/05/2026 \u00e0s 17:18:36\n"
                    "De\n"
                    "EUCLIDES CARNEIRO NEIVA NETO\n"
                    "CPF/CNPJ: 787.484.883-72\n"
                    "Instituicao: ITAU UNIBANCO S.A\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
            (
                "mercado-pago-ivanildo.txt",
                (
                    "Comprovante de Pix\n"
                    "18/maio/2026 \u00e0s 17:45:00\n"
                    "R$ 500\n"
                    "De\n"
                    "Ivanildo Ferraz Patricio Junior\n"
                    "CPF: ***.188.882-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Ivanildo Ferraz Patricio Junior",
                "500.00",
                "2026-05-18T17:45",
                "Mercado Pago",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_retorna_nome_arquivo_e_nao_reaproveita_upload_anterior(self):
        primeiro = SimpleUploadedFile(
            "mercado_pago_100.txt",
            (
                "Comprovante de Pix\n"
                "17/05/2026 11:09\n"
                "Valor R$ 100\n"
                "@ De\n"
                "Cliente Mercado Pago\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        segundo = SimpleUploadedFile(
            "comprovante_picpay_50.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "21/set/2025 - 13:20:41\n"
                "Valor\n"
                "R$ 50,00\n"
                "Pagador: ISA ALVES DE SOUZA\n"
                "Instituicao: PicPay\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        url = reverse("estoque:central_pix_analisar_comprovante")

        resposta_primeira = self.client.post(url, {"comprovante": primeiro}, secure=True)
        self.assertEqual(resposta_primeira.status_code, 200)
        dados_primeiro = resposta_primeira.json()
        self.assertEqual(dados_primeiro["valor"], "100.00")
        self.assertEqual(dados_primeiro["data_pagamento"], "2026-05-17T11:09")


class PedidoTests(TestCase):
    def setUp(self):
        """Criar dados de teste para pedidos"""
        self.cliente = Cliente.objects.create(
            nome="Cliente Teste",
            cpf_cnpj="12345678901234",
            ativo=True,
        )
        self.produto = Produto.objects.create(
            nome="Produto Teste",
            preco_compra=50.00,
            preco_venda=100.00,
            preco_vista=100.00,
            preco_prazo=110.00,
            quantidade=50,
        )

    def _criar_pedido_com_item(self, quantidade=Decimal("2.000"), total=Decimal("200.00")):
        from .models import Pedido, ItemPedido

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            operador="Operador Pedido",
            total=total,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=quantidade,
            unidade="Un",
            preco_unitario=Decimal("100.00"),
            valor_total=total,
            estoque_no_momento=self.produto.quantidade,
        )
        return pedido

    def _post_gravar_venda_com_itens(self, pedido_id=None, itens=None):
        if itens is None:
            itens = [
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                }
            ]
        dados = {
            "cliente_id": self.cliente.id,
            "data_venda": timezone.localdate().isoformat(),
            "data_vencimento": "",
            "tipo_pagamento": "A vista",
            "operador": "Operador Teste",
            "total": "200.00",
            "itens": itens,
        }
        if pedido_id is not None:
            dados["pedido_id"] = pedido_id
        return self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(dados),
            content_type="application/json",
            secure=True,
        )

    def _post_gravar_venda_com_item(self, pedido_id=None, quantidade="2.000"):
        return self._post_gravar_venda_com_itens(
            pedido_id=pedido_id,
            itens=[
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": quantidade,
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                }
            ],
        )

    def _post_editar_pedido(self, pedido, itens, cliente=None):
        return self.client.post(
            reverse("estoque:pedido_editar", args=[pedido.id]),
            data={
                "data_pedido": pedido.data_pedido.isoformat(),
                "cliente_id": (cliente or pedido.cliente or self.cliente).id,
                "data_prevista_entrega": pedido.data_prevista_entrega.isoformat() if pedido.data_prevista_entrega else "",
                "operador": "Operador Editado",
                "observacao": "Observacao editada",
                "itens_json": json.dumps(itens),
            },
            secure=True,
        )

    def _post_cancelar_pedido(self, pedido):
        return self.client.post(
            reverse("estoque:pedido_cancelar", args=[pedido.id]),
            secure=True,
        )

    def _post_criar_pedido(self, proxima_acao="", operador="Operador Pedido"):
        itens = [
            {
                "produto_id": self.produto.id,
                "produto_nome": self.produto.nome,
                "quantidade": "2.000",
                "unidade": "Un",
                "preco_unitario": "100.00",
                "valor_total": "200.00",
                "estoque_no_momento": self.produto.quantidade,
                "observacao": "",
            }
        ]
        return self.client.post(
            reverse("estoque:pedido_criar"),
            data={
                "data_pedido": timezone.localdate().isoformat(),
                "cliente_id": self.cliente.id,
                "data_prevista_entrega": "",
                "operador": operador,
                "observacao": "",
                "itens_json": json.dumps(itens),
                "proxima_acao": proxima_acao,
            },
            secure=True,
        )

    def test_pedido_exibe_apenas_funcionarios_operadores_no_campo_operador(self):
        operador = Funcionario.objects.create(
            nome="Livia Operadora",
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Marcos Sem Operador",
            pode_operar_sistema=False,
        )
        Funcionario.objects.create(
            nome="Operador Inativo",
            ativo=False,
            pode_operar_sistema=True,
        )

        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)

        self.assertContains(resposta, '<option value="">Sem operador</option>', html=True)
        self.assertContains(resposta, f'<option value="{operador.id}">Livia Operadora</option>', html=True)
        self.assertNotContains(resposta, "Marcos Sem Operador")
        self.assertNotContains(resposta, "Operador Inativo")

    def test_pedido_enter_do_cabecalho_passa_por_operador_sem_observacao(self):
        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)
        conteudo = resposta.content.decode("utf-8")

        self.assertIn('const operadorPedido = document.getElementById("operador");', conteudo)
        self.assertIn('<textarea id="observacao" name="observacao"', conteudo)
        self.assertIn("avancarComEnter(dataPrevistaEntrega, operadorPedido);", conteudo)
        self.assertIn("avancarComEnter(operadorPedido, produtoBusca);", conteudo)
        self.assertIn("if (!operadorPedidoConfirmado) return;", conteudo)
        self.assertNotIn("avancarComEnter(operadorPedido, observacaoPedido);", conteudo)
        self.assertNotIn("avancarComEnter(observacaoPedido, produtoBusca);", conteudo)

    def test_pedido_criar_tem_protecao_para_acoes_perigosas(self):
        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)
        conteudo = resposta.content.decode("utf-8")

        self.assertContains(resposta, 'id="btn-cancelar-form"')
        self.assertIn("Sair sem salvar o pedido?", conteudo)
        self.assertIn("Deseja limpar as sugestoes carregadas?", conteudo)
        self.assertIn("Deseja ocultar as sugestoes deste pedido?", conteudo)
        self.assertIn("Salvar este pedido e abrir o envio para venda?", conteudo)
        self.assertIn("salvamentoEmAndamento", conteudo)
        self.assertIn('index === remocaoPendente ? "Confirmar" : "Remover"', conteudo)

    def test_funcionario_marcado_na_tela_aparece_como_operador_no_pedido(self):
        resposta_funcionario = self.client.post(
            reverse("estoque:funcionarios"),
            data={
                "nome": "Rita Operadora",
                "telefone_whatsapp": "",
                "pode_operar_sistema": "on",
                "ativo": "on",
            },
            secure=True,
        )
        self.assertEqual(resposta_funcionario.status_code, 302)
        operador = Funcionario.objects.get(nome="Rita Operadora")
        self.assertTrue(operador.pode_operar_sistema)

        resposta_pedido = self.client.get(reverse("estoque:pedido_criar"), secure=True)

        self.assertContains(resposta_pedido, f'<option value="{operador.id}">Rita Operadora</option>', html=True)

    def test_salvar_pedido_com_operador_funcionario_grava_e_exibe_nome(self):
        from .models import Pedido

        operador = Funcionario.objects.create(
            nome="Paula Operadora",
            pode_operar_sistema=True,
        )

        resposta = self._post_criar_pedido(operador=str(operador.id))

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(pedido.operador, "Paula Operadora")

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Paula Operadora")

        resposta_lista = self.client.get(reverse("estoque:pedidos"), secure=True)
        self.assertContains(resposta_lista, "Paula Operadora")

    def test_pedido_antigo_sem_operador_continua_exibindo_sem_operador(self):
        resposta = self._post_criar_pedido(operador="")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[dados["pedido_id"]]), secure=True)
        self.assertContains(resposta_detalhe, "Sem operador")

    def test_criar_pedido_com_cliente_e_itens_salva(self):
        """Criar pedido com cliente e itens deve salvar Pedido e ItemPedido"""
        from .models import Pedido, ItemPedido
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            operador="Operador Teste",
            total=100.00,
        )
        
        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )
        
        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(ItemPedido.objects.count(), 1)
        self.assertEqual(item.pedido.id, pedido.id)

    def test_salvar_pedido_normal_continua_redirecionando_para_detalhe(self):
        from .models import Pedido

        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_criar_pedido()

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(dados["redirect_url"], reverse("estoque:pedido_detalhe", args=[pedido.id]))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)

    def test_salvar_pedido_e_enviar_para_venda_redireciona_sem_gravar_venda(self):
        from .models import Pedido

        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_criar_pedido(proxima_acao="enviar_venda")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(dados["redirect_url"], f"{reverse('estoque:vendas')}?pedido_id={pedido.id}")
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(pedido.itens.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)

        resposta_vendas = self.client.get(reverse("estoque:vendas"), {"pedido_id": pedido.id}, secure=True)
        self.assertEqual(resposta_vendas.status_code, 200)
        self.assertContains(resposta_vendas, f"Venda preparada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta_vendas, "Produto Teste")

    def test_gravar_pedido_nao_altera_quantidade_produto(self):
        """Gravar pedido não deve alterar Produto.quantidade"""
        from .models import Pedido, ItemPedido
        
        quantidade_inicial = self.produto.quantidade
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=10,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=1000.00,
            estoque_no_momento=quantidade_inicial,
        )
        
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, quantidade_inicial)

    def test_pedido_nao_cria_conta_receber(self):
        """Pedido não deve criar ContaReceber"""
        from .models import Pedido, ItemPedido
        
        conta_receber_inicial = ContaReceber.objects.count()
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )
        
        self.assertEqual(ContaReceber.objects.count(), conta_receber_inicial)

    def test_item_pedido_salva_estoque_no_momento(self):
        """ItemPedido deve salvar estoque_no_momento corretamente"""
        from .models import Pedido, ItemPedido
        
        estoque_no_momento = self.produto.quantidade
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=estoque_no_momento,
        )
        
        self.assertEqual(item.estoque_no_momento, estoque_no_momento)

    def test_detalhe_pedido_aberto_mostra_enviar_para_venda(self):
        pedido = self._criar_pedido_com_item()

        resposta = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Enviar para Venda")
        self.assertContains(resposta, f'{reverse("estoque:vendas")}?pedido_id={pedido.id}')
        self.assertNotContains(resposta, "Enviar pend")
        self.assertContains(resposta, "Itens do Pedido")
        self.assertContains(resposta, "Total do Pedido")
        self.assertNotContains(resposta, "Itens pendentes")
        self.assertContains(resposta, "Editar Pedido")
        self.assertContains(resposta, "Enviar este pedido para venda? Confira os dados antes de continuar.")
        self.assertContains(resposta, "Tem certeza que deseja cancelar este pedido? O historico sera preservado, mas o pedido deixara de ficar ativo.")

    def test_editar_pedido_aberto_atualiza_itens_sem_baixar_estoque_ou_criar_financeiro(self):
        from .models import ItemPedido

        produto_novo = Produto.objects.create(
            nome="Produto Novo Pedido",
            preco_compra=Decimal("20.00"),
            preco_venda=Decimal("40.00"),
            preco_vista=Decimal("40.00"),
            preco_prazo=Decimal("45.00"),
            quantidade=7,
        )
        pedido = self._criar_pedido_com_item()
        item_original = pedido.itens.get()
        estoque_original = self.produto.quantidade
        estoque_novo = produto_novo.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta_get = self.client.get(reverse("estoque:pedido_editar", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, "Editar Pedido")
        self.assertContains(resposta_get, "Produto Teste")

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_original.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "3.000",
                    "unidade": "Cx",
                    "preco_unitario": "95.50",
                    "valor_total": "286.50",
                    "observacao": "Qtd e preco alterados",
                },
                {
                    "produto_id": produto_novo.id,
                    "produto_nome": produto_novo.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "40.00",
                    "valor_total": "80.00",
                    "observacao": "Produto adicionado",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("pedido_editado=1", dados["redirect_url"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        produto_novo.refresh_from_db()
        self.assertEqual(pedido.total, Decimal("366.50"))
        self.assertEqual(pedido.observacao, "Observacao editada")
        self.assertEqual(self.produto.quantidade, estoque_original)
        self.assertEqual(produto_novo.quantidade, estoque_novo)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemPedido.objects.filter(pedido=pedido).count(), 2)
        item_editado = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_editado.quantidade, Decimal("3.000"))
        self.assertEqual(item_editado.preco_unitario, Decimal("95.50"))
        self.assertEqual(item_editado.unidade, "Cx")
        self.assertEqual(item_editado.observacao, "Qtd e preco alterados")
        self.assertTrue(pedido.itens.filter(produto=produto_novo, quantidade=Decimal("2.000")).exists())

    def test_editar_pedido_existente_substitui_quantidade_corrigida(self):
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5000.003"), total=Decimal("500000.30"))
        item = pedido.itens.get()

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "5",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "valor_total": "500.00",
                    "observacao": "Quantidade corrigida",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        pedido.refresh_from_db()
        item_corrigido = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_corrigido.quantidade, Decimal("5.000"))
        self.assertEqual(item_corrigido.valor_total, Decimal("500.00"))
        self.assertEqual(pedido.total, Decimal("500.00"))

        resposta_decimal = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_corrigido.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "2,5",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "valor_total": "250.00",
                    "observacao": "Quantidade decimal corrigida",
                },
            ],
        )

        self.assertEqual(resposta_decimal.status_code, 200)
        self.assertTrue(resposta_decimal.json()["sucesso"])
        pedido.refresh_from_db()
        item_decimal = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_decimal.quantidade, Decimal("2.500"))
        self.assertEqual(item_decimal.valor_total, Decimal("250.00"))
        self.assertEqual(pedido.total, Decimal("250.00"))

    def test_editar_pedido_convertido_total_bloqueia(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        pedido.status = Pedido.STATUS_CONVERTIDO_EM_VENDA
        pedido.save(update_fields=["status", "atualizado_em"])
        item_original = pedido.itens.get()

        resposta_get = self.client.get(reverse("estoque:pedido_editar", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_get.status_code, 302)
        self.assertEqual(resposta_get.url, reverse("estoque:pedido_detalhe", args=[pedido.id]))

        resposta_post = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_original.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "5.000",
                    "unidade": "Un",
                    "preco_unitario": "10.00",
                    "valor_total": "50.00",
                    "observacao": "",
                }
            ],
        )

        self.assertEqual(resposta_post.status_code, 400)
        self.assertFalse(resposta_post.json()["sucesso"])
        pedido.refresh_from_db()
        item_original.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertEqual(pedido.total, Decimal("200.00"))
        self.assertEqual(item_original.quantidade, Decimal("2.000"))

    def test_editar_pedido_parcial_edita_pendente_sem_mudar_item_ja_vendido(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))
        resposta_venda = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")
        self.assertEqual(resposta_venda.status_code, 200)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        item_pendente = pedido.itens.get(produto=self.produto)
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        estoque_apos_venda = self.produto.quantidade
        vendas_antes = Venda.objects.count()
        itens_venda_antes = ItemVenda.objects.count()
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_pendente.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "120.00",
                    "valor_total": "240.00",
                    "observacao": "Saldo renegociado",
                }
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        pedido.refresh_from_db()
        item_pendente.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("240.00"))
        self.assertEqual(item_pendente.quantidade, Decimal("2.000"))
        self.assertEqual(item_pendente.preco_unitario, Decimal("120.00"))
        self.assertEqual(item_pendente.valor_total, Decimal("240.00"))
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)
        self.assertEqual(Venda.objects.count(), vendas_antes)
        self.assertEqual(ItemVenda.objects.count(), itens_venda_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)

    def test_cancelar_pedido_aberto_marca_cancelado_sem_apagar(self):
        from .models import ItemPedido, Pedido

        pedido = self._criar_pedido_com_item()
        item_id = pedido.itens.get().id

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertTrue(ItemPedido.objects.filter(pk=item_id, pedido=pedido).exists())

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Cancelado")
        self.assertNotContains(resposta_detalhe, "Cancelar Pedido")
        self.assertNotContains(resposta_detalhe, "Editar Pedido")

    def test_cancelar_pedido_cancelado_nao_mexe_no_estoque_nem_financeiro(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()
        vendas_antes = Venda.objects.count()

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), vendas_antes)

    def test_cancelar_pedido_convertido_total_bloqueia(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        pedido.status = Pedido.STATUS_CONVERTIDO_EM_VENDA
        pedido.save(update_fields=["status", "atualizado_em"])
        item_id = pedido.itens.get().id

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertTrue(pedido.itens.filter(pk=item_id).exists())

    def test_cancelar_pedido_parcial_nao_cancela_venda_ja_gerada(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))
        resposta_venda = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")
        self.assertEqual(resposta_venda.status_code, 200)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        venda = Venda.objects.get()
        estoque_apos_venda = self.produto.quantidade
        vendas_antes = Venda.objects.count()
        itens_venda_antes = ItemVenda.objects.count()
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        venda.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertFalse(venda.cancelada)
        self.assertEqual(Venda.objects.count(), vendas_antes)
        self.assertEqual(ItemVenda.objects.count(), itens_venda_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)

    def test_importar_pedido_para_vendas_prepara_tela_sem_gravar(self):
        from .models import Pedido

        quantidade_inicial = self.produto.quantidade
        pedido = self._criar_pedido_com_item()

        resposta = self.client.get(
            reverse("estoque:vendas"),
            {"pedido_id": pedido.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Venda preparada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta, "pedidoImportadoVenda")
        self.assertContains(resposta, "Produto Teste")
        self.assertContains(resposta, f'"produto_id": {self.produto.id}')
        self.assertContains(resposta, f'"detalhe_url": "{reverse("estoque:pedido_detalhe", args=[pedido.id])}"')
        self.assertContains(resposta, f"Voltar ao Pedido #{pedido.id}")
        conteudo = resposta.content.decode("utf-8")
        self.assertLess(
            conteudo.index("let linhaSelecionada = null;"),
            conteudo.rindex("prepararVendaComPedidoImportado();"),
        )
        self.assertContains(resposta, 'produtoBusca.focus({ preventScroll: true });')
        self.assertContains(resposta, 'produtoBusca.scrollIntoView({ behavior: "smooth", block: "center" });')
        self.assertContains(resposta, 'window.setTimeout(() => produtoBusca.focus({ preventScroll: true }), 180);')
        self.assertContains(resposta, 'let pedidoImportadoVenda = JSON.parse')
        self.assertContains(resposta, 'pedido_id: pedidoImportadoVenda?.id || null')
        self.assertContains(resposta, 'function limparPedidoImportadoVenda()')
        self.assertContains(resposta, 'document.querySelector(".pedido-importado-aviso")?.remove();')
        self.assertContains(resposta, 'function limparVendaAposGravacao()')
        self.assertContains(resposta, 'limparVendaAposGravacao();')

        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(self.produto.quantidade, quantidade_inicial)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)

    def test_gravar_venda_a_partir_de_pedido_converte_pedido_apos_sucesso(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id)

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("venda_id", dados)
        self.assertIn("visualizar_url", dados)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(self.produto.quantidade, 48)

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Convertido em venda")
        self.assertContains(resposta_detalhe, "Pedido convertido em venda.")
        self.assertNotContains(resposta_detalhe, "Enviar para Venda")
        self.assertNotContains(resposta_detalhe, "Enviar pend")
        self.assertContains(resposta_detalhe, "Ir para Vendas")
        self.assertContains(resposta_detalhe, "Itens do Pedido")
        self.assertNotContains(resposta_detalhe, "Itens pendentes")

        resposta_lista = self.client.get(reverse("estoque:pedidos"), secure=True)
        self.assertContains(resposta_lista, reverse("estoque:pedido_detalhe", args=[pedido.id]))
        self.assertContains(resposta_lista, "Convertido em venda")

        resposta_abertos = self.client.get(reverse("estoque:pedidos"), {"status": Pedido.STATUS_ABERTO}, secure=True)
        self.assertNotContains(resposta_abertos, reverse("estoque:pedido_detalhe", args=[pedido.id]))

    def test_gravar_venda_de_pedido_com_item_zerado_grava_disponiveis_e_deixa_pendente(self):
        from .models import Pedido, ItemPedido

        produto_zerado = Produto.objects.create(
            nome="Produto Sem Estoque",
            preco_compra=Decimal("10.00"),
            preco_venda=Decimal("50.00"),
            preco_vista=Decimal("50.00"),
            preco_prazo=Decimal("60.00"),
            quantidade=0,
        )
        pedido = self._criar_pedido_com_item()
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_zerado,
            quantidade=Decimal("1.000"),
            unidade="Un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
            estoque_no_momento=0,
        )

        resposta = self._post_gravar_venda_com_itens(
            pedido_id=pedido.id,
            itens=[
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                },
                {
                    "produto_nome": produto_zerado.nome,
                    "quantidade": "1.000",
                    "unidade": "Un",
                    "preco_unitario": "50.00",
                    "subtotal": "50.00",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("itens disponiveis", dados["mensagem"])
        self.assertIn("Produto Sem Estoque", dados["mensagem"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        produto_zerado.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("50.00"))
        self.assertEqual(Venda.objects.count(), 1)
        venda = Venda.objects.get()
        self.assertEqual(venda.total, Decimal("200.00"))
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.get().produto, self.produto)
        self.assertEqual(self.produto.quantidade, 48)
        self.assertEqual(produto_zerado.quantidade, 0)
        item_vendido = pedido.itens.get(produto=self.produto)
        item_pendente = pedido.itens.get(produto=produto_zerado)
        self.assertEqual(item_vendido.quantidade, Decimal("0.000"))
        self.assertEqual(item_vendido.valor_total, Decimal("0.00"))
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        self.assertEqual(item_pendente.valor_total, Decimal("50.00"))

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Parcial")
        self.assertContains(resposta_detalhe, "Pedido parcialmente atendido.")
        self.assertContains(resposta_detalhe, "Itens pendentes do pedido")
        self.assertContains(resposta_detalhe, "Produto Sem Estoque")
        self.assertContains(resposta_detalhe, "Total pendente")
        self.assertContains(resposta_detalhe, "R$ 50.00")
        self.assertNotContains(resposta_detalhe, "Produto Teste")

    def test_gravar_venda_de_pedido_com_estoque_parcial_vende_disponivel_e_deixa_restante(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("Produto Teste: 1 Un", dados["mensagem"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("100.00"))
        self.assertEqual(Venda.objects.count(), 1)
        venda = Venda.objects.get()
        self.assertEqual(venda.total, Decimal("400.00"))
        item = ItemVenda.objects.get()
        self.assertEqual(item.quantidade, Decimal("4.000"))
        self.assertEqual(item.valor_total, Decimal("400.00"))
        self.assertEqual(self.produto.quantidade, 0)
        item_pedido = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_pedido.quantidade, Decimal("1.000"))
        self.assertEqual(item_pedido.valor_total, Decimal("100.00"))

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Itens pendentes do pedido")
        self.assertContains(resposta_detalhe, "Produto Teste")
        self.assertContains(resposta_detalhe, 'data-label="Quantidade">1</td>')
        self.assertNotContains(resposta_detalhe, "1.000")
        self.assertContains(resposta_detalhe, "R$ 100.00")

    def test_venda_de_pedido_parcial_exibe_aviso_na_nota_e_whatsapp(self):
        from .models import Pedido

        self.cliente.whatsapp = "11999999999"
        self.cliente.save(update_fields=["whatsapp"])
        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        venda = Venda.objects.get()
        item_pendente = pedido.itens.get(produto=self.produto)
        estoque_apos_venda = self.produto.quantidade
        contas_apos_venda = ContaReceber.objects.count()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        self.assertEqual(item_pendente.valor_total, Decimal("100.00"))
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, f"Venda parcial gerada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta_detalhe, "Esta nota cont")
        self.assertContains(resposta_detalhe, "Itens pendentes:")
        self.assertContains(resposta_detalhe, "Produto Teste: 1 Un")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)
        self.assertEqual(ContaReceber.objects.count(), contas_apos_venda)
        self.assertEqual(Venda.objects.count(), 1)

        whatsapp_url = views.montar_link_whatsapp_venda(venda)
        mensagem_whatsapp = parse_qs(urlsplit(whatsapp_url).query)["text"][0]
        self.assertIn(f"Pedido #{pedido.id}", mensagem_whatsapp)
        self.assertIn("Itens pendentes:", mensagem_whatsapp)
        self.assertIn("Produto Teste: 1 Un", mensagem_whatsapp)

    def test_venda_parcial_antiga_sem_evento_inferida_pelo_pedido(self):
        from .models import ItemPedido, Pedido

        produto_vendido = Produto.objects.create(
            nome="Produto Vendido Legado",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("20.00"),
            preco_vista=Decimal("20.00"),
            preco_prazo=Decimal("22.00"),
            quantidade=0,
        )
        produto_pendente = Produto.objects.create(
            nome="Produto Pendente Legado",
            preco_compra=Decimal("2.00"),
            preco_venda=Decimal("6.80"),
            preco_vista=Decimal("6.80"),
            preco_prazo=Decimal("7.50"),
            quantidade=0,
        )
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.localdate(),
            status=Pedido.STATUS_PARCIAL,
            total=Decimal("6.80"),
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_vendido,
            quantidade=Decimal("0.000"),
            unidade="Un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("0.00"),
            estoque_no_momento=2,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="Un",
            preco_unitario=Decimal("6.80"),
            valor_total=Decimal("6.80"),
            estoque_no_momento=0,
        )
        venda = Venda.objects.create(
            cliente=self.cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("40.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("40.00"),
        )
        EventoVenda.objects.create(
            venda=venda,
            tipo_evento="venda_gravada",
            descricao="Venda gravada com sucesso. Estoque baixado para os itens vendidos.",
            canal="sistema",
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Venda parcial gerada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta, "Produto Pendente Legado - 1 Un")
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

    def test_venda_normal_sem_pedido_parcial_nao_exibe_aviso(self):
        resposta = self._post_gravar_venda_com_item()

        self.assertEqual(resposta.status_code, 200)
        venda = Venda.objects.get()

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertNotContains(resposta_detalhe, "Venda parcial gerada a partir do Pedido")
        self.assertNotContains(resposta_detalhe, "Esta nota contém os itens disponíveis agora")
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

    def test_detalhe_pedido_parcial_antigo_calcula_saldo_pendente_pela_venda_compativel(self):
        from .models import Pedido, ItemPedido

        produto_vendido = Produto.objects.create(
            nome="Produto Vendido Pedido Parcial",
            preco_compra=Decimal("10.00"),
            preco_venda=Decimal("48.00"),
            preco_vista=Decimal("48.00"),
            preco_prazo=Decimal("48.00"),
            quantidade=0,
        )
        produto_pendente = Produto.objects.create(
            nome="Produto Pendente Pedido Parcial",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("10.50"),
            preco_vista=Decimal("10.50"),
            preco_prazo=Decimal("10.50"),
            quantidade=0,
        )
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=Decimal("127.50"),
            status=Pedido.STATUS_ABERTO,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("48.00"),
            valor_total=Decimal("96.00"),
            estoque_no_momento=2,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_pendente,
            quantidade=Decimal("3.000"),
            unidade="Un",
            preco_unitario=Decimal("10.50"),
            valor_total=Decimal("31.50"),
            estoque_no_momento=0,
        )
        venda = Venda.objects.create(
            cliente=self.cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("96.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("48.00"),
            valor_total=Decimal("96.00"),
        )
        pedido.status = Pedido.STATUS_PARCIAL
        pedido.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Itens pendentes do pedido")
        self.assertContains(resposta, "Produto Pendente Pedido Parcial")
        self.assertContains(resposta, 'data-label="Quantidade">3</td>')
        self.assertNotContains(resposta, "3.000")
        self.assertContains(resposta, "R$ 31.50")
        self.assertNotContains(resposta, "Produto Vendido Pedido Parcial")
        self.assertContains(resposta, "Enviar pend")
        self.assertContains(resposta, f'{reverse("estoque:vendas")}?pedido_id={pedido.id}')
        self.assertNotContains(resposta, ">Ir para Venda<")

        resposta_vendas = self.client.get(reverse("estoque:vendas"), {"pedido_id": pedido.id}, secure=True)
        self.assertEqual(resposta_vendas.status_code, 200)
        conteudo_vendas = resposta_vendas.content.decode("utf-8")
        self.assertIn("Venda preparada a partir do Pedido", conteudo_vendas)
        self.assertIn('"produto_nome": "Produto Pendente Pedido Parcial"', conteudo_vendas)
        self.assertIn('"quantidade": "3.000"', conteudo_vendas)
        self.assertIn('"valor_total": "31.50"', conteudo_vendas)
        self.assertNotIn('"produto_nome": "Produto Vendido Pedido Parcial"', conteudo_vendas)

    def test_gravar_venda_de_pedido_sem_estoque_nao_grava_e_mantem_aberto(self):
        from .models import Pedido

        self.produto.quantidade = 0
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id)

        self.assertEqual(resposta.status_code, 400)
        dados = resposta.json()
        self.assertFalse(dados["sucesso"])
        self.assertEqual(
            dados["mensagem"],
            f"Nenhum item do Pedido #{pedido.id} possui estoque disponivel para gerar venda. "
            "Os itens continuam pendentes no pedido.",
        )
        self.assertEqual(dados["toast_duracao_ms"], 12000)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertEqual(self.produto.quantidade, 0)

    def test_gravar_venda_sem_pedido_id_nao_altera_pedidos_abertos(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item()

        self.assertEqual(resposta.status_code, 200)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(Venda.objects.count(), 1)

    def test_lista_de_pedidos_carrega(self):
        """Lista de pedidos deve carregar corretamente"""
        from .models import Pedido
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        url = reverse("estoque:pedidos")
        resposta = self.client.get(url, secure=True)
        
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("pedidos", resposta.context)
        self.assertIn("localidades", resposta.context)

    def test_lista_de_pedidos_filtra_por_bairro_ou_cidade_do_cliente(self):
        from .models import Pedido

        self.cliente.bairro = "Centro"
        self.cliente.cidade = "Fortaleza"
        self.cliente.save(update_fields=["bairro", "cidade"])
        cliente_outro = Cliente.objects.create(
            nome="Cliente Outra Localidade",
            bairro="Aldeota",
            cidade="Caucaia",
            ativo=True,
        )
        pedido_centro = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=Decimal("100.00"),
        )
        pedido_outro = Pedido.objects.create(
            cliente=cliente_outro,
            data_pedido=timezone.now().date(),
            total=Decimal("80.00"),
        )

        resposta_bairro = self.client.get(reverse("estoque:pedidos"), {"localidade": "Centro"}, secure=True)
        self.assertEqual(resposta_bairro.status_code, 200)
        self.assertContains(resposta_bairro, reverse("estoque:pedido_detalhe", args=[pedido_centro.id]))
        self.assertNotContains(resposta_bairro, reverse("estoque:pedido_detalhe", args=[pedido_outro.id]))
        self.assertContains(resposta_bairro, "Centro")

        resposta_cidade = self.client.get(reverse("estoque:pedidos"), {"localidade": "Caucaia"}, secure=True)
        self.assertEqual(resposta_cidade.status_code, 200)
        self.assertContains(resposta_cidade, reverse("estoque:pedido_detalhe", args=[pedido_outro.id]))
        self.assertNotContains(resposta_cidade, reverse("estoque:pedido_detalhe", args=[pedido_centro.id]))

    def test_lista_de_pedidos_mantem_filtros_atuais_com_localidade(self):
        from .models import Pedido

        hoje = timezone.now().date()
        self.cliente.bairro = "Messejana"
        self.cliente.cidade = "Fortaleza"
        self.cliente.save(update_fields=["bairro", "cidade"])
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=hoje,
            status=Pedido.STATUS_CANCELADO,
            total=Decimal("55.00"),
        )

        resposta = self.client.get(
            reverse("estoque:pedidos"),
            {
                "status": Pedido.STATUS_CANCELADO,
                "cliente_id": self.cliente.id,
                "localidade": "Messejana",
                "data_inicio": hoje.isoformat(),
                "data_fim": hoje.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"#{pedido.id}")
        self.assertContains(resposta, "Cancelado")
        self.assertContains(resposta, "Messejana")

    def test_criar_pedido_retorna_sugestoes_por_vendas_ativas_do_cliente(self):
        """Sugestoes de pedido devem usar ultimas vendas ativas do cliente"""
        hoje = timezone.now().date()
        venda_antiga = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje - timedelta(days=2),
            total=100,
            cancelada=False,
        )
        venda_recente = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje,
            total=200,
            cancelada=False,
        )
        venda_cancelada = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje,
            total=300,
            cancelada=True,
        )
        produto_cancelado = Produto.objects.create(
            nome="Produto Cancelado",
            preco_compra=10,
            preco_venda=20,
            preco_vista=20,
            preco_prazo=25,
            quantidade=5,
        )

        ItemVenda.objects.create(
            venda=venda_antiga,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100,
            valor_total=100,
        )
        ItemVenda.objects.create(
            venda=venda_recente,
            produto=self.produto,
            quantidade=2,
            unidade="Un",
            preco_unitario=90,
            valor_total=180,
        )
        ItemVenda.objects.create(
            venda=venda_cancelada,
            produto=produto_cancelado,
            quantidade=3,
            unidade="Un",
            preco_unitario=20,
            valor_total=60,
        )

        resposta = self.client.get(
            reverse("estoque:pedido_criar"),
            {"sugestoes_cliente_id": self.cliente.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        sugestoes = resposta.json()["sugestoes"]
        self.assertEqual(len(sugestoes), 1)
        self.assertEqual(sugestoes[0]["produto"], self.produto.nome)
        self.assertEqual(sugestoes[0]["quantidade"], "2")
        self.assertEqual(sugestoes[0]["preco"], "R$ 90,00")
        self.assertEqual(sugestoes[0]["preco_valor"], "90.00")
        self.assertEqual(sugestoes[0]["produto_id"], self.produto.id)
        self.assertEqual(sugestoes[0]["unidade"], "Un")
        self.assertEqual(sugestoes[0]["frequencia"], 2)

    def test_detalhe_de_pedido_carrega(self):
        """Detalhe do pedido deve carregar corretamente"""
        from .models import Pedido, ItemPedido
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )
        produto_decimal = Produto.objects.create(
            nome="Produto Quantidade Decimal Pedido",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("12.00"),
            preco_vista=Decimal("12.00"),
            preco_prazo=Decimal("12.00"),
            quantidade=5,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_decimal,
            quantidade=Decimal("2.500"),
            unidade="Un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("30.00"),
            estoque_no_momento=5,
        )
        
        url = reverse("estoque:pedido_detalhe", args=[pedido.id])
        resposta = self.client.get(url, secure=True)
        
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("pedido", resposta.context)
        self.assertContains(resposta, 'data-label="Quantidade">1</td>')
        self.assertContains(resposta, 'data-label="Quantidade">2,5</td>')

    def test_pedido_com_produto_sem_estoque_pode_ser_gravado(self):
        """Pedido com produto sem estoque suficiente deve poder ser gravado (apenas aviso)"""
        from .models import Pedido, ItemPedido
        
        self.produto.quantidade = 0
        self.produto.save()
        
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )
        
        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=10,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=1000.00,
            estoque_no_momento=0,
        )
        
        self.assertEqual(item.quantidade, 10)
        self.assertEqual(item.estoque_no_momento, 0)

    def test_analisar_comprovante_pix_picpay_data_mes_abreviado_e_instituicao(self):
        datas_ocr = [
            "21/set/2025 - 13:20:41",
            "21 / set / 2025 - 13:20:41",
            "21/set/2025 13:20",
            "21/set/2025 - 13:20",
            "21/set/2025 - 13.20.41",
            "21/set/2025 13h20",
            "21/set/2025 às 13h20",
            "21/set/2025 as 13h20",
            "21/set./2025 - 13:20:41",
            "21 / set. / 2025 - 13:20:41",
            "21/5et/2O25 - l3:2O:41",
            "21/sct/2025 - 13.2O.41",
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for indice, data_ocr in enumerate(datas_ocr):
            with self.subTest(data_ocr=data_ocr):
                conteudo = (
                    "PicPay\n"
                    "Comprovante de Pix\n"
                    f"{data_ocr}\n"
                    "Valor\n"
                    "R$ 50,00\n"
                    "De\n"
                    "ISA ALVES DE SOUZA\n"
                    "PIC PAY\n"
                ).encode("utf-8")
                arquivo = SimpleUploadedFile(f"picpay-{indice}.txt", conteudo, content_type="text/plain")

                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["valor"], "50.00")
                self.assertEqual(dados["data_pagamento"], "2025-09-21T13:20")
                self.assertEqual(dados["pagador"], "ISA ALVES DE SOUZA")
                self.assertEqual(dados["instituicao_pix"], "PicPay")
                self.assertIn("Data enviada ao frontend: 2025-09-21T13:20", dados["debug_data_pagamento"])

    def test_analisar_comprovante_pix_lida_com_datas_e_valores_comuns_sem_fallback_atual(self):
        casos = [
            (
                "mercado_maio_as.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 as 8h42\nValor\nR$ 50,00\nPagador: Maria Silva\n",
                "50.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "mercado_maio_acento.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 às 8h42\nR$ 100\nPagador: Jose Silva\n",
                "100.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "nubank_mai.txt",
                "Nu Pagamentos\nComprovante Pix\n05 MAI 2026 - 17:36\nValor R$ 50,00\nOrigem\nNome: Ana Souza\n",
                "50.00",
                "2026-05-05T17:36",
                "Nubank",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome, conteudo, valor_esperado, data_esperada, instituicao_esperada in casos:
            with self.subTest(nome=nome):
                arquivo = SimpleUploadedFile(nome, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)
                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["nome_arquivo"], nome)
                self.assertEqual(dados["valor"], valor_esperado)
                self.assertEqual(dados["data_pagamento"], data_esperada)
                self.assertEqual(dados["instituicao_pix"], instituicao_esperada)

    def test_analisar_comprovante_pix_banco_inter_prioriza_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Dados do pagador\n"
            "Nome: Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_extrair_valor_mercado_pago_sem_separador_decimal_11150_vira_111_50(self):
        """Problema: OCR Mercado Pago retorna 'R$ 11150' em vez de 'R$ 111,50'.
        Esperado: valor deve ser interpretado como 111,50 (últimos 2 dígitos são centavos).
        """
        conteudo = (
            "Comprovante de Pix\n"
            "23/maio/2026 as 18:55:32\n"
            "R$ 11150\n"
            "De\n"
            "Joao de Almeida E Silva\n"
            "Para\n"
            "Lincoln Albuquerque Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("mercado-pago-sem-separador.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "111.50", f"Erro: OCR retornou '{dados['valor']}' em vez de '111.50'")
        self.assertEqual(dados["data_pagamento"], "2026-05-23T18:55")

    def test_extrair_valor_com_virgula_345_00_continua_funcionando(self):
        """Garantir que valores com vírgula como 'R$ 345,00' continuam sendo extraídos corretamente."""
        conteudo = (
            "Comprovante de Pix\n"
            "Valor R$ 345,00\n"
            "Data 10/06/2026 14:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-virgula.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "345.00")

    def test_extrair_valor_com_ponto_e_virgula_1449_08_continua_funcionando(self):
        """Garantir que valores com ponto e virgula como 'R$ 1.449,08' continuam sendo extraídos corretamente."""
        conteudo = (
            "Comprovante de Pix\n"
            "Valor R$ 1.449,08\n"
            "Data 10/06/2026 14:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-ponto-virgula.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "1449.08")

    def test_analisar_comprovante_pix_banco_inter_nao_usa_recebedor_como_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_normaliza_horario_com_o_no_lugar_de_zero(self):
        conteudo = (
            "Domingo, 17/05/2026\n"
            "O8h09\n"
            "Valor R$ 5,00\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_inter_whatsapp_ocr_real_extrai_pagador(self):
        conteudo = (
            "Sobre a transagao\n"
            "Data da transacao\n"
            "Horario\n"
            "\n"
            "Identificador\n"
            "\n"
            "ID da transacao\n"
            "\n"
            "sinter\n"
            "\n"
            "Pix enviado\n"
            "R$ 5,00\n"
            "\n"
            "Domingo, 17/05/2026\n"
            "\n"
            "O8h09\n"
            "\n"
            "\u00a300416968202605171109huk671IHk1t\n"
            "\n"
            "Quem recebeu\n"
            "\n"
            "Nome\n"
            "\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Chave Pix\n"
            "\n"
            "Quem pagou\n"
            "Nome\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Lincoin Albuquerque Neiva\n"
            "\n"
            "*#*,319.532-\u00ab*\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "\n"
            "+5591984111011\n"
            "\n"
            "FRANCISCO NETO DA SILVA MIRANDA\n"
            "\n"
            "***,467.952-**\n"
            "\n"
            "BANCO INTER\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "FRANCISCO NETO DA SILVA MIRANDA")
        self.assertNotEqual(dados["pagador"], "Lincoin Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "NU PAGAMENTOS - IP")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_instituicao_do_cabecalho(self):
        conteudo = (
            "BANCO DO ESTADO DO PAR\u00c1 S.A. - BANPAR\u00c1\n"
            "COMPROVANTE DE PIX\n"
            "Data da Opera\u00e7\u00e3o: 03/02/2026 17:59:09\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Institui\u00e7\u00e3o: NU PAGAMENTOS - IP\n"
            "Valor: 1.449,08\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-banpara.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["instituicao_pix"], "Banpar\u00e1")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "1449.08")
        self.assertEqual(dados["data_pagamento"], "2026-02-03T17:59")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_caixa_tem_usa_quem_vai_enviar(self):
        cliente = Cliente.objects.create(nome="ELIANA NAZARE DA SILVA FERREIRA", ativo=True)
        conteudo = (
            "CAIXA Tem\n"
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "\n"
            "Valor\n"
            "R$ 826,62\n"
            "\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque\n"
            "Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA\n"
            "SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "\n"
            "Dados da transa\u00e7\u00e3o\n"
            "ID\n"
            "E003603052026051711183ffbb3dd95\n"
            "NSU\n"
            "64892124094\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-caixa-tem.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertEqual(dados["instituicao_pix"], "Caixa Econ\u00f4mica Federal")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_usa_titular_da_origem(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao: 17/05/2026 01:54:04\n"
            "Codigo da Sessao: 123456\n"
            "\n"
            "Dados de Origem\n"
            "Titular: LINCOLN ALBUQUERQUE NEIVA\n"
            "Agencia: 0057\n"
            "Conta: 000341187-7\n"
            "Tipo de Conta: CC\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao: NU PAGAMENTOS - IP\n"
            "Titular: RECEBEDOR ERRADO\n"
            "CPF: ***.319.532-**\n"
            "Agencia: 0001\n"
            "Conta: 123\n"
            "Tipo de Conta: Conta de Pagamento\n"
            "\n"
            "ID Transacao: E123\n"
            "Tipo de Pagamento: Chave\n"
            "Finalidade: Compra/Transferencia\n"
            "Valor: 4,40\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "LINCOLN ALBUQUERQUE NEIVA")
        self.assertNotEqual(dados["pagador"], "RECEBEDOR ERRADO")
        self.assertEqual(dados["valor"], "4.40")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T01:54")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_origem_e_data_operacao(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao:                  05/05/2026 16:45:50\n"
            "Codigo da Sessao:                  APP00570000023744PP639135962448580000\n"
            "\n"
            "Dados de Origem\n"
            "Titular:                           RUBEM ARRUDA DE SOUZA\n"
            "Agencia:                           0057\n"
            "Conta:                             000002374-4\n"
            "Tipo de Conta:                     PP\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao:                       NU PAGAMENTOS - IP\n"
            "Titular:                           Lincoln Albuquerque Neiva\n"
            "CPF:                               ***.319.532-**\n"
            "Agencia:                           0001\n"
            "Conta:                             088228354-6\n"
            "Tipo de Conta:                     Conta de Pagamento\n"
            "\n"
            "ID Transacao:                      E04913711202605051945RF704X8VJHS\n"
            "Tipo de Pagamento:                 Chave\n"
            "Finalidade:                        Compra/Transferencia\n"
            "Valor:                             847,70\n"
            "Autenticacao:                      209002000000000002633000000.16\n"
            "                                   00530000087366805058084946550.44\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_data_operacao_quebrada(self):
        conteudo = (
            "BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operacao:\n"
            "05/05/2026\n"
            "16:45:50\n"
            "Codigo da Sessao: APP00570000023744PP639135962448580000\n"
            "Dados de Origem\n"
            "Titular:\n"
            "RUBEM ARRUDA DE SOUZA\n"
            "Agencia: 0057\n"
            "Conta: 000002374-4\n"
            "Tipo de Conta: PP\n"
            "Dados do Recebedor\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")

    def test_analisar_comprovante_pix_banco_inter_caso_real_recebido(self):
        conteudo = (
            "Inter\n"
            "Comprovante de Pix recebido\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "Beneficiario\n"
            "Lincoln Albuquerque Neiva\n"
            "Chave Pix do recebedor\n"
            "Instituicao do recebedor\n"
            "Banco Inter\n"
            "De\n"
            "Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banco_inter_bloqueia_nome_recebedor_conhecido(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "De\n"
            "Lincoln Albuquerque\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")

    def test_analisar_comprovante_pix_banco_inter_reconstroi_destino_nome_quebrado(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituicao BANCO INTER\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_sugere_cliente_com_jr_e_nome_intermediario(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["cliente_sugerido_nome"], "Ivanildo Patricio Jr")
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nome_generico_nao_sugere_cliente(self):
        Cliente.objects.create(nome="Maria Silva", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Maria")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "baixa")

    def test_analisar_comprovante_pix_nao_sugere_cliente_quando_ambiguo(self):
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "ambigua")
        self.assertIn("mais de um cliente parecido", dados["mensagem_cliente"])

    def test_analisar_comprovante_pix_nao_usa_instituicao_como_pagador(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Valor R$ 156,50\n"
            "16/05/2026 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")

    def test_analisar_comprovante_pix_falha_sem_bloquear_preenchimento_manual(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"texto sem dados de pix", content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertIn("Preencha manualmente", dados["mensagem"])
        self.assertIsNone(dados["cliente_sugerido_id"])

    def test_central_pix_bloqueia_duplicado_pendente_evidente(self):
        cliente = Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 51))
        pix_parecido = PixRecebido.objects.create(
            nome_pagador="Joelson Ferreira dos Santos",
            valor="156.50",
            data_pagamento=data_pagamento,
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(reverse("estoque:central_pix"), data={
            "cliente": cliente.id,
            "nome_pagador": "  joelson ferreira dos santos ",
            "valor": "156.50",
            "data_pagamento": "2026-05-16T17:52",
            "observacao": "Tentativa duplicada",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(PixRecebido.objects.count(), 2)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_parecido)
        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": novo_pix.id})
        self.assertTrue(any(url.startswith(detalhe_url) for url, _status in resposta.redirect_chain))
        mensagem = f"Pix salvo como possivel duplicado. Confira a comparacao com o Pix #{pix_parecido.id}."
        self.assertContains(resposta, mensagem, count=1)
        self.assertContains(resposta, f"Pix parecido #{pix_parecido.id}")
