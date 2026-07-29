from decimal import Decimal
from datetime import date

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from estoque.models import Cliente

from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, ItemLocacao, Locacao, MovimentoEstoqueLocacao


class LocacoesBaseIsoladaTests(TestCase):
    def test_models_locacoes_nao_se_relacionam_com_venda_produto_ou_item_venda(self):
        models_bloqueados = {
            apps.get_model("estoque", "Produto"),
            apps.get_model("estoque", "Venda"),
            apps.get_model("estoque", "ItemVenda"),
        }

        for model in apps.get_app_config("locacoes").get_models():
            for field in model._meta.get_fields():
                related_model = getattr(field, "related_model", None)
                remote_model = getattr(getattr(field, "remote_field", None), "model", None)
                self.assertNotIn(related_model, models_bloqueados)
                self.assertNotIn(remote_model, models_bloqueados)

    def test_jogo_permanece_composicao_fixa_de_uma_mesa_e_quatro_cadeiras(self):
        self.assertEqual(ConfiguracaoLocacao.JOGO_MESAS, 1)
        self.assertEqual(ConfiguracaoLocacao.JOGO_CADEIRAS, 4)
        self.assertEqual(
            ConfiguracaoLocacao.composicao_jogo(),
            {"mesas": 1, "cadeiras": 4},
        )

    def test_configuracoes_e_precos_sao_gravados_corretamente(self):
        configuracao = ConfiguracaoLocacao.obter()
        configuracao.total_mesas = 12
        configuracao.total_cadeiras = 48
        configuracao.preco_mesa_avulsa_diaria = Decimal("3.50")
        configuracao.preco_cadeira_avulsa_diaria = Decimal("1.25")
        configuracao.valor_reposicao_cadeira = Decimal("45.00")
        configuracao.valor_reposicao_mesa = Decimal("90.00")
        configuracao.save()

        faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        faixa.preco_jogo_diaria = Decimal("9.00")
        faixa.save()

        configuracao.refresh_from_db()
        faixa.refresh_from_db()

        self.assertEqual(configuracao.total_mesas, 12)
        self.assertEqual(configuracao.total_cadeiras, 48)
        self.assertEqual(configuracao.preco_mesa_avulsa_diaria, Decimal("3.50"))
        self.assertEqual(configuracao.preco_cadeira_avulsa_diaria, Decimal("1.25"))
        self.assertEqual(configuracao.valor_reposicao_cadeira, Decimal("45.00"))
        self.assertEqual(configuracao.valor_reposicao_mesa, Decimal("90.00"))
        self.assertEqual(faixa.preco_jogo_diaria, Decimal("9.00"))

    def test_bloqueia_alteracao_silenciosa_de_saldo_ja_configurado(self):
        configuracao = ConfiguracaoLocacao.obter()
        configuracao.total_mesas = 10
        configuracao.total_cadeiras = 40
        configuracao.save()

        configuracao.total_mesas = 11

        with self.assertRaises(ValidationError):
            configuracao.save()

    def test_entrada_registra_historico_e_aumenta_saldo(self):
        configuracao = ConfiguracaoLocacao.obter()

        movimento = MovimentoEstoqueLocacao.registrar(
            item=MovimentoEstoqueLocacao.ITEM_MESA,
            tipo=MovimentoEstoqueLocacao.TIPO_ENTRADA,
            quantidade=5,
            responsavel="Lincoln",
            observacao="Compra inicial",
        )

        configuracao.refresh_from_db()
        self.assertEqual(configuracao.total_mesas, 5)
        self.assertEqual(movimento.saldo_anterior, 0)
        self.assertEqual(movimento.saldo_posterior, 5)
        self.assertEqual(movimento.quantidade, 5)

    def test_baixa_definitiva_reduz_saldo_e_nao_permite_negativo(self):
        configuracao = ConfiguracaoLocacao.obter()
        configuracao.total_cadeiras = 8
        configuracao.save()

        movimento = MovimentoEstoqueLocacao.registrar(
            item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
            tipo=MovimentoEstoqueLocacao.TIPO_BAIXA_QUEBRA,
            quantidade=3,
            responsavel="Camila",
            observacao="Quebra interna",
        )

        configuracao.refresh_from_db()
        self.assertEqual(configuracao.total_cadeiras, 5)
        self.assertEqual(movimento.saldo_anterior, 8)
        self.assertEqual(movimento.saldo_posterior, 5)

        with self.assertRaises(ValidationError):
            MovimentoEstoqueLocacao.registrar(
                item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
                tipo=MovimentoEstoqueLocacao.TIPO_BAIXA_PERDA,
                quantidade=6,
                responsavel="Camila",
                observacao="Perda interna",
            )

    def test_ajuste_de_inventario_exige_motivo_e_grava_saldo_contado(self):
        configuracao = ConfiguracaoLocacao.obter()
        configuracao.total_mesas = 10
        configuracao.save()

        with self.assertRaises(ValidationError):
            MovimentoEstoqueLocacao.registrar(
                item=MovimentoEstoqueLocacao.ITEM_MESA,
                tipo=MovimentoEstoqueLocacao.TIPO_AJUSTE_INVENTARIO,
                responsavel="Lincoln",
                saldo_contado=9,
            )

        movimento = MovimentoEstoqueLocacao.registrar(
            item=MovimentoEstoqueLocacao.ITEM_MESA,
            tipo=MovimentoEstoqueLocacao.TIPO_AJUSTE_INVENTARIO,
            responsavel="Lincoln",
            observacao="Inventario mensal",
            saldo_contado=9,
        )

        configuracao.refresh_from_db()
        self.assertEqual(configuracao.total_mesas, 9)
        self.assertEqual(movimento.quantidade, 1)
        self.assertEqual(movimento.saldo_anterior, 10)
        self.assertEqual(movimento.saldo_posterior, 9)


