from decimal import Decimal
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from offline.browser_support import Chrome

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import views
from .models import ContaPagar, Fornecedor, MovimentoFinanceiro, PagamentoContaPagar


class EncargosAutomaticosContaPagarTests(TestCase):
    def setUp(self):
        self.conta = ContaPagar.objects.create(
            fornecedor=Fornecedor.objects.create(nome="Garcia Distribuidora"),
            data_emissao=timezone.localdate(),
            data_vencimento=timezone.localdate(),
            valor_original=Decimal("700.00"),
            valor_em_aberto=Decimal("617.00"),
        )
        self.banco = views._conta_financeira_saida_pagar_fornecedor("banco")
        self.banco.saldo_inicial = Decimal("1000.00")
        self.banco.save()

    def baixar(self, total=None, principal="627,00", juros="0,00", saida="627,00"):
        dados = {
            "valor_pago": principal, "juros_bancarios": juros,
            "valor_saida_banco": saida, "forma_pagamento": "Pix",
        }
        if total is not None:
            dados["total_efetivamente_pago"] = total
        return self.client.post(
            reverse("estoque:conta_pagar_baixar", args=[self.conta.pk]),
            dados, secure=True,
        )

    def conferir(self, principal, encargos, saldo):
        self.conta.refresh_from_db()
        pagamento = PagamentoContaPagar.objects.get(conta=self.conta)
        movimento = MovimentoFinanceiro.objects.get(pagamento_conta_pagar=pagamento)
        self.assertEqual(pagamento.valor, Decimal(principal))
        self.assertEqual(pagamento.juros_bancarios, Decimal(encargos))
        self.assertEqual(self.conta.valor_em_aberto, Decimal(saldo))
        self.assertEqual(self.conta.valor_original, Decimal("700.00"))
        self.assertEqual(movimento.valor, pagamento.valor + pagamento.juros_bancarios)
        self.assertEqual(views._saldo_conta_financeira(self.banco), Decimal("1000.00") - movimento.valor)

    def test_pagamento_exato_recalcula_campos_incorretos(self):
        resposta = self.baixar("617,00", juros="99,00", saida="617,00")
        self.assertEqual(resposta.status_code, 200)
        self.conferir("617.00", "0.00", "0.00")

    def test_total_maior_calcula_encargos_com_principal_incorreto(self):
        resposta = self.baixar("627,00")
        self.assertEqual(resposta.status_code, 200)
        self.conferir("617.00", "10.00", "0.00")

    def test_total_parcial_nao_cria_encargos_negativos(self):
        resposta = self.baixar("400,00", juros="10,00", saida="400,00")
        self.assertEqual(resposta.status_code, 200)
        self.conferir("400.00", "0.00", "217.00")

    def test_principal_acima_do_saldo_sem_total_e_rejeitado(self):
        resposta = self.baixar()
        self.assertEqual(resposta.status_code, 400)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_em_aberto, Decimal("617.00"))
        self.assertFalse(PagamentoContaPagar.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_distribuicao_deve_incluir_encargos(self):
        resposta = self.baixar("627,00", saida="617,00")
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(PagamentoContaPagar.objects.exists())

    def test_parcial_com_encargos_explicitos_preserva_regra_existente(self):
        resposta = self.baixar(principal="400,00", juros="10,00", saida="410,00")
        self.assertEqual(resposta.status_code, 200)
        self.conferir("400.00", "10.00", "217.00")

    @skipUnless(Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").is_file(), "Chrome indisponivel")
    def test_modal_calcula_total_e_envia_separacao(self):
        resposta = self.client.get(reverse("estoque:contas_pagar"), secure=True)
        self.assertEqual(resposta.status_code, 200)
        html = resposta.content.decode()
        script = next(s for s in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
                      if 'const valorPago = ' in s)
        with TemporaryDirectory(prefix="encargos-modal-") as profile:
            chrome = Chrome(r"C:\Program Files\Google\Chrome\Application\chrome.exe", profile).start()
            try:
                tab = chrome.tab()
                for nome, conteudo in [("htmlTeste", html), ("scriptTeste", script)]:
                    tab.evaluate(f"window.{nome} = ''")
                    for inicio in range(0, len(conteudo), 8000):
                        tab.evaluate(f"window.{nome} += " + json.dumps(conteudo[inicio:inicio + 8000]))
                tab.evaluate("document.body.innerHTML = window.htmlTeste;true")
                tab.evaluate("eval(window.scriptTeste)")
                tab.evaluate("window.setTimeout=()=>0;window.fetch=async(url,options)=>{window.dadosBaixa=Object.fromEntries(options.body);return {ok:true,json:async()=>({ok:true})}}")
                for total, principal, encargos in [
                    ("627,00", "617,00", "10,00"),
                    ("617,00", "617,00", "0,00"),
                    ("400,00", "400,00", "0,00"),
                ]:
                    with self.subTest(total=total):
                        tab.evaluate("document.querySelector('.btnAbrirBaixaContaPagar').click()")
                        tab.evaluate("document.getElementById('totalEfetivoContaPagar').value=" + json.dumps(total) + ";document.getElementById('totalEfetivoContaPagar').dispatchEvent(new Event('input',{bubbles:true}))")
                        valores = tab.evaluate("['valorPagoContaPagar','jurosBancariosContaPagar','totalEfetivoContaPagar','valorSaidaBancoContaPagar'].map(id=>document.getElementById(id).value)")
                        self.assertEqual(valores, [principal, encargos, total, total])
                        tab.evaluate("document.getElementById('totalEfetivoContaPagar').dispatchEvent(new Event('blur'));document.getElementById('formBaixaContaPagar').dispatchEvent(new Event('submit',{cancelable:true}))")
                        dados = tab.evaluate("window.dadosBaixa")
                        self.assertEqual(dados["valor_pago"], principal)
                        self.assertEqual(dados["juros_bancarios"], encargos)
                        self.assertEqual(dados["total_efetivamente_pago"], total)
            finally:
                chrome.stop()
