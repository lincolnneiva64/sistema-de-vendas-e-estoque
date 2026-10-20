"""Consulta mobile sem mudar dados comerciais ou a selecao existente."""
import base64
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from offline.browser_support import Chrome
from estoque.models import (Produto, Fornecedor, ProdutoFornecedor, Compra, ItemCompra,
    ListaCompraFornecedor, ItemListaCompraFornecedor, ContaPagar, MovimentoFinanceiro)
from estoque.views import _produto_consulta_sugestao, _lista_fornecedor_produto_payload


class DadosSugestoes:
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor teste sugestoes")
        self.produto = Produto.objects.create(nome="Skol produto com nome muito longo para testar truncamento seguro no celular 24/600Ml",
            quantidade=Decimal("1234.567"), preco_compra=Decimal("62.25"), preco_vista=Decimal("75.90"), preco_prazo=80, unidade_compra="CX")
        self.zero = Produto.objects.create(nome="Skol Zero Estoque", quantidade=0, preco_compra=62, preco_vista=75, preco_prazo=80, unidade_compra="CX")
        self.ausente = Produto(nome="Skol Sem Precos", quantidade=Decimal("0.125"), preco_compra=None, preco_vista=0, preco_prazo=0)
        # Simula cadastro legado incompleto, exclusivamente na base descartavel.
        Produto.objects.bulk_create([self.ausente])
        ProdutoFornecedor.objects.create(produto=self.produto, fornecedor=self.fornecedor)
        self.lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor, data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(), data_fim_periodo=timezone.localdate())
        self.item = ItemListaCompraFornecedor.objects.create(lista=self.lista, produto=self.produto,
            estoque_atual=99, preco_compra=12, quantidade_final=1, preco_unitario=12, total=12)


