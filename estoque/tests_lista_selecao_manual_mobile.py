"""Manual selection preview: compact fields, duplicate warning and real save."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque import tests_lista_cards_responsivos as existing
from estoque.models import ListaCompraFornecedor
from estoque.tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome


@skipUnless(Path(existing.existing.CHROME).is_file(), 'Chrome unavailable')
class SelecaoManualMobileTests(StaticLiveServerTestCase):
    setUp = existing.ListaCardsResponsivosTests.setUp
    open_page = existing.ListaCardsResponsivosTests.open_page
    effects = existing.ListaCardsResponsivosTests.effects
    clean_console = existing.ListaCardsResponsivosTests.clean_console

    def test_400px_escolher_pesquisar_adicionar_duas_vezes_e_recarregar(self):
        import json
        from estoque.models import ProdutoFornecedor
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        # The second product has no supplier link.
        self.manual.nome = 'Amstel ficticia'
        self.manual.save(update_fields=['nome'])
        self.products[0].nome = 'Skol ficticia'
        self.products[0].save(update_fields=['nome'])
        vinculos = list(ProdutoFornecedor.objects.values())
        before = self.effects()
        tab = self.catalogo_instrumentado(width=400)
        tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=1))
        tocar = lambda selector: ColetaBrowserTests.tocar(self, tab, selector)

        def controles():
            self.assertEqual(tab.evaluate("document.querySelectorAll('#btnToggleAdicionarProdutosMobile').length"), 1)
            self.assertEqual(tab.evaluate("document.getElementById('btnToggleAdicionarProdutosMobile').textContent.trim()"), '+ Escolher produto')
            self.assertTrue(tab.evaluate("!!document.getElementById('btnToggleAdicionarProdutosMobile').getClientRects().length"))
            self.assertEqual(tab.evaluate("document.getElementById('produtoManualBuscaSugestao').placeholder"), 'Pesquisar produto pelo nome')
            self.assertEqual(tab.evaluate("document.getElementById('produtoManualBuscaSugestao').getAttribute('aria-label')"), 'Pesquisar produto pelo nome')
            self.assertEqual(tab.evaluate("document.getElementById('btnAdicionarProdutoSugestao').textContent.trim()"), 'Adicionar à lista')

        controles()
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        requests = tab.evaluate('pedidosCatalogo.length')
        for indice, (termo, produto) in enumerate([('sko', self.products[0]), ('ams', self.manual)], 1):
            controles()
            tocar('#btnToggleAdicionarProdutosMobile')
            tab.wait("document.activeElement.id==='produtoManualBuscaSugestao' && document.getElementById('produtoManualSugestoes').classList.contains('aberta')")
            self.assertEqual(tab.evaluate("document.getElementById('btnToggleAdicionarProdutosMobile').getAttribute('aria-expanded')"), 'true')
            # A first direct touch also keeps the suggestions open.
            tocar('#produtoManualBuscaSugestao')
            self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
            tab.call('Input.insertText', dict(text=termo))
            selector = f'#produtoManualSugestoes [data-produto-id="{produto.pk}"]'
            tab.wait('!!document.querySelector('+json.dumps(selector)+')')
            tocar(selector)
            self.assertFalse(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
            self.assertEqual(tab.evaluate("['produtoManualEstoque','produtoManualMinimo','produtoManualVendido','produtoManualPedidos'].map(id=>document.getElementById(id).textContent)"), ['0.000', '1.000', '1.000', '0.000'])
            self.assertEqual(Decimal(tab.evaluate("document.getElementById('produtoManualPrecoCompra').value")), produto.preco_compra)
            tab.evaluate("document.getElementById('produtoManualQtd').value='0';true")
            tocar('#btnAdicionarProdutoSugestao')
            self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), indice-1)
            tab.evaluate("document.getElementById('produtoManualQtd').value='2';document.getElementById('produtoManualQtd').dispatchEvent(new Event('input',{bubbles:true}));true")
            tocar('#btnAdicionarProdutoSugestao')
            self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), indice)
            self.assertTrue(tab.evaluate("document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile')"))
            self.assertEqual(tab.evaluate("document.getElementById('btnToggleAdicionarProdutosMobile').textContent.trim()"), '+ Escolher produto')
        self.assertEqual(tab.evaluate('pedidosCatalogo.length'), requests)
        self.assertEqual(self.effects(), before)
        self.assertEqual(list(ProdutoFornecedor.objects.values()), vinculos)
        self.assertFalse(ListaCompraFornecedor.objects.exists())
        origem = tab.evaluate('performance.timeOrigin')
        tab.call('Page.reload', dict(ignoreCache=True))
        tab.wait('performance.timeOrigin!=='+str(origem)+" && document.readyState==='complete' && !!window.prepararProdutoListaFornecedor")
        controles()
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        tocar('#btnToggleAdicionarProdutosMobile')
        tab.wait("document.activeElement.id==='produtoManualBuscaSugestao' && document.getElementById('produtoManualSugestoes').classList.contains('aberta')")
        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
        self.clean_console(tab)

    def test_400px_primeiro_toque_abre_catalogo_e_retorno(self):
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        tab = self.catalogo_instrumentado(width=400)
        tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=1))
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        tab.evaluate("window.eventosAbertura=[];['pointerdown','focus','click','blur'].forEach(tipo=>document.addEventListener(tipo,e=>eventosAbertura.push({tipo,id:e.target.id,aberta:document.getElementById('produtoManualSugestoes').classList.contains('aberta'),busy:document.getElementById('produtoManualBuscaSugestao').getAttribute('aria-busy')}),true));true")
        ColetaBrowserTests.tocar(self, tab, '#produtoManualBuscaSugestao')
        print('Primeiro toque:', tab.evaluate('eventosAbertura'))
        self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        ColetaBrowserTests.tocar(self, tab, '#produtoManualBuscaSugestao')
        self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        tab.evaluate("reterCatalogo=true;document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('periodo').focus();true")
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='14'&&!!p.liberar)")
        ColetaBrowserTests.tocar(self, tab, '#produtoManualBuscaSugestao')
        # Losing browser focus is not an outside click or a cancelled search.
        tab.evaluate("document.getElementById('produtoManualBuscaSugestao').blur();reterCatalogo=false;pedidosCatalogo.forEach(p=>p.liberar?.());true")
        tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"), 'Primeiro toque durante consulta perdeu a abertura ao chegar o catalogo')
        tocar = lambda selector: ColetaBrowserTests.tocar(self, tab, selector)
        tocar('#periodo')
        self.assertFalse(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        tocar('#produtoManualBuscaSugestao')
        self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        requests = tab.evaluate('pedidosCatalogo.length')
        tab.call('Input.insertText', dict(text='Mobile 0'))
        selector = f'#produtoManualSugestoes [data-produto-id="{self.products[0].pk}"]'
        tab.wait('!!document.querySelector('+repr(selector)+')')
        self.assertEqual(tab.evaluate("document.querySelectorAll('#produtoManualSugestoes [data-produto-id]').length"), 1)
        tocar(selector)
        self.assertFalse(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"), '1.000')
        self.assertEqual(tab.evaluate("document.getElementById('produtoManualSugestao').value"), str(self.products[0].pk))
        tab.evaluate("document.getElementById('produtoManualQtd').value='0';true")
        tocar('#btnAdicionarProdutoSugestao')
        self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 0)
        tab.evaluate("document.getElementById('produtoManualQtd').value='2';true")
        tocar('#btnAdicionarProdutoSugestao')
        self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 1)
        self.assertEqual(tab.evaluate('pedidosCatalogo.length'), requests)
        origem = tab.evaluate('performance.timeOrigin')
        tab.call('Page.reload', dict(ignoreCache=True))
        tab.wait('performance.timeOrigin!=='+str(origem)+" && document.readyState==='complete' && !!window.prepararProdutoListaFornecedor")
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        tocar('#produtoManualBuscaSugestao')
        self.assertTrue(tab.evaluate("document.getElementById('produtoManualSugestoes').classList.contains('aberta')"))
        tab.evaluate("document.getElementById('produtoManualQtd').value='2';true")
        tocar('#btnAdicionarProdutoSugestao')
        self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 0)
        self.clean_console(tab)

    def test_json_200_produtos_pesquisa_digitada_e_foco_fora_apos_recargas(self):
        import json
        from estoque.models import Fornecedor
        self.supplier=Fornecedor.objects.create(pk=2308,nome='Fornecedor F12 ficticio')
        produto=dict(id=self.products[0].pk,nome='Produto F12 ficticio',estoque_atual='5.000',
            estoque_minimo='1.000',quantidade_vendida='3.000',quantidade_pedidos_abertos='0.000',
            sugestao='2.000',unidade='PCT',fator_conversao='1.000',preco_compra='12.50',
            preco_unitario_calculado='12.50',total_sugerido='25.00',historico_ultimas_compras=[],
            consulta_atual=dict(estoque='5.000',pc='12.50',pv='20.00'))
        tab=self.catalogo_instrumentado(width=400)
        payload=json.dumps({'produtos':[produto],'periodo_valido':True},ensure_ascii=True)
        script="""window.respostasF12=[];const fetchF12Original=fetch;
          window.fetch=(url,options)=>{
            if(!String(url).includes('consulta_manual=1'))return fetchF12Original(url,options);
            return new Promise(resolve=>window.entregarF12=()=>{
              respostasF12.push({url:String(url),status:200,body:PAYLOAD});
              resolve(new Response(JSON.stringify(PAYLOAD),
                {status:200,headers:{'Content-Type':'application/json','Cache-Control':'private, no-store'}}));
            });
          };""".replace('PAYLOAD',payload)
        tab.call('Page.addScriptToEvaluateOnNewDocument',dict(source=script))
        tab.evaluate(script+'true')
        for tentativa in range(2):
            origem=tab.evaluate('performance.timeOrigin')
            tab.call('Page.reload',dict(ignoreCache=True))
            tab.wait('performance.timeOrigin!=='+json.dumps(origem)+" && document.readyState==='complete'")
            self.filtros_catalogo(tab)
            tab.wait("typeof entregarF12==='function' && document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
            tab.evaluate("(async()=>{const b=document.getElementById('produtoManualBuscaSugestao');b.focus();b.value='Produto F12';b.dispatchEvent(new Event('input',{bubbles:true}));await new Promise(r=>setTimeout(r,30));document.getElementById('periodo').focus();entregarF12();return true})()")
            tab.wait("respostasF12.length>0 && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
            estado=tab.evaluate("({recebidas:respostasF12,ativo:document.activeElement.id,pesquisa:document.getElementById('produtoManualBuscaSugestao').value,mensagem:document.getElementById('msgAdicionarProdutoSugestao').textContent,opcoes:document.getElementById('produtoManualSugestoes').childElementCount,erros:purchaseConsoleErrors})")
            print('Resposta F12 e apresentacao:',estado)
            if not estado['opcoes']:
                tab.evaluate("document.getElementById('produtoManualBuscaSugestao').focus();true")
                print('Mesmo JSON apos novo foco:',tab.evaluate("({respostas:respostasF12.length,opcoes:document.getElementById('produtoManualSugestoes').childElementCount})"))
            self.assertGreater(estado['opcoes'],0,'JSON aceito, mas pesquisa digitada nao foi apresentada sem novo foco')
            selector=f'#produtoManualSugestoes [data-produto-id="{self.products[0].pk}"]'
            tab.wait('!!document.querySelector('+json.dumps(selector)+')')
            tab.evaluate('document.querySelector('+json.dumps(selector)+').click();true')
            self.assertEqual(tab.evaluate("['produtoManualEstoque','produtoManualMinimo','produtoManualVendido'].map(id=>document.getElementById(id).textContent)"),['5.000','1.000','3.000'])
            self.assertEqual(tab.evaluate("document.getElementById('produtoManualPrecoCompra').value"),'12.50')
            tab.evaluate("document.getElementById('produtoManualQtd').value='2';document.getElementById('btnAdicionarProdutoSugestao').click();true")
            self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"),1)
            self.clean_console(tab)

    def test_400px_duas_recargas_sem_cache_digitacao_e_inclusao_reais(self):
        import json
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        tocar=lambda tab,selector:ColetaBrowserTests.tocar(self,tab,selector)
        with TemporaryDirectory(prefix='manual-400-real-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(existing.existing.CHROME,profile).start()
            try:
                tab=self.open_page(AuditedChrome(chrome),width=400,
                    path=reverse('estoque:sugestao_compra_fornecedor'))
                tab.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
                observer="""window.httpCatalogo=[];const fetchObservado=fetch;
                  window.fetch=async(...args)=>{
                    const r=await fetchObservado(...args);
                    if(String(args[0]).includes('consulta_manual=1')){
                      const p={url:String(args[0]),status:r.status};httpCatalogo.push(p);
                      r.clone().json().then(d=>p.resposta=d).catch(e=>p.erro=String(e));
                    }return r;
                  };"""
                tab.call('Page.addScriptToEvaluateOnNewDocument',dict(source=observer))
                tab.call('Network.setCacheDisabled',dict(cacheDisabled=True))
                for tentativa in range(2):
                    with self.subTest(recarga=tentativa):
                        origem=tab.evaluate('performance.timeOrigin')
                        tab.call('Page.reload',dict(ignoreCache=True))
                        tab.wait('performance.timeOrigin!=='+json.dumps(origem)+" && document.readyState==='complete'")
                        tocar(tab,'#fornecedorBuscaSugestao')
                        tab.call('Input.insertText',dict(text=self.supplier.nome))
                        fornecedor=f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]'
                        tab.wait('!!document.querySelector('+json.dumps(fornecedor)+')')
                        tocar(tab,fornecedor)
                        tocar(tab,'#periodo')
                        tab.call('Input.insertText',dict(text='7' if tentativa==0 else '14'))
                        tocar(tab,'#produtoManualBuscaSugestao')
                        tab.call('Input.insertText',dict(text='Produto Mobile'))
                        produto=f'#produtoManualSugestoes [data-produto-id="{self.products[0].pk}"]'
                        try:
                            tab.wait('!!document.querySelector('+json.dumps(produto)+')')
                        except AssertionError:
                            print('400px real:',tab.evaluate("({http:httpCatalogo,fornecedor:document.getElementById('fornecedor').value,periodo:document.getElementById('periodo').value,busca:document.getElementById('produtoManualBuscaSugestao').value,busy:document.getElementById('produtoManualBuscaSugestao').getAttribute('aria-busy'),mensagem:document.getElementById('msgAdicionarProdutoSugestao').textContent,erros:purchaseConsoleErrors})"))
                            self.screenshot(tab,'manual-400-falha')
                            raise
                        tocar(tab,produto)
                        self.assertEqual(tab.evaluate("document.getElementById('produtoManualSugestao').value"),str(self.products[0].pk))
                        self.assertEqual(tab.evaluate("['produtoManualEstoque','produtoManualMinimo','produtoManualVendido'].map(id=>document.getElementById(id).textContent)"),['0.000','1.000','1.000'])
                        self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"),'1.000')
                        tocar(tab,'#produtoManualQtd')
                        tab.call('Input.insertText',dict(text='2'))
                        tocar(tab,'#btnAdicionarProdutoSugestao')
                        self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"),1)
                        print('400px real, recarga',tentativa,tab.evaluate('httpCatalogo.map(p=>({url:p.url,status:p.status,produtos:p.resposta?.produtos?.length}))'))
                        self.clean_console(tab)
            finally:
                chrome.stop()

    def test_catalogo_400px_http_pendente_expira_e_retry_inclui(self):
        from threading import Event
        from unittest.mock import patch
        from estoque.urls import urlpatterns
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        pattern=next(p for p in urlpatterns if p.name=='sugestao_compra_fornecedor')
        original=pattern.callback
        release=Event(); requests=[]
        self.addCleanup(release.set)
        tab=self.catalogo_instrumentado(width=400)
        tab.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))

        def primeira_pendente(request,*args,**kwargs):
            if request.GET.get('consulta_manual')=='1':
                requests.append(dict(request.GET.items()))
                if len(requests)==1: release.wait(25)
            return original(request,*args,**kwargs)

        with patch.object(pattern,'callback',primeira_pendente):
            self.filtros_catalogo(tab)
            tab.wait("document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
            try:
                tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy') && document.getElementById('msgAdicionarProdutoSugestao').textContent.includes('tentar novamente')",timeout=18)
            finally:
                release.set()
            self.assertEqual(requests[0]['periodo'],'7')
            tab.evaluate("document.getElementById('produtoManualBuscaSugestao').focus();document.getElementById('produtoManualBuscaSugestao').click();true")
            tab.wait("!!document.querySelector('#produtoManualSugestoes [data-produto-id]')")
            self.select(tab,self.products[0])
            self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"),'1.000')
            ColetaBrowserTests.tocar(self,tab,'#produtoManualQtd')
            tab.call('Input.insertText',dict(text='2'))
            ColetaBrowserTests.tocar(self,tab,'#btnAdicionarProdutoSugestao')
            self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"),1)
            self.assertGreaterEqual(len(requests),2)
            self.clean_console(tab)

    def test_catalogo_requisicao_interrompida_nao_deixa_atualizando(self):
        from threading import Event
        from unittest.mock import patch
        from estoque.urls import urlpatterns
        pattern=next(p for p in urlpatterns if p.name=='sugestao_compra_fornecedor')
        original=pattern.callback
        release=Event()
        self.addCleanup(release.set)
        tab=self.catalogo_instrumentado(width=400)
        tab.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
        tab.evaluate("window.fetchAntesAbortar=fetch;window.fetch=(u,o)=>{if(!String(u).includes('consulta_manual=1'))return fetchAntesAbortar(u,o);window.abortadorTeste=new AbortController();return fetchAntesAbortar(u,{...o,signal:abortadorTeste.signal})};true")

        def lenta(request,*args,**kwargs):
            if request.GET.get('consulta_manual')=='1': release.wait(5)
            return original(request,*args,**kwargs)

        with patch.object(pattern,'callback',lenta):
            self.filtros_catalogo(tab)
            tab.wait("!!window.abortadorTeste && document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
            tab.evaluate('abortadorTeste.abort();true')
            tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
            mensagem=tab.evaluate("document.getElementById('msgAdicionarProdutoSugestao').textContent")
            print('Mensagem apos interrupcao real:',mensagem)
            self.assertIn('tentar novamente',mensagem)
            release.set()
            tab.evaluate("window.fetch=fetchAntesAbortar;document.getElementById('produtoManualBuscaSugestao').focus();document.getElementById('produtoManualBuscaSugestao').click();true")
            tab.wait("!!document.querySelector('#produtoManualSugestoes [data-produto-id]')")
            self.select(tab,self.products[0])
            self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"),'1.000')
            from estoque.tests_coleta_lista_browser import ColetaBrowserTests
            ColetaBrowserTests.tocar(self,tab,'#produtoManualQtd')
            tab.call('Input.insertText',dict(text='2'))
            ColetaBrowserTests.tocar(self,tab,'#btnAdicionarProdutoSugestao')
            self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"),1)
            self.clean_console(tab)

    def catalogo_instrumentado(self,width=390):
        profile=TemporaryDirectory(prefix='catalogo-transicao-',ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        chrome=Chrome(existing.existing.CHROME,profile.name).start()
        self.addCleanup(chrome.stop)
        tab=self.open_page(AuditedChrome(chrome),width=width,
            path=reverse('estoque:sugestao_compra_fornecedor')+'?nova=1')
        source="""window.pedidosCatalogo=[];window.reterCatalogo=false;window.falhaCatalogo='';
          const fetchCatalogoOriginal=window.fetch;
          window.fetch=async(url,options)=>{
            if(!String(url).includes('consulta_manual=1'))return fetchCatalogoOriginal(url,options);
            const p={params:Object.fromEntries(new URL(url,location.href).searchParams)};
            pedidosCatalogo.push(p);
            const r=await fetchCatalogoOriginal(url,options);
            p.status=r.status;p.dados=await r.clone().json();
            if(reterCatalogo)await new Promise(resolve=>p.liberar=resolve);
            if(falhaCatalogo==='500')return new Response('{}',{status:500,headers:{'Content-Type':'application/json'}});
            if(falhaCatalogo==='vazio')return new Response(JSON.stringify({produtos:[],periodo_valido:true}),{headers:{'Content-Type':'application/json'}});
            return r;
          };"""
        tab.call('Page.addScriptToEvaluateOnNewDocument',dict(source=source))
        tab.evaluate(source+'true')
        return tab

    def filtros_catalogo(self,tab,periodo='7',change=True):
        import json
        tab.evaluate("const f=document.getElementById('fornecedorBuscaSugestao');f.value="+json.dumps(self.supplier.nome)+
            ";f.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#fornecedorSugestoes [data-fornecedor-id=\""+str(self.supplier.pk)+"\"]').click();true")
        if periodo:
            tab.evaluate("const p=document.getElementById('periodo');p.focus();p.value="+json.dumps(periodo)+
                ";p.dispatchEvent(new Event('input',{bubbles:true}));"+
                ("p.dispatchEvent(new Event('change',{bubbles:true}));" if change else '')+'true')

    def test_catalogo_funciona_recarrega_e_periodo_digitado_sem_blur(self):
        tab=self.catalogo_instrumentado()
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='7'&&p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        tab.evaluate("document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='14'&&p.dados?.produtos.length)")
        tab.call('Page.reload')
        tab.wait("document.readyState==='complete' && !!window.prepararProdutoListaFornecedor")
        tab.evaluate('reterCatalogo=true;true')
        self.filtros_catalogo(tab,periodo='')
        tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,350));return true})()')
        print('Antes de digitar o periodo apos recarga:',tab.evaluate('pedidosCatalogo.map(p=>({params:p.params,status:p.status,produtos:p.dados?.produtos.length,periodo_valido:p.dados?.periodo_valido}))'))
        tab.evaluate("reterCatalogo=false;const p=document.getElementById('periodo');p.focus();p.value='7';p.dispatchEvent(new Event('input',{bubbles:true}));pedidosCatalogo.forEach(p=>p.liberar?.());true")
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='7'&&p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        self.select(tab,self.products[0])
        self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"),'1.000')
        self.clean_console(tab)

    def test_catalogo_resposta_atrasada_preserva_pesquisa_atual(self):
        import json
        tab=self.catalogo_instrumentado()
        self.filtros_catalogo(tab)
        tab.wait("pedidosCatalogo.some(p=>p.dados?.produtos.length) && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        self.select(tab,self.products[0])
        tab.evaluate("reterCatalogo=true;document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='14'&&!!p.liberar)")
        tab.evaluate("const b=document.getElementById('produtoManualBuscaSugestao');b.focus();b.value="+json.dumps(self.products[1].nome)+";b.dispatchEvent(new Event('input',{bubbles:true}));reterCatalogo=false;pedidosCatalogo.forEach(p=>p.liberar?.());true")
        tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        self.assertEqual(tab.evaluate("document.getElementById('produtoManualBuscaSugestao').value"),self.products[1].nome)
        self.clean_console(tab)

    def test_catalogo_erro_vazio_e_nova_tentativa(self):
        tab=self.catalogo_instrumentado()
        tab.evaluate("falhaCatalogo='500';true")
        self.filtros_catalogo(tab)
        tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy') && document.getElementById('msgAdicionarProdutoSugestao').textContent.includes('possível')")
        tab.evaluate("document.getElementById('produtoManualBuscaSugestao').value='Produto';document.getElementById('produtoManualBuscaSugestao').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.assertIn('tentar novamente',tab.evaluate("document.getElementById('msgAdicionarProdutoSugestao').textContent"))
        tab.evaluate("falhaCatalogo='vazio';document.getElementById('produtoManualBuscaSugestao').click();true")
        tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy') && document.getElementById('msgAdicionarProdutoSugestao').textContent.includes('Nenhum produto ativo')")
        tab.evaluate("falhaCatalogo='';document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('produtoManualBuscaSugestao').focus();true")
        tab.wait("!!document.querySelector('#produtoManualSugestoes [data-produto-id]')")
        self.clean_console(tab)

    def test_catalogo_respostas_invertidas_e_fornecedor_limpo(self):
        import json
        from datetime import timedelta
        from django.utils import timezone
        from estoque.models import Cliente, Venda, ItemVenda
        venda=Venda.objects.create(cliente=Cliente.objects.first(),
            data_venda=timezone.localdate()-timedelta(days=10),total=200)
        ItemVenda.objects.create(venda=venda,produto=self.products[0],quantidade=4,
            unidade='PCT',preco_unitario=50,valor_total=200)
        tab=self.catalogo_instrumentado()
        tab.evaluate('reterCatalogo=true;true')
        self.filtros_catalogo(tab,periodo='14')
        tab.wait("pedidosCatalogo.some(p=>p.params.periodo==='14'&&!!p.liberar)")
        tab.evaluate("reterCatalogo=false;document.getElementById('periodo').value='7';document.getElementById('periodo').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));const b=document.getElementById('produtoManualBuscaSugestao');b.focus();b.value="+json.dumps(self.products[0].nome)+";b.dispatchEvent(new Event('input',{bubbles:true}));true")
        tab.wait("document.getElementById('produtoManualVendido').textContent==='1.000' && !document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
        tab.evaluate('(async()=>{pedidosCatalogo.forEach(p=>p.liberar?.());await new Promise(r=>setTimeout(r,80));return true})()')
        self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"),'1.000')
        dados=tab.evaluate('pedidosCatalogo.map(p=>({periodo:p.params.periodo,status:p.status,produtos:p.dados?.produtos}))')
        self.assertEqual(next(p for p in dados[0]['produtos'] if p['id']==self.products[0].pk)['quantidade_vendida'],'5.000')
        tab.evaluate("document.getElementById('fornecedorBuscaSugestao').value='';document.getElementById('fornecedorBuscaSugestao').dispatchEvent(new Event('input',{bubbles:true}));true")
        tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy') && document.getElementById('msgAdicionarProdutoSugestao').textContent.includes('Selecione o fornecedor')")
        self.clean_console(tab)

    def test_catalogo_manual_fornecedor_ativo_fora_sugestao_mesmos_calculos(self):
        from estoque.models import ProdutoFornecedor
        product=self.products[0]
        product.quantidade=1000
        product.save(update_fields=['quantidade'])
        ProdutoFornecedor.objects.filter(produto=self.products[1]).update(ativo=False)
        self.products[2].ativo=False
        self.products[2].save(update_fields=['ativo'])
        url=reverse('estoque:sugestao_compra_fornecedor')
        params={'fornecedor':self.supplier.pk,'periodo':'7'}
        automatic=self.client.get(url, params)
        self.assertNotIn(product.pk, [row['produto_id'] for row in automatic.context['linhas']])
        before=self.effects()
        response=self.client.get(url,{**params,'consulta_manual':'1'})
        self.assertEqual(response.status_code,200)
        payload=response.json()
        from estoque.models import Produto
        self.assertEqual({p['id'] for p in payload['produtos']},
                         set(Produto.objects.filter(ativo=True, excluido=False).values_list('pk', flat=True)))
        manual=payload['produtos'][0]
        original=next(p for p in automatic.context['produtos_manual_payload'] if p['id']==product.pk)
        self.assertEqual(manual,original)
        self.assertTrue(payload['periodo_valido'])
        self.assertEqual(self.effects(),before)

    def test_fluxo_real_nova_lista_dois_produtos_sem_gerar(self):
        import json
        from estoque.models import ProdutoFornecedor, Compra
        from estoque.tests_coleta_lista_browser import ColetaBrowserTests
        tocar = lambda tab, selector: ColetaBrowserTests.tocar(self, tab, selector)
        self.manual.quantidade = 1000
        self.manual.save(update_fields=['quantidade'])
        ProdutoFornecedor.objects.get_or_create(produto=self.manual, fornecedor=self.supplier)
        with TemporaryDirectory(prefix='manual-interface-real-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=1440,
                    path=reverse('estoque:sugestao_compra_fornecedor'))
                # Same sequence as opening F12 and then enabling the mobile viewport.
                tab.call('Emulation.setDeviceMetricsOverride',dict(width=390,height=844,deviceScaleFactor=1,mobile=True))
                tab.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=1))
                tab.wait("innerWidth===390 && !!document.getElementById('produtoManualBuscaSugestao').getClientRects().length")
                tocar(tab, '#fornecedorBuscaSugestao')
                tab.call('Input.insertText', dict(text=self.supplier.nome))
                tab.wait('!!document.querySelector('+json.dumps(f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]')+')')
                tocar(tab, f'#fornecedorSugestoes [data-fornecedor-id="{self.supplier.pk}"]')
                tocar(tab, '#periodo')
                tab.call('Input.insertText', dict(text='7'))
                tab.call('Input.dispatchKeyEvent', dict(type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9))
                tab.call('Input.dispatchKeyEvent', dict(type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9))
                tab.wait("!document.getElementById('produtoManualBuscaSugestao').disabled && document.getElementById('msgAdicionarProdutoSugestao').textContent===''")
                self.assertTrue(tab.evaluate("!!document.getElementById('produtoManualBuscaSugestao').getClientRects().length"))
                for product in [self.manual, self.products[0]]:
                    if tab.evaluate("document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile')"):
                        tocar(tab, '#btnToggleAdicionarProdutosMobile')
                        tab.wait("document.getElementById('btnToggleAdicionarProdutosMobile').getAttribute('aria-expanded')==='true' && document.activeElement.id==='produtoManualBuscaSugestao'")
                    tocar(tab, '#produtoManualBuscaSugestao')
                    tab.call('Input.insertText', dict(text=product.nome))
                    selector=f'#produtoManualSugestoes [data-produto-id="{product.pk}"]'
                    try:
                        tab.wait('!!document.querySelector('+json.dumps(selector)+')')
                    except AssertionError:
                        print('Diagnostico busca:', tab.evaluate("({fornecedor:document.getElementById('fornecedor').value,periodo:document.getElementById('periodo').value,inicio:document.getElementById('data_inicio').value,fim:document.getElementById('data_fim').value,busca:document.getElementById('produtoManualBuscaSugestao').value,msg:document.getElementById('msgAdicionarProdutoSugestao').textContent,opcoes:document.getElementById('produtoManualSugestoes').innerText,erros:purchaseConsoleErrors})"))
                        self.screenshot(tab, 'manual-fluxo-real-falha')
                        raise
                    tocar(tab, selector)
                    self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"), '1.000')
                    tocar(tab, '#produtoManualQtd')
                    tab.call('Input.insertText', dict(text='2'))
                    self.screenshot(tab, 'manual-fluxo-real-'+str(product.pk))
                    tocar(tab, '#btnAdicionarProdutoSugestao')
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 2)
                tocar(tab, '#btnGravarListaFornecedor')
                tab.wait('location.pathname==="/compras/listas-fornecedor/"')
                saved=ListaCompraFornecedor.objects.latest('pk')
                self.assertEqual(set(saved.itens.values_list('produto_id', flat=True)), {self.manual.pk,self.products[0].pk})
                tab=self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:compras_lista_fornecedor_editar',args=[saved.pk]))
                for product in [self.manual,self.products[0]]:
                    tocar(tab,f'#mobileSugestaoProdutos [data-produto-id="{product.pk}"] .sugestao-adicionar-lista-mobile')
                    tab.wait('!coletaLista.ocupado')
                tocar(tab,'#btnSalvarGerarCompraConfirmados')
                tab.wait("location.search.includes('continuar_itens=1')")
                self.assertEqual(Compra.objects.filter(lista_fornecedor=saved).get().itens.count(),2)
                self.clean_console(tab)
            finally:
                chrome.stop()

    def select(self, tab, product):
        if tab.evaluate("document.getElementById('areaAdicionarProdutosManual').classList.contains('sugestao-manual-recolhida-mobile')"):
            tab.evaluate("document.getElementById('btnToggleAdicionarProdutosMobile').click();true")
            tab.wait("document.activeElement.id==='produtoManualBuscaSugestao'")
        tab.wait('document.getElementById("btnToggleAdicionarProdutosMobile").getAttribute("aria-expanded")==="true"')
        import json
        tab.evaluate('window.search=document.getElementById("produtoManualBuscaSugestao");search.value=' + json.dumps(product.nome) + ';search.dispatchEvent(new Event("input",{bubbles:true}));true')
        selector = f'#produtoManualSugestoes [data-produto-id="{product.pk}"]'
        tab.wait(f'!!document.querySelector({selector!r})')
        tab.evaluate(f'document.querySelector({selector!r}).click();true')
        tab.wait(f'document.getElementById("produtoManualSugestao").value==="{product.pk}"')

    def screenshot(self, tab, name):
        folder = Path(__file__).resolve().parent.parent / 'evidencias-lista-cards'
        folder.mkdir(exist_ok=True)
        clip = tab.evaluate('''(() => {
            const r=document.getElementById('areaAdicionarProdutosManual').getBoundingClientRect();
            return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1};
        })()''')
        data = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True, 'clip': clip})['data']
        (folder / (name + '.png')).write_bytes(base64.b64decode(data))

    def test_compact_selection_available_and_already_listed_keep_all_fields_and_history(self):
        before = self.effects()
        with TemporaryDirectory(prefix='manual-selection-states-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Adicionar à lista', False])
                        metrics = tab.evaluate('''(() => {
                            const cells=[...document.getElementById('linhaProdutoManualSugestao').children];
                            return {height:cells[0].parentElement.getBoundingClientRect().height,
                                stock:cells[1].getBoundingClientRect().top,min:cells[2].getBoundingClientRect().top,
                                qty:cells[5].getBoundingClientRect().top,price:cells[7].getBoundingClientRect().top,
                                qtyInput:cells[5].querySelector('input').getBoundingClientRect().top,
                                priceInput:cells[7].querySelector('input').getBoundingClientRect().top,
                                unitPrice:cells[8].getBoundingClientRect().top,total:cells[9].getBoundingClientRect().top,
                                fields:cells.length,labelsFit:cells.slice(1).every(c=>c.scrollWidth<=c.clientWidth),
                                searchFits:cells[0].querySelector('input[type=text]').getBoundingClientRect().width<=cells[0].clientWidth,
                                overflow:document.documentElement.scrollWidth>innerWidth};
                        })()''')
                        self.assertEqual(metrics['stock'], metrics['min'])
                        self.assertEqual(metrics['qty'], metrics['price'])
                        self.assertLess(abs(metrics['qtyInput'] - metrics['priceInput']), 1)
                        self.assertEqual(metrics['unitPrice'], metrics['total'])
                        self.assertEqual(metrics['fields'], 11)
                        self.assertLess(metrics['height'], 650)
                        self.assertFalse(metrics['overflow'])
                        self.assertTrue(metrics['labelsFit'])
                        self.assertTrue(metrics['searchFits'])
                        # Exercise the native disclosure before captureBeyondViewport,
                        # which temporarily resizes the page.
                        tab.evaluate('(async () => {await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true})()')
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
                        tab.wait('document.querySelector("#produtoManualHistoricoMobile details").open')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#produtoManualHistoricoMobile .sugestao-historico-desktop-item").length'), 3)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        tab.evaluate('document.querySelector("#produtoManualHistoricoMobile summary").click();true')
                        self.screenshot(tab, f'selecao-manual-disponivel-{width}')
                        self.select(tab, self.products[0])
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        count = tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length')
                        tab.evaluate('document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#mobileSugestaoProdutos .sugestao-mobile-card").length'), count)
                        self.screenshot(tab, f'selecao-manual-ja-incluido-{width}')
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_search_add_edit_pick_save_reopen_and_duplicate_warning(self):
        before = self.effects()
        with TemporaryDirectory(prefix='manual-selection-full-flow-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                for width in [320, 360, 390]:
                    with self.subTest(width=width):
                        tab = self.open_page(AuditedChrome(chrome), width=width)
                        self.select(tab, self.manual)
                        tab.evaluate('document.getElementById("produtoManualQtd").value="2";document.getElementById("produtoManualPrecoCompra").value="10,00";document.getElementById("produtoManualQtd").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        tab.wait(f'!!document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\')')
                        tab.evaluate(f'window.card=document.querySelector(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\');true')
                        self.assertEqual(tab.evaluate('[...card.querySelectorAll(".sugestao-mobile-edit-actions button")].map(b=>b.textContent.trim())'), ['Editar', 'Peguei', 'Excluir'])
                        self.assertEqual(tab.evaluate('new Set([...card.querySelectorAll(".sugestao-mobile-edit-actions button")].map(b=>b.getBoundingClientRect().top)).size'), 1)
                        tab.evaluate('''card.querySelector('.sugestao-confirmar-card-mobile').click();
                            card.querySelector('.sugestao-qtd-input').value='3';
                            card.querySelector('.sugestao-preco-input').value='12,50';
                            card.querySelector('.sugestao-preco-input').dispatchEvent(new Event('input',{bubbles:true}));
                            card.querySelector('.sugestao-adicionar-lista-mobile').click();true''')
                        self.assertEqual(tab.evaluate('card.dataset.naLista'), '1')
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        tab.evaluate('document.getElementById("btnAdicionarProdutoSugestao").click();true')
                        self.assertEqual(tab.evaluate(f'document.querySelectorAll(\'#mobileSugestaoProdutos [data-produto-id="{self.manual.pk}"]\').length'), 1)
                        self.clean_console(tab)
                        tab.evaluate('document.getElementById("btnGravarListaFornecedor").click();true')
                        tab.wait(r'location.pathname==="/compras/listas-fornecedor/" && document.readyState==="complete"')
                        saved = ListaCompraFornecedor.objects.latest('pk')
                        item = saved.itens.get()
                        self.assertEqual((item.produto_id,item.quantidade_final,item.unidade,item.preco_compra,item.total),
                            (self.manual.pk,Decimal('3'),'PCT',Decimal('12.50'),Decimal('37.50')))
                        tab = self.open_page(AuditedChrome(chrome), width=width,
                            path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                        self.select(tab, self.manual)
                        self.assertEqual(tab.evaluate('[document.getElementById("btnAdicionarProdutoSugestao").textContent.trim(),document.getElementById("btnAdicionarProdutoSugestao").disabled]'), ['Já está na lista', True])
                        self.assertEqual(tab.evaluate('document.querySelectorAll("#itensListaMobileLista .sugestao-item-lista-mobile").length'), 1)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.clean_console(tab)
                        self.assertEqual(self.effects(), before)
            finally:
                chrome.stop()

    def test_nova_manual_sem_gerar_consulta_periodo_e_salva(self):
        import json
        from estoque.models import ProdutoFornecedor, Fornecedor
        self.manual.quantidade = 1000
        self.manual.nome = 'Amstel ficticia'
        self.manual.save(update_fields=["quantidade", "nome"])
        self.products[0].nome = 'Skol ficticia'
        self.products[0].save(update_fields=['nome'])
        outro = Fornecedor.objects.create(nome='Outro atacadista ficticio')
        vinculos = list(ProdutoFornecedor.objects.values())
        with TemporaryDirectory(prefix='manual-sem-gerar-') as profile:
            chrome = Chrome(existing.existing.CHROME, profile).start()
            try:
                tab = self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:sugestao_compra_fornecedor')+'?nova=1')
                tab.evaluate('window.purchaseConsoleErrors=[];window.alertasManual=[];window.alert=m=>alertasManual.push(m);window.consultasManuais=0;window.fetchManualOriginal=fetch;window.fetch=async(...a)=>{const r=await fetchManualOriginal(...a);if(String(a[0]).includes("consulta_manual"))consultasManuais++;return r};true')
                tab.evaluate("document.getElementById('fornecedor').value="+json.dumps(str(self.supplier.pk))+";document.getElementById('periodo').value='7';document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("consultasManuais>0 && !document.getElementById('produtoManualBuscaSugestao').disabled && document.getElementById('msgAdicionarProdutoSugestao').textContent===''")
                for fornecedor in (self.supplier, outro):
                    tab.evaluate("document.getElementById('fornecedor').value="+json.dumps(str(fornecedor.pk))+";document.getElementById('fornecedor').dispatchEvent(new Event('change',{bubbles:true}));true")
                    tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
                    for termo, produto in [('ams', self.manual), ('sko', self.products[0])]:
                        tab.evaluate("const b=document.getElementById('produtoManualBuscaSugestao');b.value="+json.dumps(termo)+";b.dispatchEvent(new Event('input',{bubbles:true}));true")
                        tab.wait(f'!!document.querySelector(\'#produtoManualSugestoes [data-produto-id="{produto.pk}"]\')')
                tab.evaluate("document.getElementById('fornecedor').value="+json.dumps(str(self.supplier.pk))+";document.getElementById('fornecedor').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("!document.getElementById('produtoManualBuscaSugestao').hasAttribute('aria-busy')")
                self.select(tab, self.manual)
                self.assertEqual(tab.evaluate("document.getElementById('produtoManualVendido').textContent"), '1.000')
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 0)
                tab.evaluate("document.getElementById('data_inicio').value='2020-01-01';document.getElementById('data_fim').value='2020-01-02';document.getElementById('data_fim').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("document.getElementById('produtoManualVendido').textContent==='0.000'")
                tab.evaluate("document.getElementById('periodo').value='14';document.getElementById('periodo').dispatchEvent(new Event('change',{bubbles:true}));true")
                tab.wait("document.getElementById('produtoManualVendido').textContent==='1.000'")
                tab.evaluate("document.getElementById('produtoManualQtd').value='2';document.getElementById('produtoManualQtd').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('btnAdicionarProdutoSugestao').click();document.getElementById('btnAdicionarProdutoSugestao').click();true")
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 1)
                tab.evaluate("document.getElementById('btnGravarListaFornecedor').click();true")
                self.assertEqual(tab.evaluate('alertasManual'), [])
                tab.wait('location.pathname==="/compras/listas-fornecedor/"')
                saved = ListaCompraFornecedor.objects.latest('pk')
                self.assertEqual(saved.itens.count(), 1)
                self.assertEqual(saved.itens.get().produto_id, self.manual.pk)
                tab = self.open_page(AuditedChrome(chrome), width=390,
                    path=reverse('estoque:compras_lista_fornecedor_editar', args=[saved.pk]))
                self.assertEqual(tab.evaluate("document.querySelectorAll('#mobileSugestaoProdutos .sugestao-mobile-card').length"), 1)
                self.assertEqual(saved.itens.get().quantidade_final, Decimal('2'))
                self.assertEqual(list(ProdutoFornecedor.objects.values()), vinculos)
                self.assertEqual(tab.evaluate("document.querySelector('#mobileSugestaoProdutos .sugestao-mobile-card').dataset.naLista==='1'"), False)
                self.clean_console(tab)
            finally:
                chrome.stop()
