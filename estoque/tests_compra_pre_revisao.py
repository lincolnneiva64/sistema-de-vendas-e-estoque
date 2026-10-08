"""Regressao do rascunho financeiro e dos envios de finalizacao atrasados."""
from datetime import date
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import Client, LiveServerTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from offline.browser_support import Chrome
from . import views
from .models import (
    CartaoCredito, Compra, ContaFinanceira, ContaPagar, Fornecedor, ItemCompra,
    LancamentoCartao, ListaCompraFornecedor, MovimentoFinanceiro, Produto,
)


class CompraPreRevisaoFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="teste-pre-revisao")
        self.client.force_login(self.user)
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor teste")
        self.produto = Produto.objects.create(
            nome="Produto teste", quantidade=10, preco_compra=5,
            preco_vista=15, preco_prazo=16, unidade_compra="UN",
        )
        self.lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor, data_lista=date(2026, 10, 8),
            data_inicio_periodo=date(2026, 10, 1), data_fim_periodo=date(2026, 10, 8),
        )
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor, lista_fornecedor=self.lista,
            data_compra=date(2026, 10, 8), total=90, status=Compra.STATUS_RASCUNHO,
        )
        ItemCompra.objects.create(compra=self.compra, produto=self.produto,
                                  quantidade=9, preco_unitario=10, valor_total=90, unidade="UN")
        self.salvar_url = reverse("estoque:compra_salvar_pre_revisao", args=[self.compra.pk])
        self.editar_url = reverse("estoque:compra_editar", args=[self.compra.pk])
        self.finalizar_url = reverse("estoque:compra_finalizar", args=[self.compra.pk])

    def payload(self, **extra):
        dados = {
            "acao_compra": "finalizar", "fornecedor_id": str(self.fornecedor.pk),
            "data_compra": "2026-10-08", "tipo_pagamento": "aprazo",
            "data_vencimento": "2026-11-08", "produto_id[]": [str(self.produto.pk)],
            "quantidade[]": ["9"], "preco_unitario[]": ["10,00"], "unidade[]": ["UN"],
            "compra_forma_cobranca_nota": "varios_boletos", "compra_quantidade_boletos": "3",
            "compra_valor_nota_boleto": "90,00", "compra_observacao_pagamento_nota": "Nota teste",
            "observacao": "Rascunho teste",
        }
        for n in range(1, 4):
            dados[f"compra_parcela_valor_{n}"] = "30,00"
            dados[f"compra_parcela_vencimento_{n}"] = f"2026-11-{8 + (n-1)*10:02d}"
            dados[f"compra_parcela_observacao_{n}"] = f"Documento {n}"
        dados.update(extra)
        return dados

    def assert_sem_efeitos(self):
        self.compra.refresh_from_db(); self.produto.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_RASCUNHO)
        self.assertFalse(self.compra.estoque_entrada_realizada)
        self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertEqual(self.produto.quantidade, Decimal("10"))
        self.assertEqual(self.produto.preco_compra, Decimal("5"))
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(LancamentoCartao.objects.exists())
        self.assertFalse(self.compra.itens.filter(revisao_preco_concluida=True).exists())


