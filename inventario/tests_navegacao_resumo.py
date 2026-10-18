"""Navegação administrativa e indicadores calculados sem qualquer escrita."""
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario
from . import services, views, tests_browser
from .models import Inventario, RodadaContagem
from .tests import produto


class NavegacaoResumoFixture:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-navegacao", "", "teste")
        self.funcionario = Funcionario.objects.create(nome="Contador navegação", pode_operar_sistema=True)
        produtos = [produto(nome=f"Produto resumo {i}") for i in range(5)]
        self.inventario = services.criar_inventario(self.usuario, "Resumo fictício", "manual", [p.pk for p in produtos])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inventario).order_by("pk"))
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [r.pk for r in self.rodadas])
        # Aguardando OK, correto, falta, sobra e recontagem: 5 itens atuais.
        for rodada, contagem in zip(self.rodadas[1:], ("10", "8", "12", "7")):
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, contagem, "0", True)
        self.recontagem = services.recontar(self.usuario, self.rodadas[-1].pk, "Recontagem fictícia")


class NavegacaoResumoTests(NavegacaoResumoFixture, TestCase):
    def test_indicadores_usam_ultima_rodada_sem_duplicar_historico(self):
        with self.assertNumQueries(1):
            self.assertEqual(views.resumo_inicial(), {"conferencias": 1, "aguardando_ok": 2, "divergencias": 2, "recontagens": 1})
        # O mesmo produto em outra conferência é um item distinto.
        services.criar_inventario(self.usuario, "Outro inventário", "manual", [self.rodadas[0].item.produto_id])
        self.assertEqual(views.resumo_inicial(), {"conferencias": 2, "aguardando_ok": 3, "divergencias": 2, "recontagens": 1})

    def test_indicadores_atualizam_apos_ok_recontagem_e_revisao(self):
        acesso, _ = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.recontagem.pk])
        services.salvar_contagem(acesso.pk, self.recontagem.pk, 0, "10", "0", True)
        services.revisar(self.usuario, self.recontagem.pk, "Recontagem verificada")
        self.assertEqual(views.resumo_inicial(), {"conferencias": 1, "aguardando_ok": 1, "divergencias": 2, "recontagens": 0})
        # Não há encerramento geral por revisão. Rodada encerrada não é pendência.
        from django.utils import timezone
        self.rodadas[0].encerrada_em = timezone.now()
        self.rodadas[0].save(update_fields=["encerrada_em"])
        self.assertEqual(views.resumo_inicial()["aguardando_ok"], 0)

    def test_resumo_vazio_tem_zeros(self):
        # Todos os itens foram encerrados no cenário de teste, sem ajustar saldo.
        from django.utils import timezone
        RodadaContagem.objects.update(encerrada_em=timezone.now())
        self.assertEqual(views.resumo_inicial(), {"conferencias": 0, "aguardando_ok": 0, "divergencias": 0, "recontagens": 0})

    def test_get_lista_e_indicadores_nao_escrevem(self):
        self.client.force_login(self.usuario)
        with CaptureQueriesContext(connection) as consultas:
            for _ in range(2):
                resposta = self.client.get(reverse("inventario:lista"))
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.context["resumo"]["divergencias"], 2)
        escritas = [q["sql"] for q in consultas if re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", q["sql"], re.I)]
        self.assertEqual(escritas, [])

    def test_botoes_e_rotas_respeitam_permissoes_reais(self):
        lista = reverse("inventario:lista")
        for permissao in (None, "gerenciar_inventario", "revisar_inventario", "aprovar_ajuste"):
            usuario = get_user_model().objects.create_user("navegacao-" + str(permissao))
            if permissao:
                usuario.user_permissions.add(Permission.objects.get(content_type__app_label="inventario", codename=permissao))
            client = Client()
            client.force_login(usuario)
            for url, botao in [(reverse("estoque:home"), "btnConferenciaEstoqueProdutos"), (reverse("estoque:vendas"), "btnConferenciaEstoqueVendas")]:
                resposta = client.get(url)
                self.assertEqual(resposta.status_code, 200)
                if permissao or botao == "btnConferenciaEstoqueVendas":
                    self.assertContains(resposta, 'id="' + botao + '"')
                    self.assertContains(resposta, 'href="' + lista + '"')
                else:
                    self.assertNotContains(resposta, 'id="' + botao + '"')
            self.assertEqual(client.get(lista).status_code, 200 if permissao else 403)
        anonimo = Client()
        self.assertEqual(anonimo.get(lista, {"operador": self.funcionario.nome}).status_code, 302)
        for url, botao in [(reverse("estoque:home"), "btnConferenciaEstoqueProdutos"), (reverse("estoque:vendas"), "btnConferenciaEstoqueVendas")]:
            if botao == "btnConferenciaEstoqueVendas":
                self.assertContains(anonimo.get(url), 'id="' + botao + '"')
            else:
                self.assertNotContains(anonimo.get(url), 'id="' + botao + '"')

    def test_link_limitado_nao_acessa_resumo_detalhes_relatorio_ou_acoes(self):
        self.client.post(reverse("inventario:convite", args=[self.token]))
        for url in (reverse("inventario:lista"), reverse("inventario:detalhe", args=[self.inventario.pk]), reverse("inventario:relatorio", args=[self.inventario.pk])):
            esperado = 302 if url == reverse("inventario:lista") else 403
            self.assertEqual(self.client.get(url).status_code, esperado)
            esperado_post = 405 if url == reverse("inventario:relatorio", args=[self.inventario.pk]) else esperado
            self.assertEqual(self.client.post(url, {"acao": "recontar", "rodada": self.rodadas[0].pk}).status_code, esperado_post)
        resposta = self.client.get(reverse("inventario:mobile"))
        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "data-indicador")
        self.assertNotContains(resposta, "btnConferenciaEstoque")

    def test_autorizacao_curta_de_estoque_nao_libera_administracao(self):
        self.assertEqual(self.client.post(reverse("estoque:conferencia_estoque_autorizar"),
            {"username": self.usuario.username, "password": "teste"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("inventario:lista")).status_code, 302)
        self.assertNotContains(self.client.get(reverse("estoque:home")), 'id="btnConferenciaEstoqueProdutos"')
        self.assertContains(self.client.get(reverse("estoque:vendas")), 'id="btnConferenciaEstoqueVendas"')

    def test_permissao_de_revisao_nao_permite_criar_conferencia(self):
        usuario = get_user_model().objects.create_user("somente-revisao")
        usuario.user_permissions.add(Permission.objects.get(content_type__app_label="inventario", codename="revisar_inventario"))
        self.client.force_login(usuario)
        resposta = self.client.post(reverse("inventario:lista"), {"criterio": "manual", "produtos": [self.rodadas[0].item.produto_id], "titulo": "Não deve criar"})
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(Inventario.objects.count(), 1)


@skipUnless(Path(tests_browser.CHROME).is_file(), "Chrome indisponível")
class NavegacaoResumoBrowserTests(NavegacaoResumoFixture, StaticLiveServerTestCase):
    def test_produtos_administracao_e_atalhos_abrem_mesma_pagina(self):
        client = Client()
        client.force_login(self.usuario)
        with TemporaryDirectory(prefix="inventario-chrome-navegacao-") as profile:
            chrome = tests_browser.Chrome(tests_browser.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": client.cookies["sessionid"].value, "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:home")})
                tab.wait("!!document.querySelector('#btnConferenciaEstoqueProdutos')")
                self.assertTrue(tab.evaluate("document.querySelector('#btnConferenciaEstoqueProdutos').closest('section').getAttribute('aria-labelledby') === 'produtos-nav-admin'"))
                self.assertTrue(tab.evaluate("document.querySelector('#btnConferenciaEstoqueProdutos').closest('section').textContent.includes('Ver Lixeira')"))
                destino = tab.evaluate("document.querySelector('#btnConferenciaEstoqueProdutos').getAttribute('href')")
                tab.evaluate("document.querySelector('#btnConferenciaEstoqueProdutos').click();true")
                tab.wait("!!document.querySelector('#resumo-conferencias')")
                self.assertEqual(tab.evaluate("Object.fromEntries([...document.querySelectorAll('[data-indicador]')].map(e=>[e.dataset.indicador,Number(e.textContent)]))"), {"conferencias": 1, "aguardando_ok": 2, "divergencias": 2, "recontagens": 1})
                tab.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                self.assertTrue(tab.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                tab.call("Emulation.clearDeviceMetricsOverride")
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:vendas")})
                tab.wait("document.readyState==='complete' && !!document.querySelector('#btnConferenciaEstoqueVendas')")
                self.assertEqual(tab.evaluate("document.querySelector('#btnConferenciaEstoqueVendas').getAttribute('href')"), destino)
                self.assertFalse(tab.evaluate("document.querySelector('#atalhosVenda').classList.contains('aberto')"))
                tab.evaluate("document.querySelector('#btnAtalhosVenda').click();true")
                tab.wait("document.querySelector('#btnAtalhosVenda').getAttribute('aria-expanded') === 'true'")
                self.assertTrue(tab.evaluate("document.querySelector('#btnConferenciaEstoqueVendas').getBoundingClientRect().width > 0"))
                tab.evaluate("document.querySelector('#btnConferenciaEstoqueVendas').click();true")
                tab.wait("!!document.querySelector('#resumo-conferencias')")
                self.assertEqual(tab.evaluate("location.pathname"), destino)
                tab.call("Network.clearBrowserCookies")
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:vendas")})
                tab.wait("document.readyState==='complete' && !!document.querySelector('#btnAtalhosVenda')")
                self.assertFalse(tab.evaluate("!!document.querySelector('#btnConferenciaEstoqueVendas')"))
                tab.call("Page.navigate", {"url": self.live_server_url + destino})
                tab.wait("document.readyState==='complete'")
                self.assertFalse(tab.evaluate("!!document.querySelector('#resumo-conferencias')"))
            finally:
                chrome.stop()
