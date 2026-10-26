"""Consulta histórica sem gravação e expansão sem alterar a conferência."""
import base64
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import EventoVenda, ItemVenda, SeparacaoVenda, SeparacaoVendaItem
from estoque.tests_separacao_peso_fracionado import PesoFixtures
from estoque.views import _montar_grupos_rota_separacao
from offline.browser_support import Chrome, DevTools


class ConsultaFixtures(PesoFixtures):
    def concluir(self):
        sep, item, linha = self.create("4", "UN")
        self.assertEqual(self.save(sep, item, status="conferido", value="4").status_code, 200)
        sep.refresh_from_db()
        return sep, item, linha

    def alterar(self, sep, linha):
        linha.quantidade = 5
        linha.save(update_fields=["quantidade"])
        EventoVenda.objects.create(venda=sep.venda, tipo_evento="quantidade_item_alterada",
            descricao=f"Quantidade alterada na nota: {linha.produto.nome}. De 4.000 UN para 5.000 UN.", canal="sistema")

    def retrato(self):
        return [list(m.objects.order_by("pk").values()) for m in
                (SeparacaoVenda, SeparacaoVendaItem, ItemVenda, EventoVenda)]


class ConsultaChecklistTests(ConsultaFixtures, TestCase):
    def consultar_sem_gravar(self, sep):
        antes = self.retrato()
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(reverse("estoque:separacao_venda_detalhe", args=[sep.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(antes, self.retrato())
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in consultas))
        return resposta

    def test_concluido_sem_pendencia_em_consulta_com_dados_historicos(self):
        sep, item, _ = self.concluir()
        r = self.consultar_sem_gravar(sep)
        for texto in ("Consulta do checklist concluído", "Conferido por:", self.user.username, "Solicitado: 4", "Separado: 4", "Voltar para fila"):
            self.assertContains(r, texto)
        self.assertNotContains(r, 'id="sepSubmitBtn"')
        self.assertNotContains(r, 'class="sep-step-input"')
        self.assertNotContains(r, 'type="radio"')

    def test_alteracao_nova_gera_aviso_sem_gravar_revisao(self):
        sep, _, linha = self.concluir()
        self.alterar(sep, linha)
        r = self.consultar_sem_gravar(sep)
        self.assertContains(r, "necessitam de conferência")
        sep.refresh_from_db()
        self.assertFalse(sep.revisao_pendente)
        _montar_grupos_rota_separacao([sep])
        self.assertTrue(sep.tem_revisao_pendente_fila)
        fila = self.client.get(reverse("estoque:separacao_vendas_fila"))
        self.assertContains(fila, "ALTERACAO APOS SEPARACAO")
        self.assertNotContains(fila, "VENDA ALTERADA DURANTE A SEPARACAO")

    def test_alteracao_atendida_nao_recria_pendencia(self):
        sep, _, linha = self.concluir()
        self.alterar(sep, linha)
        self.client.post(reverse("estoque:separacao_venda_alteracao_atendida", args=[sep.pk]))
        r = self.consultar_sem_gravar(sep)
        self.assertNotContains(r, "necessitam de conferência")
        self.assertNotContains(r, "atualizado/reemitido")
        sep.refresh_from_db()
        _montar_grupos_rota_separacao([sep])
        self.assertFalse(sep.tem_revisao_pendente_fila)

    def test_nova_alteracao_apos_atendimento_continua_visivel(self):
        sep, _, linha = self.concluir()
        self.alterar(sep, linha)
        self.client.post(reverse("estoque:separacao_venda_alteracao_atendida", args=[sep.pk]))
        EventoVenda.objects.create(venda=sep.venda, tipo_evento="item_adicionado_na_nota",
            descricao="Item adicionado na nota: Produto novo fictício, quantidade 2 UN.", canal="sistema")
        self.assertContains(self.consultar_sem_gravar(sep), "necessitam de conferência")

    def test_revisao_pendente_e_falta_preservadas(self):
        sep, item, _ = self.create("4", "UN")
        self.save(sep, item, status="quantidade_insuficiente", value="3")
        SeparacaoVenda.objects.filter(pk=sep.pk).update(revisao_pendente=True)
        r = self.consultar_sem_gravar(sep)
        self.assertContains(r, "Separado: 3")
        self.assertContains(r, "falta 1")
        self.assertContains(r, "necessitam de conferência")

    def test_post_direto_nao_modifica_checklist_concluido(self):
        sep, item, _ = self.concluir()
        antes = self.retrato()
        self.assertEqual(self.save(sep, item, status="nao_encontrado").status_code, 409)
        self.assertEqual(self.client.post(reverse("estoque:separacao_venda_detalhe", args=[sep.pk]), {}).status_code, 302)
        self.assertEqual(antes, self.retrato())

    def test_get_aberto_desatualizado_tambem_nao_sincroniza_nem_grava(self):
        sep, _, linha = self.create("4", "UN")
        self.alterar(sep, linha)
        r = self.consultar_sem_gravar(sep)
        self.assertContains(r, "atualizado/reemitido")


class ConsultaChecklistBrowserTests(ConsultaFixtures, StaticLiveServerTestCase):
    def test_expandir_recolher_sem_posts_desktop_mobile(self):
        out = Path(__file__).resolve().parents[1] / "amostras/separacao-consulta"
        out.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="consulta-checklist-browser-", ignore_cleanup_errors=True) as profile:
            browser = Chrome(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", profile).start()
            try:
                time.sleep(1)
                with urlopen(f"http://127.0.0.1:{browser.port}/json/list") as response:
                    targets = json.load(response)
                tab = DevTools(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"))
                tab.call("Page.enable")
                tab.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                for width in (1440, 390, 320):
                    for concluida in (False, True):
                        if concluida:
                            sep, _, linha = self.concluir()
                            self.alterar(sep, linha)
                        else:
                            sep, item, _ = self.create("4", "UN")
                            # Um produto OK e outro pendente: checklist ainda editável.
                            linha = ItemVenda.objects.get(pk=item.item_venda_id)
                            linha.pk = None
                            linha.save()
                            from estoque.services.separacao_vendas import sincronizar_checklist_aberto_com_venda
                            sincronizar_checklist_aberto_com_venda(sep)
                            self.save(sep, item, status="conferido", value="4")
                        antes = self.retrato()
                        tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
                        tab.call("Emulation.setTouchEmulationEnabled", {"enabled": width < 600})
                        tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:separacao_venda_detalhe", args=[sep.pk])})
                        tab.wait('document.readyState==="complete" && !!document.querySelector("[data-sep-item]")')
                        self.assertFalse(tab.evaluate("!!document.querySelector('.sep-card.is-active')"))
                        tab.evaluate("window.posts=0;window.oldFetch=fetch;window.fetch=(...a)=>{if(String(a[0]).includes('/separacao-vendas/') && String(a[1]?.method||'GET').toUpperCase()==='POST')posts++;return oldFetch(...a)};window.button=document.querySelector('[data-reabrir-item]');true")
                        for abrir in (True, False, True):
                            tab.evaluate("document.documentElement.style.setProperty('scroll-behavior','auto','important');button.scrollIntoView({block:'center',behavior:'instant'});true")
                            point = tab.evaluate("(()=>{const r=button.getBoundingClientRect();return {x:r.x+r.width/2-visualViewport.offsetLeft,y:r.y+r.height/2-visualViewport.offsetTop}})()")
                            if width < 600:
                                tab.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
                                tab.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                            else:
                                for event in ("mousePressed", "mouseReleased"):
                                    tab.call("Input.dispatchMouseEvent", {"type": event, **point, "button": "left", "clickCount": 1})
                            tab.wait(f"button.getAttribute('aria-expanded')==='{str(abrir).lower()}'")
                        self.assertEqual(tab.evaluate("posts"), 0)
                        self.assertEqual(antes, self.retrato())
                        self.assertLessEqual(tab.evaluate("document.documentElement.scrollWidth"), width)
                        if concluida:
                            self.assertFalse(tab.evaluate("!!document.querySelector('[data-qtd-input],#sepSubmitBtn')"))
                            data = tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]
                            (out / f"consulta-{width}.png").write_bytes(base64.b64decode(data))
                        # A saída é navegação, sem submeter nem reprocessar o checklist.
                        tab.evaluate("[...document.querySelectorAll('a')].find(a=>a.textContent.trim()==='Voltar para fila').click();true")
                        tab.wait(f"location.pathname==={reverse('estoque:separacao_vendas_fila')!r} && document.readyState==='complete'")
                        self.assertEqual(antes, self.retrato())
            finally:
                browser.stop()