class CompraPreRevisaoTests(CompraPreRevisaoFixture, TestCase):
    def test_persiste_tres_boletos_e_reabre_sem_efeitos(self):
        resposta = self.client.post(self.salvar_url, self.payload())
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        self.assert_sem_efeitos()
        self.assertEqual(self.compra.tipo_pagamento, "aprazo")
        self.assertEqual(self.compra.data_vencimento, date(2026, 11, 8))
        pagina = self.client.get(self.editar_url)
        linhas = pagina.context["pagamento_nota_compra"]["linhas"]
        self.assertEqual(len(linhas), 3)
        for n, linha in enumerate(linhas, 1):
            self.assertEqual(linha["valor"], Decimal("30"))
            self.assertEqual(linha["observacao"], f"Documento {n}")
            self.assertEqual(linha["vencimento"], f"2026-11-{8 + (n-1)*10:02d}")

    def test_validacao_reverte_rascunho_e_parcelas(self):
        self.client.post(self.salvar_url, self.payload())
        ids = list(self.lista.parcelas_nota.values_list("id", flat=True))
        for extra in ({"compra_parcela_valor_2": "31"}, {"compra_parcela_vencimento_2": ""}):
            with self.subTest(extra=extra):
                resposta = self.client.post(self.salvar_url, self.payload(observacao="Nao persistir", **extra))
                self.assertEqual(resposta.status_code, 400)
                self.assertIn("erro", resposta.json())
                self.compra.refresh_from_db()
                self.assertEqual(self.compra.observacao, "Rascunho teste")
                self.assertEqual(list(self.lista.parcelas_nota.values_list("id", flat=True)), ids)
        self.assert_sem_efeitos()

    def test_falha_de_gravacao_reverte_tudo(self):
        with patch("estoque.views.logger"), patch("estoque.views.ParcelaNotaListaCompraFornecedor.objects.bulk_create", side_effect=RuntimeError("falha teste")):
            self.assertEqual(self.client.post(self.salvar_url, self.payload()).status_code, 500)
        self.compra.refresh_from_db(); self.lista.refresh_from_db()
        self.assertEqual(self.compra.tipo_pagamento, "")
        self.assertIsNone(self.lista.valor_nota_boleto)
        self.assertFalse(self.lista.parcelas_nota.exists())
        self.assert_sem_efeitos()

    def test_finalizacao_repetida_em_ambas_rotas(self):
        self.client.post(self.salvar_url, self.payload())
        self.client.post(self.editar_url, self.payload())
        itens = list(self.compra.itens.values_list("id", flat=True))
        for url in (self.editar_url, self.finalizar_url):
            self.client.post(url, self.payload(acao_compra="confirmar_financeiro"))
        self.produto.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)
        self.assertEqual(list(self.compra.itens.values_list("id", flat=True)), itens)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertEqual(self.client.post(self.salvar_url, self.payload()).status_code, 409)

    def test_finalizada_entre_leitura_inicial_e_lock(self):
        # Intercalacao deterministica: a requisicao B termina enquanto A ainda
        # tem a instancia antiga. A deve revalidar a instancia obtida pelo lock.
        original = views._dados_compra_post
        for url in (self.editar_url, self.finalizar_url):
            with self.subTest(url=url):
                self.compra.status = Compra.STATUS_RASCUNHO
                self.compra.estoque_entrada_realizada = False
                self.compra.save()
                ContaPagar.objects.all().delete()
                Produto.objects.filter(pk=self.produto.pk).update(quantidade=10)

                def finalizar_outra_requisicao(request, exigir_itens=True):
                    dados = original(request, exigir_itens)
                    with patch("estoque.views._dados_compra_post", original):
                        self.client.post(self.editar_url, self.payload())
                    return dados

                with patch("estoque.views._dados_compra_post", side_effect=finalizar_outra_requisicao):
                    self.client.post(url, self.payload(acao_compra="confirmar_financeiro"))
                self.compra.refresh_from_db(); self.produto.refresh_from_db()
                self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
                self.assertEqual(self.produto.quantidade, Decimal("19"))
                self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)

    def test_salvador_nao_rebaixa_compra_finalizada(self):
        self.client.post(self.editar_url, self.payload())
        self.compra.refresh_from_db()
        with self.assertRaises(ValueError):
            views._salvar_compra_e_itens(self.compra, {}, Compra.STATUS_RASCUNHO)

    def test_rollback_da_finalizacao_preserva_snapshot_pre_revisao(self):
        self.client.post(self.salvar_url, self.payload())
        ids = list(self.lista.parcelas_nota.values_list("id", flat=True))
        for url in (self.editar_url, self.finalizar_url):
            with self.subTest(url=url):
                with patch("estoque.views.logger"), patch("estoque.views._criar_contas_pagar_compra", side_effect=RuntimeError("falha teste")):
                    self.client.post(url, self.payload(acao_compra="confirmar_financeiro", observacao="Nao persistir"))
                self.assert_sem_efeitos()
                self.assertEqual(self.compra.tipo_pagamento, "aprazo")
                self.assertEqual(self.compra.observacao, "Rascunho teste")
                self.assertEqual(list(self.lista.parcelas_nota.values_list("id", flat=True)), ids)

    def test_revisar_depois_finaliza_e_mantem_pendencia(self):
        self.client.post(self.salvar_url, self.payload())
        self.client.post(self.finalizar_url, self.payload(
            acao_compra="confirmar_financeiro", revisao_precos_pendente="1",
            fluxo_mobile_compra="1",
            **{"atualizar_custo_produto_ids[]": [str(self.produto.pk)]},
        ))
        self.compra.refresh_from_db(); self.produto.refresh_from_db()
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(views._itens_revisao_precos_posterior().count(), 1)
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)

    def test_a_vista_pre_salva_sem_movimento_e_finaliza_uma_vez(self):
        ContaFinanceira.objects.create(nome="Caixa teste", tipo="caixa")
        dados = self.payload(tipo_pagamento="avista", origem_caixa="90", acao_compra="confirmar_financeiro")
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.assert_sem_efeitos()
        for _ in range(2):
            self.client.post(self.finalizar_url, dados)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(), 1)
        self.assertFalse(ContaPagar.objects.exists())

    def test_cartao_pre_salva_sem_lancamento_e_finaliza_uma_vez(self):
        cartao = CartaoCredito.objects.create(nome="Cartao teste", titular="Teste")
        dados = self.payload(tipo_pagamento="cartao_credito", cartao_id=str(cartao.pk))
        self.assertEqual(self.client.post(self.salvar_url, dados).status_code, 200)
        self.assert_sem_efeitos()
        for _ in range(2):
            self.client.post(self.editar_url, dados)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(LancamentoCartao.objects.filter(compra=self.compra).count(), 1)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_compra_nova_reutiliza_token_no_pre_salvamento(self):
        url = reverse("estoque:compra_nova_salvar_pre_revisao")
        dados = self.payload(fechamento_token="a" * 32)
        respostas = [self.client.post(url, dados) for _ in range(2)]
        self.assertEqual([r.status_code for r in respostas], [200, 200])
        self.assertEqual(respostas[0].json(), respostas[1].json())
        self.assertEqual(Compra.objects.filter(fechamento_token="a" * 32).count(), 1)
        self.assertFalse(ContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())


