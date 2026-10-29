"""Printing regressions with synthetic documents and disposable stock only."""
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from estoque.models import EventoVenda, ItemCompra, ItemVenda, Produto, Venda
from . import conciliacao_documental
from . import tests_ajustes as ajustes_tests
from . import tests_conciliacao_documental as conciliacao_tests
from . import tests_historico as historico_tests
from . import tests_saldos_historicos as saldos_tests
from .evidencia_saldos import conflito_eventos
from .models import (AplicacaoAjuste, ConfirmacaoContagem, MovimentoObservado,
                     PropostaAjuste, RodadaContagem)


class PoliticaImpressaoTests(SimpleTestCase):
    def test_default_conservador_e_opcao_limitada_a_impressao(self):
        eventos = [dict(tipo_evento='nota_impressa', descricao='Nota impressa.')]
        self.assertTrue(conflito_eventos(eventos, set()))
        self.assertEqual(conflito_eventos(eventos, set(), aceitar_nota_impressa=True), '')
        eventos.append(dict(tipo_evento='evento_desconhecido', descricao='Sem prova'))
        self.assertTrue(conflito_eventos(eventos, set(), aceitar_nota_impressa=True))


class ImpressaoExtratoTests(historico_tests.HistoricoFixture, TestCase):
    venda_snapshot = saldos_tests.SaldosHistoricosTests.venda_snapshot
    linhas = saldos_tests.SaldosHistoricosTests.linhas

    def setUp(self):
        super().setUp()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=41)
        self.anterior = self.venda_snapshot(-1, 23, 6, 17)
        self.posterior = self.venda_snapshot(1, 58, 7, 51)
        self.ultima = self.venda_snapshot(2, 51, 3, 48)

    def imprimir(self):
        resposta = self.client.post(reverse('estoque:registrar_impressao',
                                            args=[self.posterior.venda_id]))
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()['sucesso'])

    def test_impressao_unica_e_repetida_nao_bloqueia_extrato(self):
        for total in (1, 2):
            self.imprimir()
            vendas, compras, _ = self.linhas()
            self.assertEqual(vendas[self.posterior.pk]['saldo'], 51)
            self.assertEqual(compras[0]['saldo'], 58)
            self.assertEqual(EventoVenda.objects.filter(venda_id=self.posterior.venda_id,
                                                       tipo_evento='nota_impressa').count(), total)

    def test_caso_998_equivalente_certifica_cinco_para_zero_pct(self):
        # Match the audited shape, without reading or reserving operational IDs.
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=5, unidade='PCT',
            estoque_antes=5, estoque_movimentado=5, estoque_depois=0,
            estoque_unidade_snapshot='PCT')
        self.imprimir()
        self.imprimir()
        vendas, _, _ = self.linhas()
        linha = vendas[self.posterior.pk]
        self.assertEqual((linha['saida'], linha['saldo_antes'], linha['saldo']), (5, 5, 0))
        self.assertEqual(linha['saldo_unidade'], 'PCT')
        self.assertEqual(linha['saldo_tipo'], 'Snapshot registrado')

    def test_impressao_nao_encobre_edicao_real_ou_evento_desconhecido(self):
        self.imprimir()
        for tipo, descricao in (
            ('quantidade_item_alterada',
             f'Quantidade alterada na nota: {self.produto.nome}. De 6 CX para 7 CX.'),
            ('evento_desconhecido', 'Sem identidade comprovada'),
        ):
            with self.subTest(tipo=tipo):
                evento = EventoVenda.objects.create(venda=self.posterior.venda,
                    tipo_evento=tipo, descricao=descricao)
                vendas, compras, _ = self.linhas()
                self.assertIsNone(vendas[self.posterior.pk]['saldo'])
                self.assertIsNone(compras[0]['saldo'])
                evento.delete()

    def test_impressao_nao_encobre_snapshot_inconsistente_ou_conversao_ausente(self):
        self.imprimir()
        for campos in (dict(estoque_depois=49), dict(estoque_antes=None),
                       dict(unidade='UN', quantidade=84)):
            with self.subTest(campos=campos):
                ItemVenda.objects.filter(pk=self.posterior.pk).update(**campos)
                vendas, compras, _ = self.linhas()
                self.assertIsNone(vendas[self.posterior.pk]['saldo'])
                self.assertIsNone(compras[0]['saldo'])
                ItemVenda.objects.filter(pk=self.posterior.pk).update(
                    estoque_depois=51, estoque_antes=58, unidade='CX', quantidade=7)

    def test_movimento_intermediario_sem_origem_continua_interrompendo_ponte(self):
        self.imprimir()
        _, compras, _ = self.linhas()
        self.assertEqual(compras[0]['saldo'], 58)
        MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=17, depois=17, origem='nao_identificada',
            registrado_em=self.inicio + timedelta(hours=1))
        vendas, compras, _ = self.linhas()
        self.assertIsNone(compras[0]['saldo'])
        self.assertEqual(vendas[self.posterior.pk]['saldo'], 51)
        self.assertTrue(vendas[self.posterior.pk]['saldo_pontual'])

    def test_impressao_nao_libera_evidencia_truncada(self):
        self.imprimir()
        with patch('inventario.saldos_historicos.LIMITE_EVIDENCIAS', 1):
            vendas, compras, _ = self.linhas()
        self.assertIsNone(vendas[self.posterior.pk]['saldo'])
        self.assertIsNone(compras[0]['saldo'])

    def test_endpoint_impressao_e_leitura_preservam_estoque_e_documentos(self):
        modelos = (Produto, Venda, ItemVenda, MovimentoObservado, RodadaContagem,
                   ConfirmacaoContagem, PropostaAjuste, AplicacaoAjuste)
        antes = {m: list(m.objects.order_by('pk').values()) for m in modelos}
        self.imprimir()
        self.linhas()
        for modelo in modelos:
            with self.subTest(modelo=modelo.__name__):
                self.assertEqual(list(modelo.objects.order_by('pk').values()), antes[modelo])
        self.assertEqual(EventoVenda.objects.filter(tipo_evento='nota_impressa').count(), 1)


class ConciliacaoImpressaoTests(ajustes_tests.AjustesFixture, TestCase):
    vender = conciliacao_tests.ConciliacaoDocumentalTests.vender

    def test_conciliacao_real_permanece_conservadora_apos_impressao(self):
        venda, _, movimento = self.vender()
        # SQLite's observer clock has millisecond precision. Keep the synthetic
        # sale and its observer at an explicit instant after the count.
        MovimentoObservado.objects.filter(pk=movimento.pk).update(registrado_em=venda.criado_em)
        movimento.refresh_from_db()
        self.assertEqual(len(conciliacao_documental.conciliar(self.fresca(), [movimento])), 1)
        resposta = self.client.post(reverse('estoque:registrar_impressao', args=[venda.pk]))
        self.assertEqual(resposta.status_code, 200)
        with self.assertRaises(ValueError):
            conciliacao_documental.conciliar(self.fresca(), [movimento])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 4)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(AplicacaoAjuste.objects.exists())