class LocacoesConfiguracoesViewTests(TestCase):
    def setUp(self):
        self.configuracao = ConfiguracaoLocacao.obter()
        self.faixa_centro = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa_distante = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.MAIS_DISTANTE)
        self.faixa_muito_distante = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.MUITO_DISTANTE)

    def test_tela_salva_configuracoes_de_locacao(self):
        response = self.client.post(
            reverse("locacoes:configuracoes"),
            {
                "acao": "salvar_configuracoes",
                "total_mesas": "20",
                "total_cadeiras": "80",
                "preco_mesa_avulsa_diaria": "4.00",
                "preco_cadeira_avulsa_diaria": "1.50",
                "valor_reposicao_cadeira": "40.00",
                "valor_reposicao_mesa": "80.00",
                "faixas-TOTAL_FORMS": "3",
                "faixas-INITIAL_FORMS": "3",
                "faixas-MIN_NUM_FORMS": "0",
                "faixas-MAX_NUM_FORMS": "1000",
                "faixas-0-id": str(self.faixa_centro.id),
                "faixas-0-preco_jogo_diaria": "8.00",
                "faixas-1-id": str(self.faixa_distante.id),
                "faixas-1-preco_jogo_diaria": "11.00",
                "faixas-2-id": str(self.faixa_muito_distante.id),
                "faixas-2-preco_jogo_diaria": "16.00",
            },
            secure=True,
        )

        self.assertRedirects(
            response,
            reverse("locacoes:configuracoes"),
            fetch_redirect_response=False,
        )
        self.configuracao.refresh_from_db()
        self.faixa_distante.refresh_from_db()
        self.faixa_muito_distante.refresh_from_db()
        self.assertEqual(self.configuracao.total_mesas, 20)
        self.assertEqual(self.configuracao.total_cadeiras, 80)
        self.assertEqual(self.faixa_distante.preco_jogo_diaria, Decimal("11.00"))
        self.assertEqual(self.faixa_muito_distante.preco_jogo_diaria, Decimal("16.00"))

    def test_tela_registra_movimentacao_de_estoque_de_locacao(self):
        response = self.client.post(
            reverse("locacoes:configuracoes"),
            {
                "acao": "registrar_movimentacao",
                "item": MovimentoEstoqueLocacao.ITEM_CADEIRA,
                "tipo": MovimentoEstoqueLocacao.TIPO_ENTRADA,
                "quantidade": "12",
                "saldo_contado": "",
                "responsavel": "Camila",
                "observacao": "Compra para locacao",
            },
            secure=True,
        )

        self.assertRedirects(
            response,
            reverse("locacoes:configuracoes"),
            fetch_redirect_response=False,
        )
        self.configuracao.refresh_from_db()
        self.assertEqual(self.configuracao.total_cadeiras, 12)
        self.assertTrue(
            MovimentoEstoqueLocacao.objects.filter(
                item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
                tipo=MovimentoEstoqueLocacao.TIPO_ENTRADA,
                quantidade=12,
                saldo_anterior=0,
                saldo_posterior=12,
            ).exists()
        )


