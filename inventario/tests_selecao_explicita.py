import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque.models import Fornecedor, ProdutoFornecedor, Funcionario, Produto, MovimentacaoEstoqueManual
from . import services, views, ajustes
from .models import (Inventario, ItemInventario, RodadaContagem, AtribuicaoContagem,
                     ConfirmacaoContagem, EventoInventario)
from .selecao import salvar_selecao, itens_incluidos, RETIRADO, INCLUIDO
from .tests import produto
from offline.browser_support import Chrome


class DadosSelecao:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("gestor-selecao", "", "teste")
        self.funcionario = Funcionario.objects.create(nome="Ana teste")
        self.outro = Funcionario.objects.create(nome="Bruno teste")
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor da conferência teste")
        self.produtos = [produto(nome=nome, unidade_compra="UN", vende_fracionado=False) for nome in
            ["Império Lata 12/350Ml", "Pepsi Cola 12/1L", "Tuchaua 6/1,5L"]]
        for p in self.produtos:
            ProdutoFornecedor.objects.create(produto=p, fornecedor=self.fornecedor)
        self.inv = services.criar_inventario(self.user, criterio="fornecedor", selecionados=self.fornecedor.pk)
        self.itens = list(self.inv.itens.order_by("produto_id"))
        self.rodadas = [i.rodadas.get() for i in self.itens]

    def selecionar(self, ids, motivo="Retirado explicitamente pelo responsável"):
        self.inv.refresh_from_db()
        return salvar_selecao(self.user, self.inv.pk, ids, self.inv.versao, motivo)

    def atribuir(self, indices, funcionario=None):
        return services.gerar_acesso(self.user, self.inv.pk, (funcionario or self.funcionario).pk,
            [self.rodadas[i].pk for i in indices])[0]

    def ok(self, acesso, indice):
        rodada = RodadaContagem.objects.get(pk=self.rodadas[indice].pk)
        return services.salvar_contagem(acesso.pk, rodada.pk, rodada.versao, "10", "", True)

    def enviar(self, acesso):
        self.inv.refresh_from_db()
        return services.finalizar_conferencia(acesso.pk, self.inv.versao)


