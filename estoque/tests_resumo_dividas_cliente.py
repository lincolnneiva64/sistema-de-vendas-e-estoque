from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente, ContaReceber, Venda
from estoque.services.resumo_dividas_cliente import resumir_dividas_cliente


class ResumoDividasClienteTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nome="Cliente de teste")
        self.user = get_user_model().objects.create_superuser("resumo", "", "teste")
        self.client.force_login(self.user)

    def conta(self, original="420.50", saldo="400.90", vencimento=date(2026, 10, 30), status="parcial"):
        return ContaReceber.objects.create(
            cliente=self.cliente, data_emissao=date(2026, 10, 1),
            data_vencimento=vencimento, valor_original=Decimal(original),
            valor_em_aberto=Decimal(saldo), status=status,
        )

    @patch("django.utils.timezone.now", return_value=datetime(2026, 11, 28, 15, tzinfo=dt_timezone.utc))
    def test_referencia_parcial_e_sem_consultas_adicionais(self, _):
        conta = self.conta()
        with self.assertNumQueries(0):
            resumo = resumir_dividas_cliente([conta])
        self.assertEqual((resumo["original"], resumo["pago"], resumo["saldo"]),
                         (Decimal("420.50"), Decimal("19.60"), Decimal("400.90")))
        self.assertEqual((resumo["vencidas"], resumo["maior_atraso"]), (1, 29))

    @patch("django.utils.timezone.now", return_value=datetime(2026, 11, 28, 15, tzinfo=dt_timezone.utc))
    def test_varias_contas_e_exclusao_de_quitadas(self, _):
        parcial = self.conta()
        antiga = self.conta("100", "50", date(2026, 10, 1))
        futura = self.conta("80", "80", date(2026, 12, 1), "aberta")
        hoje = self.conta("20", "20", date(2026, 11, 28), "aberta")
        sem_data = self.conta("10", "10", None, "aberta")
        paga = self.conta("900", "0", status="paga")
        cancelada = self.conta("30", "30", status="cancelada")
        resumo = resumir_dividas_cliente([futura, paga, parcial, sem_data, antiga, cancelada, hoje])
        self.assertEqual([i["id"] for i in resumo["contas"]], [antiga.id, parcial.id, hoje.id, futura.id, sem_data.id])
        self.assertEqual((resumo["original"], resumo["pago"], resumo["saldo"]),
                         (Decimal("630.50"), Decimal("69.60"), Decimal("560.90")))
        self.assertEqual((resumo["vencidas"], resumo["maior_atraso"]), (2, 58))

    @patch("django.utils.timezone.now", return_value=datetime(2026, 11, 29, 1, tzinfo=dt_timezone.utc))
    def test_virada_utc_utiliza_belem_mesmo_com_outro_fuso_ativo(self, _):
        conta = self.conta(vencimento=date(2026, 11, 28))
        with timezone.override("Asia/Tokyo"):
            resumo = resumir_dividas_cliente([conta])
        self.assertEqual(resumo["vencidas"], 0)
        self.assertEqual(resumo["maior_atraso"], 0)

    def test_sem_dividas(self):
        resumo = resumir_dividas_cliente([self.conta(saldo="0", status="paga")])
        self.assertEqual(resumo["contas"], [])
        self.assertEqual(resumo["saldo"], 0)
        self.assertEqual(resumo["vencidas"], 0)
        self.assertEqual(resumo["maior_atraso"], 0)

    def test_view_completa_e_troca_assincrona_sem_alterar_saldo(self):
        conta = self.conta()
        for headers in ({}, {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}):
            response = self.client.get(reverse("estoque:receber_cliente", args=[self.cliente.pk]), **headers)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Detalhar dívidas")
            self.assertEqual(response.context["resumo_dividas"]["pago"], Decimal("19.60"))
            self.assertNotContains(response, '<details class="rc-consulta-dividas" open')
        conta.refresh_from_db()
        self.assertEqual(conta.valor_em_aberto, Decimal("400.90"))

    def test_view_sem_dividas(self):
        self.conta(saldo="0", status="paga")
        response = self.client.get(reverse("estoque:receber_cliente", args=[self.cliente.pk]))
        self.assertContains(response, "Nenhuma conta de venda em aberto.")
        self.assertEqual(response.context["resumo_dividas"]["original"], 0)
        self.assertEqual(response.context["resumo_dividas"]["pago"], 0)

    def test_muitas_contas_sem_multiplicar_consultas(self):
        for _ in range(40):
            self.conta()
        contas = list(ContaReceber.objects.filter(cliente=self.cliente))
        with self.assertNumQueries(0):
            resumo = resumir_dividas_cliente(contas)
        self.assertEqual(len(resumo["contas"]), 40)
        self.assertEqual(resumo["pago"], Decimal("784.00"))

    def test_data_compra_vem_da_venda_e_nao_da_emissao_da_conta(self):
        conta = self.conta()
        venda = Venda.objects.create(cliente=self.cliente, data_venda=date(2026, 9, 23))
        conta.venda = venda
        conta.save(update_fields=["venda"])
        for headers in ({}, {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}):
            response = self.client.get(reverse("estoque:receber_cliente", args=[self.cliente.pk]), **headers)
            item = response.context["resumo_dividas"]["contas"][0]
            self.assertEqual(item["data_compra"], venda.data_venda)
            self.assertNotEqual(item["data_compra"], conta.data_emissao)
            self.assertContains(response, '<strong>Compra:</strong> 23/09/2026')
            self.assertContains(response, '<strong>Vencimento:</strong> 30/10/2026')

    def test_conta_sem_venda_nao_inventa_data_compra(self):
        self.conta()
        response = self.client.get(reverse("estoque:receber_cliente", args=[self.cliente.pk]))
        self.assertIsNone(response.context["resumo_dividas"]["contas"][0]["data_compra"])
        self.assertContains(response, '<strong>Compra:</strong> Não informada')
