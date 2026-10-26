"""Classificação operacional e evidências legadas, exclusivamente com fixtures."""
import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque import views
from estoque.models import (EventoVenda, ItemVenda, Produto, SeparacaoVenda,
    SeparacaoVendaItem, ContaReceber, MovimentoFinanceiro, Venda)
from estoque.services.pendencias_separacao import classificar_separacao, TIPO_RESOLUCAO
from estoque.tests_separacao_resolucoes import ResolucaoFixtures
from offline.browser_support import Chrome, DevTools


class OperacionalFixtures(ResolucaoFixtures):
    def legado(self, prova=True, parcial=False, extras=0):
        sep, venda, a, b = self.montar()
        for n in range(extras):
            produto = Produto.objects.create(nome=f"Produto fictício {n}", quantidade=20,
                preco_compra=5, preco_vista=10, preco_prazo=10, unidade_compra="UN", unidade_venda_1="UN")
            linha = ItemVenda.objects.create(venda=venda, produto=produto, quantidade=1,
                unidade="UN", preco_unitario=10, valor_total=10)
            sep.itens.create(item_venda=linha, produto_nome_snapshot=produto.nome,
                unidade_snapshot="UN", quantidade_solicitada=1, quantidade_separada=1, status="conferido")
        for linha, qtd in ((a, "3"), (b, "2" if parcial else "1")):
            if linha.quantidade == Decimal(qtd):
                continue
            anterior = linha.quantidade
            linha.quantidade = Decimal(qtd)
            linha.save(update_fields=["quantidade"])
            if prova:
                EventoVenda.objects.create(venda=venda, tipo_evento="quantidade_item_alterada",
                    descricao=f"Quantidade alterada na nota: {linha.produto.nome}. De {anterior} {linha.unidade} para {qtd}.000 {linha.unidade}.", canal="sistema")
        # Simula os marcos legados posteriores às correções, sem alterar a ocorrência física.
        sep.status = "separada"
        sep.finalizado_em = timezone.now()
        sep.revisao_pendente = False
        sep.save()
        return sep, venda, a, b

    def retrato_operacional(self):
        return [list(m.objects.order_by("pk").values()) for m in (SeparacaoVenda,
            SeparacaoVendaItem, EventoVenda, ItemVenda, Produto, ContaReceber, MovimentoFinanceiro, Venda)]

    def leitura(self, sep):
        return classificar_separacao(SeparacaoVenda.objects.select_related("venda").get(pk=sep.pk))


