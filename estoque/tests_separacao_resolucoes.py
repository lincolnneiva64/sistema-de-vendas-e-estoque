"""Resolução comercial comprovada, sem reescrever a conferência física."""
import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import urlopen

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque import views
from estoque.models import Produto, ItemVenda, ContaReceber, EventoVenda
from estoque.services.pendencias_separacao import TIPO_RESOLUCAO, preparar_pendencias
from estoque.services.separacao_vendas import sincronizar_checklist_aberto_com_venda
from estoque.tests_separacao_peso_fracionado import PesoFixtures
from offline.browser_support import Chrome, DevTools


class ResolucaoFixtures(PesoFixtures):
    def montar(self, quantidade_a="3", fracionavel=False):
        sep, _, a = self.create("4", "PCT")
        if fracionavel:
            a.produto.vende_fracionado = True
            a.produto.fator_conversao = 4
            a.produto.unidade_venda_2 = "UN"
            a.produto.save()
        venda = a.venda
        venda.tipo_pagamento = "A prazo"
        venda.data_vencimento = timezone.localdate()
        venda.operador = "Teste"
        venda.total = 60
        venda.save()
        p = Produto.objects.create(nome="Cerveja B fictícia", quantidade=20, preco_compra=5,
                                    preco_vista=10, preco_prazo=10, unidade_compra="DZ", unidade_venda_1="DZ")
        b = ItemVenda.objects.create(venda=venda, produto=p, quantidade=2, unidade="DZ", preco_unitario=10, valor_total=20)
        sincronizar_checklist_aberto_com_venda(sep)
        ContaReceber.objects.create(venda=venda, cliente=venda.cliente, data_emissao=venda.data_venda,
            data_vencimento=venda.data_vencimento, valor_original=60, valor_em_aberto=60, status="aberta")
        for linha, separada in ((a, quantidade_a), (b, "1")):
            self.assertEqual(self.save(sep, sep.itens.get(item_venda=linha), status="quantidade_insuficiente", value=separada).status_code, 200)
        return sep, venda, a, b

    def salvar_nota(self, venda, a, b, quantidades=("3", "1"), ajuste=None, produto_a=None, unidade_a=None):
        itens = []
        for linha, qtd in zip((a, b), quantidades):
            produto = produto_a if produto_a and linha.pk == a.pk else linha.produto
            itens.append({"item_id": linha.pk, "produto_id": produto.pk, "produto_nome": produto.nome,
                          "quantidade": qtd, "unidade": unidade_a if linha.pk == a.pk and unidade_a else linha.unidade, "preco_unitario": "10.00"})
        dados = {"venda_id": venda.pk, "cliente_id": venda.cliente_id, "data_venda": venda.data_venda.isoformat(),
                 "data_vencimento": venda.data_vencimento.isoformat(), "tipo_pagamento": "A prazo", "operador": "Teste", "itens": itens}
        if ajuste:
            dados["ajuste_separacao_id"] = ajuste.pk
        return self.client.post(reverse("estoque:gravar_venda"), data=json.dumps(dados),
            content_type="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def fila(self, sep):
        sep.refresh_from_db()
        if hasattr(sep, "_resolucoes_quantidade"):
            del sep._resolucoes_quantidade
        views._montar_grupos_rota_separacao([sep])
        return sep


class ResolucaoSeparacaoTests(ResolucaoFixtures, TestCase):
    def test_salvar_manualmente_resolve_sem_aplicar_e_preserva_historico_estoque_financeiro(self):
        sep, venda, a, b = self.montar()
        antes = list(sep.itens.order_by("pk").values())
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        self.fila(sep)
        self.assertEqual((sep.total_pendentes, sep.total_pendencias, sep.total_resolvidos), (0, 0, 2))
        self.assertEqual(sep.status, "separada")
        self.assertTrue(sep.nota_pronta_fila)
        self.assertFalse(sep.tem_ajuste_nota)
        self.assertFalse(sep.tem_revisao_pendente_fila)
        self.assertEqual(antes, list(sep.itens.order_by("pk").values()))
        self.assertEqual(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).count(), 2)
        evento = EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).first()
        self.assertIn("Quantidade ajustada na venda — resolvido", evento.descricao_exibicao)
        self.assertIn("solicitado originalmente", evento.descricao_exibicao)
        self.assertNotIn("eventos_atendidos", evento.descricao_exibicao)
        for linha in (a, b):
            linha.produto.refresh_from_db()
            self.assertEqual(linha.produto.quantidade, Decimal("21"))
        venda.refresh_from_db()
        self.assertEqual(venda.total, Decimal("40"))
        self.assertEqual(ContaReceber.objects.get(venda=venda).valor_em_aberto, Decimal("40"))
        r = self.client.get(reverse("estoque:separacao_venda_detalhe", args=[sep.pk]))
        self.assertContains(r, "Quantidade ajustada na venda — resolvido", count=4)
        self.assertContains(r, "Solicitado: 4")
        self.assertContains(r, "Separado: 3")

    def test_aplicar_contexto_e_edicao_direta_usam_o_mesmo_criterio(self):
        sep, venda, a, b = self.montar()
        self.assertEqual(self.salvar_nota(venda, a, b, ajuste=sep).status_code, 200)
        self.assertEqual(self.fila(sep).total_resolvidos, 2)

    def test_correcao_parcial_nao_encerra_outra_falta_e_reenvio_nao_duplica(self):
        sep, venda, a, b = self.montar()
        self.assertEqual(self.salvar_nota(venda, a, b, ("3", "2")).status_code, 200)
        self.fila(sep)
        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (1, 1))
        self.assertEqual(sep.status, "com_pendencia")
        self.assertFalse(sep.nota_pronta_fila)
        self.assertTrue(sep.tem_ajuste_nota)
        self.assertFalse(sep.tem_revisao_pendente_fila)
        self.assertEqual(self.salvar_nota(venda, a, b, ("3", "2")).status_code, 200)
        self.assertEqual(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).count(), 1)
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        self.assertEqual(self.fila(sep).total_resolvidos, 2)

    def test_alteracao_nova_apos_resolucao_exige_conferencia(self):
        sep, venda, a, b = self.montar()
        self.salvar_nota(venda, a, b)
        self.assertEqual(self.salvar_nota(venda, a, b, ("5", "1")).status_code, 200)
        self.fila(sep)
        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (1, 1))
        self.assertTrue(sep.tem_revisao_pendente_fila)
        self.assertEqual(sep.itens_com_pendencia[0]["quantidade_faltante"], "2")

    def test_produto_trocado_mesmo_nome_nao_e_resolvido_automaticamente(self):
        sep, venda, a, b = self.montar()
        outro = Produto.objects.create(nome=a.produto.nome, quantidade=20, preco_compra=5,
            preco_vista=10, preco_prazo=10, unidade_compra="PCT", unidade_venda_1="PCT")
        self.assertEqual(self.salvar_nota(venda, a, b, produto_a=outro).status_code, 200)
        self.fila(sep)
        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (1, 1))
        self.assertTrue(sep.tem_revisao_pendente_fila)

    def test_troca_anterior_mesmo_nome_tambem_exige_conferencia(self):
        sep, venda, a, b = self.montar()
        outro = Produto.objects.create(nome=a.produto.nome, quantidade=20, preco_compra=5,
            preco_vista=10, preco_prazo=10, unidade_compra="PCT", unidade_venda_1="PCT")
        a.produto = outro
        a.save(update_fields=["produto"])
        for tipo, verbo in (("item_removido_da_nota", "removido"), ("item_adicionado_na_nota", "adicionado")):
            preposicao = "da" if verbo == "removido" else "na"
            EventoVenda.objects.create(venda=venda, tipo_evento=tipo,
                descricao=f"Item {verbo} {preposicao} nota: {outro.nome}, quantidade 4.000 PCT.", canal="sistema")
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        self.fila(sep)
        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (1, 1))
        self.assertTrue(sep.tem_revisao_pendente_fila)

    def test_evento_fisico_nao_relacionado_nao_e_atendido_por_correcao(self):
        sep, venda, a, b = self.montar()
        EventoVenda.objects.create(venda=venda, tipo_evento="item_adicionado_na_nota",
            descricao="Item adicionado na nota: Produto que exige conferência, quantidade 1 UN.", canal="sistema")
        self.salvar_nota(venda, a, b)
        self.fila(sep)
        self.assertTrue(sep.tem_revisao_pendente_fila)
        self.assertFalse(sep.nota_pronta_fila)
        self.assertEqual(sep.status, "com_pendencia")
        self.assertGreaterEqual(sep.total_pendencias, 1)
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="alteracao_separacao_atendida").exists())

    def test_correcao_legada_pode_ser_reconhecida_no_proximo_salvamento(self):
        sep, venda, a, b = self.montar()
        for linha, qtd in ((a, "3"), (b, "1")):
            anterior = linha.quantidade
            linha.quantidade = Decimal(qtd)
            linha.save(update_fields=["quantidade"])
            EventoVenda.objects.create(venda=venda, tipo_evento="quantidade_item_alterada",
                descricao=f"Quantidade alterada na nota: {linha.produto.nome}. De {anterior} {linha.unidade} para {qtd}.000 {linha.unidade}.", canal="sistema")
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        self.fila(sep)
        self.assertTrue(sep.nota_pronta_fila)
        self.assertEqual(sep.total_resolvidos, 2)

    def test_rollback_da_venda_tambem_desfaz_registro_de_resolucao(self):
        sep, venda, a, b = self.montar()
        with patch("estoque.views._recalcular_status_separacao_da_venda", side_effect=ValueError("Falha fictícia")):
            self.assertEqual(self.salvar_nota(venda, a, b).status_code, 400)
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).exists())
        a.refresh_from_db()
        self.assertEqual(a.quantidade, Decimal("4"))

    def test_get_nao_resolve_nem_grava_e_leitura_nao_cresce_por_item(self):
        sep, venda, a, b = self.montar()
        antes = list(EventoVenda.objects.values())
        with CaptureQueriesContext(connection) as consultas:
            self.client.get(reverse("estoque:separacao_venda_detalhe", args=[sep.pk]))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in consultas))
        self.assertEqual(antes, list(EventoVenda.objects.values()))
        itens = list(sep.itens.select_related("item_venda__produto"))
        if hasattr(sep, "_resolucoes_quantidade"):
            del sep._resolucoes_quantidade
        with self.assertNumQueries(1):
            preparar_pendencias(sep, itens)
        with self.assertNumQueries(0):
            preparar_pendencias(sep, itens * 20)

    def test_fracoes_legitimas_e_troca_de_unidade_nao_sao_confundidas(self):
        sep, venda, a, b = self.montar(quantidade_a="3.25", fracionavel=True)
        self.assertEqual(self.salvar_nota(venda, a, b, ("3.25", "1")).status_code, 200)
        self.assertEqual(self.fila(sep).total_resolvidos, 2)
        self.assertEqual(self.salvar_nota(venda, a, b, ("13", "1"), unidade_a="UN").status_code, 200)
        self.fila(sep)
        self.assertEqual(sep.total_resolvidos, 1)
        self.assertTrue(sep.tem_revisao_pendente_fila)
        self.assertEqual(sep.itens_com_pendencia[0]["status_texto"], "Necessita conferência")

    def test_atendimento_generico_nao_comprova_complementacao_fisica(self):
        sep, venda, a, b = self.montar()
        EventoVenda.objects.create(venda=venda, tipo_evento="alteracao_separacao_atendida", descricao="Atendimento genérico", canal="sistema")
        self.fila(sep)
        self.assertEqual(sep.total_resolvidos, 0)
        self.assertEqual(sep.total_pendencias, 2)
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento=TIPO_RESOLUCAO).exists())

    def test_renomear_mesmo_produto_nao_recria_falta_resolvida(self):
        sep, venda, a, b = self.montar()
        self.assertEqual(self.salvar_nota(venda, a, b).status_code, 200)
        a.produto.nome = "Nome atualizado do mesmo produto fictício"
        a.produto.save(update_fields=["nome"])
        self.fila(sep)
        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (0, 2))
        self.assertTrue(sep.nota_pronta_fila)
        self.assertFalse(views.divergencias_separacao_venda(sep))


