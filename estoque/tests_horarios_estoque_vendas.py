"""Presentation only, with synthetic stock and disposable PostgreSQL."""
import base64
import json
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import Context, Template
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from inventario import services
from inventario.models import ConfirmacaoContagem, Inventario, MovimentoObservado, RodadaContagem
from offline.browser_support import Chrome
from estoque.models import (EntregaRota, EntregaRotaItem, EntregaChecklistItem, EventoVenda,
                            Funcionario, ItemVenda, MovimentacaoEstoqueManual, Produto,
                            SeparacaoVenda, SeparacaoVendaItem)
from estoque.tests_rastreabilidade_apresentacao import Fixture as DocumentoFixture, INSTANTE, CHROME

OUT = Path(__file__).resolve().parent.parent / "artifacts/horarios-estoque-vendas-20261028"
TEXTO = "24/10/2026 às 22:02:03"


class Fixture(DocumentoFixture):
    def setUp(self):
        super().setUp()
        self.item = self.venda.itens.get()
        self.rota = EntregaRota.objects.create(tipo="rota")
        self.bloco = EntregaRotaItem.objects.create(rota=self.rota, venda=self.venda)
        self.check = EntregaChecklistItem.objects.create(rota_item=self.bloco, item_venda=self.item)
        self.sep = SeparacaoVenda.objects.create(venda=self.venda, enviado_em=INSTANTE,
            iniciado_em=INSTANTE, revisao_solicitada_em=INSTANTE, revisao_concluida_em=INSTANTE)
        SeparacaoVendaItem.objects.create(separacao=self.sep, item_venda=self.item,
            produto_nome_snapshot=self.produto.nome, unidade_snapshot="PCT", quantidade_solicitada=1)
        funcionario = Funcionario.objects.create(nome="Contador fictício de horários")
        self.inv = services.criar_inventario(self.user, "Conferência de horários", "manual", [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.user, self.inv.pk, funcionario.pk, [self.rodada.pk])
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "0", True,
                                token_hash=self.acesso.token_hash)
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(ok_em=INSTANTE)
        ConfirmacaoContagem.objects.filter(rodada=self.rodada).update(ok_em=INSTANTE)
        for obj in [self.rota, self.check, self.sep]:
            type(obj).objects.filter(pk=obj.pk).update(criado_em=INSTANTE, atualizado_em=INSTANTE)
        EntregaRotaItem.objects.filter(pk=self.bloco.pk).update(criado_em=INSTANTE)
        Inventario.objects.filter(pk=self.inv.pk).update(criado_em=INSTANTE)
        EventoVenda.objects.create(venda=self.venda, tipo_evento="venda_editada", descricao="Evento fictício de auditoria")
        EventoVenda.objects.filter(venda=self.venda, tipo_evento="venda_editada").update(criado_em=INSTANTE)

    def paginas(self):
        return [
            ("inventario:lista", []), ("inventario:detalhe", [self.inv.pk]),
            ("inventario:ajustes", [self.inv.pk]),
            ("inventario:movimento_manual", [self.inv.pk, self.rodada.pk, self.registro.pk]),
            ("estoque:historico_estoque_manual", [self.produto.pk]),
            ("estoque:venda_detalhe", [self.venda.pk]),
            ("estoque:separacao_venda_detalhe", [self.sep.pk]),
            ("estoque:entrega_rota_detalhe", [self.rota.pk]),
            ("estoque:entrega_rota_checklist", [self.rota.pk]),
            ("estoque:entrega_rota_checklist_item", [self.rota.pk, self.bloco.pk]),
        ]

    def retrato(self):
        return [list(model.objects.order_by("pk").values()) for model in
            [Produto, ItemVenda, MovimentoObservado, MovimentacaoEstoqueManual, EntregaRota,
             EntregaRotaItem, EntregaChecklistItem, SeparacaoVenda, SeparacaoVendaItem,
             RodadaContagem, ConfirmacaoContagem]]


