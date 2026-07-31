from decimal import Decimal
from datetime import date, datetime, time, timedelta

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente, ContaFinanceira, ContaReceber, ItemVenda, MovimentoFinanceiro, Produto, Venda

from .models import (
    ConfiguracaoLocacao,
    ConferenciaEntregaLocacao,
    ConferenciaRecolhimentoLocacao,
    FaixaPrecoLocacao,
    ItemLocacao,
    Locacao,
    MovimentoEstoqueLocacao,
    PagamentoLocacao,
    RegistroCobrancaLocacao,
    TarefaOperacionalLocacao,
)
from .services import checklist_operacional_locacoes, obter_ou_criar_tarefa_operacional


class LocacoesBaseIsoladaTests(TestCase):
    def test_models_locacoes_nao_se_relacionam_com_venda_produto_ou_item_venda(self):
        models_bloqueados = {
            apps.get_model("estoque", "ContaReceber"),
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


class LocacoesUxPadraoTests(TestCase):
    def setUp(self):
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 4
        self.configuracao.total_cadeiras = 16
        self.configuracao.save()

    def test_lista_abre_com_periodo_de_hoje_preenchido(self):
        hoje = timezone.localdate().isoformat()

        response = self.client.get(reverse("locacoes:lista"), secure=True)

        self.assertContains(response, f'name="data_inicio" value="{hoje}"')
        self.assertContains(response, f'name="data_fim" value="{hoje}"')
        self.assertContains(response, "data-enter-nav")

    def test_nova_locacao_abre_com_datas_de_hoje_preenchidas(self):
        hoje = timezone.localdate().isoformat()

        response = self.client.get(reverse("locacoes:nova"), secure=True)

        self.assertContains(response, f'name="data_entrega"')
        self.assertContains(response, f'value="{hoje}"', count=4)
        self.assertContains(response, "data-enter-nav")


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


class LocacoesOperacaoTests(TestCase):
    def setUp(self):
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 5
        self.configuracao.total_cadeiras = 20
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()

    def dados_base(self, **extras):
        dados = {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Joao Evento",
            "pessoa_avulsa_telefone": "91988887777",
            "endereco_entrega": "Rua A, 1",
            "data_entrega": date(2026, 9, 1),
            "horario_entrega": "09:00",
            "data_evento": date(2026, 9, 1),
            "horario_evento": "18:00",
            "data_prevista_devolucao": date(2026, 9, 2),
            "faixa_preco": self.faixa,
            "observacao": "",
        }
        dados.update(extras)
        return dados

    def criar_locacao(self, jogos=1, mesas=0, cadeiras=0):
        itens = []
        if jogos:
            itens.append({
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": jogos,
                "preco_diaria": Decimal("8.00"),
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
        return Locacao.criar_reserva(self.dados_base(), itens)

    def test_transicoes_validas_e_invalidas(self):
        locacao = self.criar_locacao()

        with self.assertRaises(ValidationError):
            locacao.confirmar_entrega()

        locacao.marcar_saiu_para_entrega(responsavel="Camila")
        locacao.refresh_from_db()
        self.assertEqual(locacao.status, Locacao.STATUS_SAIU_PARA_ENTREGA)

        with self.assertRaises(ValidationError):
            locacao.cancelar()

        locacao.confirmar_entrega(responsavel="Camila")
        locacao.refresh_from_db()
        self.assertEqual(locacao.status, Locacao.STATUS_ENTREGUE)

        locacao.status = Locacao.STATUS_DEVOLVIDA
        with self.assertRaises(ValidationError):
            locacao.save()

    def test_material_entregue_continua_indisponivel_apos_data_prevista(self):
        locacao = self.criar_locacao(jogos=2)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()

        disponibilidade = Locacao.disponibilidade_periodo(date(2026, 9, 5), date(2026, 9, 5))

        self.assertEqual(disponibilidade["reservado_mesas"], 2)
        self.assertEqual(disponibilidade["reservado_cadeiras"], 8)

    def test_devolucao_normal_libera_disponibilidade(self):
        locacao = self.criar_locacao(jogos=1)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()
        item = locacao.itens.get()

        locacao.registrar_devolucao({item.id: {"devolvida_boa": 1}}, responsavel="Camila")
        locacao.refresh_from_db()
        disponibilidade = Locacao.disponibilidade_periodo(date(2026, 9, 5), date(2026, 9, 5))

        self.assertEqual(locacao.status, Locacao.STATUS_DEVOLVIDA)
        self.assertEqual(disponibilidade["reservado_mesas"], 0)
        self.assertEqual(disponibilidade["reservado_cadeiras"], 0)

    def test_devolucao_parcial_mantem_pendencia_na_rua(self):
        locacao = self.criar_locacao(jogos=2)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()
        item = locacao.itens.get()

        locacao.registrar_devolucao({item.id: {"devolvida_boa": 1}}, responsavel="Camila")
        locacao.refresh_from_db()
        disponibilidade = Locacao.disponibilidade_periodo(date(2026, 9, 5), date(2026, 9, 5))

        self.assertEqual(locacao.status, Locacao.STATUS_PENDENTE_DEVOLUCAO)
        self.assertEqual(disponibilidade["reservado_mesas"], 1)
        self.assertEqual(disponibilidade["reservado_cadeiras"], 4)

    def test_perda_e_quebra_baixam_estoque_fisico_com_vinculo_a_locacao(self):
        locacao = self.criar_locacao(jogos=1, cadeiras=1)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()
        item_jogo = locacao.itens.get(tipo=ItemLocacao.TIPO_JOGO)
        item_cadeira = locacao.itens.get(tipo=ItemLocacao.TIPO_CADEIRA_AVULSA)

        locacao.registrar_devolucao(
            {
                item_jogo.id: {"quebrada": 1},
                item_cadeira.id: {"perdida": 1},
            },
            responsavel="Camila",
            observacao="Ocorrencias no retorno",
        )
        locacao.refresh_from_db()
        self.configuracao.refresh_from_db()

        self.assertEqual(locacao.status, Locacao.STATUS_DEVOLVIDA_COM_AVARIA)
        self.assertEqual(self.configuracao.total_mesas, 4)
        self.assertEqual(self.configuracao.total_cadeiras, 15)
        self.assertTrue(
            MovimentoEstoqueLocacao.objects.filter(
                locacao=locacao,
                item_locacao=item_jogo,
                tipo=MovimentoEstoqueLocacao.TIPO_BAIXA_QUEBRA,
                item=MovimentoEstoqueLocacao.ITEM_MESA,
                quantidade=1,
            ).exists()
        )


class LocacoesPagamentosTermoTests(TestCase):
    def setUp(self):
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 5
        self.configuracao.total_cadeiras = 20
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.valor_reposicao_mesa = Decimal("80.00")
        self.configuracao.valor_reposicao_cadeira = Decimal("40.00")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()
        self.conta_caixa = ContaFinanceira.objects.create(
            nome="Caixa em especie",
            tipo=ContaFinanceira.TIPO_CAIXA,
            ativo=True,
        )
        self.conta_banco = ContaFinanceira.objects.create(
            nome="Banco/Pix",
            tipo=ContaFinanceira.TIPO_BANCO,
            ativo=True,
        )

    def dados_base(self):
        return {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Ana Locacao",
            "pessoa_avulsa_telefone": "91999990000",
            "endereco_entrega": "Rua B, 2",
            "data_entrega": date(2026, 10, 1),
            "horario_entrega": "09:00",
            "data_evento": date(2026, 10, 1),
            "horario_evento": "18:00",
            "data_prevista_devolucao": date(2026, 10, 2),
            "faixa_preco": self.faixa,
            "observacao": "",
        }

    def criar_locacao(self):
        return Locacao.criar_reserva(
            self.dados_base(),
            [{
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": 2,
                "preco_diaria": Decimal("8.00"),
            }],
        )

    def test_sinal_reduz_saldo_sem_quitar_indevidamente(self):
        locacao = self.criar_locacao()

        pagamento = locacao.registrar_pagamento(
            Decimal("5.00"),
            PagamentoLocacao.FORMA_DINHEIRO,
            responsavel="Camila",
        )
        locacao.refresh_from_db()

        self.assertEqual(locacao.total, Decimal("16.00"))
        self.assertEqual(locacao.total_pago, Decimal("5.00"))
        self.assertEqual(locacao.saldo_devedor, Decimal("11.00"))
        self.assertEqual(locacao.status_financeiro, Locacao.FINANCEIRO_PARCIAL)
        self.assertEqual(pagamento.recibo_status, PagamentoLocacao.RECIBO_PENDENTE)

    def test_varios_pagamentos_atualizam_total_pago_e_saldo(self):
        locacao = self.criar_locacao()

        locacao.registrar_pagamento(Decimal("5.00"), PagamentoLocacao.FORMA_PIX)
        locacao.registrar_pagamento(Decimal("11.00"), PagamentoLocacao.FORMA_DINHEIRO)
        locacao.refresh_from_db()

        self.assertEqual(locacao.total_pago, Decimal("16.00"))
        self.assertEqual(locacao.saldo_devedor, Decimal("0.00"))
        self.assertEqual(locacao.status_financeiro, Locacao.FINANCEIRO_QUITADA)

    def test_nao_permite_pagamento_acima_do_total_contratado(self):
        locacao = self.criar_locacao()

        with self.assertRaises(ValidationError):
            locacao.registrar_pagamento(Decimal("17.00"), PagamentoLocacao.FORMA_PIX)

    def test_pagamento_cria_no_maximo_um_movimento_financeiro_de_locacao(self):
        locacao = self.criar_locacao()

        pagamento = locacao.registrar_pagamento(Decimal("8.00"), PagamentoLocacao.FORMA_PIX)
        pagamento.criar_movimento_financeiro()
        pagamento.refresh_from_db()

        self.assertEqual(
            MovimentoFinanceiro.objects.filter(origem="locacao", descricao__icontains=f"#{locacao.id}").count(),
            1,
        )
        self.assertEqual(pagamento.movimento_financeiro.origem, "locacao")

    def test_recibo_pendente_enviado_e_dispensado(self):
        locacao = self.criar_locacao()
        pagamento = locacao.registrar_pagamento(Decimal("4.00"), PagamentoLocacao.FORMA_PIX)

        self.assertEqual(pagamento.recibo_status, PagamentoLocacao.RECIBO_PENDENTE)
        pagamento.confirmar_recibo_enviado(responsavel="Camila")
        pagamento.refresh_from_db()
        self.assertEqual(pagamento.recibo_status, PagamentoLocacao.RECIBO_ENVIADO)
        self.assertIsNotNone(pagamento.recibo_enviado_em)

        outro = locacao.registrar_pagamento(Decimal("4.00"), PagamentoLocacao.FORMA_PIX)
        outro.dispensar_recibo(responsavel="Camila", observacao="Cliente dispensou")
        outro.refresh_from_db()
        self.assertEqual(outro.recibo_status, PagamentoLocacao.RECIBO_DISPENSADO)
        self.assertEqual(outro.recibo_dispensa_observacao, "Cliente dispensou")

    def test_recibo_mostra_saldo_corretamente(self):
        locacao = self.criar_locacao()
        pagamento = locacao.registrar_pagamento(Decimal("5.00"), PagamentoLocacao.FORMA_PIX)

        response = self.client.get(reverse("locacoes:recibo_pagamento", kwargs={"pk": pagamento.pk}), secure=True)

        self.assertContains(response, "Saldo restante")
        self.assertContains(response, "11.00")
        self.assertContains(response, "Ainda existe saldo devedor")

    def test_snapshot_de_precos_e_reposicao_nao_muda_com_configuracao_posterior(self):
        locacao = self.criar_locacao()
        item = locacao.itens.get()

        self.faixa.preco_jogo_diaria = Decimal("15.00")
        self.faixa.save()
        self.configuracao.valor_reposicao_mesa = Decimal("100.00")
        self.configuracao.valor_reposicao_cadeira = Decimal("60.00")
        self.configuracao.save()
        locacao.refresh_from_db()
        item.refresh_from_db()

        self.assertEqual(item.preco_diaria_snapshot, Decimal("8.00"))
        self.assertEqual(locacao.valor_reposicao_mesa_snapshot, Decimal("80.00"))
        self.assertEqual(locacao.valor_reposicao_cadeira_snapshot, Decimal("40.00"))

    def test_pagamentos_nao_criam_venda_produto_itemvenda_ou_conta_receber(self):
        locacao = self.criar_locacao()

        locacao.registrar_pagamento(Decimal("5.00"), PagamentoLocacao.FORMA_PIX)

        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertEqual(Produto.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)


class LocacoesAlertasCobrancaTests(TestCase):
    def setUp(self):
        self.hoje = timezone.localdate()
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 8
        self.configuracao.total_cadeiras = 32
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()

    def dados_base(self, **extras):
        data_entrega = extras.pop("data_entrega", self.hoje)
        dados = {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Cliente Locacao Etapa 6",
            "pessoa_avulsa_telefone": "91999990000",
            "endereco_entrega": "Rua L, 6",
            "data_entrega": data_entrega,
            "horario_entrega": "09:30",
            "data_evento": data_entrega,
            "horario_evento": "18:00",
            "data_prevista_devolucao": data_entrega + timedelta(days=1),
            "faixa_preco": self.faixa,
            "observacao": "",
        }
        dados.update(extras)
        return dados

    def criar_locacao(self, **extras):
        return Locacao.criar_reserva(
            self.dados_base(**extras),
            [{
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": 1,
                "preco_diaria": Decimal("8.00"),
            }],
        )

    def test_vencimento_sugerido_na_data_da_entrega(self):
        locacao = self.criar_locacao(data_entrega=self.hoje + timedelta(days=3))

        self.assertEqual(locacao.data_vencimento_saldo, locacao.data_entrega)

    def test_vencimento_alteravel_por_locacao_registra_historico(self):
        locacao = self.criar_locacao()
        nova_data = self.hoje + timedelta(days=2)

        locacao.alterar_vencimento_saldo(nova_data, responsavel="Camila", observacao="Pagar no recolhimento")
        locacao.refresh_from_db()

        self.assertEqual(locacao.data_vencimento_saldo, nova_data)
        self.assertTrue(
            locacao.eventos.filter(
                tipo="vencimento_saldo",
                descricao__icontains="Pagar no recolhimento",
            ).exists()
        )

    def test_saldo_so_aparece_vencido_apos_data_de_vencimento(self):
        locacao = self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=1),
            data_prevista_devolucao=self.hoje,
            data_vencimento_saldo=self.hoje,
        )

        response = self.client.get(reverse("estoque:central_cobrancas"), secure=True)
        self.assertNotContains(response, f"Locacao #{locacao.id}")

        locacao.alterar_vencimento_saldo(self.hoje - timedelta(days=1))
        response = self.client.get(reverse("estoque:central_cobrancas"), secure=True)
        self.assertContains(response, f"Locacao #{locacao.id}")
        self.assertContains(response, "Locacao")

    def test_entrega_e_recolhimento_do_dia_aparecem_no_painel(self):
        entrega = self.criar_locacao(data_entrega=self.hoje, pessoa_avulsa_nome="Entrega Hoje")
        recolhimento = self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=1),
            data_prevista_devolucao=self.hoje,
            pessoa_avulsa_nome="Recolher Hoje",
        )
        recolhimento.marcar_saiu_para_entrega()
        recolhimento.confirmar_entrega()

        response = self.client.get(reverse("estoque:home"), secure=True)

        self.assertContains(response, f"Hoje: entregar para {entrega.nome_contratante}")
        self.assertContains(response, f"Hoje: recolher mesas/cadeiras de {recolhimento.nome_contratante}")
        self.assertContains(response, "locacoes-operacionais-alerta")

    def test_devolucao_atrasada_persiste_ate_encerramento(self):
        locacao = self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=3),
            data_prevista_devolucao=self.hoje - timedelta(days=2),
            pessoa_avulsa_nome="Atraso Persistente",
        )
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()

        response = self.client.get(reverse("estoque:home"), secure=True)
        self.assertContains(response, "Devolucao atrasada: Atraso Persistente")

        item = locacao.itens.get()
        locacao.registrar_devolucao({item.id: {"devolvida_boa": 1}})

        response = self.client.get(reverse("estoque:home"), secure=True)
        self.assertNotContains(response, "Devolucao atrasada: Atraso Persistente")

    def test_pagamento_quitacao_remove_cobranca_de_locacao(self):
        locacao = self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=3),
            data_prevista_devolucao=self.hoje - timedelta(days=2),
            data_vencimento_saldo=self.hoje - timedelta(days=2),
        )

        response = self.client.get(reverse("estoque:central_cobrancas"), secure=True)
        self.assertContains(response, f"Locacao #{locacao.id}")

        locacao.registrar_pagamento(locacao.total, PagamentoLocacao.FORMA_PIX)

        response = self.client.get(reverse("estoque:central_cobrancas"), secure=True)
        self.assertNotContains(response, f"Locacao #{locacao.id}")

    def test_registra_contato_cobranca_especifico_da_locacao(self):
        locacao = self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=3),
            data_prevista_devolucao=self.hoje - timedelta(days=2),
            data_vencimento_saldo=self.hoje - timedelta(days=2),
        )

        response = self.client.post(
            reverse("estoque:central_cobrancas"),
            {
                "acao": "registrar_cobranca_locacao",
                "locacao_id": str(locacao.id),
                "tipo": RegistroCobrancaLocacao.TIPO_WHATSAPP,
                "status": RegistroCobrancaLocacao.STATUS_CONTATADO,
                "observacao": "Cobranca da locacao enviada.",
            },
            secure=True,
        )

        self.assertRedirects(response, reverse("estoque:central_cobrancas"), fetch_redirect_response=False)
        self.assertEqual(RegistroCobrancaLocacao.objects.filter(locacao=locacao).count(), 1)
        self.assertEqual(ContaReceber.objects.count(), 0)
        self.assertEqual(Venda.objects.count(), 0)

    def test_pagamento_total_na_criacao_deixa_locacao_quitada_e_fora_da_cobranca(self):
        response = self.client.post(
            reverse("locacoes:nova"),
            {
                "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
                "pessoa_avulsa_nome": "Quitada Na Reserva",
                "pessoa_avulsa_telefone": "91999990000",
                "endereco_entrega": "Rua Q, 1",
                "data_entrega": (self.hoje - timedelta(days=2)).isoformat(),
                "horario_entrega": "09:00",
                "data_evento": (self.hoje - timedelta(days=2)).isoformat(),
                "horario_evento": "18:00",
                "data_prevista_devolucao": (self.hoje - timedelta(days=1)).isoformat(),
                "data_vencimento_saldo": (self.hoje - timedelta(days=2)).isoformat(),
                "faixa_preco": str(self.faixa.id),
                "observacao": "",
                "responsavel": "Camila",
                "sinal_valor": "8.00",
                "sinal_forma_pagamento": PagamentoLocacao.FORMA_PIX,
                "sinal_observacao": "Pagamento total na reserva.",
                "jogos": "1",
                "preco_jogo_diaria": "8.00",
                "mesas_avulsas": "0",
                "preco_mesa_avulsa_diaria": "4.00",
                "cadeiras_avulsas": "0",
                "preco_cadeira_avulsa_diaria": "1.50",
            },
            secure=True,
        )

        locacao = Locacao.objects.get(pessoa_avulsa_nome="Quitada Na Reserva")
        self.assertRedirects(
            response,
            reverse("locacoes:recibo_pagamento", kwargs={"pk": locacao.pagamentos.get().pk}),
            fetch_redirect_response=False,
        )
        self.assertEqual(locacao.status_financeiro, Locacao.FINANCEIRO_QUITADA)
        self.assertEqual(locacao.total_pago, locacao.total)
        self.assertEqual(locacao.saldo_devedor, Decimal("0.00"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="locacao").count(), 1)

        response = self.client.get(reverse("estoque:central_cobrancas"), secure=True)
        self.assertNotContains(response, "Quitada Na Reserva")
        self.assertEqual(ContaReceber.objects.count(), 0)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(Produto.objects.count(), 0)


