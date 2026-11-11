import os
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.http import HttpResponse
from django.template.loader import get_template
from django.test import override_settings
from django.urls import include, path, reverse
from django.utils import timezone
from offline.browser_support import Chrome

from .models import Compra, ItemCompra, Produto
from .tests_precos_seletivos import SeletivosFixture


def revisao_navigation_test_page(request):
    return HttpResponse(
        get_template("estoque/includes/revisao_precos_posterior.html").render(
            {"revisao_precos_pendentes_qtd": 2, "revisao_precos_compras_qtd": 1},
            request=request,
        )
    )


urlpatterns = [
    path("revisao-teste/", revisao_navigation_test_page),
    path("", include("sistema.urls")),
]


CHROME = os.environ.get(
    "OFFLINE_TEST_CHROME",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


@skipUnless(Path(CHROME).is_file(), "Chrome unavailable")
@override_settings(ROOT_URLCONF=__name__)
class RevisaoPrecosPosteriorNavegacaoBrowserTests(StaticLiveServerTestCase):
    host = "127.0.0.1"

    def setUp(self):
        from .tests_precos_vinculados import criar_operador_precos

        self.operador = criar_operador_precos("posterior-navigation-test")
        self.operador.is_staff = True
        self.operador.is_superuser = True
        self.operador.save(update_fields=("is_staff", "is_superuser"))
        self.client.force_login(self.operador)
        self.compra = Compra.objects.create(
            data_compra=timezone.localdate(),
            status=Compra.STATUS_FINALIZADA,
            revisao_precos_pendente=True,
            estoque_entrada_realizada=True,
        )
        self.produtos = []
        self.itens = []
        for indice in range(2):
            produto = Produto.objects.create(
                nome=f"Produto Navegacao {indice + 1}",
                preco_compra=Decimal("9.00"),
                preco_venda=Decimal("12.00"),
                preco_vista=Decimal("12.00"),
                preco_prazo=Decimal("14.00"),
                quantidade=Decimal("10.000"),
            )
            item = ItemCompra.objects.create(
                compra=self.compra,
                produto=produto,
                quantidade=1,
                unidade="UN",
                preco_unitario=Decimal("9.00"),
                preco_compra_anterior=Decimal("8.00"),
                valor_total=Decimal("9.00"),
            )
            self.produtos.append(produto)
            self.itens.append(item)

    def navegador(self):
        profile = TemporaryDirectory(prefix="revisao-precos-nav-browser-")
        self.addCleanup(profile.cleanup)
        chrome = Chrome(CHROME, profile.name).start()
        self.addCleanup(chrome.stop)
        tab = chrome.tab()
        tab.call("Network.enable")
        tab.call("Network.setCookie", {
            "name": "sessionid",
            "value": self.client.cookies["sessionid"].value,
            "url": self.live_server_url,
        })
        tab.call("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
              window.__revisaoPrecosBrowserErrors = [];
              addEventListener("unhandledrejection", event => window.__revisaoPrecosBrowserErrors.push(String(event.reason)));
              const originalFetch = window.fetch.bind(window);
              const traceKey = "revisao-precos-nav-trace";
              window.fetch = async (input, init = {}) => {
                const url = new URL(typeof input === "string" ? input : input.url, location.href);
                const method = (init.method || "GET").toUpperCase();
                const trace = JSON.parse(sessionStorage.getItem(traceKey) || "[]");
                trace.push({method, url:url.href});
                sessionStorage.setItem(traceKey, JSON.stringify(trace));
                if (method === "POST" && sessionStorage.getItem("revisao-nav-drop-before-send") === "1") {
                  sessionStorage.setItem("revisao-nav-drop-before-send", "0");
                  throw new TypeError("Resposta do POST perdida antes do envio");
                }
                const response = await originalFetch(input, init);
                if (method === "POST") {
                  trace[trace.length - 1].status = response.status;
                  sessionStorage.setItem(traceKey, JSON.stringify(trace));
                }
                if (method === "POST" && sessionStorage.getItem("revisao-nav-lose-post-response") === "1") {
                  sessionStorage.setItem("revisao-nav-lose-post-response", "0");
                  throw new TypeError("Resposta do POST perdida depois da gravação");
                }
                if (method === "GET" && url.pathname.endsWith("/compras/revisao-precos/pendentes/") &&
                    !url.searchParams.has("operacao_id") &&
                    sessionStorage.getItem("revisao-nav-fail-next-queue-get") === "1") {
                  sessionStorage.setItem("revisao-nav-fail-next-queue-get", "0");
                  throw new TypeError("Falha simulada ao atualizar a fila");
                }
                return response;
              };
            """,
        })
        tab.call("Page.navigate", {"url": self.live_server_url + "/revisao-teste/"})
        tab.wait("document.readyState === 'complete' && !!document.getElementById('rpAgora')")
        return chrome, tab

    @staticmethod
    def trace(tab):
        return tab.evaluate(
            "JSON.parse(sessionStorage.getItem('revisao-precos-nav-trace') || '[]')"
        )

    def iniciar_revisao(self, tab):
        tab.wait("!!document.querySelector('#revisaoPrecosPosterior.visivel')")
        tab.evaluate("document.getElementById('rpAgora').click(); true")
        tab.wait("!document.getElementById('rpSalvar').hidden")
        tab.wait("document.querySelector('.rp-compras-preco-progresso')?.textContent.includes('Produto 1 de 2')")

    def aguardar_produto_seguinte(self, tab):
        try:
            tab.wait("document.querySelector('.rp-compras-preco-produto')?.textContent.toLowerCase().includes('produto navegacao 2')")
        except AssertionError:
            print("REVIEW_SAVE_DIAGNOSTIC", tab.evaluate(
                "JSON.stringify({error:document.getElementById('rpErro')?.textContent,"
                "progress:document.querySelector('.rp-compras-preco-progresso')?.textContent,"
                "product:document.querySelector('.rp-compras-preco-produto')?.textContent,"
                "trace:sessionStorage.getItem('revisao-precos-nav-trace'),"
                "modal:document.getElementById('revisaoPrecosPosterior')?.className})"
            ), flush=True)
            raise

    def test_post_confirmado_get_seguinte_falha_e_retry_nao_repete_post(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("""
          sessionStorage.setItem('revisao-nav-fail-next-queue-get', '1');
          document.getElementById('rpManter').click();
          true
        """)
        tab.wait("!document.getElementById('rpAtualizar').hidden")
        self.itens[0].refresh_from_db()
        self.assertTrue(self.itens[0].revisao_preco_concluida)
        self.assertIn("foi salva", tab.evaluate("document.getElementById('rpErro').textContent"))
        self.assertNotIn("Não foi possível salvar", tab.evaluate("document.getElementById('rpErro').textContent"))
        posts = [row for row in self.trace(tab) if row["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], 200)
        antes_atualizar = len(self.trace(tab))
        tab.evaluate("document.getElementById('rpAtualizar').click(); true")
        tab.wait("document.querySelector('.rp-compras-preco-progresso')?.textContent.includes('Produto 2 de 2')")
        self.assertIn(self.produtos[1].nome, tab.evaluate("document.querySelector('.rp-compras-preco-produto').textContent"))
        novas_consultas = self.trace(tab)[antes_atualizar:]
        self.assertEqual([row["method"] for row in novas_consultas], ["GET"])
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)

    def test_post_confirmado_avanca_normalmente(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("document.getElementById('rpManter').click(); true")
        self.aguardar_produto_seguinte(tab)
        self.itens[0].refresh_from_db()
        self.assertTrue(self.itens[0].revisao_preco_concluida)
        posts = [row for row in self.trace(tab) if row["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], 200)

    def test_voltar_ao_concluido_e_avancar_nao_reenvia_nem_trava(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("document.getElementById('rpManter').click(); true")
        self.aguardar_produto_seguinte(tab)
        tab.wait("!document.getElementById('rpDepois').disabled")
        tab.wait("!document.getElementById('rpVoltar').disabled")
        tab.evaluate("document.getElementById('rpVoltar').click(); true")
        tab.wait("document.getElementById('rpSalvar').textContent === 'Próximo'")
        self.assertTrue(tab.evaluate("Array.from(document.querySelectorAll('#rpLista input')).every(el=>el.disabled)"))
        tab.evaluate("document.getElementById('rpSalvar').click(); true")
        self.aguardar_produto_seguinte(tab)
        self.assertEqual(len([r for r in self.trace(tab) if r['method']=='POST']),1)
        tab.wait("!document.getElementById('rpDepois').disabled")
        tab.evaluate("document.getElementById('rpDepois').click(); true")
        tab.wait("!document.querySelector('#revisaoPrecosPosterior.visivel')")
        self.itens[1].refresh_from_db()
        self.assertFalse(self.itens[1].revisao_preco_concluida)

    def test_voltar_e_avancar_com_produto_vinculado(self):
        from .models import GrupoProdutoVinculado, MembroGrupoProduto
        grupo=GrupoProdutoVinculado.objects.create(nome='Grupo de teste de navegacao')
        MembroGrupoProduto.objects.create(grupo=grupo,produto=self.produtos[0])
        self.test_voltar_ao_concluido_e_avancar_nao_reenvia_nem_trava()

    def test_adiar_apos_recusa_de_manter_preco_invalido_preserva_pendencia(self):
        Produto.objects.filter(pk=self.produtos[0].pk).update(preco_compra=Decimal('20'))
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("document.getElementById('rpManter').click(); true")
        tab.wait("!document.getElementById('rpErro').hidden && !document.getElementById('rpDepois').disabled")
        self.assertEqual([r['status'] for r in self.trace(tab) if r['method']=='POST'],[400])
        tab.evaluate("document.getElementById('rpDepois').click(); true")
        tab.wait("!document.querySelector('#revisaoPrecosPosterior.visivel')")
        self.compra.refresh_from_db();self.itens[0].refresh_from_db()
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertFalse(self.itens[0].revisao_preco_concluida)
        self.assertEqual(len([r for r in self.trace(tab) if r['method']=='POST']),1)

    def test_fila_esvaziada_antes_do_get_fecha_sem_erro_de_interface(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        ItemCompra.objects.filter(pk=self.itens[1].pk).update(revisao_preco_concluida=True)
        tab.evaluate("document.getElementById('rpManter').click(); true")
        tab.wait("!document.getElementById('revisaoPrecosPosterior').classList.contains('visivel')")
        tab.evaluate("new Promise(resolve => setTimeout(resolve, 50))")
        self.itens[0].refresh_from_db()
        self.assertTrue(self.itens[0].revisao_preco_concluida)
        self.assertEqual(tab.evaluate("window.__revisaoPrecosBrowserErrors.length"), 0)

    def test_resposta_post_perdida_exige_get_antes_de_novo_envio(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("""
          sessionStorage.setItem('revisao-nav-lose-post-response', '1');
          document.getElementById('rpManter').click();
          true
        """)
        tab.wait("!document.getElementById('rpVerificar').hidden")
        self.itens[0].refresh_from_db()
        self.assertTrue(self.itens[0].revisao_preco_concluida)
        posts = [row for row in self.trace(tab) if row["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], 200)

        tab.evaluate("""
          sessionStorage.setItem('revisao-nav-fail-next-queue-get', '1');
          document.getElementById('rpVerificar').click();
          true
        """)
        tab.wait("!document.getElementById('rpVerificar').disabled")
        self.assertIn("estado no servidor", tab.evaluate("document.getElementById('rpErro').textContent"))
        self.assertFalse(tab.evaluate("document.getElementById('rpVerificar').hidden"))
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)

        tab.evaluate("document.getElementById('rpVerificar').click(); true")
        self.aguardar_produto_seguinte(tab)
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)
        self.assertTrue(any(
            row["method"] == "GET" and row["url"].endswith("/compras/revisao-precos/pendentes/")
            for row in self.trace(tab)
        ))

    def test_reabertura_apos_perda_preserva_estado_e_nao_reenvia(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        tab.evaluate("""
          sessionStorage.setItem('revisao-nav-drop-before-send', '1');
          document.getElementById('rpManter').click();
          true
        """)
        tab.wait("!document.getElementById('rpVerificar').hidden")
        self.itens[0].refresh_from_db()
        self.assertFalse(self.itens[0].revisao_preco_concluida)
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)

        tab.call("Page.reload", {"ignoreCache": True})
        tab.wait("document.readyState === 'complete'")
        tab.wait("!!document.querySelector('#revisaoPrecosPosterior.visivel')")
        tab.evaluate("document.getElementById('rpAgora').click(); true")
        tab.wait("!document.getElementById('rpVerificar').hidden")
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)

        tab.evaluate("document.getElementById('rpVerificar').click(); true")
        tab.wait("document.getElementById('rpErro').textContent.toLowerCase().includes('continua pendente')")
        tab.wait("document.querySelector('.rp-compras-preco-progresso')?.textContent.includes('Produto 1 de 2')")
        self.assertIn("continua pendente", tab.evaluate("document.getElementById('rpErro').textContent"))
        self.assertFalse(tab.evaluate("document.getElementById('rpSalvar').disabled"))
        self.assertEqual(len([r for r in self.trace(tab) if r["method"] == "POST"]), 1)

    def test_alteracao_de_preco_confirmada_avanca_sem_mudar_estoque_ou_compra(self):
        _, tab = self.navegador()
        self.iniciar_revisao(tab)
        antes = (
            self.produtos[0].quantidade,
            self.compra.total,
            self.compra.status,
        )
        tab.evaluate("""
          const row = document.querySelector('#rpLista .rp-compras-preco-simulador[data-campo-preco="preco_vista"]');
          row.querySelector('.campoNovoPrecoVendaCompra').value = '15,00';
          row.querySelector('.campoNovoPrecoVendaCompra').dispatchEvent(new Event('input', {bubbles:true}));
          row.querySelector('.marcarAtualizarVendaCompra').checked = true;
          document.getElementById('rpSalvar').click();
          true
        """)
        self.aguardar_produto_seguinte(tab)
        self.produtos[0].refresh_from_db()
        self.compra.refresh_from_db()
        self.assertEqual(self.produtos[0].preco_vista, Decimal("15.00"))
        self.assertEqual(self.produtos[0].preco_venda, Decimal("15.00"))
        self.assertEqual(
            (self.produtos[0].quantidade, self.compra.total, self.compra.status),
            antes,
        )
        posts = [row for row in self.trace(tab) if row["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], 200)


@override_settings(ROOT_URLCONF="estoque.tests_precos_seletivos")
@skipUnless(Path(CHROME).is_file(), "Chrome unavailable")
class RevisaoPrecosVinculadosNavegacaoBrowserTests(SeletivosFixture, StaticLiveServerTestCase):
    host = "127.0.0.1"

    def setUp(self):
        super().setUp()
        self.operador.is_staff = True
        self.operador.is_superuser = True
        self.operador.save(update_fields=("is_staff", "is_superuser"))
        self.client.force_login(self.operador)

    def test_produtos_vinculados_desktop(self):
        from .tests_precos_seletivos import RevisaoSeletivaPosteriorBrowserTests

        RevisaoSeletivaPosteriorBrowserTests.revisao(self, 1280, False)

    def test_produtos_vinculados_mobile(self):
        from .tests_precos_seletivos import RevisaoSeletivaPosteriorBrowserTests

        RevisaoSeletivaPosteriorBrowserTests.revisao(self, 390, True)