CHROME = os.environ.get("COMPRA_TEST_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class CompraFinalizacaoConcorrenteTests(CompraPreRevisaoFixture, TransactionTestCase):
    def test_dois_posts_com_instancias_antigas_finalizam_uma_vez(self):
        self.client.post(self.salvar_url, self.payload())
        original = views._dados_compra_post
        barreira = Barrier(2)

        def validar_antes_do_lock(request, exigir_itens=True):
            dados = original(request, exigir_itens)
            barreira.wait(timeout=10)
            return dados

        clientes = [Client(), Client()]
        for cliente in clientes:
            cliente.force_login(self.user)

        def enviar(cliente, url):
            close_old_connections()
            try:
                return cliente.post(url, self.payload(acao_compra="confirmar_financeiro")).status_code
            finally:
                close_old_connections()

        with patch("estoque.views._dados_compra_post", side_effect=validar_antes_do_lock):
            with ThreadPoolExecutor(max_workers=2) as executor:
                envios = [executor.submit(enviar, cliente, url) for cliente, url in zip(clientes, (self.editar_url, self.finalizar_url))]
                self.assertEqual([envio.result(timeout=20) for envio in envios], [302, 302])
        self.compra.refresh_from_db(); self.produto.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, Decimal("19"))
        self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)
        self.assertFalse(MovimentoFinanceiro.objects.exists())


