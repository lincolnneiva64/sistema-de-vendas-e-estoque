"""Eventos reais do template em Edge isolado; sem Django, banco ou rede."""
import json
import re
import unittest

from estoque.tests_vendas_avisos_edicao_mobile import AvisosEdicaoMobileTests
from estoque.tests_vendas_rascunhos_mobile import TEMPLATE, function


class EnterMobileTests(AvisosEdicaoMobileTests):
    def prepare_flow(self, width=400, original=False):
        super().prepare(width=width)
        source = TEMPLATE.read_text(encoding='utf-8')
        if original:
            import subprocess
            source = subprocess.check_output(['git', 'show', 'b7061e141ec4fab7117095caafad98699e80d644:estoque/templates/estoque/vendas_layout_teste.html'], cwd=TEMPLATE.parents[3]).decode('utf-8')
        self.evaluate('''document.body.insertAdjacentHTML('afterbegin', `<input id="clienteBusca"><input id="dataVenda" type="date"><input id="vencimentoVenda" type="date"><select id="tipoVenda"><option value="prazo">Prazo</option></select><select id="operadorVenda"><option value=""></option><option value="1">Operador</option></select><button id="btnGravarVenda">Gravar Venda</button><button id="btnConfirmarFechamentoVenda">Confirmar</button><button id="btnConfirmarConfirmacaoVenda">Confirmar</button><div id="clienteSugestoesBox"></div>`);
        ''')
        setup = '''
        const clienteBusca=document.getElementById('clienteBusca'),dataVenda=document.getElementById('dataVenda'),vencimentoVenda=document.getElementById('vencimentoVenda'),tipoVenda=document.getElementById('tipoVenda'),operadorVenda=document.getElementById('operadorVenda'),clienteSugestoesBox=document.getElementById('clienteSugestoesBox');
        let clientesSugestoes=[{id:1,nome:'Cliente teste'}],clienteIndexAtivo=0,clienteSelecionado={id:1};
        window.operatorWarnings=0;window.addCalls=0;window.gravacoes=0;
        window.mostrarToastVenda=(message)=>{window.lastError=message};
        function mostrarModalOperadorObrigatorio(){operatorWarnings++;}
        function vendaEdicaoConsumoProprio(){return false;}
        function clienteSelecionadoValido(){return !!clienteSelecionado.id;}
        function preencherResumoCliente(c){clienteSelecionado=c;clienteBusca.value=c.nome;clienteSugestoesBox.style.display='none';}
        function buscarClientes(){}function renderizarSugestoesClientes(){}
        function produtoAtualPermiteEscolhaUnidadeVenda(){return true;}
        function bloquearQuantidadeInvalida(){lastError='Quantidade';quantidade.focus();}
        function criarLinhaItemVenda(item){const tr=document.createElement('tr');tr.dataset.produtoId=item.produto_id;tr.dataset.quantidade=item.quantidade;tr.innerHTML='<td>'+item.produto_nome+'</td><td>'+item.quantidade+'</td><td>'+item.unidade+'</td><td>'+item.preco_unitario+'</td>';return tr;}
        function preencherProximoProdutoDepoisDeAdicionar(){produtoBusca.focus();return false;}
        let ultimoItemRemovido=null;
        btnGravarVenda.addEventListener('click',()=>gravacoes++);
        tabelaBody.innerHTML='';
        '''
        setup += function(source, 'focarProximoCampo') + function(source, 'configurarEnterCabecalho')
        start = source.index('configurarEnterCabecalho(dataVenda, clienteBusca);')
        setup += source[start:source.index('let vendaToastTimer', start)]
        for marker in ['clienteBusca.addEventListener("keydown", (e) => {', 'preco.addEventListener("keydown", (e) => {', 'quantidade.addEventListener("keydown", (e) => {', 'unidade.addEventListener("keydown", (e) => {']:
            start = source.index(marker)
            setup += source[start:source.index('\n});', start)+4]
        start=source.index('  document.addEventListener("keydown", async function (e) {')
        setup += source[start:source.index('// ================= AUTOCOMPLETE PRODUTO',start)]
        script=next(x for x in re.findall(r'<script[^>]*>(.*?)</script>',source,re.S) if 'function criarBotaoParaSelect' in x)
        setup += script
        script=next(x for x in re.findall(r'<script[^>]*>(.*?)</script>',source,re.S) if 'function ehCampoDepoisDoOperador' in x)
        setup += script
        setup += '''
        const sugestoesBox=document.createElement('div');document.body.appendChild(sugestoesBox);
        let sugestoes=[],indexAtivo=-1,autocompleteAberto=false;
        function bloquearProdutoDuplicadoNoCampoProdutoVenda(){return false;}
        function filtrarProdutos(){sugestoes=Array.from(produto.options).filter(o=>o.value);indexAtivo=0;}
        function renderSugestoes(){sugestoesBox.style.display='block';}
        function selecionarProduto(opt){produto.value=opt.value;produtoBusca.value=opt.value;sugestoesBox.style.display='none';quantidade.focus();}
        '''
        start=source.index('produtoBusca.addEventListener("keydown", (e) => {')
        setup += source[start:source.index('\n});', start)+4]
        setup += '''
        document.dispatchEvent(new Event('DOMContentLoaded'));
        const originalAdd=adicionarItemVendaAtual;adicionarItemVendaAtual=()=>{addCalls++;return originalAdd();};
        window.seed=(unit='UN')=>{produto.value=produto.options[1].value;produtoBusca.value=produto.value;quantidade.value='2';unidade.innerHTML='';unidade.add(new Option(unit,unit));preco.value='10';window.atualizarSelectsBonitosVenda();};
        window.focused=()=>document.activeElement.id||document.activeElement.dataset.selectId||document.activeElement.className;
        '''
        self.evaluate(setup)

    def prepare_client_selection(self, width=400):
        self.prepare_flow(width=width)
        source = TEMPLATE.read_text(encoding='utf-8')
        self.evaluate('''
        const clienteId=document.createElement('input');
        const clientesAutocompleteUrl='/autocomplete-teste';
        let clienteBuscaTimer=null,clienteBuscaAbortController=null;
        let clienteResumoSuspenso=false,clienteDetalheRequisicao=0,condicoesPagamentoRevisao=0;
        function guardarClienteSelecionado(){}
        function atualizarCabecalhoCliente(){}
        function atualizarPrazoEVencimentoCliente(){}
        function atualizarHistoricoProdutoCliente(){}
        function normalizarStatusCliente(){return '';}
        window.pendingClientResponses=[];
        window.fetch=()=>new Promise(resolve=>pendingClientResponses.push(resolve));
        clienteBusca.focus();
        window.focusCalls=[];
        const originalFocus=HTMLElement.prototype.focus;
        HTMLElement.prototype.focus=function(...args){focusCalls.push(this.id);return originalFocus.apply(this,args);};
        ''')
        self.evaluate(function(source, 'preencherResumoCliente')
                      + 'async ' + function(source, 'atualizarClienteSelecionadoDoServidor')
                      + function(source, 'renderizarSugestoesClientes'))
        self.evaluate('renderizarSugestoesClientes();')

    def select_client_touch(self, swipe=False):
        self.evaluate('''
        window.clientOption=clienteSugestoesBox.querySelector('.cliente-sugestao-item');
        const start=new Event('touchstart',{bubbles:true});
        start.touches=[{clientX:10,clientY:10}];clientOption.dispatchEvent(start);
        ''' + ('''
        const move=new Event('touchmove',{bubbles:true});
        move.touches=[{clientX:10,clientY:40}];clientOption.dispatchEvent(move);
        ''' if swipe else '') + '''
        clientOption.dispatchEvent(new Event('touchend',{bubbles:true,cancelable:true}));
        ''')

    def test_mobile_client_touch_and_compatibility_click_focus_once(self):
        self.prepare_client_selection()
        self.select_client_touch()
        self.evaluate('clientOption.click();')
        self.assertEqual(self.evaluate('focused()'), 'vencimentoVenda')
        self.assertEqual(self.evaluate('focusCalls'), ['vencimentoVenda'])
        self.assertEqual(self.evaluate('pendingClientResponses.length'), 1)
        self.assertEqual(self.evaluate('[addCalls,gravacoes,operatorWarnings]'), [0, 0, 0])

    def test_client_click_focus_changes_only_on_mobile(self):
        for width in (400, 1366):
            with self.subTest(width=width):
                self.prepare_client_selection(width=width)
                self.evaluate("clienteSugestoesBox.querySelector('.cliente-sugestao-item').click();")
                self.assertEqual(self.evaluate('focused()'), 'vencimentoVenda' if width == 400 else 'clienteBusca')
                self.assertEqual(self.evaluate('focusCalls'), ['vencimentoVenda'] if width == 400 else [])

    def test_client_swipe_does_not_select_or_advance(self):
        self.prepare_client_selection()
        self.select_client_touch(swipe=True)
        self.assertEqual(self.evaluate('focused()'), 'clienteBusca')
        self.assertEqual(self.evaluate('pendingClientResponses.length'), 0)

    def test_client_async_refresh_and_summary_do_not_steal_focus(self):
        self.prepare_client_selection()
        self.select_client_touch()
        self.evaluate('focarProximoCampo(tipoVenda);')
        self.assertEqual(self.evaluate('focused()'), 'tipoVenda')
        self.evaluate("pendingClientResponses[0]({json:async()=>({clientes:[{id:1,nome:'Cliente atualizado'}]})});")
        self.tab.wait("clienteSelecionado.nome==='Cliente atualizado'")
        self.assertEqual(self.evaluate('focused()'), 'tipoVenda')
        self.evaluate("preencherResumoCliente({id:1,nome:'Cliente restaurado'});")
        self.assertEqual(self.evaluate('focused()'), 'tipoVenda')
        self.assertEqual(self.evaluate("focusCalls.filter(id=>id==='vencimentoVenda').length"), 1)

    def enter(self):
        self.tab.call('Input.dispatchKeyEvent', {'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
        self.tab.call('Input.dispatchKeyEvent', {'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})

    def test_client_names_do_not_trigger_operator(self):
        self.prepare_flow()
        for i in range(30):
            self.evaluate("clienteBusca.value="+json.dumps(['Cliente Produtos','Preco Bom','Quantidade Comercio'][i%3])+";clienteBusca.focus();clienteSugestoesBox.style.display='none';")
            self.enter()
            self.assertEqual(self.evaluate('focused()'), 'vencimentoVenda')
        self.assertEqual(self.evaluate('operatorWarnings'),0)

    def test_complete_sequence_units_and_single_add(self):
        self.prepare_flow()
        self.evaluate("clienteBusca.value='Cliente';clienteBusca.focus();clienteSugestoesBox.style.display='block';")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'vencimentoVenda')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'tipoVenda')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'operadorVenda')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'mobile-select-opcao')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'produtoBusca')
        self.evaluate("seed();produtoBusca.focus();sugestoesBox.style.display='none';")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'quantidade')
        for unit in ['UN','PCT','DZ','KG']:
            self.evaluate('tabelaBody.innerHTML="";seed('+json.dumps(unit)+');quantidade.focus();')
            self.enter()
            self.assertEqual(self.evaluate('focused()'),'unidade')
            self.enter()
            self.assertEqual(self.evaluate('focused()'),'preco')
            before=self.evaluate('addCalls')
            self.enter()
            self.assertEqual(self.evaluate('addCalls'),before+1)
            self.assertEqual(self.evaluate('tabelaBody.rows.length'),1)
            self.assertEqual(self.evaluate('focused()'),'produtoBusca')
        self.evaluate('btnGravarVenda.focus()')
        self.enter()
        self.assertEqual(self.evaluate('gravacoes'),0)

    def test_invalid_quantity_and_price_keep_focus(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';seed();quantidade.value='';quantidade.focus();")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'quantidade')
        self.evaluate("quantidade.value='2';preco.value='';preco.focus();")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'preco')
        self.assertEqual(self.evaluate('tabelaBody.rows.length'),0)
        self.assertEqual(self.evaluate('addCalls'),1)

    def test_selector_click_and_enter_confirmation(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';seed('PCT');unidade.add(new Option('DZ','DZ'));document.querySelector('[data-select-id=unidade]').click();document.querySelectorAll('.mobile-select-opcao')[1].click();")
        self.assertEqual(self.evaluate('unidade.value'),'DZ')
        self.assertEqual(self.evaluate('focused()'),'unidade')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'preco')

    def test_repeat_composition_and_price_keyup_do_not_add_again(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';seed();preco.focus();preco.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',repeat:true,bubbles:true,cancelable:true}));preco.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}));")
        self.assertEqual(self.evaluate('addCalls'),0)
        self.enter()
        self.assertEqual(self.evaluate('addCalls'),1)
        self.assertEqual(self.evaluate('focused()'),'produtoBusca')

    def test_failed_stock_and_cost_do_not_add(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';seed();validarEstoqueItemVendaAtual=()=>{lastError='Estoque';quantidade.focus();return false;};preco.focus();")
        self.enter()
        self.assertEqual(self.evaluate('tabelaBody.rows.length'),0)
        self.assertEqual(self.evaluate('focused()'),'quantidade')
        self.evaluate("validarEstoqueItemVendaAtual=()=>true;validarPrecoAcimaDoCustoVenda=()=>{lastError='Custo';preco.focus();return false;};preco.focus();")
        self.enter()
        self.assertEqual(self.evaluate('tabelaBody.rows.length'),0)
        self.assertEqual(self.evaluate('focused()'),'preco')

    def test_desktop_enter_keeps_existing_unit_behavior(self):
        self.prepare_flow(width=1366)
        self.evaluate("operadorVenda.value='1';seed();quantidade.focus();")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'preco')

    def test_compiles_all_inline_javascript(self):
        self.prepare_flow()
        from django.conf import settings
        from django.template import Context, Engine
        if not settings.configured:
            settings.configure(USE_I18N=False, USE_TZ=False)
        text=TEMPLATE.read_text(encoding='utf-8')
        text=re.sub(r'{% (?:url|static) .*?%}', '#', text)
        text=re.sub(r'{% load .*?%}|{% get_current_timezone .*?%}|{% include .*?%}', '', text)
        rendered=Engine(builtins=['django.templatetags.l10n','django.templatetags.tz']).from_string(text).render(Context({'locacoes_operacionais':{}}))
        scripts=[body for attrs,body in re.findall(r'<script([^>]*)>(.*?)</script>',rendered,re.S) if 'application/json' not in attrs and body.strip()]
        self.assertGreater(len(scripts),10)
        for script in scripts:
            self.evaluate('window.compileSource=""')
            for offset in range(0,len(script),12000):
                self.evaluate('compileSource+='+json.dumps(script[offset:offset+12000]))
            self.assertTrue(self.evaluate('new Function(compileSource);true'))

    def test_price_updates_once_and_returns_to_product(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';seed();preco.focus();")
        self.enter()
        self.evaluate("linhaSelecionada=tabelaBody.rows[0];seed();quantidade.value='3';preco.focus();")
        self.enter()
        self.assertEqual(self.evaluate('updates'),1)
        self.assertEqual(self.evaluate('addCalls'),2)
        self.assertEqual(self.evaluate('tabelaBody.rows.length'),1)
        self.assertEqual(self.evaluate('focused()'),'produtoBusca')

    def test_product_requires_confirmation(self):
        self.prepare_flow()
        self.evaluate("operadorVenda.value='1';produto.value='';produtoBusca.value='Produto';produtoBusca.focus();sugestoesBox.style.display='none';")
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'produtoBusca')
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'quantidade')

    def test_next_product_has_no_delayed_mobile_focus(self):
        self.prepare_flow()
        source=TEMPLATE.read_text(encoding='utf-8')
        self.evaluate(function(source,'selecionarTextoProdutoBuscaSugerido')+function(source,'preencherProximoProdutoDepoisDeAdicionar'))
        self.evaluate("operadorVenda.value='1';function normalizarTextoCliente(text){return text.toLowerCase();}produto.add(new Option('Proximo produto','Proximo produto'));produto.options[2].dataset.produtoId='2';seed();window.scheduled=[];window.setTimeout=(fn,delay)=>scheduled.push(delay);preencherProximoProdutoDepoisDeAdicionar(produto.value,produtoBusca.value);")
        self.assertEqual(self.evaluate('focused()'),'produtoBusca')
        self.assertEqual(self.evaluate('scheduled'),[])
        self.enter()
        self.assertEqual(self.evaluate('focused()'),'quantidade')

    def test_original_reproduces_client_operator_warning(self):
        self.prepare_flow(original=True)
        self.evaluate("clienteBusca.value='Cliente Produtos';clienteBusca.focus();")
        self.tab.wait('operatorWarnings > 0')


if __name__ == '__main__':
    unittest.main()