class SugestoesProdutosDadosTests(DadosSugestoes, TestCase):
    def test_dados_atuais_na_lista_editada_sem_consultas_por_produto(self):
        with self.assertNumQueries(0):
            dados = _lista_fornecedor_produto_payload(self.produto, item=self.item, produtos_vinculados_ids=set())
        self.assertEqual(dados["consulta_atual"], {"estoque":"1234.567", "pc":"62.25", "pv":"75.90"})
        self.assertNotEqual(dados["preco_compra"], dados["consulta_atual"]["pc"])
        self.assertEqual(_produto_consulta_sugestao(SimpleNamespace(quantidade=None, preco_compra=None)), {"estoque":None,"pc":None,"pv":None})

    def test_payload_criacao_edicao_e_opcoes_compra(self):
        for url in [reverse("estoque:sugestao_compra_fornecedor")+"?fornecedor="+str(self.fornecedor.pk)+"&data_inicio="+timezone.localdate().isoformat()+"&data_fim="+timezone.localdate().isoformat(),
                    reverse("estoque:compras_lista_fornecedor_editar", args=[self.lista.pk])]:
            resposta = self.client.get(url)
            self.assertEqual(resposta.status_code, 200)
            dados = next(p for p in resposta.context["produtos_manual_payload"] if p["id"] == self.produto.pk)
            self.assertEqual(dados["consulta_atual"]["pc"], "62.25")
            self.assertEqual(dados["consulta_atual"]["estoque"], "1234.567")
        resposta = self.client.get(reverse("estoque:compras_nova"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'data-consulta-pc=""')


class SugestoesProdutosBrowserTests(DadosSugestoes, StaticLiveServerTestCase):
    def test_mobile_desktop_dados_visiveis_selecao_e_sem_mutacoes(self):
        self.client.force_login(get_user_model().objects.create_superuser("sugestoes", "teste@example.test", "somente-teste"))
        modelos = [Produto, Compra, ItemCompra, ListaCompraFornecedor, ItemListaCompraFornecedor, ContaPagar, MovimentoFinanceiro]
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        evidence = Path(__file__).resolve().parents[1]/"diagnostics"/"sugestoes-produtos-mobile"
        evidence.mkdir(parents=True, exist_ok=True)
        rotas = [("compra", reverse("estoque:compras_nova")),
                 ("lista", reverse("estoque:sugestao_compra_fornecedor")+"?fornecedor="+str(self.fornecedor.pk)+"&data_inicio="+timezone.localdate().isoformat()+"&data_fim="+timezone.localdate().isoformat()),
                 ("lista-edicao", reverse("estoque:compras_lista_fornecedor_editar", args=[self.lista.pk]))]
        with TemporaryDirectory(prefix="sugestoes-mobile-") as profile:
            chrome = Chrome(r"C:\Program Files\Google\Chrome\Application\chrome.exe", profile).start()
            try:
                for width in [390, 320, 1440]:
                    for nome, rota in rotas:
                        with self.subTest(width=width, tela=nome):
                            tab = chrome.tab()
                            tab.call("Network.enable")
                            tab.call("Network.setBlockedURLs", {"urls":["https://*"]})
                            tab.call("Network.setCookie", {"name":"sessionid", "value":self.client.cookies["sessionid"].value,"url":self.live_server_url})
                            tab.call("Emulation.setDeviceMetricsOverride", {"width":width,"height":844,"deviceScaleFactor":1,"mobile":width<860})
                            tab.call("Page.navigate", {"url":self.live_server_url+rota})
                            tab.wait('document.readyState === "complete"')
                            selector = ('#tbodyAdicionarProdutoMobile [name="produto_busca[]"]' if width<860 else '#tabelaItensCompra [name="produto_busca[]"]') if nome=="compra" else '#produtoManualBuscaSugestao'
                            if nome != "compra":
                                tab.wait('!!document.getElementById("btnToggleAdicionarProdutosMobile")')
                                tab.evaluate('document.getElementById("btnToggleAdicionarProdutosMobile").click();true')
                            tab.wait('!![...document.querySelectorAll('+json.dumps(selector)+')].find(e=>e.getClientRects().length)')
                            tab.evaluate('window.busca=[...document.querySelectorAll('+json.dumps(selector)+')].find(e=>e.getClientRects().length);busca.focus();busca.select();true')
                            tab.call("Input.insertText", {"text":"Skol"})
                            tab.wait('[...document.querySelectorAll(".spc-'+('compra' if nome=='compra' else 'lista')+'")].filter(e=>e.getClientRects().length).length>=3')
                            target = '.spc-compra' if nome=='compra' else '.spc-lista'
                            tab.evaluate('window.botao=[...document.querySelectorAll('+json.dumps(target)+')].find(e=>e.getClientRects().length && e.dataset.produtoId==='+json.dumps(str(self.produto.pk))+');botao.scrollIntoView({block:"center",behavior:"instant"});true')
                            metrics = tab.evaluate(r'''(()=>{
                              const r=botao.querySelector('.spc-mobile'), n=r.firstElementChild;
                              const luminancia=cor=>{
                                const rgb=cor.match(/[\d.]+/g).slice(0,3).map(v=>{
                                  v=Number(v)/255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;
                                });
                                return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
                              };
                              const contraste=(texto,fundo)=>{
                                const a=luminancia(texto), b=luminancia(fundo);
                                return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
                              };
                              return {valores:[...r.children].slice(1).map(e=>e.textContent), title:botao.title,
                                mobile:!!r.getClientRects().length, desktop:!!botao.querySelector('.spc-desktop').getClientRects().length,
                                altura:botao.getBoundingClientRect().height, truncado:n.scrollWidth>n.clientWidth,
                                larguraNome:n.getBoundingClientRect().width,
                                fonteValores:parseFloat(getComputedStyle(r.children[1]).fontSize),
                                gap:parseFloat(getComputedStyle(r).columnGap),
                                separacaoNome:r.children[1].getBoundingClientRect().left-n.getBoundingClientRect().right,
                                contrasteMinimo:Math.min(...[...r.children].slice(1).flatMap(e=>{
                                  const s=getComputedStyle(e), l=getComputedStyle(e.firstElementChild);
                                  return [contraste(s.color,s.backgroundColor),contraste(l.color,s.backgroundColor)];
                                })),
                                valoresCompletos:[...r.children].slice(1).every(e=>e.scrollWidth<=e.clientWidth+1 && e.getBoundingClientRect().right<=r.getBoundingClientRect().right+1),
                                mesmaLinha:[...r.children].every(e=>{
                                  const a=e.getBoundingClientRect(), b=n.getBoundingClientRect();
                                  return Math.abs((a.top+a.bottom)/2-(b.top+b.bottom)/2)<1;
                                }),
                                overflow:document.documentElement.scrollWidth>innerWidth,
                                sobreposto:n.getBoundingClientRect().right>r.children[1].getBoundingClientRect().left};
                            })()''')
                            self.assertEqual(metrics["valores"], ["Est: 1.234,567", "PC: 62,25", "PV: 75,90"])
                            self.assertEqual(metrics["title"], self.produto.nome)
                            self.assertEqual(metrics["mobile"], width<860)
                            self.assertEqual(metrics["desktop"], width>=860)
                            if width<860:
                                self.assertTrue(metrics["mesmaLinha"], metrics)
                                self.assertTrue(metrics["truncado"], metrics)
                                self.assertFalse(metrics["sobreposto"], metrics)
                                self.assertLessEqual(metrics["altura"], 42)
                                self.assertFalse(metrics["overflow"], metrics)
                                self.assertTrue(metrics["valoresCompletos"], metrics)
                                self.assertGreaterEqual(metrics["fonteValores"], 11.5)
                                self.assertGreaterEqual(metrics["separacaoNome"], 12)
                                self.assertGreaterEqual(metrics["contrasteMinimo"], 7)
                            self.assertEqual(tab.evaluate('[...document.querySelector('+json.dumps(target+'[data-produto-id="'+str(self.ausente.pk)+'"]')+').querySelectorAll(".spc-valor")].map(e=>e.textContent)'), ["Est: 0,125", "PC: —", "PV: —"])
                            tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,600));botao.scrollIntoView({block:"center",behavior:"instant"});await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
                            tab.wait('(()=>{const r=botao.getBoundingClientRect();return botao.isConnected && r.top>=0 && r.bottom<=innerHeight})()')
                            shot=tab.call("Page.captureScreenshot", {"format":"png"})["data"]
                            (evidence/f"{nome}-{width}.png").write_bytes(base64.b64decode(shot))
                            (evidence/f"{nome}-{width}.json").write_text(json.dumps(metrics,indent=2),encoding="utf-8")
                            tab.evaluate('botao.click();true')
                            if nome=="compra":
                                self.assertEqual(tab.evaluate('busca.closest(".linha-item").querySelector(\'[name="produto_id[]"]\').value'),str(self.produto.pk))
                            else:
                                self.assertEqual(tab.evaluate('document.getElementById("produtoManualSugestao").value'),str(self.produto.pk))
            finally:
                chrome.stop()
        self.assertEqual(antes, {m:list(m.objects.order_by("pk").values()) for m in modelos})