class SelecaoExplicitaTests(DadosSelecao, TestCase):
    def test_fornecedor_categoria_manual_sugerem_todos_e_retirada_e_explicita(self):
        for criterio, valor in [("fornecedor", self.fornecedor.pk), ("categoria", "Bebidas"),
                                ("manual", [p.pk for p in self.produtos])]:
            with self.subTest(criterio=criterio):
                inv = services.criar_inventario(self.user, criterio=criterio, selecionados=valor)
                itens = list(inv.itens.order_by("pk"))
                self.assertEqual(len(itens), 3)
                salvar_selecao(self.user, inv.pk, [i.pk for i in itens[1:]], inv.versao, "Não participa")
                self.assertEqual(set(itens_incluidos(inv).values_list("pk", flat=True)), {i.pk for i in itens[1:]})
                self.assertEqual(inv.itens.count(), 3)

    def test_resultado_contadores_csv_e_ajustes_usam_mesmo_escopo(self):
        self.client.force_login(self.user)
        antes = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by("pk").values())
        self.selecionar([i.pk for i in self.itens[1:]])
        resposta = self.client.get(reverse("inventario:detalhe", args=[self.inv.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(len(resposta.context["linhas"]), 2)
        self.assertEqual(resposta.context["resumo_painel"]["pendentes"], 2)
        self.assertEqual(resposta.context["rodadas_disponiveis"], 2)
        self.assertEqual(views.resumo_inicial()["aguardando_ok"], 2)
        csv = self.client.get(reverse("inventario:relatorio", args=[self.inv.pk])).content.decode("utf-8")
        self.assertNotIn(self.itens[0].cadastro["nome"], csv)
        self.assertIn(self.itens[1].cadastro["nome"], csv)
        ajustes_pagina = self.client.get(reverse("inventario:ajustes", args=[self.inv.pk]))
        self.assertEqual(ajustes_pagina.context["resumo"]["total"], 2)
        self.assertEqual(antes, list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by("pk").values()))
        evento = EventoInventario.objects.get(acao=RETIRADO, inventario=self.inv)
        self.assertEqual(evento.usuario, self.user)
        self.assertEqual(evento.dados["item_id"], self.itens[0].pk)
        self.assertTrue(evento.dados["motivo"])
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_retirado_nao_pode_ser_atribuido_nem_recontado(self):
        self.selecionar([i.pk for i in self.itens[1:]])
        with self.assertRaises(ValueError):
            self.atribuir([0])
        with self.assertRaises(ValueError):
            services.recontar(self.user, self.rodadas[0].pk, "Tentativa indevida")
        with self.assertRaises(ValueError):
            ajustes.bloquear(self.inv.pk, [self.rodadas[0].pk])
        self.assertFalse(AtribuicaoContagem.objects.exists())

    def test_atribuicao_parcial_nao_retira_e_multiplos_funcionarios_finalizam(self):
        self.selecionar([i.pk for i in self.itens[1:]])
        ana = self.atribuir([1])
        self.ok(ana, 1)
        self.assertIsNone(self.enviar(ana).finalizada_em)
        self.assertEqual(len(views.linhas_revisao(self.inv)), 2)
        bruno = self.atribuir([2], self.outro)
        self.ok(bruno, 2)
        self.assertIsNotNone(self.enviar(bruno).finalizada_em)
        self.assertEqual(self.inv.itens.count(), 3)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.assertEqual(views.resumo_painel(views.linhas_revisao(self.inv))["pendentes"], 0)

    def test_reinclusao_auditada_idempotente_e_reabertura_preserva_retirada(self):
        self.selecionar([i.pk for i in self.itens[1:]])
        self.selecionar([i.pk for i in self.itens], "Reincluir antes da atribuição")
        self.selecionar([i.pk for i in self.itens], "Sem alteração")
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
        self.assertEqual(EventoInventario.objects.filter(acao=INCLUIDO).count(), 1)
        self.selecionar([i.pk for i in self.itens[1:]])
        acesso = self.atribuir([1, 2])
        self.ok(acesso, 1)
        self.ok(acesso, 2)
        self.enviar(acesso)
        services.reabrir_conferencia(self.user, self.inv.pk, "Recontagem autorizada")
        self.assertEqual(itens_incluidos(self.inv).count(), 2)
        self.assertEqual(self.itens[0].rodadas.count(), 1)
        self.assertEqual(self.itens[1].rodadas.count(), 2)

    def test_bloqueia_atribuido_rascunho_e_confirmacao_sem_apagar(self):
        acesso = self.atribuir([0])
        for etapa in ["atribuido", "rascunho", "ok"]:
            if etapa == "rascunho":
                services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 0, "0", "", False)
            if etapa == "ok":
                self.ok(acesso, 0)
            antes = EventoInventario.objects.count()
            with self.assertRaises(ValueError):
                self.selecionar([i.pk for i in self.itens[1:]])
            self.assertEqual(EventoInventario.objects.count(), antes)
            self.assertEqual(itens_incluidos(self.inv).count(), 3)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)

    def test_finalizada_sem_novos_eventos_e_sem_mudar_historico(self):
        acesso = self.atribuir([0, 1, 2])
        for i in range(3):
            self.ok(acesso, i)
        self.enviar(acesso)
        modelos = [Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem, EventoInventario]
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        with self.assertRaises(ValueError):
            self.selecionar([self.itens[0].pk])
        self.client.force_login(self.user)
        self.client.get(reverse("inventario:detalhe", args=[self.inv.pk]))
        self.assertEqual(antes, {m: list(m.objects.order_by("pk").values()) for m in modelos})

    def test_checklist_mobile_contem_somente_atribuicao_incluida(self):
        self.selecionar([i.pk for i in self.itens[1:]])
        acesso, token = services.gerar_acesso(self.user, self.inv.pk, self.funcionario.pk,
            [self.rodadas[1].pk])
        self.assertEqual(self.client.post(reverse("inventario:convite", args=[token])).status_code, 302)
        resposta = self.client.get(reverse("inventario:mobile"), HTTP_ACCEPT="application/json")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([p["id"] for p in resposta.json()["produtos"]], [self.rodadas[1].pk])
        self.assertEqual(itens_incluidos(self.inv).count(), 2)  # Não atribuído continua incluído.

    def test_contagem_sem_atribuicao_tambem_bloqueia_e_lote_e_atomico(self):
        RodadaContagem.objects.filter(pk=self.rodadas[0].pk).update(embalagens=0)
        with self.assertRaises(ValueError):
            self.selecionar([self.itens[1].pk])  # Tentaria retirar também o terceiro item.
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
        self.assertFalse(EventoInventario.objects.filter(acao=RETIRADO).exists())
        self.assertEqual(RodadaContagem.objects.get(pk=self.rodadas[0].pk).embalagens, 0)

    def test_ids_vazios_e_reinclusao_de_inativo_nao_alteram_escopo(self):
        with self.assertRaises(ValueError):
            self.selecionar([])
        self.selecionar([i.pk for i in self.itens[1:]])
        Produto.objects.filter(pk=self.produtos[0].pk).update(ativo=False)
        with self.assertRaises(ValueError):
            self.selecionar([i.pk for i in self.itens])
        self.assertEqual(itens_incluidos(self.inv).count(), 2)

    def test_inativacao_nao_retira_produto_antes_ou_depois_do_ok(self):
        acesso = self.atribuir([0])
        Produto.objects.filter(pk=self.produtos[0].pk).update(ativo=False)
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
        with self.assertRaises(ValueError):
            self.ok(acesso, 0)
        Produto.objects.filter(pk=self.produtos[0].pk).update(ativo=True)
        self.ok(acesso, 0)
        Produto.objects.filter(pk=self.produtos[0].pk).update(ativo=False)
        self.assertEqual(len(views.linhas_revisao(self.inv)), 3)
        self.assertIsNotNone(RodadaContagem.objects.get(pk=self.rodadas[0].pk).ok_em)

    def test_permissao_versao_e_produto_de_outra_conferencia(self):
        sem = get_user_model().objects.create_user("sem-permissao")
        with self.assertRaises(PermissionDenied):
            salvar_selecao(sem, self.inv.pk, [self.itens[0].pk], 0, "Teste")
        with self.assertRaises(ValueError):
            salvar_selecao(self.user, self.inv.pk, [self.itens[0].pk], 999, "Teste")
        with self.assertRaises(ValueError):
            self.selecionar([999999])
        with self.assertRaises(ValueError):
            self.selecionar([self.itens[0].pk], "")
        self.assertEqual(itens_incluidos(self.inv).count(), 3)