@skipUnless(Path(CHROME).is_file(), "Chrome indisponivel")
class CompraPreRevisaoBrowserTests(CompraPreRevisaoFixture, LiveServerTestCase):
    def test_revisao_so_abre_apos_salvar_e_boletos_sobrevivem_reload(self):
        self.verificar_fluxo(mobile=False)

    def test_mobile_finalizar_e_revisar_depois(self):
        self.verificar_fluxo(mobile=True)

    def verificar_fluxo(self, mobile):
        with TemporaryDirectory(prefix="compra-pre-revisao-", ignore_cleanup_errors=True) as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                if mobile:
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + self.editar_url})
                tab.wait("document.readyState === 'complete' && !!document.getElementById('compraQuantidadeBoletos')")
                tab.evaluate("""document.getElementById('tipoPagamentoCompra').value='aprazo';
                    document.getElementById('tipoPagamentoCompra').dispatchEvent(new Event('change'));
                    document.getElementById('compraFormaCobrancaNota').value='varios_boletos';
                    document.getElementById('compraQuantidadeBoletos').value='3';
                    document.getElementById('compraFormaCobrancaNota').dispatchEvent(new Event('change'));
                    for(let n=1;n<=3;n++) {
                        document.querySelector('[name="compra_parcela_valor_'+n+'"]').value='30,00';
                        document.querySelector('[name="compra_parcela_vencimento_'+n+'"]').value='2026-11-'+String(8+(n-1)*10).padStart(2,'0');
                        document.querySelector('[name="compra_parcela_observacao_'+n+'"]').value='Documento '+n;
                    }
                    document.querySelector('[name="compra_parcela_valor_2"]').value='31,00';
                    document.querySelector('[name="compra_parcela_valor_2"]').dispatchEvent(new Event('input',{bubbles:true}));
                    document.getElementById('btnSalvarCompra').click();true""")
                tab.wait("!document.getElementById('erroSalvarPreRevisaoCompra').hidden")
                self.assertFalse(tab.evaluate("document.getElementById('modalConferenciaPrecosCompra').classList.contains('visivel')"))
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_valor_2]').value"), "31,00")
                self.assert_sem_efeitos()
                tab.evaluate("""window.fetchAntesDoTeste=window.fetch;
                    window.fetch=function(url, opcoes) {
                        if(String(url).includes('/salvar-pre-revisao/')) return Promise.reject(new Error('Falha de conexao simulada'));
                        return window.fetchAntesDoTeste(url,opcoes);
                    };
                    document.getElementById('btnSalvarCompra').click();true""")
                tab.wait("document.getElementById('erroSalvarPreRevisaoCompra').textContent.includes('Falha de conexao simulada')")
                self.assertFalse(tab.evaluate("document.getElementById('modalConferenciaPrecosCompra').classList.contains('visivel')"))
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_valor_2]').value"), "31,00")
                tab.evaluate("window.fetch=window.fetchAntesDoTeste;true")
                tab.evaluate("document.querySelector('[name=compra_parcela_valor_2]').value='30,00';document.querySelector('[name=compra_parcela_valor_2]').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('btnSalvarCompra').click();true")
                tab.wait("document.getElementById('modalConferenciaPrecosCompra').classList.contains('visivel')")
                self.assertEqual(self.lista.parcelas_nota.count(), 3)
                self.assert_sem_efeitos()
                tab.call("Page.reload")
                tab.wait("!!document.querySelector('[name=compra_parcela_observacao_3]')")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_observacao_3]').value"), "Documento 3")
                self.assertEqual(tab.evaluate("document.querySelector('[name=compra_parcela_vencimento_3]').value"), "2026-11-28")
                tab.evaluate("document.getElementById('btnSalvarCompra').click();true")
                tab.wait("document.getElementById('modalConferenciaPrecosCompra').classList.contains('visivel')")
                if mobile:
                    tab.evaluate("document.getElementById('btnFinalizarRevisarDepoisCompra').click();true")
                else:
                    tab.evaluate("document.getElementById('btnAtualizarPrecosContinuarCompra').click();true")
                tab.wait("location.pathname === '/compras/'")
                self.compra.refresh_from_db(); self.produto.refresh_from_db()
                self.assertEqual(self.compra.status, Compra.STATUS_FINALIZADA)
                self.assertEqual(self.compra.revisao_precos_pendente, mobile)
                self.assertEqual(self.produto.quantidade, Decimal("19"))
                self.assertEqual(ContaPagar.objects.filter(compra=self.compra).count(), 3)
            finally:
                chrome.stop()
