"""Reserva com endereço opcional e retorno acessível dos erros, com dados fictícios."""
from pathlib import Path
import tempfile
from unittest import skipUnless

from django.test import TestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .forms import LocacaoForm
from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao, PagamentoLocacao
from . import tests as testes_existentes


class FormularioErrosTests(TestCase):
    dados_nova_locacao = testes_existentes.LocacoesUxPadraoTests.dados_nova_locacao

    def setUp(self):
        config = ConfiguracaoLocacao.obter()
        config.total_mesas = 10
        config.total_cadeiras = 40
        config.save()
        self.faixa, _ = FaixaPrecoLocacao.objects.get_or_create(
            codigo=FaixaPrecoLocacao.CENTRO_PERTO,
            defaults={"nome": "Faixa fictícia", "preco_jogo_diaria": 8},
        )

    def salvar_endereco(self, endereco, esperado):
        resposta = self.client.post(reverse("locacoes:nova"), self.dados_nova_locacao(endereco_entrega=endereco))
        self.assertEqual(resposta.status_code, 302)
        locacao = Locacao.objects.get()
        self.assertEqual(locacao.endereco_entrega, esperado)
        self.assertEqual(PagamentoLocacao.objects.count(), 0)

    def test_endereco_vazio(self):
        self.salvar_endereco("", "")

    def test_endereco_so_espacos(self):
        self.salvar_endereco("   \n  ", "")

    def test_endereco_normalizado(self):
        self.salvar_endereco("  rua fictícia,   10 ", "Rua Fictícia, 10")

    def resposta_invalida(self, **campos):
        dados = self.dados_nova_locacao(endereco_entrega="", horario_entrega="", observacao="Não perder esta observação", **campos)
        resposta = self.client.post(reverse("locacoes:nova"), dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(Locacao.objects.exists())
        self.assertContains(resposta, "LOCAÇÃO NÃO GRAVADA — corrija os campos indicados.")
        self.assertContains(resposta, 'href="#id_horario_entrega"')
        self.assertContains(resposta, 'aria-invalid="true"')
        self.assertEqual(resposta.context["locacao_form"].data.dict(), dados)
        self.assertNotIn("endereco_entrega", resposta.context["locacao_form"].errors)
        return resposta

    def test_obrigatorio_ausente_preserva_dados(self):
        self.resposta_invalida()

    def test_erros_dos_dois_formularios(self):
        resposta = self.resposta_invalida(jogos="-1")
        self.assertContains(resposta, 'href="#id_jogos"')
        self.assertIn("jogos", resposta.context["itens_form"].errors)

    def test_endereco_continua_opcional(self):
        self.assertFalse(LocacaoForm().fields["endereco_entrega"].required)

    def test_sinal_sem_forma_de_pagamento_continua_bloqueado(self):
        dados = self.dados_nova_locacao(endereco_entrega="", sinal_valor="5.00", sinal_forma_pagamento="")
        resposta = self.client.post(reverse("locacoes:nova"), dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("sinal_forma_pagamento", resposta.context["locacao_form"].errors)
        self.assertContains(resposta, 'href="#id_sinal_forma_pagamento"')
        self.assertFalse(Locacao.objects.exists())
        self.assertFalse(PagamentoLocacao.objects.exists())
        self.assertEqual(resposta.context["locacao_form"]["sinal_valor"].value(), "5.00")

    def test_material_indisponivel_continua_bloqueado(self):
        dados = self.dados_nova_locacao(endereco_entrega="", jogos="100")
        resposta = self.client.post(reverse("locacoes:nova"), dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAÇÃO NÃO GRAVADA — corrija os campos indicados.")
        self.assertFalse(Locacao.objects.exists())
        self.assertFalse(PagamentoLocacao.objects.exists())

    def test_edicao_permite_remover_endereco(self):
        self.salvar_endereco("Rua fictícia", "Rua Fictícia")
        locacao = Locacao.objects.get()
        resposta = self.client.post(reverse("locacoes:editar", args=[locacao.pk]), self.dados_nova_locacao(endereco_entrega="   "))
        self.assertEqual(resposta.status_code, 302)
        locacao.refresh_from_db()
        self.assertEqual(locacao.endereco_entrega, "")


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@skipUnless(CHROME.exists(), "Chrome necessário")
class FormularioErrosBrowserTests(TestCase):
    setUp = FormularioErrosTests.setUp
    dados_nova_locacao = FormularioErrosTests.dados_nova_locacao
    resposta_invalida = FormularioErrosTests.resposta_invalida

    def test_foco_e_dados_desktop_mobile(self):
        html = self.resposta_invalida(sinal_valor="5.00", sinal_forma_pagamento="dinheiro").content.decode()
        # HTML real do POST; sem rede, dados reais ou consultas dinâmicas externas.
        import re
        html = re.sub(r'<link[^>]+href="https://[^>]+>', '', html)
        with tempfile.TemporaryDirectory(prefix="locacao-erros-", ignore_cleanup_errors=True) as perfil:
            chrome = Chrome(str(CHROME), perfil)
            try:
                chrome.start()
                tab = chrome.tab()
            except (OSError, RuntimeError, EOFError) as erro:
                if hasattr(chrome, "browser"):
                    chrome.stop()
                self.skipTest("Chrome indisponível: " + str(erro))
            try:
                for largura in (1366, 390, 320):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 900, "deviceScaleFactor": 1, "mobile": largura < 600})
                    tab.call("Page.setDocumentContent", {"frameId": tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"], "html": html})
                    tab.wait('document.activeElement.id === "id_horario_entrega"')
                    self.assertTrue(tab.evaluate('(()=>{const r=document.activeElement.getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight})()'))
                    self.assertEqual(tab.evaluate('document.getElementById("id_observacao").value'), "Não perder esta observação")
                    self.assertEqual(tab.evaluate('document.getElementById("id_endereco_entrega").value'), "")
                    self.assertEqual(tab.evaluate('document.getElementById("id_sinal_valor").value'), "5.00")
                    self.assertEqual(tab.evaluate('document.getElementById("id_sinal_forma_pagamento").value'), "dinheiro")
                    tab.evaluate('document.getElementById("id_jogos").focus();document.querySelector("#erros-reserva a").click();true')
                    self.assertEqual(tab.evaluate('document.activeElement.id'), "id_horario_entrega")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    # A validação nativa também deve exibir o aviso e indicar o campo.
                    tab.evaluate('document.getElementById("id_data_evento").value="";document.getElementById("id_data_evento").reportValidity();true')
                    self.assertTrue(tab.evaluate('Array.from(document.querySelectorAll("#erros-reserva a")).some(a => a.hash === "#id_data_evento")'))
            finally:
                chrome.stop()
