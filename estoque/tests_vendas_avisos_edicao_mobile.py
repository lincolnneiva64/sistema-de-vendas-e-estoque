"""Aviso e edicao em DOM isolado: sem banco, rede ou operacoes reais de venda."""
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from offline.browser_support import Chrome
from estoque.tests_vendas_rascunhos_mobile import BROWSER, ROOT, TEMPLATE, function


@unittest.skipUnless(Path(BROWSER).is_file(), "Configure OFFLINE_TEST_CHROME para Chrome/Edge")
class AvisosEdicaoMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="vendas-aviso-edicao-", ignore_cleanup_errors=True)
        cls.browser = Chrome(BROWSER, str(Path(cls.temp.name) / "profile"))
        original_popen = subprocess.Popen
        with patch("offline.browser_support.subprocess.Popen", side_effect=lambda cmd, **kwargs:
                   original_popen([cmd[0], "--no-sandbox", *cmd[1:]], **kwargs)):
            cls.browser.start()

    @classmethod
    def tearDownClass(cls):
        cls.browser.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.tab = self.browser.tab()

    def tearDown(self):
        self.tab.call("Page.close")
        self.tab.socket.close()

    def evaluate(self, expression):
        return self.tab.evaluate(expression)

    def prepare(self, width=400, published=False, full_page=False, height=850, source_override=None):
        source = source_override or (subprocess.check_output([
            "git", "show", "243d1d86aee24b909e64d6bfbd8dfc7443fbb16f:estoque/templates/estoque/vendas_layout_teste.html"
        ], cwd=ROOT).decode("utf-8") if published else TEMPLATE.read_text(encoding="utf-8"))
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", source, re.S))
        names = [
            "mostrarAviso", "obterProdutoOptionVenda", "obterProdutoIdLinhaVenda", "linhaTemProdutoVenda",
            "encontrarLinhaProdutoDuplicadoVenda", "bloquearProdutoDuplicadoVenda", "obterNomeProdutoLinhaVenda",
            "existeLinhaProdutoPendenteVenda", "refreshPendingProductLine", "setPendingProductLineVisible",
            "atualizarBotaoEdicaoItemVenda", "limparEstadoEdicaoItemVenda", "limparCamposProdutoVenda", "adicionarItemVendaAtual", "navegacaoMobileVenda",
        ]
        setup = "\n".join(function(source, name) for name in names)
        if "function mostrarFaixaEdicaoItemMobile(" in source:
            setup += function(source, "mostrarFaixaEdicaoItemMobile")
            setup += re.search(r'^\(window.visualViewport \|\| window\).addEventListener[^\n]+', source, re.M)[0]
        start = source.index('const pendingProductLine = document.createElement("div")')
        setup += source[start:source.index('const historicoProdutoCliente', start)]
        for name in ["commitPendingProduct", "restorePendingProduct", "cancelPendingProduct"]:
            start = source.index(name + '.addEventListener("click"')
            setup += source[start:source.index("\n});", start) + 5]
        start = source.index('tabelaBody.addEventListener("click", function (e)')
        setup += source[start:source.index("\nfunction mostrarAviso", start)]
        setup += re.search(r'btnAdicionarItemVenda.addEventListener\("click", \(\) => \{.*?\n    \}\);', source, re.S)[0]
        prelude = """
            const produtoBusca=document.getElementById('produtoBusca'),produto=document.getElementById('produto');
            const quantidade=document.getElementById('quantidade'),unidade=document.getElementById('unidade'),preco=document.getElementById('preco');
            const tabelaBody=document.getElementById('tabelaProdutos'),btnAdicionarItemVenda=document.getElementById('btnAdicionarItemVenda');
            const custoVenda=null,precoMargemCusto=null,estoquePreview=document.createElement('span');
            let linhaSelecionada=null,linhaProdutoIniciadaVenda=false,preenchendoEdicaoProdutoVenda=false,produtoAutoPreenchidoVenda=false;
            let vendaEdicaoBloqueada=false,ultimoProdutoLancadoVenda='';
            window.salesOffline={active:false};window.updates=0;window.saves=0;
            window.fetch=()=>{throw Error('Rede bloqueada no teste isolado')};
            function normalizarTextoVendaEstoque(text){return String(text).toLowerCase();}
            function normalizarNumeroVenda(text){return String(text).replace(',','.');}
            function quantidadeInformadaValida(){return Number(quantidade.value)>0;}
            function atualizarEstoqueProdutoSelecionado(){}function atualizarCustoMargemVenda(){}
            function mostrarMsgEdicao(){}function ocultarHistoricoProdutoCliente(){}function resetarAutocompleteProduto(){}
            function garantirTipoVendaEdicaoConsumoProprio(){}function fecharTecladoVendaMobile(){}
            function validarPrecoAcimaDoCustoVenda(){return true;}function validarEstoqueItemVendaAtual(){return true;}
            function criarSnapshotVisualEstoqueItemVenda(){return {};}
            function atualizarTotalGeral(){}function focarProximoCampo(campo){campo.focus();}
            function atualizarLinhaItemVenda(row,item){updates++;row.dataset.quantidade=item.quantidade;row.dataset.precoUnitario=item.preco_unitario;}
            function mostrarToastVenda(message){throw Error(message);}
        """
        expose = """
            window.parts={pendingProductLine,pendingProductMessage,commitPendingProduct,cancelPendingProduct,restorePendingProduct};
            window.setName=(name)=>{produto.options[1].value=name;produto.options[1].textContent=name;tabelaBody.rows[0].dataset.produtoNome=name;tabelaBody.rows[0].cells[0].textContent=name;};
            window.warn=()=>bloquearProdutoDuplicadoVenda('1',produto.options[1].value);
            window.state=()=>({editing:!!linhaSelecionada,label:btnAdicionarItemVenda.textContent,rows:tabelaBody.rows.length});
        """
        dom = """<div id='layout-vendas'><div class='linha'><div class='produto-autocomplete-wrap'>
            <input id='produtoBusca'></div><select id='produto'><option value=''></option>
            <option data-produto-id='1' data-unidade1='UN' data-preco='10'>Produto isolado</option></select>
            <input id='quantidade'><select id='unidade'><option>UN</option></select><input id='preco'>
            <button type='button' id='btnAdicionarItemVenda'>Adicionar Item</button></div>
            <div class='grade-itens'><div class='bloco-itens-scroll'><table class='grade-itens-body'><tbody id='tabelaProdutos'><tr data-produto-id='1' data-produto-nome='Produto isolado' data-quantidade='2' data-unidade='UN' data-preco-unitario='10'>
            <td>Produto isolado</td><td>2</td><td>UN</td><td>10</td></tr></tbody></table></div></div></div>"""
        page = "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>" + css + "</style></head><body>" + dom + "<script>" + prelude + setup + expose + "</script></body></html>"
        if full_page:
            from django.conf import settings
            from django.template import Context, Engine
            if not settings.configured:
                settings.configure(USE_I18N=False, USE_TZ=False)

            def expand(text):
                return re.sub(r'''{% include ["']([^"']+)["'][^%]*%}''',
                              lambda m: expand((ROOT / "estoque/templates" / m[1]).read_text(encoding="utf-8")), text)

            template = expand(source)
            template = re.sub(r'<script\b[^>]*>.*?</script>', '', template, flags=re.S)
            template = re.sub(r'{% (?:url|static) .*?%}', '#', template)
            template = re.sub(r'{% load .*?%}|{% get_current_timezone .*?%}', '', template)
            template = re.sub(r'<link[^>]*href="#\?v=8"[^>]*>', '<style>' +
                              (ROOT / 'static/offline/indicator.css').read_text(encoding='utf-8') + '</style>', template)
            page = Engine(builtins=['django.templatetags.l10n', 'django.templatetags.tz']).from_string(template).render(
                Context({'locacoes_operacionais': {}, 'operadores_venda': [{'nome': 'Operador isolado'}]}))
            row = re.search(r'<tr .*?</tr>', dom, re.S)[0]
            seed = """
                const option=new Option('Produto isolado','Produto isolado');
                Object.assign(option.dataset,{produtoId:'1',unidade1:'UN',preco:'10'});
                document.getElementById('produto').add(option);
                document.getElementById('clienteResumo').classList.add('visivel');
                document.getElementById('tabelaProdutos').innerHTML=
            """ + json.dumps(row) + ";"
            page += "<script>" + seed + prelude + setup + expose + "</script>"
        path = Path(self.temp.name) / "fixture.html"
        path.write_text(page, encoding="utf-8")
        self.tab.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width <= 980})
        self.tab.call("Page.navigate", {"url": path.as_uri()})
        self.tab.wait("document.readyState==='complete' && !!window.parts")

    def visible(self, expression):
        return self.evaluate("!!(" + expression + ").getClientRects().length")

    def test_duplicate_warning_names_fit_and_ok_keeps_original_focus(self):
        names = ["Skol Lata 12/350Ml", "Produto " + "com nome muito comprido " * 12, "Produto" + "SemEspacos" * 30]
        for width in (320, 360, 390, 400, 430):
            for name in names:
                with self.subTest(width=width, name=name[:25]):
                    self.prepare(width)
                    self.evaluate("setName(" + json.dumps(name) + ");warn()")
                    result = self.evaluate("""(()=>{const box=document.getElementById('aviso-validacao'),text=document.getElementById('aviso-validacao-texto'),button=box.querySelector('button');
                        return {left:box.getBoundingClientRect().left,right:box.getBoundingClientRect().right,
                            okLeft:button.getBoundingClientRect().left,okRight:button.getBoundingClientRect().right,okBottom:button.getBoundingClientRect().bottom,
                            text:text.textContent,textOverflow:text.scrollWidth>text.clientWidth+1,scroll:document.documentElement.scrollWidth};})()""")
                    self.assertGreaterEqual(result["left"], 0)
                    self.assertLessEqual(result["right"], width)
                    self.assertGreaterEqual(result["okLeft"], 0)
                    self.assertLessEqual(result["okRight"], width)
                    self.assertLessEqual(result["okBottom"], 850)
                    self.assertIn(name, result["text"])
                    self.assertFalse(result["textOverflow"])
                    self.assertLessEqual(result["scroll"], width)
                    self.evaluate("document.querySelector('#aviso-validacao button').click()")
                    self.assertFalse(self.evaluate("!!document.getElementById('aviso-validacao')"))
                    self.assertEqual(self.evaluate("document.activeElement.id"), "produtoBusca")
                    self.assertEqual(self.evaluate("tabelaBody.rows.length"), 1)
        self.prepare(400, published=True)
        self.evaluate("warn()")
        self.assertLess(self.evaluate("document.getElementById('aviso-validacao').getBoundingClientRect().left"), 0)

    def test_edit_cancel_and_update_use_existing_handlers(self):
        self.prepare()
        self.evaluate("tabelaBody.rows[0].click()")
        self.assertEqual(self.evaluate("state().label"), "Atualizar Item")
        self.assertEqual(self.evaluate("getComputedStyle(parts.pendingProductMessage,'::before').content"), '"Editando item"')
        self.assertEqual(self.evaluate("getComputedStyle(parts.pendingProductMessage).fontSize"), "0px")
        self.assertFalse(self.visible("parts.commitPendingProduct"))
        self.assertTrue(self.visible("parts.cancelPendingProduct"))
        self.evaluate("parts.cancelPendingProduct.click()")
        self.assertFalse(self.evaluate("state().editing"))
        self.assertEqual(self.evaluate("state().label"), "Adicionar Item")
        self.assertFalse(self.visible("parts.pendingProductLine"))
        self.assertEqual(self.evaluate("tabelaBody.rows[0].dataset.quantidade"), "2")
        self.evaluate("tabelaBody.rows[0].click();quantidade.value='3';btnAdicionarItemVenda.click()")
        self.assertEqual(self.evaluate("updates"), 1)
        self.assertEqual(self.evaluate("tabelaBody.rows[0].dataset.quantidade"), "3")
        self.assertEqual(self.evaluate("state().rows"), 1)
        self.assertEqual(self.evaluate("state().label"), "Adicionar Item")
        self.assertFalse(self.visible("parts.pendingProductLine"))
        self.assertEqual(self.evaluate("saves"), 0)

    def test_restore_and_unfinished_addition_remain_available(self):
        self.prepare()
        self.evaluate("tabelaBody.rows[0].click();produtoBusca.value='Outra busca';refreshPendingProductLine()")
        self.assertTrue(self.visible("parts.restorePendingProduct"))
        self.assertFalse(self.visible("parts.commitPendingProduct"))
        self.evaluate("parts.restorePendingProduct.click()")
        self.assertEqual(self.evaluate("produtoBusca.value"), "Produto isolado")
        self.assertEqual(self.evaluate("document.activeElement.id"), "quantidade")
        self.assertEqual(self.evaluate("updates"), 0)
        self.evaluate("limparEstadoEdicaoItemVenda();quantidade.value='';linhaProdutoIniciadaVenda=true;refreshPendingProductLine()")
        self.assertTrue(self.visible("parts.commitPendingProduct"))
        self.assertEqual(self.evaluate("parts.commitPendingProduct.textContent"), "Adicionar")

    def test_desktop_matches_published_warning_and_editing_controls(self):
        snapshot = """(()=>{const items=[document.getElementById('aviso-validacao'),document.getElementById('aviso-validacao-texto'),parts.pendingProductLine,parts.pendingProductMessage,parts.commitPendingProduct,parts.cancelPendingProduct];
            return items.map(e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return {rect:[r.x,r.y,r.width,r.height],css:Array.from(s).map(p=>[p,s.getPropertyValue(p)])};});})()"""
        for width in (981, 1366):
            with self.subTest(width=width):
                self.prepare(width, published=True)
                self.evaluate("tabelaBody.rows[0].click();warn()")
                before = self.evaluate(snapshot)
                self.prepare(width)
                self.evaluate("tabelaBody.rows[0].click();warn()")
                self.assertEqual(self.evaluate(snapshot), before)
                self.assertTrue(self.visible("parts.commitPendingProduct"))

    def test_editing_strip_and_cancel_stay_visible_after_keyboard_resize(self):
        for width in (320, 360, 390, 400, 430):
            with self.subTest(width=width):
                self.prepare(width, full_page=True)
                self.evaluate("document.querySelector('.bloco-itens-scroll').scrollIntoView();tabelaBody.rows[0].click()")
                self.evaluate("(async()=>{await new Promise(r=>setTimeout(r,250));return true;})()")
                self.tab.call('Emulation.setDeviceMetricsOverride', {
                    'width': width, 'height': 350, 'deviceScaleFactor': 1, 'mobile': True,
                })
                self.evaluate("(async()=>{await new Promise(r=>setTimeout(r,100));return true;})()")
                geometry = self.evaluate("""(()=>{const field=produtoBusca.getBoundingClientRect(),strip=parts.pendingProductLine.getBoundingClientRect(),button=parts.cancelPendingProduct.getBoundingClientRect();
                    const viewport=visualViewport;return {fieldTop:field.top,fieldBottom:field.bottom,top:strip.top,bottom:strip.bottom,
                        viewportTop:viewport.offsetTop,viewportBottom:viewport.offsetTop+viewport.height,
                        hit:document.elementFromPoint(button.left+button.width/2,button.top+button.height/2)===parts.cancelPendingProduct,
                        label:getComputedStyle(parts.pendingProductMessage,'::before').content};})()""")
                self.assertGreaterEqual(geometry['fieldTop'], geometry['viewportTop'])
                self.assertGreaterEqual(geometry['top'], geometry['fieldBottom'])
                self.assertLessEqual(geometry['top'] - geometry['fieldBottom'], 8)
                self.assertLessEqual(geometry['bottom'], geometry['viewportBottom'])
                self.assertTrue(geometry['hit'])
                self.assertEqual(geometry['label'], '"Editando item"')
                self.assertEqual(self.evaluate("document.activeElement.id"), "produtoBusca")
                self.assertEqual(self.evaluate("state().label"), "Atualizar Item")
                self.assertFalse(self.visible("parts.commitPendingProduct"))
                self.evaluate("parts.cancelPendingProduct.click()")
                self.assertEqual(self.evaluate("state().label"), "Adicionar Item")
                self.assertFalse(self.visible("parts.pendingProductLine"))

    def test_quantity_focus_does_not_force_scroll_back_to_product(self):
        self.prepare(full_page=True, height=350)
        self.evaluate("tabelaBody.rows[0].click()")
        self.evaluate("(async()=>{await new Promise(r=>setTimeout(r,100));return true;})()")
        self.evaluate("quantidade.focus({preventScroll:true});window.savedScroll=scrollY")
        self.tab.call('Emulation.setDeviceMetricsOverride', {'width': 400, 'height': 330, 'deviceScaleFactor': 1, 'mobile': True})
        self.evaluate("(async()=>{await new Promise(r=>setTimeout(r,100));return true;})()")
        self.assertEqual(self.evaluate("scrollY"), self.evaluate("savedScroll"))
        self.assertEqual(self.evaluate("document.activeElement.id"), "quantidade")


if __name__ == "__main__":
    unittest.main()
