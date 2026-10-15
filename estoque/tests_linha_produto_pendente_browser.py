"""Regressões da linha de produto na página completa, com SQLite descartável."""
import copy
from contextlib import contextmanager
from decimal import Decimal
import json
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from .models import Cliente, Funcionario, ItemVenda, Produto, Venda
from .tests_previa_financeira_browser import CHROME, navegador_isolado


@skipUnless(CHROME.is_file(), "Chrome indisponível")
class LinhaProdutoPendenteBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        from django.conf import settings
        templates = copy.deepcopy(settings.TEMPLATES)
        processors = templates[0]["OPTIONS"]["context_processors"]
        if "estoque.context_processors.ambiente_sistema" not in processors:
            processors.append("estoque.context_processors.ambiente_sistema")
        override = override_settings(TEMPLATES=templates)
        override.enable()
        self.addCleanup(override.disable)
        user = get_user_model().objects.create_user("linha_ficticia", password="teste-local")
        user.user_permissions.add(Permission.objects.get(content_type__app_label="offline", codename="registrar_observacao"))
        self.cliente = Cliente.objects.create(nome="Cliente Ficticio da Regressao", prazo_padrao_dias=7)
        Funcionario.objects.create(nome="Operador Teste", pode_operar_sistema=True)
        self.produtos = [Produto.objects.create(
            nome=nome, quantidade=100, unidade_compra="UN", unidade_venda_1="UN",
            preco_compra=1, preco_vista=Decimal(preco), preco_prazo=Decimal(preco),
            estoque_conferido=True, preco_conferido=True,
        ) for nome, preco in [
            ("A Produto Teste", "9.90"), ("B Produto Teste", "19.50"),
            ("Sacola Teste", "10.00"), ("Z Produto Iniciado", "5.00"),
        ]]

    @contextmanager
    def montagem(self):
        with navegador_isolado() as tab:
            tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": """