class SeparacaoOperacionalTests(OperacionalFixtures, TestCase):
    def test_legado_comprovado_sem_salvar_e_contadores_13(self):
        sep, venda, a, b = self.legado(extras=11)
        antes = self.retrato_operacional()
        op = self.leitura(sep)
        self.assertEqual((op["produtos"], op["resolvidos"], op["pendencias"]), (13, 13, 0))
        self.assertTrue(op["concluida"])
        self.assertTrue(op["ajustada"])
        self.assertEqual(antes, self.retrato_operacional())
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).exists())
        self.assertIsNone(views._montar_payload_ajuste_separacao_venda(sep))

    def test_igualdade_sem_prova_nao_resolve_nem_ao_reenviar(self):
        sep, venda, a, b = self.legado(prova=False)
        self.assertEqual(self.leitura(sep)["pendencias"], 2)
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        self.assertEqual(self.leitura(sep)["pendencias"], 2)
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).exists())
        self.assertTrue(self.leitura(sep)["itens"][0].motivo_operacional)

    def test_atendimento_posterior_nao_oculta_prova_antiga(self):
        sep, venda, _, _ = self.legado()
        EventoVenda.objects.create(venda=venda, tipo_evento="alteracao_separacao_atendida", descricao="Atendimento genérico")
        self.assertTrue(self.leitura(sep)["concluida"])

    def test_atendimento_generico_sem_reducao_nao_comprova_complemento(self):
        sep, venda, _, _ = self.montar()
        EventoVenda.objects.create(venda=venda, tipo_evento="alteracao_separacao_atendida", descricao="Atendimento genérico")
        op = self.leitura(sep)
        self.assertEqual(op["pendencias"], 2)
        self.assertFalse(op["concluida"])

    def test_troca_homonima_unidade_e_evento_incompleto_impedem_inferencia(self):
        for modo in ("homonimo", "unidade", "incompleto", "duplicado"):
            with self.subTest(modo=modo):
                sep, venda, a, _ = self.legado()
                if modo == "homonimo":
                    EventoVenda.objects.create(venda=venda, tipo_evento="item_removido_da_nota",
                        descricao=f"Item removido da nota: {a.produto.nome}, quantidade 4 PCT.")
                elif modo == "unidade":
                    a.unidade = "UN"
                    a.save(update_fields=["unidade"])
                elif modo == "incompleto":
                    EventoVenda.objects.create(venda=venda, tipo_evento="quantidade_item_alterada", descricao="Descrição sem identidade")
                else:
                    outra_linha = ItemVenda.objects.create(venda=venda, produto=a.produto,
                        quantidade=3, unidade=a.unidade, preco_unitario=10, valor_total=30)
                    sep.itens.create(item_venda=outra_linha, produto_nome_snapshot=a.produto.nome,
                        unidade_snapshot=a.unidade, quantidade_solicitada=4, quantidade_separada=3,
                        status="quantidade_insuficiente")
                self.assertGreater(self.leitura(sep)["pendencias"], 0)

    def test_parcial_13_conta_cada_produto_uma_vez(self):
        sep, _, _, _ = self.legado(parcial=True, extras=11)
        op = self.leitura(sep)
        self.assertEqual((op["produtos"], op["resolvidos"], op["pendencias"]), (13, 12, 1))
        self.assertFalse(op["concluida"])
        self.assertEqual(op["itens"][1].texto_operacional, "Quantidade insuficiente — falta 1")

    def test_aumento_novo_prevalece_sobre_resolucao(self):
        sep, venda, a, _ = self.legado()
        a.quantidade = 5
        a.save(update_fields=["quantidade"])
        EventoVenda.objects.create(venda=venda, tipo_evento="quantidade_item_alterada",
            descricao=f"Quantidade alterada na nota: {a.produto.nome}. De 3.000 PCT para 5.000 PCT.")
        op = self.leitura(sep)
        self.assertEqual((op["resolvidos"], op["pendencias"]), (1, 1))
        self.assertFalse(op["concluida"])

    def test_revisao_global_e_acrescimo_nao_inventam_produto(self):
        for tipo in ("global", "acrescimo"):
            sep, venda, _, _ = self.legado(extras=11)
            if tipo == "global":
                SeparacaoVenda.objects.filter(pk=sep.pk).update(revisao_pendente=True)
            else:
                EventoVenda.objects.create(venda=venda, tipo_evento="item_adicionado_na_nota",
                    descricao="Item adicionado na nota: Produto novo fictício, quantidade 1 UN.")
            op = self.leitura(sep)
            self.assertEqual(op["produtos"], 13)
            self.assertEqual(op["resolvidos"] + op["pendencias"], 13)
            self.assertGreater(op["pendencias"], 0)
            self.assertTrue(op["motivo_global"])
            self.assertFalse(op["concluida"])

    def test_conferencia_fisica_confirmada_e_diferente_de_ajuste_comercial(self):
        sep, item, linha = self.create("4", "UN")
        self.assertEqual(self.save(sep, item, status="conferido", value="4").status_code, 200)
        op = self.leitura(sep)
        self.assertTrue(op["concluida"])
        self.assertEqual(op["itens"][0].situacao_operacional, "conferido")
        self.assertFalse(op["itens"][0].falta_resolvida)
        self.assertEqual(op["pendencias"], 0)

    def test_peso_confirmado_com_nota_nao_ajustada_nao_fica_pronto(self):
        sep, item, linha = self.create("4", "KG")
        SeparacaoVendaItem.objects.filter(pk=item.pk).update(status="conferido", quantidade_separada=3)
        SeparacaoVenda.objects.filter(pk=sep.pk).update(status="separada", finalizado_em=timezone.now())
        antes = self.retrato_operacional()
        op = self.leitura(sep)
        self.assertEqual((op["produtos"], op["resolvidos"], op["pendencias"]), (1, 0, 1))
        self.assertFalse(op["concluida"])
        self.assertIn("peso", op["itens"][0].motivo_operacional)
        self.assertEqual(antes, self.retrato_operacional())

    def test_fila_rota_checklist_edicao_get_sem_escrita(self):
        sep, venda, _, _ = self.legado(extras=11)
        antes = self.retrato_operacional()
        with CaptureQueriesContext(connection) as consultas:
            grupos = views._montar_grupos_rota_separacao([sep])
            self.assertEqual(grupos[0]["total_com_pendencia"], 0)
            self.assertTrue(grupos[0]["rota_concluida"])
            for nome, args in (("separacao_vendas_fila", []), ("separacao_venda_detalhe", [sep.pk]), ("venda_editar_revisao", [venda.pk])):
                r = self.client.get(reverse("estoque:" + nome, args=args))
                self.assertContains(r, "Separação concluída — nota pronta")
                if nome == "venda_editar_revisao":
                    self.assertContains(r, "Nota ajustada após a separação — alterações resolvidas.")
                else:
                    self.assertNotContains(r, "Nota ajustada após a separação — alterações resolvidas.")
                self.assertNotContains(r, "falta 1")
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in consultas))
        self.assertEqual(antes, self.retrato_operacional())

    def test_fila_aberta_desatualizada_nao_sincroniza_em_get(self):
        sep, _, linha = self.create("4", "UN")
        linha.quantidade = 5
        linha.save(update_fields=["quantidade"])
        antes = self.retrato_operacional()
        with CaptureQueriesContext(connection) as consultas:
            self.client.get(reverse("estoque:separacao_vendas_fila"))
        self.assertEqual(antes, self.retrato_operacional())
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in consultas))
        # A sincronização continua na operação apropriada de escrita.
        self.client.post(reverse("estoque:separacao_venda_item_salvar", args=[sep.pk, sep.itens.first().pk]), {})
        self.assertEqual(sep.itens.get().quantidade_solicitada, Decimal("5"))

    def test_salvamento_posterior_registra_prova_uma_vez_sem_reescrever_snapshots(self):
        sep, venda, a, b = self.legado()
        antes = list(sep.itens.order_by("pk").values())
        for _ in range(2):
            self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        eventos = list(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO))
        self.assertEqual(len(eventos), 2)
        self.assertTrue(all(json.loads(e.descricao)["eventos_atendidos"] for e in eventos))
        self.assertEqual(antes, list(sep.itens.order_by("pk").values()))

    def test_salvamento_atual_identifica_linhas_homonimas_por_id(self):
        sep, venda, a, b = self.montar()
        outra = ItemVenda.objects.create(venda=venda, produto=a.produto, quantidade=4,
            unidade=a.unidade, preco_unitario=10, valor_total=40)
        sep.itens.create(item_venda=outra, produto_nome_snapshot=a.produto.nome,
            unidade_snapshot=a.unidade, quantidade_solicitada=4, quantidade_separada=2,
            status="quantidade_insuficiente")
        dados = {"venda_id": venda.pk, "cliente_id": venda.cliente_id,
            "data_venda": venda.data_venda.isoformat(), "data_vencimento": venda.data_vencimento.isoformat(),
            "tipo_pagamento": "A prazo", "operador": "Teste", "itens": [
                {"item_id": linha.pk, "produto_id": linha.produto_id, "produto_nome": linha.produto.nome,
                 "quantidade": qtd, "unidade": linha.unidade, "preco_unitario": "10.00"}
                for linha, qtd in ((a, "3"), (b, "1"), (outra, "2"))]}
        r = self.client.post(reverse("estoque:gravar_venda"), data=json.dumps(dados), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        op = self.leitura(sep)
        self.assertEqual((op["produtos"], op["resolvidos"], op["pendencias"]), (3, 3, 0))
        self.assertEqual(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).count(), 3)


