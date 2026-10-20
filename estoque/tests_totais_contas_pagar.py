"""Totais do conjunto filtrado e apresentacao real, com dados sinteticos."""
import base64
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from offline.browser_support import Chrome
from estoque.models import Compra, ContaPagar, Fornecedor, MovimentoFinanceiro, PagamentoContaPagar


class DadosContasMixin:
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Ademar Ceasa (teste)")
        self.url = reverse("estoque:contas_pagar")
        self.contas = []
        for indice, (original, pago) in enumerate([("200", "100"), ("185", "60.50"), ("200", "0")], 1):
            compra = Compra.objects.create(fornecedor=self.fornecedor, data_compra=date(2026, 10, indice))
            conta = ContaPagar.objects.create(
                compra=compra, fornecedor=self.fornecedor, data_emissao=date(2026, 10, indice),
                data_vencimento=date(2026, 10, 10 + indice), valor_original=original,
                valor_em_aberto=Decimal(original) - Decimal(pago),
                status=ContaPagar.STATUS_PARCIAL if Decimal(pago) else ContaPagar.STATUS_ABERTA,
                documento_legado=f"TOTAL-{indice}",
            )
            if Decimal(pago):
                PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 5), valor=pago)
            self.contas.append(conta)
        outro = Fornecedor.objects.create(nome="Outro fornecedor (teste)")
        ContaPagar.objects.create(fornecedor=outro, data_emissao=date(2026, 10, 1), valor_original=999, valor_em_aberto=999)

    def consultar(self, **params):
        return self.client.get(self.url, {"fornecedor": self.fornecedor.pk, **params})


class TotaisContasPagarTests(DadosContasMixin, TestCase):
    def conferir(self, resposta, quantidade, original, pago, juros, aberto):
        self.assertEqual(resposta.status_code, 200)
        total = resposta.context["totais_resultados"]
        self.assertEqual(total["quantidade"], quantidade)
        self.assertEqual([total[k] for k in ["original", "pago", "juros", "aberto"]],
                         list(map(Decimal, [original, pago, juros, aberto])))
        self.assertEqual(total["original"], resposta.context["total_original"])
        if quantidade:
            self.assertContains(resposta, "data-resultados-totais", count=2)
        return total

    def test_fornecedor_exemplo_e_consulta_sem_alteracao_financeira(self):
        modelos = [ContaPagar, PagamentoContaPagar, MovimentoFinanceiro]
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        resposta = self.consultar()
        self.conferir(resposta, 3, "585", "160.50", "0", "424.50")
        self.assertContains(resposta, "R$ 424,50")
        self.assertContains(resposta, "Todos os resultados")
        self.assertEqual(resposta.context["total_aberto"], Decimal("424.50"))
        self.assertEqual(antes, {m: list(m.objects.order_by("pk").values()) for m in modelos})

    def test_situacoes_quitada_parcial_aberta_e_cancelada(self):
        conta = self.contas[0]
        PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 6), valor=100)
        conta.valor_em_aberto = 0
        conta.status = ContaPagar.STATUS_PAGA
        conta.save()
        self.conferir(self.consultar(situacao="paga"), 1, "200", "200", "0", "0")
        self.conferir(self.consultar(situacao="parcial"), 1, "185", "60.50", "0", "124.50")
        self.conferir(self.consultar(situacao="aberta"), 1, "200", "0", "0", "200")
        self.conferir(self.consultar(situacao="todas"), 3, "585", "260.50", "0", "324.50")
        conta = self.contas[2]
        conta.status = ContaPagar.STATUS_CANCELADA
        conta.save()
        self.conferir(self.consultar(situacao="cancelada"), 1, "200", "0", "0", "200")

    def test_datas_documento_busca_e_atalho(self):
        self.conferir(self.consultar(data_inicio="2026-10-02", data_fim="2026-10-03", data_por="compra"), 2, "385", "60.50", "0", "324.50")
        self.conferir(self.consultar(data_inicio="2026-10-12", data_fim="2026-10-12", data_por="vencimento"), 1, "185", "60.50", "0", "124.50")
        self.conferir(self.consultar(compra="TOTAL-1", q="Ademar"), 1, "200", "100", "0", "100")
        self.conferir(self.consultar(atalho="parciais"), 2, "385", "160.50", "0", "224.50")

    def test_juros_pagamentos_cancelados_e_multiplos_sem_duplicar_contas(self):
        conta = self.contas[0]
        PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 6), valor=10, juros_bancarios="2.50")
        PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 7), valor=5, juros_bancarios="1.25")
        PagamentoContaPagar.objects.create(conta=conta, data_pagamento=date(2026, 10, 6), valor=999, juros_bancarios=999, cancelado=True)
        self.conferir(self.consultar(atalho="com_juros", data_inicio="2026-10-06", data_fim="2026-10-07"), 1, "200", "115", "3.75", "100")

    def test_pagamento_pela_baixa_atualiza_rodape(self):
        from estoque.views import _conta_financeira_saida_pagar_fornecedor
        banco = _conta_financeira_saida_pagar_fornecedor("banco")
        banco.saldo_inicial = 1000
        banco.save()
        resposta = self.client.post(reverse("estoque:conta_pagar_baixar", args=[self.contas[2].pk]), {
            "valor_pago": "50,00", "juros_bancarios": "2,00", "valor_saida_banco": "52,00", "forma_pagamento": "Pix",
        })
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.conferir(self.consultar(), 3, "585", "210.50", "2", "374.50")

    def test_legado_nao_inventa_pagamento(self):
        conta = self.contas[2]
        conta.status = ContaPagar.STATUS_PAGA
        conta.valor_em_aberto = 0
        conta.save()
        resposta = self.consultar(situacao="paga")
        self.conferir(resposta, 1, "200", "0", "0", "0")
        self.assertContains(resposta, "pago não informado")

    def test_sem_resultados_omite_totais(self):
        resposta = self.consultar(q="inexistente")
        self.conferir(resposta, 0, "0", "0", "0", "0")
        self.assertNotContains(resposta, "data-resultados-totais")
        self.assertContains(resposta, "Nenhuma conta corresponde")


