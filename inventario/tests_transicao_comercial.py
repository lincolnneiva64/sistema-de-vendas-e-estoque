"""Transição autorizada: identidade real no modal, Vendas permanece pública."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase
from django.urls import reverse

from estoque.models import MovimentacaoEstoqueManual
from . import tests_isolamento, tests_browser
from estoque.autorizacao_estoque import CHAVE


class TransicaoComercialTests(TestCase):
    setUp = tests_isolamento.IsolamentoTests.setUp

    def usuario(self, *permissoes):
        usuario = get_user_model().objects.create_user("autorizacao-modal", password="teste")
        usuario.user_permissions.add(*Permission.objects.filter(content_type__app_label="estoque", codename__in=permissoes))
        self.client.force_login(usuario)
        return usuario

    def test_login_sem_permissao_nao_autoriza_mesmo_com_nome_valido(self):
        self.usuario()
        for rota, metodo in [("conferencia_estoque_produto", "get"), ("conferencia_estoque_confirmar", "post"), ("conferencia_estoque_corrigir", "post")]:
            resposta = getattr(self.client, metodo)(reverse("estoque:" + rota, args=[self.produto.pk]),
                {"operador": self.funcionario.nome, "novo_estoque": "99"}, HTTP_X_OPERADOR_VENDA=self.funcionario.nome)
            self.assertEqual(resposta.status_code, 403)
            self.assertNotIn("produto", resposta.json())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_leitor_consulta_sem_poder_confirmar_ou_corrigir(self):
        self.usuario("view_produto")
        resposta = self.client.get(reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("no-store", resposta["Cache-Control"])
        self.assertEqual(resposta.json()["permissoes"], {"consultar": True, "confirmar": False, "corrigir": False})
        for rota in ("conferencia_estoque_confirmar", "conferencia_estoque_corrigir"):
            self.assertEqual(self.client.post(reverse("estoque:" + rota, args=[self.produto.pk]), {"novo_estoque": "99"}).status_code, 403)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_confirmacao_exige_permissao_de_historico_e_nao_da_poder_de_correcao(self):
        usuario = self.usuario("view_produto", "add_movimentacaoestoquemanual")
        resposta = self.client.post(reverse("estoque:conferencia_estoque_confirmar", args=[self.produto.pk]), HTTP_X_OPERADOR_VENDA="Nome forjado")
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.json()["permissoes"]["corrigir"])
        movimento = MovimentacaoEstoqueManual.objects.get()
        self.assertEqual((movimento.usuario_id, movimento.operador_nome), (usuario.pk, usuario.username))
        self.assertEqual(self.client.post(reverse("estoque:conferencia_estoque_corrigir", args=[self.produto.pk]), {"novo_estoque": "99"}).status_code, 403)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_correcao_exige_historico_alem_de_change_produto(self):
        self.usuario("view_produto", "change_produto")
        resposta = self.client.post(reverse("estoque:conferencia_estoque_corrigir", args=[self.produto.pk]), {"novo_estoque": "99"})
        self.assertEqual(resposta.status_code, 403)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_autorizacao_em_outra_aba_exige_senha_e_csrf_sem_escrever_estoque(self):
        self.usuario("view_produto", "change_produto", "add_movimentacaoestoquemanual")
        client = Client(enforce_csrf_checks=True)
        url = reverse("estoque:conferencia_estoque_autorizar")
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.post(url, {"username": "autorizacao-modal", "password": "teste"}).status_code, 403)
        resposta = client.post(url, {"username": "autorizacao-modal", "password": "errada", "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertEqual(resposta.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)
        resposta = client.post(url, {"username": "autorizacao-modal", "password": "teste", "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertRedirects(resposta, reverse("estoque:conferencia_estoque_autorizada"))
        self.assertNotIn("_auth_user_id", client.session)
        self.assertIn(CHAVE, client.session)
        self.assertFalse(client.get("/api/offline/session/").json()["authenticated"])
        self.assertEqual(client.get(reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk])).status_code, 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_autorizacao_curta_expira_e_respeita_revogacao_de_permissoes_e_senha(self):
        usuario = self.usuario("view_produto", "change_produto", "add_movimentacaoestoquemanual")
        client = Client()
        autorizar = reverse("estoque:conferencia_estoque_autorizar")
        consulta = reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk])
        client.post(autorizar, {"username": usuario.username, "password": "teste"})
        self.assertEqual(client.get(consulta).status_code, 200)
        sessao = client.session
        prova = sessao[CHAVE]
        prova["expira"] = 0
        sessao[CHAVE] = prova
        sessao.save()
        self.assertEqual(client.get(consulta).status_code, 403)
        client.post(autorizar, {"username": usuario.username, "password": "teste"})
        usuario.user_permissions.clear()
        self.assertEqual(client.get(consulta).status_code, 403)
        usuario.user_permissions.add(Permission.objects.get(content_type__app_label="estoque", codename="view_produto"))
        self.assertEqual(client.get(consulta).status_code, 200)
        usuario.set_password("senha-alterada")
        usuario.save()
        self.assertEqual(client.get(consulta).status_code, 403)

    def test_catalogo_publico_ainda_expoe_saldo_risco_nao_resolvido(self):
        resposta = Client().get("/vendas/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'data-estoque="10.000"')
        self.assertContains(resposta, 'data-custo=')

    def test_autorizacao_nao_troca_usuario_comercial_ja_autenticado(self):
        comercial = self.usuario()
        resposta = self.client.post(reverse("estoque:conferencia_estoque_autorizar"),
            {"username": self.gestor.username, "password": "teste"})
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), comercial.pk)
        self.assertEqual(self.client.session[CHAVE]["usuario"], self.gestor.pk)
        self.assertEqual(self.client.get("/api/offline/session/").json()["username"], comercial.username)


@skipUnless(Path(tests_browser.CHROME).is_file(), "Chrome indisponível")
class TransicaoComercialBrowserTests(StaticLiveServerTestCase):
    setUp = tests_isolamento.IsolamentoTests.setUp

    def test_modal_autoriza_sem_recarregar_vendas_ou_reenviar_correcao(self):
        self.gestor.set_password("teste")
        self.gestor.save()
        with TemporaryDirectory(prefix="inventario-chrome-transicao-") as profile:
            chrome = tests_browser.Chrome(tests_browser.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.inventarioErrosTeste=[];addEventListener('error',e=>inventarioErrosTeste.push(e.message));addEventListener('unhandledrejection',e=>inventarioErrosTeste.push(String(e.reason)))"})
                tab.call("Page.navigate", {"url": self.live_server_url + "/vendas/"})
                tab.wait("!!window.salesOffline && !!document.querySelector('#produto option[data-produto-id]')")
                tab.wait("document.readyState === 'complete'")
                tab.evaluate("document.querySelector('#operadorVenda').value='Operador fictício';document.querySelector('#operadorVenda').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#produtoBusca').value='Produto';document.querySelector('#produtoBusca').dispatchEvent(new Event('input',{bubbles:true}));true")
                tab.wait("!!document.querySelector('.produto-sugestao-item .estoque-item-sugestao')")
                tab.evaluate("document.querySelector('.produto-sugestao-item .estoque-item-sugestao').click();window.inventarioRascunhoTeste='preservado';document.querySelector('#produtoBusca').value='Rascunho fictício';true")
                try:
                    tab.wait("document.querySelector('#autorizacaoConferenciaEstoque').hidden === false && !document.querySelector('#consultarConferenciaNovamente').disabled")
                except AssertionError:
                    self.fail(str(tab.evaluate("({modal:document.querySelector('#modalConferenciaEstoque').innerText,hidden:document.querySelector('#autorizacaoConferenciaEstoque').hidden,retry:document.querySelector('#consultarConferenciaNovamente').outerHTML,close:document.querySelector('#btnCancelarConferenciaEstoque').outerHTML,active:window.salesOffline.active,offline:document.querySelector('#sales-offline-notice')?.textContent,draft:document.querySelector('#sales-draft-status')?.textContent,errors:window.inventarioErrosTeste})")))
                self.assertTrue(tab.evaluate("document.querySelector('#btnCorrigirConferenciaEstoque').disabled && document.querySelector('#btnConfirmarConferenciaEstoque').disabled"))
                self.assertEqual(tab.evaluate("document.querySelector('#modalConferenciaEstoqueAtual').textContent"), "--")
                self.assertEqual(tab.evaluate("document.querySelector('#autorizacaoConferenciaEstoque a').target"), "_blank")
                # Segunda aba real do Chrome, mesmo perfil, autentica sem tocar a venda.
                outra = chrome.tab()
                outra.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:conferencia_estoque_autorizar")})
                try:
                    outra.wait("!!document.querySelector('[name=username]')")
                    outra.evaluate("document.querySelector('[name=username]').value='gestor-isolamento';document.querySelector('[name=password]').value='teste';document.querySelector('form').requestSubmit();true")
                    outra.wait("document.body.textContent.includes('Identidade confirmada')")
                    tab.evaluate("document.querySelector('#consultarConferenciaNovamente').click();true")
                    tab.wait("!document.querySelector('#btnCorrigirConferenciaEstoque').disabled")
                    self.assertEqual(tab.evaluate("document.querySelector('#modalConferenciaEstoqueAtual').textContent"), "10.000")
                    self.assertEqual(tab.evaluate("document.querySelector('#produtoBusca').value"), "Rascunho fictício")
                    self.assertEqual(tab.evaluate("window.inventarioRascunhoTeste"), "preservado")
                    self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
                    self.assertFalse(tab.evaluate("(async()=>{return (await (await fetch('/api/offline/session/')).json()).authenticated})()"))
                    # Confirmação explícita autorizada, sem ajuste de quantidade.
                    tab.evaluate("document.querySelector('#btnConfirmarConferenciaEstoque').click();true")
                    tab.wait("document.querySelector('#modalConferenciaEstoqueFeedback').textContent.includes('Estoque confirmado')")
                    self.assertEqual(MovimentacaoEstoqueManual.objects.get().usuario_id, self.gestor.pk)
                    self.produto.refresh_from_db()
                    self.assertEqual(self.produto.quantidade, 10)
                finally:
                    outra.socket.close()
            finally:
                chrome.stop()