class ApresentacaoTests(Fixture, TestCase):
    def test_paginas_horarios_negrito_e_belem_com_outro_fuso_ativo(self):
        with timezone.override("Asia/Tokyo"):
            for nome, args in self.paginas():
                with self.subTest(pagina=nome):
                    resposta = self.client.get(reverse(nome, args=args))
                    self.assertEqual(resposta.status_code, 200)
                    self.assertIn(TEXTO, strip_tags(resposta.content.decode()))
                    self.assertContains(resposta, 'class="horario-registro"')
                    self.assertContains(resposta, "Belém")

    def test_consultas_preservam_saldos_itens_confirmacoes_e_checklists(self):
        antes = self.retrato()
        for nome, args in self.paginas():
            self.assertEqual(self.client.get(reverse(nome, args=args)).status_code, 200)
        self.assertEqual(antes, self.retrato())

    def test_cancelamento_exibe_timestamp_armazenado_sem_alterar_saldo(self):
        type(self.venda).objects.filter(pk=self.venda.pk).update(cancelada=True, cancelada_em=INSTANTE)
        antes = self.retrato()
        resposta = self.client.get(reverse("estoque:venda_detalhe", args=[self.venda.pk]))
        self.assertIn("Cancelada em " + TEXTO, strip_tags(resposta.content.decode()))
        self.assertEqual(antes, self.retrato())

    def test_agrupamento_do_historico_usa_o_dia_de_belem(self):
        evento = EventoVenda.objects.create(venda=self.venda, tipo_evento="venda_editada", descricao="Outro dia local")
        EventoVenda.objects.filter(pk=evento.pk).update(criado_em=INSTANTE.replace(hour=10))
        with timezone.override("Asia/Tokyo"):
            resposta = self.client.get(reverse("estoque:venda_detalhe", args=[self.venda.pk]))
        html = resposta.content.decode()
        self.assertEqual(html.count('data-historico-data="24/10/2026"'), 1)
        self.assertEqual(html.count('data-historico-data="25/10/2026"'), 1)

    def test_legado_sem_horario_e_iso_ausente_nao_inventam_instante(self):
        modelo = Template("{% load horarios_documentos %}{% instante_registro valor %}")
        for valor in [None, "", "data inválida", date(2026, 10, 22), datetime(2026, 10, 22), "2026-10-22"]:
            html = modelo.render(Context({"valor": valor}))
            self.assertIn("Horário não registrado", html)
            self.assertNotIn("00:00:00", html)
        self.assertIn(TEXTO, modelo.render(Context({"valor": INSTANTE.isoformat()})))
        self.assertIn("Horário não registrado", modelo.render(Context({"valor": '<script>alert(1)</script>'})))

    def test_checklists_separam_atualizacao_de_execucao_fisica(self):
        resposta = self.client.get(reverse("estoque:entrega_rota_checklist_item", args=[self.rota.pk, self.bloco.pk]))
        html = strip_tags(resposta.content.decode())
        self.assertIn("Atualização do registro:", html)
        self.assertIn("não comprovam, por si, o momento físico", html)
        self.assertNotIn("Entregue em " + TEXTO, html)

    def test_eventos_ajax_devolvem_horario_do_evento_persistido(self):
        antes = self.retrato()
        for nome in ["registrar_impressao", "registrar_whatsapp_aberto", "confirmar_whatsapp"]:
            resposta = self.client.post(reverse("estoque:" + nome, args=[self.venda.pk]),
                data="{}", content_type="application/json")
            self.assertEqual(resposta.status_code, 200)
            evento = EventoVenda.objects.filter(venda=self.venda).order_by("-id").first()
            self.assertEqual(resposta.json()["evento_criado_em"], evento.criado_em.isoformat())
        self.assertEqual(antes, self.retrato())

    def test_contagem_cega_so_expoe_confirmacao_sem_saldo_esperado(self):
        contador = Client()
        convite = reverse("inventario:convite", args=[self.token])
        contador.get(convite)
        contador.post(convite)
        resposta = contador.get(reverse("inventario:mobile"))
        self.assertContains(resposta, TEXTO)
        for proibido in ["saldo_esperado", 'name="esperado"', "Estoque registrado no OK"]:
            self.assertNotContains(resposta, proibido)
        self.assertEqual(contador.get(reverse("inventario:detalhe", args=[self.inv.pk])).status_code, 403)
        self.assertEqual(contador.get(reverse("estoque:historico_estoque_manual", args=[self.produto.pk])).status_code, 403)