class SelecaoConcorrenciaTests(DadosSelecao, TransactionTestCase):
    def test_disputa_atribuicao_retirada_nao_perde_itens_nem_evidencias(self):
        barreira = Barrier(2)
        def executar(retirar):
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                if retirar:
                    self.selecionar([i.pk for i in self.itens[1:]])
                else:
                    self.atribuir([0])
                return True
            except ValueError:
                return False
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(executar, [True, False]))
        self.assertEqual(sum(resultados), 1)
        incluido = itens_incluidos(self.inv).filter(pk=self.itens[0].pk).exists()
        self.assertEqual(AtribuicaoContagem.objects.filter(rodada=self.rodadas[0]).exists(), incluido)


class SelecaoBrowserTests(DadosSelecao, StaticLiveServerTestCase):
    def test_distribuicao_unica_desktop_mobile(self):
        self.client.force_login(self.user)
        pasta = Path(__file__).resolve().parents[1] / "diagnostics" / "distribuicao-simplificada"
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="inventario-distribuicao-browser-") as profile:
            chrome = Chrome(r"C:\Program Files\Google\Chrome\Application\chrome.exe", profile).start()
            try:
                for largura in [1280, 390, 320]:
                    tab = chrome.tab()
                    tab.call("Network.enable")
                    tab.call("Network.setBlockedURLs", {"urls": ["https://*"]})
                    tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 1000, "deviceScaleFactor": 1, "mobile": largura < 800})
                    tab.call("Page.navigate", {"url": self.live_server_url + reverse("inventario:detalhe", args=[self.inv.pk])})
                    tab.wait('!!document.querySelector(".resumo-inventario") && window.inventarioRevisaoPronta===true')
                    self.assertFalse(tab.evaluate('!!document.getElementById("form-selecao-participantes")'))
                    tab.evaluate('document.getElementById("distribuicao").open=true;document.getElementById("distribuicao").scrollIntoView({block:"start",behavior:"instant"});true')
                    if largura == 1280:
                        self.assertFalse(tab.evaluate('document.querySelector("[name=manter_nao_selecionados]").checked'))
                        selecionadas = json.dumps([r.pk for r in self.rodadas[:2]])
                        tab.evaluate('document.querySelector("[name=funcionario]").value="'+str(self.funcionario.pk)+'";document.querySelectorAll("[name=rodadas]").forEach(c=>c.checked='+selecionadas+'.includes(Number(c.value)));true')
                    if largura == 1280:
                        tab.evaluate('document.getElementById("form-atribuicao").dispatchEvent(new Event("change", {bubbles:true}));true')
                        self.assertFalse(tab.evaluate('document.getElementById("confirmar-atribuicao").disabled'))
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    rect = tab.evaluate('(()=>{const r=document.getElementById("distribuicao").getBoundingClientRect();return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1}})()')
                    (pasta / f"distribuicao-{largura}.png").write_bytes(base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": rect})["data"]))
                    if largura == 1280:
                        tab.evaluate('document.getElementById("form-atribuicao").requestSubmit();true')
                        tab.wait('!!document.getElementById("link-funcionario")')
                    self.assertEqual(tab.evaluate('document.querySelectorAll(".resumo-linha").length'), 2)
                    tab.evaluate('document.querySelector(".indicadores-painel").scrollIntoView({block:"start",behavior:"instant"});true')
                    (pasta / f"resultado-{largura}.png").write_bytes(base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png"})["data"]))
            finally:
                chrome.stop()