class LocacoesChecklistOperacionalTests(TestCase):
    def setUp(self):
        self.hoje = timezone.localdate()
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 30
        self.configuracao.total_cadeiras = 120
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()

    def dados_base(self, **extras):
        data_entrega = extras.pop("data_entrega", self.hoje)
        dados = {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Cliente Checklist",
            "pessoa_avulsa_telefone": "91999990000",
            "endereco_entrega": "Rua Checklist, 1",
            "data_entrega": data_entrega,
            "horario_entrega": "09:30",
            "data_evento": data_entrega,
            "horario_evento": "18:00",
            "data_prevista_devolucao": data_entrega + timedelta(days=1),
            "faixa_preco": self.faixa,
            "observacao": "Levar toalhas separadas.",
        }
        dados.update(extras)
        return dados

    def criar_locacao(self, **extras):
        return Locacao.criar_reserva(
            self.dados_base(**extras),
            [{
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": 1,
                "preco_diaria": Decimal("8.00"),
            }],
        )

    def colocar_na_rua(self, locacao):
        locacao.marcar_saiu_para_entrega(responsavel="Camila")
        locacao.refresh_from_db()
        locacao.confirmar_entrega(responsavel="Camila")
        locacao.refresh_from_db()
        return locacao

    def test_checklist_ordena_por_horario_e_sem_horario_fica_no_final(self):
        tarde = self.criar_locacao(pessoa_avulsa_nome="Entrega Tarde", horario_entrega=time(15, 0))
        cedo = self.criar_locacao(pessoa_avulsa_nome="Entrega Cedo", horario_entrega=time(8, 0))
        recolhimento = self.colocar_na_rua(self.criar_locacao(
            pessoa_avulsa_nome="Recolhimento Sem Horario",
            data_entrega=self.hoje - timedelta(days=1),
            data_prevista_devolucao=self.hoje,
        ))

        checklist = checklist_operacional_locacoes(self.hoje)

        self.assertEqual(
            [item["locacao"].id for item in checklist["grupos"]["entregas"]],
            [cedo.id, tarde.id],
        )
        self.assertEqual(
            [item["locacao"].id for item in checklist["grupos"]["recolhimentos"]],
            [recolhimento.id],
        )
        self.assertIsNone(checklist["grupos"]["recolhimentos"][0]["horario"])

    def dados_conferencia_entrega(self, **extras):
        dados = {
            "entregue_mesas": "1",
            "entregue_cadeiras": "4",
            "recebedor_nome": "Maria",
            "recebedor_relacao": (
                ConferenciaEntregaLocacao.RELACAO_CLIENTE
            ),
            "recebedor_relacao_outro": "",
            "estado_material": (
                ConferenciaEntregaLocacao.ESTADO_BOM
            ),
            "justificativa_parcial": "",
            "previsao_conclusao": "",
            "observacao": "Material conferido no local.",
            "responsavel": "Camila",
        }
        dados.update(extras)
        return dados

    def dados_conferencia_recolhimento(self, **extras):
        dados = {
            "boa_mesas": "1",
            "boa_cadeiras": "4",
            "quebrada_mesas": "0",
            "quebrada_cadeiras": "0",
            "perdida_mesas": "0",
            "perdida_cadeiras": "0",
            "descartada_mesas": "0",
            "descartada_cadeiras": "0",
            "pessoa_local_nome": "Maria",
            "pessoa_local_relacao": (
                ConferenciaEntregaLocacao.RELACAO_CLIENTE
            ),
            "pessoa_local_relacao_outro": "",
            "justificativa_parcial": "",
            "previsao_conclusao": "",
            "observacao": "Material conferido no recolhimento.",
            "responsavel": "Lincoln",
        }
        dados.update(extras)
        return dados

    def test_confirmacao_simples_de_entrega_redireciona_para_checklist_detalhado(self):
        locacao = self.criar_locacao()
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

        response = self.client.post(
            reverse(
                "locacoes:confirmar_tarefa_operacional",
                kwargs={"pk": tarefa.pk},
            ),
            {
                "responsavel": "Camila",
                "observacao": "Tentativa pela rota antiga.",
            },
            secure=True,
        )

        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertRedirects(
            response,
            reverse(
                "locacoes:conferencia_entrega",
                kwargs={"pk": tarefa.pk},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PENDENTE,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_RESERVADA,
        )
        self.assertEqual(
            ConferenciaEntregaLocacao.objects.count(),
            0,
        )

    def test_entrega_completa_registra_conferencia_e_remove_alerta(self):
        locacao = self.criar_locacao()
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_entrega",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_entrega(),
            secure=True,
        )

        conferencia = ConferenciaEntregaLocacao.objects.get()
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertRedirects(
            response,
            (
                reverse(
                    "locacoes:conferencia_entrega",
                    kwargs={"pk": tarefa.pk},
                )
                + f"?conferencia={conferencia.pk}"
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            conferencia.situacao,
            ConferenciaEntregaLocacao.SITUACAO_COMPLETA,
        )
        self.assertEqual(conferencia.acumulado_mesas, 1)
        self.assertEqual(conferencia.acumulado_cadeiras, 4)
        self.assertEqual(conferencia.pendente_mesas, 0)
        self.assertEqual(conferencia.pendente_cadeiras, 0)
        self.assertIn(
            "Material conferido com Maria",
            conferencia.mensagem_whatsapp_snapshot,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        )
        self.assertEqual(tarefa.confirmado_por, "Camila")
        self.assertIsNotNone(tarefa.confirmado_em)
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_ENTREGUE,
        )
        self.assertEqual(
            checklist_operacional_locacoes(self.hoje)["total"],
            0,
        )
        self.assertTrue(
            locacao.eventos.filter(
                tipo="checklist_entrega_completa",
                responsavel="Camila",
            ).exists()
        )

    def test_entrega_parcial_reagenda_e_depois_pode_ser_completada(self):
        locacao = self.criar_locacao()
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )
        nova_data = self.hoje + timedelta(days=1)
        previsao = f"{nova_data.isoformat()}T10:00"

        response = self.client.post(
            reverse(
                "locacoes:conferencia_entrega",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_entrega(
                entregue_mesas="1",
                entregue_cadeiras="2",
                justificativa_parcial=(
                    "Duas cadeiras ficaram para a segunda viagem."
                ),
                previsao_conclusao=previsao,
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        primeira = (
            ConferenciaEntregaLocacao.objects
            .order_by("criado_em", "id")
            .first()
        )

        self.assertEqual(
            primeira.situacao,
            ConferenciaEntregaLocacao.SITUACAO_PARCIAL,
        )
        self.assertEqual(primeira.pendente_mesas, 0)
        self.assertEqual(primeira.pendente_cadeiras, 2)
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PARCIAL,
        )
        self.assertEqual(tarefa.data_agendada, nova_data)
        self.assertEqual(tarefa.horario_agendado, time(10, 0))
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_SAIU_PARA_ENTREGA,
        )
        self.assertEqual(
            checklist_operacional_locacoes(self.hoje)["total"],
            0,
        )

        checklist_reagendado = checklist_operacional_locacoes(
            nova_data
        )
        self.assertEqual(
            checklist_reagendado["grupos"]["entregas"][0][
                "tarefa"
            ].pk,
            tarefa.pk,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_entrega",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_entrega(
                entregue_mesas="0",
                entregue_cadeiras="2",
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertEqual(
            ConferenciaEntregaLocacao.objects.count(),
            2,
        )
        segunda = (
            ConferenciaEntregaLocacao.objects
            .order_by("criado_em", "id")
            .last()
        )
        self.assertEqual(
            segunda.situacao,
            ConferenciaEntregaLocacao.SITUACAO_COMPLETA,
        )
        self.assertEqual(segunda.acumulado_mesas, 1)
        self.assertEqual(segunda.acumulado_cadeiras, 4)
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_ENTREGUE,
        )

    def test_entrega_acima_do_previsto_e_bloqueada(self):
        locacao = self.criar_locacao()
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_entrega",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_entrega(
                entregue_mesas="2",
            ),
            secure=True,
        )

        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "A quantidade entregue esta maior que a prevista",
        )
        self.assertEqual(
            ConferenciaEntregaLocacao.objects.count(),
            0,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PENDENTE,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_RESERVADA,
        )

    def test_tela_checklist_renderiza_dados_operacionais(self):
        locacao = self.criar_locacao(pessoa_avulsa_nome="Cliente Tela Checklist")

        response = self.client.get(reverse("locacoes:checklist_operacional"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Entregas e Recolhimentos")
        self.assertContains(response, "Cliente Tela Checklist")
        self.assertContains(response, "Rua Checklist, 1")
        self.assertContains(response, "Levar toalhas separadas.")
        self.assertContains(response, reverse("locacoes:detalhe", kwargs={"pk": locacao.pk}))

    def test_confirmacao_simples_de_recolhimento_redireciona_para_checklist_detalhado(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        response = self.client.post(
            reverse(
                "locacoes:confirmar_tarefa_operacional",
                kwargs={"pk": tarefa.pk},
            ),
            {
                "responsavel": "Lincoln",
                "observacao": "Tentativa pela confirmacao simples.",
            },
            secure=True,
        )

        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertRedirects(
            response,
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PENDENTE,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_ENTREGUE,
        )
        self.assertEqual(
            ConferenciaRecolhimentoLocacao.objects.count(),
            0,
        )

    def test_recolhimento_completo_registra_conferencia_e_remove_alerta(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(),
            secure=True,
        )

        conferencia = (
            ConferenciaRecolhimentoLocacao.objects.get()
        )
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertRedirects(
            response,
            (
                reverse(
                    "locacoes:conferencia_recolhimento",
                    kwargs={"pk": tarefa.pk},
                )
                + f"?conferencia={conferencia.pk}"
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            conferencia.situacao,
            ConferenciaRecolhimentoLocacao.SITUACAO_COMPLETA,
        )
        self.assertEqual(conferencia.previsto_mesas, 1)
        self.assertEqual(conferencia.previsto_cadeiras, 4)
        self.assertEqual(conferencia.acumulado_boa_mesas, 1)
        self.assertEqual(conferencia.acumulado_boa_cadeiras, 4)
        self.assertEqual(conferencia.pendente_mesas, 0)
        self.assertEqual(conferencia.pendente_cadeiras, 0)
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        )
        self.assertEqual(tarefa.confirmado_por, "Lincoln")
        self.assertIsNotNone(tarefa.confirmado_em)
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_DEVOLVIDA,
        )
        self.assertEqual(
            checklist_operacional_locacoes(self.hoje)["total"],
            0,
        )


    def test_recolhimento_parcial_reagenda_e_depois_pode_ser_concluido(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        nova_data = self.hoje + timedelta(days=1)
        previsao = f"{nova_data.isoformat()}T10:00"

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(
                boa_mesas="1",
                boa_cadeiras="2",
                justificativa_parcial=(
                    "Duas cadeiras ficaram no local."
                ),
                previsao_conclusao=previsao,
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        primeira = (
            ConferenciaRecolhimentoLocacao.objects
            .order_by("criado_em", "id")
            .first()
        )

        self.assertEqual(
            primeira.situacao,
            ConferenciaRecolhimentoLocacao.SITUACAO_PARCIAL,
        )
        self.assertEqual(primeira.pendente_mesas, 0)
        self.assertEqual(primeira.pendente_cadeiras, 2)
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PARCIAL,
        )
        self.assertEqual(tarefa.data_agendada, nova_data)
        self.assertEqual(tarefa.horario_agendado, time(10, 0))
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_PENDENTE_DEVOLUCAO,
        )
        self.assertEqual(
            checklist_operacional_locacoes(self.hoje)["total"],
            0,
        )

        checklist_reagendado = checklist_operacional_locacoes(
            nova_data
        )
        self.assertEqual(
            checklist_reagendado["grupos"]["recolhimentos"][0][
                "tarefa"
            ].pk,
            tarefa.pk,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(
                boa_mesas="0",
                boa_cadeiras="2",
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertEqual(
            ConferenciaRecolhimentoLocacao.objects.count(),
            2,
        )

        segunda = (
            ConferenciaRecolhimentoLocacao.objects
            .order_by("criado_em", "id")
            .last()
        )

        self.assertEqual(
            segunda.situacao,
            ConferenciaRecolhimentoLocacao.SITUACAO_COMPLETA,
        )
        self.assertEqual(segunda.acumulado_boa_mesas, 1)
        self.assertEqual(segunda.acumulado_boa_cadeiras, 4)
        self.assertEqual(segunda.pendente_mesas, 0)
        self.assertEqual(segunda.pendente_cadeiras, 0)
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_DEVOLVIDA,
        )

    def test_recolhimento_com_quebra_baixa_estoque_e_registra_avaria(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(
                boa_cadeiras="3",
                quebrada_cadeiras="1",
                observacao=(
                    "Uma cadeira voltou quebrada."
                ),
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)

        conferencia = (
            ConferenciaRecolhimentoLocacao.objects.get()
        )
        locacao.refresh_from_db()
        tarefa.refresh_from_db()
        self.configuracao.refresh_from_db()

        self.assertEqual(conferencia.quebrada_cadeiras, 1)
        self.assertEqual(conferencia.boa_cadeiras, 3)
        self.assertEqual(
            conferencia.situacao,
            ConferenciaRecolhimentoLocacao.SITUACAO_COMPLETA,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_DEVOLVIDA_COM_AVARIA,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_CONFIRMADA,
        )
        self.assertEqual(
            self.configuracao.total_mesas,
            30,
        )
        self.assertEqual(
            self.configuracao.total_cadeiras,
            119,
        )
        self.assertTrue(
            MovimentoEstoqueLocacao.objects.filter(
                locacao=locacao,
                tipo=(
                    MovimentoEstoqueLocacao
                    .TIPO_BAIXA_QUEBRA
                ),
                item=MovimentoEstoqueLocacao.ITEM_CADEIRA,
                quantidade=1,
            ).exists()
        )

    def test_recolhimento_acima_do_entregue_e_bloqueado(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(
                boa_cadeiras="5",
            ),
            secure=True,
        )

        tarefa.refresh_from_db()
        locacao.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            (
                "A quantidade de cadeiras informada supera "
                "o total que ainda esta pendente."
            ),
        )
        self.assertEqual(
            ConferenciaRecolhimentoLocacao.objects.count(),
            0,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PENDENTE,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_ENTREGUE,
        )

    def test_ocorrencia_no_recolhimento_exige_observacao(self):
        locacao = self.colocar_na_rua(
            self.criar_locacao(
                data_entrega=self.hoje - timedelta(days=1),
                data_prevista_devolucao=self.hoje,
            )
        )
        tarefa = obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_RECOLHIMENTO,
        )

        response = self.client.post(
            reverse(
                "locacoes:conferencia_recolhimento",
                kwargs={"pk": tarefa.pk},
            ),
            self.dados_conferencia_recolhimento(
                boa_cadeiras="3",
                quebrada_cadeiras="1",
                observacao="",
            ),
            secure=True,
        )

        tarefa.refresh_from_db()
        locacao.refresh_from_db()
        self.configuracao.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Explique a quebra, perda ou descarte registrado.",
        )
        self.assertEqual(
            ConferenciaRecolhimentoLocacao.objects.count(),
            0,
        )
        self.assertEqual(
            MovimentoEstoqueLocacao.objects.filter(
                locacao=locacao,
            ).count(),
            0,
        )
        self.assertEqual(
            self.configuracao.total_cadeiras,
            120,
        )
        self.assertEqual(
            tarefa.status,
            TarefaOperacionalLocacao.STATUS_PENDENTE,
        )
        self.assertEqual(
            locacao.status,
            Locacao.STATUS_ENTREGUE,
        )

    def test_excecao_mantem_tarefa_pendente(self):
        locacao = self.criar_locacao()
        tarefa = obter_ou_criar_tarefa_operacional(locacao, TarefaOperacionalLocacao.TIPO_ENTREGA)

        response = self.client.post(
            reverse("locacoes:tarefa_operacional_nao_possivel", kwargs={"pk": tarefa.pk}),
            {"responsavel": "Camila", "observacao": "Cliente nao estava no local."},
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        tarefa.refresh_from_db()
        self.assertEqual(tarefa.status, TarefaOperacionalLocacao.STATUS_NAO_POSSIVEL)
        self.assertEqual(tarefa.motivo_nao_realizado, "Cliente nao estava no local.")
        checklist = checklist_operacional_locacoes(self.hoje)
        self.assertEqual(checklist["total"], 1)
        self.assertEqual(checklist["grupos"]["entregas"][0]["tarefa"].id, tarefa.id)

    def test_alerta_vermelho_ao_chegar_horario_e_devolucao_atrasada(self):
        self.criar_locacao(horario_entrega=time(8, 0))
        agora = timezone.make_aware(datetime.combine(self.hoje, time(8, 5)))

        checklist = checklist_operacional_locacoes(self.hoje, agora=agora)

        self.assertTrue(checklist["alerta"])

        checklist_sem_horario_vencido = checklist_operacional_locacoes(
            self.hoje,
            agora=timezone.make_aware(datetime.combine(self.hoje, time(7, 55))),
        )
        self.assertFalse(checklist_sem_horario_vencido["alerta"])

        atrasada = self.colocar_na_rua(self.criar_locacao(
            pessoa_avulsa_nome="Atrasada",
            data_entrega=self.hoje - timedelta(days=3),
            data_prevista_devolucao=self.hoje - timedelta(days=1),
        ))
        checklist_atrasada = checklist_operacional_locacoes(
            self.hoje,
            agora=timezone.make_aware(datetime.combine(self.hoje, time(7, 55))),
        )
        self.assertTrue(checklist_atrasada["alerta"])
        self.assertEqual(checklist_atrasada["grupos"]["devolucoes_atrasadas"][0]["locacao"].id, atrasada.id)

    def test_oculta_locacoes_canceladas_ou_encerradas(self):
        cancelada = self.criar_locacao(pessoa_avulsa_nome="Cancelada")
        cancelada.cancelar(motivo="Cliente desistiu.")
        devolvida = self.colocar_na_rua(self.criar_locacao(
            pessoa_avulsa_nome="Encerrada",
            data_entrega=self.hoje - timedelta(days=1),
            data_prevista_devolucao=self.hoje,
        ))
        item = devolvida.itens.get()
        devolvida.registrar_devolucao({item.id: {"devolvida_boa": 1}})

        checklist = checklist_operacional_locacoes(self.hoje)
        nomes = [
            item["cliente"]
            for grupo in checklist["grupos"].values()
            for item in grupo
        ]

        self.assertNotIn("Cancelada", nomes)
        self.assertNotIn("Encerrada", nomes)


class LocacoesFormularioReservaTests(TestCase):
    def setUp(self):
        self.hoje = timezone.localdate()
        self.configuracao = ConfiguracaoLocacao.obter()
        self.configuracao.total_mesas = 10
        self.configuracao.total_cadeiras = 40
        self.configuracao.preco_mesa_avulsa_diaria = Decimal("4.00")
        self.configuracao.preco_cadeira_avulsa_diaria = Decimal("1.50")
        self.configuracao.save()
        self.faixa = FaixaPrecoLocacao.objects.get(codigo=FaixaPrecoLocacao.CENTRO_PERTO)
        self.faixa.preco_jogo_diaria = Decimal("8.00")
        self.faixa.save()

    def dados_post(self, **extras):
        dados = {
            "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
            "pessoa_avulsa_nome": "Cliente Formulario",
            "pessoa_avulsa_telefone": "91999990000",
            "endereco_entrega": "Rua Entrega, 456",
            "data_entrega": self.hoje.isoformat(),
            "horario_entrega": "09:00",
            "data_evento": self.hoje.isoformat(),
            "horario_evento": "18:00",
            "data_prevista_devolucao": (self.hoje + timedelta(days=1)).isoformat(),
            "faixa_preco": str(self.faixa.id),
            "observacao": "",
            "sinal_valor": "",
            "sinal_forma_pagamento": "",
            "sinal_observacao": "",
            "data_vencimento_saldo": "",
            "jogos": "1",
            "preco_jogo_diaria": "8.00",
            "mesas_avulsas": "0",
            "preco_mesa_avulsa_diaria": "4.00",
            "cadeiras_avulsas": "0",
            "preco_cadeira_avulsa_diaria": "1.50",
        }
        dados.update(extras)
        return dados

    def criar_locacao(self, **extras):
        return Locacao.criar_reserva(
            {
                "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
                "pessoa_avulsa_nome": extras.pop("pessoa_avulsa_nome", "Ocupante"),
                "pessoa_avulsa_telefone": "91999990000",
                "endereco_entrega": "Rua O, 1",
                "data_entrega": extras.pop("data_entrega", self.hoje),
                "horario_entrega": "09:00",
                "data_evento": self.hoje,
                "horario_evento": "18:00",
                "data_prevista_devolucao": extras.pop("data_prevista_devolucao", self.hoje + timedelta(days=1)),
                "faixa_preco": self.faixa,
                "observacao": "",
                **extras,
            },
            [{
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": 1,
                "preco_diaria": Decimal("8.00"),
            }],
        )

    def disponibilidade(self, **params):
        base = {
            "data_entrega": self.hoje.isoformat(),
            "data_prevista_devolucao": (self.hoje + timedelta(days=1)).isoformat(),
            "jogos": "0",
            "mesas_avulsas": "0",
            "cadeiras_avulsas": "0",
        }
        base.update(params)
        return self.client.get(reverse("locacoes:disponibilidade_dinamica"), base, secure=True).json()

    def test_tela_exibe_apenas_endereco_da_entrega(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        self.assertContains(
            response,
            'name="endereco_entrega"',
        )
        self.assertNotContains(
            response,
            'name="pessoa_avulsa_endereco"',
        )
        self.assertNotContains(
            response,
            "Endereco avulso",
        )

    def test_enter_endereco_entrega_vai_para_faixa_preco(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        self.assertContains(
            response,
            'id="id_endereco_entrega"',
        )
        self.assertContains(
            response,
            'data-enter-next="id_faixa_preco"',
        )

    def test_resumo_valores_dinamico_usa_regra_do_servidor(self):
        response = self.client.get(
            reverse("locacoes:resumo_valores_dinamico"),
            {
                "data_entrega": self.hoje.isoformat(),
                "data_prevista_devolucao": (
                    self.hoje + timedelta(days=1)
                ).isoformat(),
                "jogos": "1",
                "preco_jogo_diaria": "8.00",
                "mesas_avulsas": "2",
                "preco_mesa_avulsa_diaria": "4.00",
                "cadeiras_avulsas": "3",
                "preco_cadeira_avulsa_diaria": "1.50",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)

        dados = response.json()

        self.assertEqual(dados["diarias"], 1)
        self.assertEqual(
            Decimal(dados["subtotal_jogos"]),
            Decimal("8.00"),
        )
        self.assertEqual(
            Decimal(dados["subtotal_mesas_avulsas"]),
            Decimal("8.00"),
        )
        self.assertEqual(
            Decimal(dados["subtotal_cadeiras_avulsas"]),
            Decimal("4.50"),
        )
        self.assertEqual(
            Decimal(dados["total"]),
            Decimal("20.50"),
        )

    def test_pagamento_inicial_acima_do_total_nao_salva(self):
        response = self.client.post(
            reverse("locacoes:nova"),
            self.dados_post(
                sinal_valor="17.00",
                sinal_forma_pagamento=PagamentoLocacao.FORMA_PIX,
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Pagamento inicial nao pode ser maior que o total contratado.",
        )
        self.assertFalse(
            Locacao.objects.filter(
                pessoa_avulsa_nome="Cliente Formulario"
            ).exists()
        )

    def test_tipo_pessoa_recebe_foco_inicial(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        html = response.content.decode("utf-8")

        self.assertIn(
            'id="id_tipo_pessoa"',
            html,
        )

        inicio_tipo = html.rfind(
            "<select",
            0,
            html.index('id="id_tipo_pessoa"') + 1,
        )
        fim_tipo = html.index("</select>", inicio_tipo)
        trecho_tipo = html[inicio_tipo:fim_tipo]

        self.assertIn("autofocus", trecho_tipo)

        inicio_cliente = html.rfind(
            "<select",
            0,
            html.index('id="id_cliente"') + 1,
        )
        fim_cliente = html.index("</select>", inicio_cliente)
        trecho_cliente = html[inicio_cliente:fim_cliente]

        self.assertNotIn("autofocus", trecho_cliente)

    def test_endereco_entrega_tem_enter_para_faixa_preco(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        html = response.content.decode("utf-8")

        self.assertIn(
            'const enderecoEntrega = document.getElementById(',
            html,
        )
        self.assertIn(
            '"id_faixa_preco"',
            html,
        )
        self.assertIn(
            "event.stopImmediatePropagation()",
            html,
        )

    def test_nova_locacao_exibe_data_da_solicitacao(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        self.assertContains(
            response,
            "Data da solicitação",
        )
        self.assertNotContains(
            response,
            "Registrada automaticamente ao salvar a locação.",
        )
        self.assertNotContains(
            response,
            "Esta tela cria uma reserva.",
        )

    def test_lista_exibe_titulos_com_acentuacao(self):
        response = self.client.get(
            reverse("locacoes:lista"),
            secure=True,
        )

        self.assertContains(response, "Locações")
        self.assertContains(response, "Nova locação")
        self.assertContains(response, "Configurações")
        self.assertNotContains(
            response,
            "Reservas de material de locacao.",
        )

    def test_aviso_de_material_aparece_junto_dos_itens(self):
        response = self.client.get(
            reverse("locacoes:nova"),
            secure=True,
        )

        self.assertContains(
            response,
            'id="avisoMaterialItens"',
        )
        self.assertContains(
            response,
            "consultarDisponibilidade",
        )
        self.assertContains(
            response,
            "destinosQuantidade",
        )

    def test_checklist_exibe_data_da_solicitacao(self):
        locacao = Locacao.criar_reserva(
            {
                "tipo_pessoa": Locacao.TIPO_PESSOA_AVULSA,
                "pessoa_avulsa_nome": "Data Solicitacao",
                "pessoa_avulsa_telefone": "91999990000",
                "endereco_entrega": "Rua Data, 1",
                "data_entrega": self.hoje,
                "horario_entrega": "09:00",
                "data_evento": self.hoje,
                "horario_evento": "18:00",
                "data_prevista_devolucao":
                    self.hoje + timedelta(days=1),
                "faixa_preco": self.faixa,
                "observacao": "",
            },
            [{
                "tipo": ItemLocacao.TIPO_JOGO,
                "quantidade": 1,
                "preco_diaria": Decimal("8.00"),
            }],
        )

        obter_ou_criar_tarefa_operacional(
            locacao,
            TarefaOperacionalLocacao.TIPO_ENTREGA,
        )

        response = self.client.get(
            reverse("locacoes:checklist_operacional"),
            secure=True,
        )

        self.assertContains(response, "Solicitada em:")
        self.assertContains(
            response,
            timezone.localtime(
                locacao.criado_em
            ).strftime("%d/%m/%Y"),
        )

    def test_responsavel_interno_nao_aparece_como_digitacao_manual(self):
        response = self.client.get(reverse("locacoes:nova"), secure=True)

        self.assertNotContains(response, "Responsavel interno")
        self.assertNotContains(response, 'name="responsavel"')

    def test_enter_apos_data_prevista_vai_para_jogos(self):
        response = self.client.get(reverse("locacoes:nova"), secure=True)

        self.assertContains(response, 'id="id_data_prevista_devolucao"')
        self.assertContains(response, 'data-enter-next="id_jogos"')

    def test_ordem_dos_horarios_na_secao_datas(self):
        response = self.client.get(reverse("locacoes:nova"), secure=True)
        html = response.content.decode("utf-8")

        self.assertLess(
            html.index("Horário do evento"),
            html.index("Horário combinado de entrega"),
        )

    def test_disponibilidade_suficiente(self):
        resultado = self.disponibilidade(jogos="1")

        self.assertEqual(resultado["status"], "disponivel")
        self.assertEqual(resultado["mensagem"], "Disponível: 10 jogo(s), 10 mesa(s) e 40 cadeira(s) para o período.")

    def test_indisponibilidade_por_mesas(self):
        resultado = self.disponibilidade(mesas_avulsas="11")

        self.assertEqual(resultado["status"], "indisponivel")
        self.assertIn("mesas: solicitado 11, disponível 10", resultado["mensagem"])

    def test_indisponibilidade_por_cadeiras(self):
        resultado = self.disponibilidade(cadeiras_avulsas="41")

        self.assertEqual(resultado["status"], "indisponivel")
        self.assertIn("cadeiras: solicitado 41, disponível 40", resultado["mensagem"])

    def test_indisponibilidade_de_jogos_pela_composicao_mesa_e_cadeiras(self):
        resultado = self.disponibilidade(jogos="11")

        self.assertEqual(resultado["status"], "indisponivel")
        self.assertIn("mesas: solicitado 11, disponível 10", resultado["mensagem"])
        self.assertIn("cadeiras: solicitado 44, disponível 40", resultado["mensagem"])

    def test_locacao_cancelada_ou_encerrada_nao_reduz_disponibilidade(self):
        cancelada = self.criar_locacao(pessoa_avulsa_nome="Cancelada")
        cancelada.cancelar(motivo="Desistencia")
        encerrada = self.criar_locacao(pessoa_avulsa_nome="Encerrada")
        encerrada.marcar_saiu_para_entrega()
        encerrada.confirmar_entrega()
        item = encerrada.itens.get()
        encerrada.registrar_devolucao({item.id: {"devolvida_boa": 1}})

        resultado = self.disponibilidade(jogos="1")

        self.assertEqual(resultado["status"], "disponivel")
        self.assertEqual(resultado["disponivel_mesas"], 10)
        self.assertEqual(resultado["disponivel_cadeiras"], 40)