@skipUnless(Path(CHROME).is_file(), "Chrome indisponível")
class BrowserTests(Fixture, StaticLiveServerTestCase):
    def test_desktop_mobile_impressao_e_evento_sem_relogio_do_aparelho(self):
        OUT.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="horarios-estoque-chrome-") as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value,
                                               "url": self.live_server_url})
                tab.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Tokyo"})
                for largura in [1366, 390, 320]:
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 900,
                        "deviceScaleFactor": 1, "mobile": largura < 768})
                    for nome, args in self.paginas():
                        tab.call("Page.navigate", {"url": self.live_server_url + reverse(nome, args=args)})
                        tab.wait('document.readyState === "complete" && document.body.textContent.includes("' + TEXTO + '")')
                        limites = tab.evaluate('''[...document.querySelectorAll('.horario-registro')].filter(e=>e.getBoundingClientRect().width>0).map(e=>({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,weight:getComputedStyle(e).fontWeight}))''')
                        self.assertTrue(limites, (largura, nome))
                        for limite in limites:
                            self.assertGreaterEqual(limite["left"], -1, (largura, nome, limite))
                            self.assertLessEqual(limite["right"], largura + 1, (largura, nome, limite))
                            self.assertGreaterEqual(int(limite["weight"]), 700)
                        tab.evaluate("document.querySelector('.horario-registro').scrollIntoView({block:'center'});true")
                        captura = tab.call("Page.captureScreenshot", {"format": "png"})
                        (OUT / f"{nome.replace(':','-')}-{largura}.png").write_bytes(base64.b64decode(captura["data"]))
                        if largura == 1366:
                            tab.call("Emulation.setEmulatedMedia", {"media": "print"})
                            pdf = tab.call("Page.printToPDF", {"printBackground": True})
                            self.assertTrue(base64.b64decode(pdf["data"]).startswith(b"%PDF"))
                            (OUT / f"{nome.replace(':','-')}-impressao.pdf").write_bytes(base64.b64decode(pdf["data"]))
                            tab.call("Emulation.setEmulatedMedia", {"media": "screen"})
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:venda_detalhe", args=[self.venda.pk])})
                tab.wait('document.readyState === "complete" && typeof formatarInstanteVenda === "function"')
                self.assertEqual(tab.evaluate('formatarInstanteVenda("2026-10-25T01:02:03Z")'), TEXTO)
                self.assertEqual(tab.evaluate('formatarInstanteVenda("")'), "Horário não registrado")
                tab.evaluate('adicionarHistorico("Evento de teste", "2026-10-25T01:02:03Z");true')
                linha_teste = tab.evaluate('''(()=>{const linha=[...document.querySelectorAll('.historico-linha')].find(e=>e.textContent.includes('Evento de teste'));return {texto:linha?.textContent,dia:linha?.closest('[data-historico-data]')?.dataset.historicoData}})()''')
                self.assertIn(TEXTO, linha_teste["texto"])
                self.assertEqual(linha_teste["dia"], "24/10/2026")
                # Enter counting using its own credential; do not grant commercial access.
                tab.call("Network.deleteCookies", {"name": "sessionid", "url": self.live_server_url})
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("inventario:convite", args=[self.token])})
                tab.wait('!!document.querySelector("form button")')
                tab.evaluate('document.querySelector("form button").click();true')
                tab.wait('window.inventarioContagemPronta === true && !!document.querySelector("[data-horario-ultimo-ok]")')
                self.assertEqual(tab.evaluate('document.querySelector("[data-horario-ultimo-ok]").textContent'), TEXTO)
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                tab.evaluate('document.querySelector("[data-horario-ultimo-ok]").scrollIntoView({block:"center"});true')
                (OUT / "contagem-cega-320.png").write_bytes(base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png"})["data"]))
            finally:
                chrome.stop()