class TotaisContasPagarBrowserTests(DadosContasMixin, StaticLiveServerTestCase):
    def test_rodape_desktop_mobile_alinhamento_e_sem_rolagem(self):
        user = get_user_model().objects.create_superuser("totais-browser", "totais@example.test", "test-only")
        self.client.force_login(user)
        evidence = Path(__file__).resolve().parents[1] / "diagnostics" / "contas-pagar-rodape"
        evidence.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="contas-pagar-browser-") as profile:
            chrome = Chrome(r"C:\Program Files\Google\Chrome\Application\chrome.exe", profile).start()
            try:
                for width in [1440, 390, 320]:
                    tab = chrome.tab()
                    tab.call("Network.enable")
                    tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 760})
                    tab.call("Page.navigate", {"url": self.live_server_url + self.url + "?fornecedor=" + str(self.fornecedor.pk)})
                    tab.wait('document.querySelectorAll("[data-resultados-totais]").length === 2')
                    metrics = tab.evaluate('''(()=>{
                      const visible=e=>!!e.getClientRects().length;
                      const footer=[...document.querySelectorAll('[data-resultados-totais]')].find(visible);
                      const values=[...footer.querySelectorAll('[data-total]')].map(e=>e.textContent.replace(/\u00a0/g,' '));
                      const row=document.querySelector('.cp-table tbody tr .cp-values-grid');
                      return {values, footer:footer.getBoundingClientRect().top,
                        last:[...document.querySelectorAll('.cp-mobile-card[data-conta-pagar-id],.cp-table tbody tr[data-conta-pagar-id]')].filter(visible).at(-1).getBoundingClientRect().bottom,
                        overflow:document.documentElement.scrollWidth>innerWidth,
                        alinhado:!row || [...footer.querySelectorAll('[data-total]')].every((e,i)=>Math.abs(e.getBoundingClientRect().left-row.children[i].getBoundingClientRect().left)<2),
                        actions:[...document.querySelectorAll('.btnAbrirBaixaContaPagar')].filter(visible).length,
                        history:[...document.querySelectorAll('.cp-history-toggle')].filter(visible).length};
                    })()''')
                    self.assertEqual(metrics["values"], ["R$ 585,00", "R$ 160,50", "R$ 0,00", "R$ 424,50"])
                    self.assertGreaterEqual(metrics["footer"], metrics["last"] - 1)
                    self.assertFalse(metrics["overflow"], metrics)
                    self.assertEqual(metrics["actions"], 3)
                    self.assertEqual(metrics["history"], 2)
                    if width == 1440:
                        self.assertTrue(metrics["alinhado"], metrics)
                    tab.evaluate("[...document.querySelectorAll('[data-resultados-totais]')].find(e=>e.getClientRects().length).scrollIntoView({block:'center',behavior:'instant'});true")
                    tab.wait("(()=>{const r=[...document.querySelectorAll('[data-resultados-totais]')].find(e=>e.getClientRects().length).getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight})()")
                    shot = tab.call("Page.captureScreenshot", {"format": "png"})["data"]
                    (evidence / f"rodape-{width}.png").write_bytes(base64.b64decode(shot))
                    (evidence / f"layout-{width}.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            finally:
                chrome.stop()