class SeparacaoOperacionalBrowserTests(OperacionalFixtures, StaticLiveServerTestCase):
    def test_previas_legadas_fila_checklist_edicao(self):
        out = Path(__file__).resolve().parents[1] / "amostras/separacao-final"
        out.mkdir(exist_ok=True)
        capturas = []
        with TemporaryDirectory(prefix="separacao-final-browser-", ignore_cleanup_errors=True) as profile:
            browser = Chrome(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", profile).start()
            try:
                time.sleep(1)
                with urlopen(f"http://127.0.0.1:{browser.port}/json/list") as response:
                    targets = json.load(response)
                tab = DevTools(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"))
                tab.call("Page.enable")
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                for estado, prova, parcial in (("resolvida", True, False), ("parcial", True, True), ("sem-prova", False, False)):
                    sep, venda, _, _ = self.legado(prova=prova, parcial=parcial, extras=11)
                    for width in (1440, 390, 320):
                        tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 950, "deviceScaleFactor": 1, "mobile": width < 600})
                        for tela, nome, args in (("fila", "separacao_vendas_fila", []), ("checklist", "separacao_venda_detalhe", [sep.pk]), ("edicao", "venda_editar_revisao", [venda.pk])):
                            tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:" + nome, args=args)})
                            tab.wait('document.readyState === "complete" && !!document.querySelector("main")')
                            self.assertLessEqual(tab.evaluate("document.documentElement.scrollWidth"), width)
                            if tela == "fila":
                                tab.evaluate(f"window.card=[...document.querySelectorAll('article.sep-card')].find(c=>c.querySelector('.sep-venda')?.textContent.trim()==='Nota #{venda.pk}');true")
                                texto = tab.evaluate("card.textContent")
                                self.assertEqual(tab.evaluate("card.querySelectorAll('.sep-count').length"), 3)
                                self.assertTrue(tab.evaluate("card.querySelector('.sep-counts').nextElementSibling.matches('.sep-actions')"))
                                self.assertFalse(tab.evaluate("!!card.querySelector('details[open]')"))
                                clip = tab.evaluate("(()=>{const r=card.getBoundingClientRect();return {x:0,y:r.top+scrollY,width:innerWidth,height:r.height,scale:1}})()")
                            else:
                                texto = tab.evaluate("document.querySelector('main').textContent")
                                clip = None
                            self.assertIn("Separação concluída — nota pronta" if estado == "resolvida" else "Separação com pendências", texto)
                            if tela == "checklist":
                                self.assertFalse(tab.evaluate("!!document.querySelector('[data-qtd-input],#sepSubmitBtn')"))
                                self.assertFalse(tab.evaluate("!!document.querySelector('[data-resumo-operacional]')"))
                            params = {"format": "png", "captureBeyondViewport": True}
                            if clip:
                                params["clip"] = clip
                            shot = tab.call("Page.captureScreenshot", params)["data"]
                            arquivo = f"{estado}-{tela}-{width}.png"
                            (out / arquivo).write_bytes(base64.b64decode(shot))
                            capturas.append(arquivo)
            finally:
                browser.stop()
        (out / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>Separação final — dados fictícios</title><style>body{font:16px system-ui;margin:24px}img{max-width:100%;border:1px solid #ccc}figure{margin:30px 0}</style><h1>Separação final — dados fictícios</h1>' + ''.join(f'<figure><figcaption>{n}</figcaption><a href="{n}"><img loading="lazy" src="{n}"></a></figure>' for n in capturas), encoding="utf-8")
