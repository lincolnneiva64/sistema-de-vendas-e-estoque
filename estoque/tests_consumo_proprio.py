import json
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from . import views
from .models import (
    CatalogoDespesa,
    Cliente,
    ContaReceber,
    DespesaDiaria,
    ItemVenda,
    MovimentoFinanceiro,
    Produto,
    Venda,
)


class VendaConsumoProprioTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nome="Cliente Normal")
        self.cliente_consumo = Cliente.objects.create(nome="Lincoln Neiva")
        self.cliente_roseli = Cliente.objects.create(nome="Roseli da Costa Gama")
        self.cliente_camila = Cliente.objects.create(nome="Camila Gama Neiva")
        self.cliente_lorena = Cliente.objects.create(nome="Lorena Gama Neiva")
        self.cliente_deposito = Cliente.objects.create(nome="Deposito L A Neiva")
        self.produto = Produto.objects.create(
            nome="Produto Teste Consumo",
            categoria="Cervejas",
            preco_compra=Decimal("6.00"),
            preco_vista=Decimal("10.00"),
            preco_prazo=Decimal("12.00"),
            unidade_compra="UN",
            quantidade=Decimal("10.000"),
        )
        self.produto_extra = Produto.objects.create(
            nome="Produto Extra Consumo",
            categoria="Cervejas",
            preco_compra=Decimal("3.00"),
            preco_vista=Decimal("5.00"),
            preco_prazo=Decimal("6.00"),
            unidade_compra="UN",
            quantidade=Decimal("5.000"),
        )
        self.produto_alimento = Produto.objects.create(
            nome="Produto Alimento Consumo",
            categoria="Estivas",
            preco_compra=Decimal("2.00"),
            preco_vista=Decimal("4.70"),
            preco_prazo=Decimal("5.00"),
            unidade_compra="UN",
            quantidade=Decimal("5.000"),
        )
        self.produto_sem_regra_empresa = Produto.objects.create(
            nome="Produto Sem Regra Empresa",
            categoria="Papelaria",
            preco_compra=Decimal("1.50"),
            preco_vista=Decimal("7.00"),
            preco_prazo=Decimal("8.00"),
            unidade_compra="UN",
            quantidade=Decimal("5.000"),
        )
        self.catalogo_lazer = CatalogoDespesa.objects.create(
            nome="Cerveja / Bar",
            tipo=CatalogoDespesa.TIPO_PESSOAL,
            grupo="Lazer",
            categoria="Cerveja / Bar",
            pessoa="Lincoln",
            ativo=True,
        )
        self.catalogo_alimentacao = CatalogoDespesa.objects.create(
            nome="Mercado",
            tipo=CatalogoDespesa.TIPO_PESSOAL,
            grupo="Alimentação",
            categoria="Mercado",
            pessoa="Lincoln",
            ativo=True,
        )

    def payload_venda(self, tipo_pagamento, cliente=None, quantidade="2,000", preco="10,00", produto=None):
        produto = produto or self.produto
        return {
            "cliente_id": cliente.pk if cliente else self.cliente.pk,
            "data_venda": "2026-09-27",
            "tipo_pagamento": tipo_pagamento,
            "operador": "Teste",
            "itens": [
                {
                    "produto_id": produto.pk,
                    "produto_nome": produto.nome,
                    "quantidade": quantidade,
                    "unidade": "UN",
                    "preco_unitario": preco,
                }
            ],
        }

    def postar_venda(self, payload):
        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(payload),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(resposta.status_code, 200, resposta.content)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"], dados)
        return Venda.objects.get(pk=dados["venda_id"])

    def postar_edicao(self, venda, tipo_pagamento, quantidade="2,000", preco="10,00"):
        payload = self.payload_venda(
            tipo_pagamento,
            cliente=venda.cliente,
            quantidade=quantidade,
            preco=preco,
        )
        item = ItemVenda.objects.get(venda=venda)
        payload["venda_id"] = venda.pk
        payload["itens"][0]["item_id"] = item.pk
        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(payload),
            content_type="application/json",
            secure=True,
        )
        return resposta

    def assert_edicao_bloqueada(self, resposta):
        self.assertEqual(resposta.status_code, 409, resposta.content)
        dados = resposta.json()
        self.assertFalse(dados["sucesso"], dados)

    def despesas_consumo(self, venda):
        return DespesaDiaria.objects.filter(
            venda_origem=venda,
            origem_automatica=DespesaDiaria.ORIGEM_AUTOMATICA_CONSUMO_PROPRIO,
        ).order_by("chave_automatica")

    def test_venda_normal_a_vista_continua_gerando_movimento_financeiro(self):
        venda = self.postar_venda(self.payload_venda("A vista"))

        movimento = MovimentoFinanceiro.objects.get(origem="venda")
        self.assertEqual(movimento.tipo, MovimentoFinanceiro.TIPO_ENTRADA)
        self.assertEqual(movimento.valor, Decimal("20.00"))
        self.assertIn(f"Venda a vista #{venda.id}", movimento.descricao)
        self.assertFalse(DespesaDiaria.objects.filter(venda_origem=venda).exists())

    def test_venda_normal_a_prazo_continua_gerando_conta_receber(self):
        venda = self.postar_venda(self.payload_venda("A prazo"))

        conta = ContaReceber.objects.get(venda=venda)
        self.assertEqual(conta.valor_original, Decimal("20.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("20.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)
        self.assertFalse(DespesaDiaria.objects.filter(venda_origem=venda).exists())

    def test_venda_consumo_proprio_nao_gera_financeiro_nem_conta_receber(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        faturamento = Venda.objects.filter(cancelada=False).aggregate(
            total=Sum("total")
        )["total"]

        self.assertEqual(venda.tipo_pagamento, Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        self.assertEqual(venda.tipo_pagamento, "consumo_proprio")
        self.assertEqual(len(venda.tipo_pagamento), 15)
        self.assertEqual(venda.total, Decimal("20.00"))
        self.assertEqual(faturamento, Decimal("20.00"))
        self.assertEqual(self.produto.quantidade, Decimal("8.000"))
        self.assertEqual(item.custo_total_snapshot, Decimal("12.00"))
        self.assertFalse(MovimentoFinanceiro.objects.filter(origem="venda").exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_despesa_automatica_consumo_fica_vinculada_sem_saida_e_sem_duplicar(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        self.assertEqual(
            despesa.origem_automatica,
            DespesaDiaria.ORIGEM_AUTOMATICA_CONSUMO_PROPRIO,
        )
        self.assertEqual(despesa.chave_automatica, f"catalogo:{self.catalogo_lazer.pk}")
        self.assertEqual(despesa.catalogo, self.catalogo_lazer)
        self.assertEqual(despesa.valor, Decimal("20.00"))
        self.assertEqual(despesa.forma_pagamento, DespesaDiaria.FORMA_OUTRO)
        self.assertFalse(
            MovimentoFinanceiro.objects.filter(origem="despesa_diaria").exists()
        )

        views._sincronizar_despesas_consumo_proprio(venda)

        self.assertEqual(DespesaDiaria.objects.filter(venda_origem=venda).count(), 1)

    def test_venda_normal_nao_gera_despesa_automatica(self):
        venda = self.postar_venda(self.payload_venda("Pix"))

        self.assertFalse(DespesaDiaria.objects.filter(venda_origem=venda).exists())

    def test_cancelamento_consumo_proprio_zera_despesa_automatica_vinculada(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        venda.cancelada = True
        venda.save(update_fields=["cancelada", "atualizado_em"])
        views._sincronizar_despesas_consumo_proprio(venda)

        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        self.assertEqual(despesa.valor, Decimal("0.00"))
        self.assertFalse(
            MovimentoFinanceiro.objects.filter(origem="despesa_diaria").exists()
        )

    def test_bloqueia_conversao_a_prazo_para_consumo_proprio_sem_modificar_venda(self):
        venda = self.postar_venda(self.payload_venda("A prazo"))
        conta = ContaReceber.objects.get(venda=venda)
        estoque_antes = Produto.objects.get(pk=self.produto.pk).quantidade

        resposta = self.postar_edicao(
            venda,
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            quantidade="3,000",
            preco="11,00",
        )

        self.assert_edicao_bloqueada(resposta)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        self.assertEqual(venda.tipo_pagamento, "A prazo")
        self.assertEqual(venda.total, Decimal("20.00"))
        self.assertEqual(conta.valor_original, Decimal("20.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("20.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)
        self.assertFalse(DespesaDiaria.objects.filter(venda_origem=venda).exists())
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(item.quantidade, Decimal("2.000"))
        self.assertEqual(item.preco_unitario, Decimal("10.00"))

    def test_bloqueia_conversao_a_vista_para_consumo_proprio_sem_modificar_venda(self):
        venda = self.postar_venda(self.payload_venda("A vista"))
        movimento = MovimentoFinanceiro.objects.get(origem="venda")
        estoque_antes = Produto.objects.get(pk=self.produto.pk).quantidade

        resposta = self.postar_edicao(
            venda,
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            quantidade="3,000",
            preco="11,00",
        )

        self.assert_edicao_bloqueada(resposta)
        venda.refresh_from_db()
        movimento.refresh_from_db()
        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        self.assertEqual(venda.tipo_pagamento, "\u00c0 vista")
        self.assertEqual(venda.total, Decimal("20.00"))
        self.assertEqual(movimento.valor, Decimal("20.00"))
        self.assertEqual(movimento.tipo, MovimentoFinanceiro.TIPO_ENTRADA)
        self.assertFalse(DespesaDiaria.objects.filter(venda_origem=venda).exists())
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(item.quantidade, Decimal("2.000"))
        self.assertEqual(item.preco_unitario, Decimal("10.00"))

    def test_bloqueia_conversao_consumo_proprio_para_a_vista(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )
        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        estoque_antes = Produto.objects.get(pk=self.produto.pk).quantidade

        resposta = self.postar_edicao(venda, "A vista", quantidade="3,000", preco="11,00")

        self.assert_edicao_bloqueada(resposta)
        venda.refresh_from_db()
        despesa.refresh_from_db()
        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        self.assertEqual(venda.tipo_pagamento, Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        self.assertEqual(venda.total, Decimal("20.00"))
        self.assertEqual(despesa.valor, Decimal("20.00"))
        self.assertFalse(MovimentoFinanceiro.objects.filter(origem="venda").exists())
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(item.quantidade, Decimal("2.000"))
        self.assertEqual(item.preco_unitario, Decimal("10.00"))

    def test_carregar_edicao_consumo_proprio_preserva_tipo_no_contexto(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        resposta = self.client.get(
            f"{reverse('estoque:vendas')}?editar={venda.pk}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta.context["venda_edicao"]["tipo_pagamento"],
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
        )
        self.assertContains(resposta, 'value="consumo_proprio"')

    def test_bloqueia_conversao_consumo_proprio_para_a_prazo(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )
        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        estoque_antes = Produto.objects.get(pk=self.produto.pk).quantidade

        resposta = self.postar_edicao(venda, "A prazo", quantidade="3,000", preco="11,00")

        self.assert_edicao_bloqueada(resposta)
        venda.refresh_from_db()
        despesa.refresh_from_db()
        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        self.assertEqual(venda.tipo_pagamento, Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        self.assertEqual(venda.total, Decimal("20.00"))
        self.assertEqual(despesa.valor, Decimal("20.00"))
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(item.quantidade, Decimal("2.000"))
        self.assertEqual(item.preco_unitario, Decimal("10.00"))

    def test_edicao_consumo_proprio_para_consumo_proprio_sincroniza_sem_duplicar(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        resposta = self.postar_edicao(
            venda,
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            quantidade="3,000",
            preco="11,00",
        )

        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()["sucesso"])
        venda.refresh_from_db()
        self.produto.refresh_from_db()
        item = ItemVenda.objects.get(venda=venda)
        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        self.assertEqual(venda.tipo_pagamento, Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        self.assertEqual(venda.total, Decimal("33.00"))
        self.assertEqual(item.quantidade, Decimal("3.000"))
        self.assertEqual(item.preco_unitario, Decimal("11.00"))
        self.assertEqual(self.produto.quantidade, Decimal("7.000"))
        self.assertEqual(despesa.valor, Decimal("33.00"))
        self.assertEqual(DespesaDiaria.objects.filter(venda_origem=venda).count(), 1)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_edicao_consumo_proprio_adicionando_item_sincroniza_sem_financeiro(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )
        item = ItemVenda.objects.get(venda=venda)
        despesa_original = DespesaDiaria.objects.get(venda_origem=venda)

        payload = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=venda.cliente,
        )
        payload["venda_id"] = venda.pk
        payload["itens"] = [
            {
                "item_id": item.pk,
                "produto_id": self.produto.pk,
                "produto_nome": self.produto.nome,
                "quantidade": "2,000",
                "unidade": "UN",
                "preco_unitario": "10,00",
            },
            {
                "produto_id": self.produto_extra.pk,
                "produto_nome": self.produto_extra.nome,
                "quantidade": "1,000",
                "unidade": "UN",
                "preco_unitario": "5,00",
            },
        ]

        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(payload),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()["sucesso"])
        venda.refresh_from_db()
        self.produto.refresh_from_db()
        self.produto_extra.refresh_from_db()
        itens = ItemVenda.objects.filter(venda=venda).order_by("id")
        despesa = DespesaDiaria.objects.get(venda_origem=venda)

        self.assertEqual(venda.tipo_pagamento, Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO)
        self.assertEqual(venda.total, Decimal("25.00"))
        self.assertEqual(self.produto.quantidade, Decimal("8.000"))
        self.assertEqual(self.produto_extra.quantidade, Decimal("4.000"))
        self.assertEqual(DespesaDiaria.objects.filter(venda_origem=venda).count(), 1)
        self.assertEqual(despesa.pk, despesa_original.pk)
        self.assertEqual(despesa.valor, Decimal("25.00"))
        self.assertEqual(despesa.catalogo, self.catalogo_lazer)
        self.assertEqual(
            sum((item.custo_total_snapshot for item in itens), Decimal("0.00")),
            Decimal("15.00"),
        )
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_consumo_proprio_dois_produtos_mesmo_catalogo_gera_uma_despesa(self):
        payload = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=self.cliente_consumo,
        )
        payload["itens"].append({
            "produto_id": self.produto_extra.pk,
            "produto_nome": self.produto_extra.nome,
            "quantidade": "1,000",
            "unidade": "UN",
            "preco_unitario": "5,00",
        })

        venda = self.postar_venda(payload)
        despesas = list(self.despesas_consumo(venda))

        self.assertEqual(len(despesas), 1)
        self.assertEqual(despesas[0].catalogo, self.catalogo_lazer)
        self.assertEqual(despesas[0].valor, Decimal("25.00"))
        self.assertEqual(sum((d.valor for d in despesas), Decimal("0.00")), venda.total)

    def test_consumo_proprio_lideres_com_alimentacao_preserva_classificacao_pessoal(self):
        cenarios = [
            (self.cliente_consumo, "Lincoln", self.catalogo_alimentacao),
            (self.cliente_roseli, "Roseli", None),
            (self.cliente_camila, "Camila", None),
            (self.cliente_lorena, "Lorena", None),
        ]

        for cliente, pessoa, catalogo_esperado in cenarios:
            with self.subTest(pessoa=pessoa):
                venda = self.postar_venda(
                    self.payload_venda(
                        Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                        cliente=cliente,
                        quantidade="1,000",
                        preco="4,70",
                        produto=self.produto_alimento,
                    )
                )

                despesa = DespesaDiaria.objects.get(venda_origem=venda)
                self.assertEqual(despesa.catalogo.tipo, CatalogoDespesa.TIPO_PESSOAL)
                self.assertEqual(despesa.catalogo.grupo, "Alimentação")
                self.assertEqual(despesa.catalogo.categoria, "Mercado")
                self.assertEqual(despesa.catalogo.pessoa, pessoa)
                if catalogo_esperado:
                    self.assertEqual(despesa.catalogo, catalogo_esperado)

    def test_consumo_proprio_deposito_com_alimentacao_gera_catalogo_empresarial(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_deposito,
                quantidade="1,000",
                preco="4,70",
                produto=self.produto_alimento,
            )
        )

        views._sincronizar_despesas_consumo_proprio(venda)
        despesas = list(self.despesas_consumo(venda))

        self.assertEqual(len(despesas), 1)
        despesa = despesas[0]
        self.assertEqual(despesa.valor, Decimal("4.70"))
        self.assertEqual(despesa.catalogo.tipo, CatalogoDespesa.TIPO_EMPRESA)
        self.assertEqual(despesa.catalogo.nome, "Copa/Alimentação")
        self.assertEqual(despesa.catalogo.grupo, "Consumo interno")
        self.assertEqual(despesa.catalogo.categoria, "Copa/Alimentação")
        self.assertEqual(despesa.catalogo.pessoa, "")
        self.assertEqual(despesa.chave_automatica, f"catalogo:{despesa.catalogo_id}")
        self.assertEqual(DespesaDiaria.objects.filter(venda_origem=venda).count(), 1)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_consumo_proprio_deposito_sem_regra_empresarial_nao_cai_em_familia_mercado(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_deposito,
                quantidade="1,000",
                preco="7,00",
                produto=self.produto_sem_regra_empresa,
            )
        )

        despesa = DespesaDiaria.objects.get(venda_origem=venda)

        self.assertIsNone(despesa.catalogo)
        self.assertEqual(despesa.chave_automatica, "sem_catalogo")
        self.assertEqual(despesa.valor, Decimal("7.00"))

    def test_editar_consumo_proprio_deposito_mantem_classificacao_empresarial(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_deposito,
                quantidade="1,000",
                preco="4,70",
                produto=self.produto_alimento,
            )
        )
        item = ItemVenda.objects.get(venda=venda)
        despesa_original = DespesaDiaria.objects.get(venda_origem=venda)

        payload = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=self.cliente_deposito,
            quantidade="2,000",
            preco="5,00",
            produto=self.produto_alimento,
        )
        payload["venda_id"] = venda.pk
        payload["itens"][0]["item_id"] = item.pk

        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(payload),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()["sucesso"])
        despesa = DespesaDiaria.objects.get(venda_origem=venda)
        item.refresh_from_db()

        self.assertEqual(despesa.pk, despesa_original.pk)
        self.assertEqual(despesa.valor, Decimal("10.00"))
        self.assertEqual(despesa.catalogo.tipo, CatalogoDespesa.TIPO_EMPRESA)
        self.assertEqual(despesa.catalogo.grupo, "Consumo interno")
        self.assertEqual(despesa.catalogo.categoria, "Copa/Alimentação")
        self.assertEqual(DespesaDiaria.objects.filter(venda_origem=venda).count(), 1)
        self.assertEqual(item.custo_total_snapshot, Decimal("4.00"))
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_consumo_proprio_alimentacao_e_lazer_gera_duas_despesas_idempotentes(self):
        payload = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=self.cliente_consumo,
        )
        payload["itens"].append({
            "produto_id": self.produto_alimento.pk,
            "produto_nome": self.produto_alimento.nome,
            "quantidade": "1,000",
            "unidade": "UN",
            "preco_unitario": "4,70",
        })

        venda = self.postar_venda(payload)
        views._sincronizar_despesas_consumo_proprio(venda)
        despesas = {d.catalogo_id: d for d in self.despesas_consumo(venda)}

        self.assertEqual(len(despesas), 2)
        self.assertEqual(despesas[self.catalogo_lazer.pk].valor, Decimal("20.00"))
        self.assertEqual(despesas[self.catalogo_alimentacao.pk].valor, Decimal("4.70"))
        self.assertEqual(
            sum((d.valor for d in despesas.values()), Decimal("0.00")),
            venda.total,
        )
        self.assertFalse(MovimentoFinanceiro.objects.filter(origem="despesa_diaria").exists())
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_editar_consumo_proprio_atualiza_agrupamentos(self):
        payload = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=self.cliente_consumo,
        )
        payload["itens"].append({
            "produto_id": self.produto_alimento.pk,
            "produto_nome": self.produto_alimento.nome,
            "quantidade": "1,000",
            "unidade": "UN",
            "preco_unitario": "4,70",
        })
        venda = self.postar_venda(payload)
        item = ItemVenda.objects.get(venda=venda, produto=self.produto)

        edicao = self.payload_venda(
            Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
            cliente=venda.cliente,
            quantidade="3,000",
            preco="11,00",
        )
        edicao["venda_id"] = venda.pk
        edicao["itens"][0]["item_id"] = item.pk
        resposta = self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(edicao),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertTrue(resposta.json()["sucesso"])
        venda.refresh_from_db()
        despesas = {d.chave_automatica: d for d in self.despesas_consumo(venda)}
        self.assertEqual(venda.total, Decimal("33.00"))
        self.assertEqual(despesas[f"catalogo:{self.catalogo_lazer.pk}"].valor, Decimal("33.00"))
        self.assertEqual(despesas[f"catalogo:{self.catalogo_alimentacao.pk}"].valor, Decimal("0.00"))
        self.assertEqual(
            sum((d.valor for d in despesas.values()), Decimal("0.00")),
            venda.total,
        )

    def test_exclusao_manual_despesa_automatica_consumo_proprio_e_bloqueada(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )
        despesa = DespesaDiaria.objects.get(venda_origem=venda)

        resposta = self.client.post(
            reverse("estoque:despesas_diarias"),
            data={"acao": "excluir", "despesa_id": despesa.pk},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(DespesaDiaria.objects.filter(pk=despesa.pk).exists())

    def test_exclusao_manual_despesa_comum_continua_permitida(self):
        despesa = DespesaDiaria.objects.create(
            valor=Decimal("12.00"),
            categoria=DespesaDiaria.CATEGORIA_OUTROS,
            forma_pagamento=DespesaDiaria.FORMA_OUTRO,
            operador="Teste",
        )

        resposta = self.client.post(
            reverse("estoque:despesas_diarias"),
            data={"acao": "excluir", "despesa_id": despesa.pk},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(DespesaDiaria.objects.filter(pk=despesa.pk).exists())

    def test_textos_visualizacao_consumo_proprio(self):
        venda = self.postar_venda(
            self.payload_venda(
                Venda.TIPO_PAGAMENTO_CONSUMO_PROPRIO,
                cliente=self.cliente_consumo,
            )
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", args=[venda.pk]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta,
            "Consumo pr&oacute;prio: sem conta a receber e sem movimenta&ccedil;&atilde;o financeira.",
            html=True,
        )
        self.assertContains(resposta, "CONSUMO PR&Oacute;PRIO REGISTRADO", html=True)
        self.assertNotContains(resposta, "Venda a vista: nao gera conta a receber.")