window.posts=[];window.resultadoVenda=null;
const original=window.fetch;
window.fetch=async(...args)=>{
 const url=String(args[0]);
 const venda=args[1]?.method==='POST' && url.includes('/api/vendas/online/');
 if(venda)posts.push(JSON.parse(args[1].body));
 const response=await original(...args);
 if(venda)resultadoVenda=await response.clone().json();
 return response;
};
"""})
            tab.call("Page.navigate", {"url": self.live_server_url + "/offline/login/?next=/vendas/"})
            tab.wait('!!document.getElementById("id_username")')
            tab.evaluate("document.getElementById('id_username').value='linha_ficticia';document.getElementById('id_password').value='teste-local';document.querySelector('form').requestSubmit();true")
            tab.wait('location.pathname==="/vendas/" && window.salesDraftUI?.ready===true', timeout=20)
            tab.evaluate("await(await import([...document.scripts].find(s=>s.src.includes('/app.js')).src)).initialized;true")
            tab.evaluate("clienteBusca.value='Cliente Ficticio';clienteBusca.dispatchEvent(new Event('input',{bubbles:true}));true")
            tab.wait('!!document.querySelector(".cliente-sugestao-item")')
            tab.evaluate('document.querySelector(".cliente-sugestao-item").click();operadorVenda.value="Operador Teste";tipoVenda.value="A prazo";tipoVenda.dispatchEvent(new Event("change",{bubbles:true}));true')
            for indice, produto in enumerate(self.produtos[:3], 1):
                tab.evaluate('produtoBusca.value=' + json.dumps(produto.nome) + ';produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('!!document.querySelector(".produto-sugestao-item")')
                tab.evaluate('document.querySelector(".produto-sugestao-item").click();quantidade.value="1";quantidade.dispatchEvent(new Event("input",{bubbles:true}));btnAdicionarItemVenda.click();true')
                tab.wait(f'document.querySelectorAll("#tabelaProdutos tr").length==={indice}')
            self.assertEqual(tab.evaluate('salesDraftBridge.capture().itens.length'), 3)
            self.assertEqual(tab.evaluate('salesDraftBridge.capture().lancamento'), None)
            yield tab

    def gravar_e_conferir(self, tab):
        itens = tab.evaluate('salesDraftBridge.capture().itens')
        tab.evaluate('btnGravarVenda.click();true')
        tab.wait('window.resultadoVenda?.sucesso===true', timeout=15)
        venda = Venda.objects.get()
        self.assertEqual(venda.total, Decimal("39.40"))
        self.assertEqual(venda.cliente_id, self.cliente.pk)
        self.assertEqual(ItemVenda.objects.count(), 3)
        self.assertEqual(sorted(ItemVenda.objects.values_list("produto_id", "quantidade")), sorted((p.pk, Decimal("1")) for p in self.produtos[:3]))
        enviado = tab.evaluate('posts[0].payload.itens')
        self.assertEqual(len(enviado), len(itens))
        self.assertEqual(len(tab.evaluate('posts')), 1)
        # Novo clique não gera outra venda nem incrementa quantidades.
        tab.evaluate('btnGravarVenda.click();true')
        self.assertEqual(Venda.objects.count(), 1)
        for produto in self.produtos[:3]:
            produto.refresh_from_db()
            self.assertEqual(produto.quantidade, Decimal("99"))
        self.produtos[3].refresh_from_db()
        self.assertEqual(self.produtos[3].quantidade, Decimal("100"))

    def test_texto_pesquisado_sem_selecao_nao_bloqueia_venda(self):
        with self.montagem() as tab:
            tab.evaluate('produto.value="";produtoBusca.value="sacola";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
            self.assertIsNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            self.gravar_e_conferir(tab)

    def test_produto_selecionado_sem_iniciar_inclusao_nao_e_adicionado_ao_gravar(self):
        with self.montagem() as tab:
            # Confirma explicitamente o próximo produto, sem começar a inclusão.
            tab.evaluate('produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));true')
            tab.wait('!!document.querySelector(".produto-sugestao-item")')
            tab.evaluate('document.querySelector(".produto-sugestao-item").click();true')
            self.assertEqual(tab.evaluate('produto.value'), self.produtos[3].nome)
            self.assertEqual(tab.evaluate('quantidade.value'), "")
            self.assertIsNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            self.gravar_e_conferir(tab)

    def test_inclusao_iniciada_incompleta_e_restaurada_continua_bloqueada(self):
        with self.montagem() as tab:
            tab.evaluate('quantidade.value="2";quantidade.dispatchEvent(new Event("input",{bubbles:true}));quantidade.value="";quantidade.dispatchEvent(new Event("input",{bubbles:true}));true')
            self.assertIsNotNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            # Captura/restauração preserva a inclusão mesmo com quantidade apagada.
            tab.evaluate('window.pendente=salesDraftBridge.capture();salesDraftBridge.restore(pendente);btnGravarVenda.click();true')
            tab.wait('document.getElementById("vendaErroOverlay").textContent.includes("Informe a quantidade da linha pendente")')
            self.assertEqual(Venda.objects.count(), 0)
            self.assertEqual(tab.evaluate('posts.length'), 0)
            self.assertIsNotNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            tab.evaluate('btnVendaErroOk.click();[...document.querySelectorAll("#venda-linha-produto-pendente button")].find(b=>b.textContent==="Cancelar linha").click();true')
            self.assertIsNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            self.gravar_e_conferir(tab)

    def test_residuo_de_produto_ja_adicionado_nao_bloqueia_nem_duplica_itens(self):
        with self.montagem() as tab:
            # Campo superior residual diverge do select que ainda aponta ao próximo produto.
            tab.evaluate('produtoBusca.value="Sacola Teste";true')
            self.assertNotEqual(tab.evaluate('produtoBusca.value'), tab.evaluate('produto.value'))
            self.assertIsNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            self.gravar_e_conferir(tab)

    def test_item_com_quantidade_nao_e_incluido_automaticamente_ao_gravar(self):
        with self.montagem() as tab:
            tab.evaluate('quantidade.value="2";quantidade.dispatchEvent(new Event("input",{bubbles:true}));btnGravarVenda.click();true')
            tab.wait('document.getElementById("vendaErroOverlay").textContent.includes("ainda não foi adicionada")')
            self.assertEqual(Venda.objects.count(), 0)
            self.assertEqual(tab.evaluate('posts.length'), 0)
            self.assertEqual(tab.evaluate('salesDraftBridge.capture().lancamento.quantidade'), "2")
            self.assertEqual(tab.evaluate('salesDraftBridge.capture().itens.length'), 3)

    def test_tentativa_de_adicionar_sem_quantidade_nao_e_descartada_ao_limpar_busca(self):
        with self.montagem() as tab:
            tab.evaluate('btnAdicionarItemVenda.click();produtoBusca.value="";produtoBusca.dispatchEvent(new Event("input",{bubbles:true}));btnGravarVenda.click();true')
            tab.wait('document.getElementById("vendaErroOverlay").textContent.includes("Informe a quantidade da linha pendente")')
            self.assertEqual(Venda.objects.count(), 0)
            self.assertIsNotNone(tab.evaluate('salesDraftBridge.capture().lancamento'))
            self.assertFalse(tab.evaluate('document.getElementById("venda-linha-produto-pendente").hidden'))