class LocacoesReservasTests(TestCase):
    def setUp(self):
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 10
        self.configuracao.total_cadeiras = 40
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()

    def dados_base(self, **extras):
        dados = {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Maria Avulsa",
            "pessoa_avulsa_telefone": "91999990000",
            "pessoa_avulsa_endereco": "Rua da Festa, 10",
            "endereco_entrega": "Rua da Festa, 10",
            "data_entrega": date(2026, 8, 10),
            "horario_entrega": "10:00",
            "data_evento": date(2026, 8, 10),
            "horario_evento": "19:00",
            "data_prevista_devolucao": date(2026, 8, 11),
            "faixa_preco": self.faixa,
            "observacao": "",
        }
        dados.update(extras)
        return dados

    def itens(self, jogos=0, mesas=0, cadeiras=0, preco_jogo=Decimal("8.00")):
        itens = []
        if jogos:
            itens.append({
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": jogos,
                "preco_diaria": preco_jogo,
                "ajuste_manual": preco_jogo != self.faixa.preco_jogo_diaria,
            })
        if mesas:
            itens.append({
                "tipo": ItemLocacao.TIPO_MESA_AVULSA,
                "quantidade": mesas,
                "preco_diaria": Decimal("4.00"),
            })
        if cadeiras:
            itens.append({
                "tipo": ItemLocacao.TIPO_CADEIRA_AVULSA,
                "quantidade": cadeiras,
                "preco_diaria": Decimal("1.50"),
            })
        return itens

    def test_jogo_consumo_calculado_com_uma_mesa_e_quatro_cadeiras(self):
        necessidade = Locacao.necessidades_itens(self.itens(jogos=2))

        self.assertEqual(necessidade, {"mesas": 2, "cadeiras": 8})

    def test_reserva_bloqueia_periodo_inclusive_e_dia_seguinte_fica_livre(self):
        Locacao.criar_reserva(self.dados_base(), self.itens(jogos=2))

        ocupado_dia_final = Locacao.disponibilidade_periodo(date(2026, 8, 11), date(2026, 8, 11))
        livre_dia_seguinte = Locacao.disponibilidade_periodo(date(2026, 8, 12), date(2026, 8, 12))

        self.assertEqual(ocupado_dia_final["reservado_mesas"], 2)
        self.assertEqual(ocupado_dia_final["reservado_cadeiras"], 8)
        self.assertEqual(livre_dia_seguinte["reservado_mesas"], 0)
        self.assertEqual(livre_dia_seguinte["reservado_cadeiras"], 0)

    def test_reserva_cancelada_libera_disponibilidade(self):
        locacao = Locacao.criar_reserva(self.dados_base(), self.itens(jogos=3))
        locacao.cancelar("Cliente desistiu", responsavel="Camila")

        disponibilidade = Locacao.disponibilidade_periodo(date(2026, 8, 10), date(2026, 8, 11))

        self.assertEqual(disponibilidade["reservado_mesas"], 0)
        self.assertEqual(disponibilidade["reservado_cadeiras"], 0)

    def test_impede_excesso_de_mesas_ou_cadeiras_no_periodo(self):
        Locacao.criar_reserva(self.dados_base(), self.itens(jogos=9))

        with self.assertRaises(ValidationError):
            Locacao.criar_reserva(
                self.dados_base(data_entrega=date(2026, 8, 11), data_prevista_devolucao=date(2026, 8, 12)),
                self.itens(jogos=2),
            )

    def test_preco_snapshot_nao_muda_ao_alterar_tabela_geral(self):
        locacao = Locacao.criar_reserva(self.dados_base(), self.itens(jogos=1))
        item = locacao.itens.get(tipo=ItemLocacao.TIPO_JOGO)

        self.faixa.preco_jogo_diaria = Decimal("15.00")
        self.faixa.save()
        item.refresh_from_db()

        self.assertEqual(item.preco_diaria_snapshot, Decimal("8.00"))
        self.assertEqual(item.valor_total, Decimal("8.00"))

    def test_preco_negociado_fica_apenas_no_item(self):
        locacao = Locacao.criar_reserva(self.dados_base(), self.itens(jogos=1, preco_jogo=Decimal("7.00")))
        item = locacao.itens.get(tipo=ItemLocacao.TIPO_JOGO)
        self.faixa.refresh_from_db()

        self.assertEqual(item.preco_diaria_snapshot, Decimal("7.00"))
        self.assertTrue(item.ajuste_manual)
        self.assertEqual(self.faixa.preco_jogo_diaria, Decimal("8.00"))

    def test_pessoa_avulsa_nao_cria_cliente(self):
        clientes_antes = Cliente.objects.count()

        locacao = Locacao.criar_reserva(self.dados_base(), self.itens(jogos=1))

        self.assertIsNone(locacao.cliente_id)
        self.assertEqual(Cliente.objects.count(), clientes_antes)
        self.assertEqual(locacao.pessoa_avulsa_nome, "Maria Avulsa")
