"""Atomicidade e identidade do pagamento direto, somente com dados fictícios."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from estoque.models import MovimentoFinanceiro
from . import tests_formulario_erros as formulario
from .models import EventoLocacao, ItemLocacao, Locacao, PagamentoLocacao


class GravacaoSeguraTests(TestCase):
    setUp = formulario.FormularioErrosTests.setUp
    dados_nova_locacao = formulario.FormularioErrosTests.dados_nova_locacao

    def criar(self, **campos):
        return self.client.post(reverse("locacoes:nova"), self.dados_nova_locacao(endereco_entrega="", **campos))

    def dados_pagamento(self, locacao, **campos):
        url = reverse("locacoes:registrar_pagamento", args=[locacao.pk])
        token = self.client.get(url).context["operacao_token"]
        return {"valor": "5.00", "forma_pagamento": "pix", "observacao": "Parcela fictícia", "operacao_token": token, **campos}

    def pagar(self, locacao, dados):
        return self.client.post(reverse("locacoes:registrar_pagamento", args=[locacao.pk]), dados)

    def conferir(self, locacao, quantidade, pago):
        locacao.refresh_from_db()
        self.assertEqual(locacao.total_pago, Decimal(pago))
        self.assertEqual(locacao.saldo_devedor, locacao.total - Decimal(pago))
        pagamentos = list(locacao.pagamentos.select_related("movimento_financeiro"))
        self.assertEqual(len(pagamentos), quantidade)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="locacao").count(), quantidade)
        for pagamento in pagamentos:
            self.assertEqual(pagamento.movimento_financeiro.valor, pagamento.valor)
            self.assertEqual(pagamento.movimento_financeiro.tipo, MovimentoFinanceiro.TIPO_ENTRADA)

    def test_criacao_sem_sinal_e_sem_endereco(self):
        self.assertEqual(self.criar().status_code, 302)
        locacao = Locacao.objects.get()
        self.assertEqual(locacao.endereco_entrega, "")
        self.conferir(locacao, 0, "0")

    def test_criacao_com_sinal_valido(self):
        resposta = self.criar(sinal_valor="5.00", sinal_forma_pagamento="dinheiro")
        self.assertEqual(resposta.status_code, 302)
        locacao = Locacao.objects.get()
        self.conferir(locacao, 1, "5")
        self.assertEqual(locacao.pagamentos.get().movimento_financeiro.conta.nome, "Caixa em especie")

    def test_falha_de_validacao_no_sinal_reverte_reserva_itens_eventos(self):
        with patch.object(PagamentoLocacao, "criar_movimento_financeiro", side_effect=ValidationError("Falha fictícia")):
            resposta = self.criar(sinal_valor="5.00", sinal_forma_pagamento="pix")
        self.assertEqual(resposta.status_code, 200)
        for model in (Locacao, ItemLocacao, EventoLocacao, PagamentoLocacao, MovimentoFinanceiro):
            self.assertEqual(model.objects.count(), 0)
        self.assertContains(resposta, "LOCAÇÃO NÃO GRAVADA")

    def test_falha_apos_criar_movimento_reverte_tudo_inclusive_quitacao(self):
        original = PagamentoLocacao.criar_movimento_financeiro
        def falhar(pagamento):
            original(pagamento)
            raise RuntimeError("Falha fictícia após movimento")
        total = "8.00"
        with patch.object(PagamentoLocacao, "criar_movimento_financeiro", falhar), patch("locacoes.views.logger.exception"):
            resposta = self.criar(sinal_valor=total, sinal_forma_pagamento="pix")
        self.assertEqual(resposta.status_code, 200)
        for model in (Locacao, ItemLocacao, EventoLocacao, PagamentoLocacao, MovimentoFinanceiro):
            self.assertEqual(model.objects.count(), 0)
        self.assertContains(resposta, "Nenhuma reserva ou pagamento foi gravado neste envio")
        self.assertEqual(resposta.context["locacao_form"]["sinal_valor"].value(), total)

    def test_reenvio_identico_retorna_mesmo_recibo(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao)
        primeira, repetida = self.pagar(locacao, dados), self.pagar(locacao, dados)
        self.assertEqual(primeira.status_code, 302)
        self.assertEqual(primeira.url, repetida.url)
        self.conferir(locacao, 1, "5")
        self.assertEqual(EventoLocacao.objects.filter(tipo="pagamento").count(), 1)

    def test_reenvio_de_quitacao_nao_falha_por_saldo_zero(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao, valor=str(locacao.total))
        primeira, repetida = self.pagar(locacao, dados), self.pagar(locacao, dados)
        self.assertEqual(primeira.status_code, 302)
        self.assertEqual(primeira.url, repetida.url)
        self.conferir(locacao, 1, str(locacao.total))

    def test_dois_pagamentos_legitimos_do_mesmo_valor(self):
        self.criar()
        locacao = Locacao.objects.get()
        primeiro, segundo = self.dados_pagamento(locacao, valor="2.00"), self.dados_pagamento(locacao, valor="2.00")
        self.assertNotEqual(primeiro["operacao_token"], segundo["operacao_token"])
        self.assertEqual(self.pagar(locacao, primeiro).status_code, 302)
        self.assertEqual(self.pagar(locacao, segundo).status_code, 302)
        self.conferir(locacao, 2, "4")

    def test_mesmo_envio_com_valor_alterado_nao_cria_pagamento(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao)
        self.pagar(locacao, dados)
        resposta = self.pagar(locacao, {**dados, "valor": "6.00"})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este envio já foi registrado com outros dados")
        self.conferir(locacao, 1, "5")

    def test_token_ausente_adulterado_ou_de_outra_locacao_nao_grava(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao)
        self.criar()
        outra = Locacao.objects.exclude(pk=locacao.pk).get()
        for token in ("", dados["operacao_token"] + "x", self.dados_pagamento(outra)["operacao_token"]):
            resposta = self.pagar(locacao, {**dados, "operacao_token": token})
            self.assertEqual(resposta.status_code, 200)
            self.assertContains(resposta, "Não foi possível identificar este envio")
        self.conferir(locacao, 0, "0")

    def test_falha_direta_preserva_token_e_permite_repetir_com_seguranca(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao)
        with patch.object(PagamentoLocacao, "criar_movimento_financeiro", side_effect=RuntimeError("Falha fictícia")), patch("locacoes.views.logger.exception"):
            resposta = self.pagar(locacao, dados)
        self.assertEqual(resposta.context["operacao_token"], dados["operacao_token"])
        self.assertContains(resposta, "Tente novamente usando este mesmo formulário")
        self.conferir(locacao, 0, "0")
        self.assertEqual(self.pagar(locacao, dados).status_code, 302)
        self.conferir(locacao, 1, "5")

    def test_formulario_antigo_recusado_e_reabertura_na_mesma_sessao_funciona(self):
        self.criar()
        locacao = Locacao.objects.get()
        sessao = self.client.session
        sessao["marcador_teste"] = "preservar"
        sessao.save()
        dados = self.dados_pagamento(locacao)
        dados.pop("operacao_token")
        resposta = self.pagar(locacao, dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Reabra Registrar pagamento para tentar novamente")
        self.conferir(locacao, 0, "0")
        # O mesmo cliente/cookies/sessão abre o formulário atual e pode salvar.
        novos_dados = self.dados_pagamento(locacao)
        self.assertEqual(self.client.session["marcador_teste"], "preservar")
        self.assertEqual(self.pagar(locacao, novos_dados).status_code, 302)
        self.conferir(locacao, 1, "5")

    def test_meios_de_pagamento_preservam_destinos(self):
        self.criar()
        locacao = Locacao.objects.get()
        for forma, conta in (("dinheiro", "Caixa em especie"), ("pix", "Banco/Pix"), ("cartao", "Cartoes a receber"), ("outro", "Banco/Pix")):
            dados = self.dados_pagamento(locacao, valor="1.00", forma_pagamento=forma)
            self.assertEqual(self.pagar(locacao, dados).status_code, 302)
            self.assertEqual(locacao.pagamentos.first().movimento_financeiro.conta.nome, conta)
        self.conferir(locacao, 4, "4")

    def test_chamada_sem_token_do_recebimento_unificado_permanece_sem_movimento_individual(self):
        self.criar()
        locacao = Locacao.objects.get()
        pagamento = locacao.registrar_pagamento(Decimal("5"), "pix", criar_movimento_financeiro=False)
        self.assertIsNone(pagamento.movimento_financeiro_id)
        self.assertEqual(locacao.total_pago, Decimal("5"))
        self.assertFalse(MovimentoFinanceiro.objects.exists())


class PagamentosSimultaneosTests(TransactionTestCase):
    setUp = GravacaoSeguraTests.setUp
    dados_nova_locacao = GravacaoSeguraTests.dados_nova_locacao
    criar = GravacaoSeguraTests.criar
    dados_pagamento = GravacaoSeguraTests.dados_pagamento
    conferir = GravacaoSeguraTests.conferir

    @skipUnlessDBFeature("has_select_for_update")
    def test_dois_posts_simultaneos_do_mesmo_envio(self):
        self.criar()
        locacao = Locacao.objects.get()
        dados = self.dados_pagamento(locacao)
        barreira = Barrier(2)
        def enviar():
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                resposta = Client().post(reverse("locacoes:registrar_pagamento", args=[locacao.pk]), dados)
                return resposta.status_code, resposta.get("Location")
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            respostas = list(pool.map(lambda _: enviar(), range(2)))
        self.assertEqual(respostas[0][0], 302)
        self.assertEqual(respostas[0], respostas[1])
        self.conferir(locacao, 1, "5")

    @skipUnlessDBFeature("has_select_for_update")
    def test_duas_operacoes_simultaneas_de_mesmo_valor(self):
        self.criar()
        locacao = Locacao.objects.get()
        tokens = [uuid4(), uuid4()]
        barreira = Barrier(2)
        def enviar(token):
            close_old_connections()
            try:
                objeto = Locacao.objects.get(pk=locacao.pk)
                barreira.wait(timeout=10)
                return objeto.registrar_pagamento(Decimal("2"), "pix", recibo_token=token).pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(enviar, tokens))
        self.assertEqual(len(set(ids)), 2)
        self.conferir(locacao, 2, "4")