class ResolucaoSeparacaoBrowserTests(ResolucaoFixtures, StaticLiveServerTestCase):
    def test_fila_ativa_parcial_resolvida_desktop_mobile(self):
        out = Path(__file__).resolve().parents[1] / "amostras/separacao-resolucoes"
        out.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="resolucoes-browser-", ignore_cleanup_errors=True) as profile:
            browser = Chrome(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", profile).start()
            try:
                time.sleep(1)
                with urlopen(f"http://127.0.0.1:{browser.port}/json/list") as response:
                    targets = json.load(response)
                tab = DevTools(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"))
                tab.call("Page.enable")
                tab.call("Network.enable")
                tab.call("Network.setCookie", {"name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url})
                for width in (1440, 390, 320):
                    sep, venda, a, b = self.montar()
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 950, "deviceScaleFactor": 1, "mobile": width < 600})
                    for estado, q, ativos, resolvidos in (("ativa", None, 2, 0), ("parcial", ("3", "2"), 1, 1), ("resolvida", ("3", "1"), 0, 2)):
                        if q:
                            self.assertEqual(self.salvar_nota(venda, a, b, q).status_code, 200)
                        tab.call("Page.navigate", {"url": self.live_server_url + reverse("estoque:separacao_vendas_fila")})
                        tab.wait('document.readyState==="complete" && !!document.querySelector(".sep-list")')
                        tab.evaluate(f"window.card=[...document.querySelectorAll('article.sep-card')].find(c=>c.querySelector('.sep-venda')?.textContent.trim()==='Nota #{venda.pk}');true")
                        texto = tab.evaluate("card.textContent")
                        self.assertIn("A conferir", texto)
                        self.assertIn("Problemas ativos", texto)
                        self.assertIn("Resolvidos", texto)
                        self.fila(sep)
                        self.assertEqual((sep.total_pendencias, sep.total_resolvidos), (ativos, resolvidos))
                        if resolvidos:
                            tab.evaluate("card.querySelector('details').open=true;true")
                            self.assertIn("Quantidade ajustada na venda — resolvido", texto)
                        if not ativos:
                            self.assertFalse(tab.evaluate("!!card.querySelector('.pend-list:not(details)')"))
                            self.assertNotIn("falta 1", texto)
                        self.assertLessEqual(tab.evaluate("document.documentElement.scrollWidth"), width)
                        clip = tab.evaluate("(()=>{const r=card.getBoundingClientRect();return {x:0,y:r.top+scrollY-8,width:innerWidth,height:r.height+16,scale:1}})()")
                        shot = tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})["data"]
                        (out / f"{estado}-{width}.png").write_bytes(base64.b64decode(shot))
            finally:
                browser.stop()
