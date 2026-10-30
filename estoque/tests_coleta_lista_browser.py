"""Mobile collection in Edge/Chrome, using only synthetic disposable fixtures."""
import base64
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .models import ItemListaCompraFornecedor, Produto, Compra
from .tests_coleta_lista_mobile import ColetaFixture

CHROME=os.environ.get('COMPRA_TEST_CHROME',r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
TEMPLATE=Path(__file__).parent/'templates/estoque/compras_sugestao_fornecedor.html'


@skipUnless(Path(CHROME).is_file(),'Navegador indisponível')
class ColetaBrowserTests(ColetaFixture,StaticLiveServerTestCase):
    def navegador(self,width=390):
        profile=TemporaryDirectory(prefix='coleta-ficticia-',ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        browser=Chrome(CHROME,profile.name).start();self.addCleanup(browser.stop)
        t=browser.tab();t.call('Network.enable')
        t.call('Network.setCookie',dict(name='sessionid',value=self.client.cookies['sessionid'].value,url=self.live_server_url))
        t.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=844,deviceScaleFactor=1,mobile=width<860))
        t.call('Emulation.setTouchEmulationEnabled',dict(enabled=width<860,maxTouchPoints=1))
        t.call('Page.addScriptToEvaluateOnNewDocument',dict(source="window.errosColeta=[];addEventListener('error',e=>errosColeta.push(e.message));addEventListener('unhandledrejection',e=>errosColeta.push(String(e.reason)));") )
        return t

    def abrir(self,t):
        t.call('Page.navigate',dict(url=self.live_server_url+self.url))
        t.wait("document.readyState==='complete' && !!window.coletaLista && !!window.prepararProdutoListaFornecedor")

    def tocar(self,t,selector):
        ponto=t.evaluate("(async()=>{const e=document.querySelector("+json.dumps(selector)+
            ");if(!e||e.disabled)return null;e.scrollIntoView({block:'center',behavior:'instant'});"
            "await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const b=e.getBoundingClientRect();"
            "const x=b.x+b.width/2,y=b.y+b.height/2;if(!b.width||!b.height||!e.contains(document.elementFromPoint(x,y)))return null;return{x,y}})()")
        self.assertIsNotNone(ponto,'Ação inacessível: '+selector)
        if t.evaluate("innerWidth<860"):
            t.call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[ponto]))
            t.call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
        else:
            for tipo in ('mousePressed','mouseReleased'):
                t.call('Input.dispatchMouseEvent',dict(type=tipo,button='left',clickCount=1,**ponto))

    def resumo(self,t,pendentes,confirmados,previsto,confirmado):
        esperado=dict(p=str(pendentes),c=str(confirmados),tp='R$ '+previsto,tc='R$ '+confirmado)
        expr="({p:document.querySelector('[data-produtos-pendentes]').textContent,c:document.querySelector('[data-produtos-confirmados]').textContent,tp:document.querySelector('[data-total-pre-lista]').textContent,tc:document.querySelector('[data-total-lista-pedido]').textContent})"
        t.wait('JSON.stringify('+expr+')===JSON.stringify('+json.dumps(esperado)+')')
        self.assertEqual(t.evaluate('errosColeta'),[])
        self.assertLessEqual(t.evaluate('document.documentElement.scrollWidth'),390)

    def iniciar(self,t,acao='iniciar_pendente'):
        self.tocar(t,'[data-iniciar-coleta='+acao+']')
        t.wait("!!window.coletaLista?.dados?.estado.iniciada && document.readyState==='complete' && document.querySelector('[data-coleta-aviso]').hidden")

    def pegar(self,t,p=None):
        p=p or self.produtos[0]
        self.tocar(t,f'#mobileSugestaoProdutos [data-produto-id="{p.pk}"] .sugestao-adicionar-lista-mobile')
        t.wait(f"!!document.querySelector('#itensListaMobileLista [data-produto-id=\"{p.pk}\"]')")

    def capturar(self,t,nome,selector='#preListaMobileTitulo'):
        t.evaluate("(async()=>{document.querySelector("+json.dumps(selector)+").scrollIntoView({block:'start',behavior:'instant'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()")
        pasta=Path('amostras/coleta-mobile');pasta.mkdir(parents=True,exist_ok=True)
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot',dict(format='png'))['data']))

    def test_notebook_13_produtos_inicio_reabertura_e_devolucao(self):
        for n in range(3,14):
            p=Produto.objects.create(nome=f'Produto fictício {n:02}',preco_compra=1,preco_vista=2,preco_prazo=3,quantidade=10)
            ItemListaCompraFornecedor.objects.create(lista=self.lista,produto=p,quantidade_final=1,preco_compra=1,total=1)
        t=self.navegador();self.abrir(t)
        self.assertEqual(t.evaluate("document.querySelectorAll('[data-iniciar-coleta]').length"),1)
        self.iniciar(t);self.resumo(t,13,0,'46,35','0,00')
        self.capturar(t,'pre-lista-13')
        self.pegar(t);self.resumo(t,12,1,'16,20','30,15')
        t.call('Page.reload');t.wait("document.readyState==='complete' && !!window.coletaLista")
        self.resumo(t,12,1,'16,20','30,15')
        self.abrir(t);self.resumo(t,12,1,'16,20','30,15')
        self.assertEqual(t.evaluate("document.querySelectorAll('[data-iniciar-coleta]').length"),0)
        self.capturar(t,'parcial-reaberta')
        self.tocar(t,f'#itensListaMobileLista [data-produto-id="{self.produtos[0].pk}"] .sugestao-devolver-lista-mobile')
        self.resumo(t,13,0,'46,35','0,00');self.abrir(t);self.resumo(t,13,0,'46,35','0,00')
        self.sem_operacoes()

    def test_antiga_duas_escolhas_manter_nao_repete(self):
        self.lista.coleta_mobile=None;self.lista.save(update_fields=['coleta_mobile'])
        t=self.navegador();self.abrir(t)
        self.assertEqual(t.evaluate("document.querySelectorAll('[data-iniciar-coleta]').length"),2)
        self.capturar(t,'escolha-legada')
        self.iniciar(t,'iniciar_pego');self.resumo(t,0,2,'0,00','35,35')
        self.abrir(t);self.resumo(t,0,2,'0,00','35,35');self.sem_operacoes()

    def test_edicao_remocao_salvamento_e_geracao(self):
        t=self.navegador();self.abrir(t);self.iniciar(t);self.pegar(t)
        item=f'#itensListaMobileLista [data-produto-id="{self.produtos[0].pk}"]'
        self.tocar(t,item+' .sugestao-editar-lista-mobile')
        t.evaluate("const i=document.querySelector("+json.dumps(item)+");i.querySelector('.sugestao-item-lista-qtd-input').value='4';i.querySelector('.sugestao-item-lista-preco-input').value='11,25';i.querySelector('.sugestao-item-lista-preco-input').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,item+' .sugestao-editar-lista-mobile')
        self.resumo(t,1,1,'5,20','45,00')
        self.tocar(t,'#btnSalvarAlteracoesListaMobile')
        t.wait("location.pathname==="+json.dumps(reverse('estoque:compras_listas_fornecedor')))
        self.abrir(t);self.resumo(t,1,1,'5,20','45,00')
        self.tocar(t,f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[1].pk}"] .sugestao-remover-item')
        self.tocar(t,'#btnConfirmarRemoverItemListaMobile')
        self.resumo(t,0,1,'0,00','45,00');self.abrir(t);self.resumo(t,0,1,'0,00','45,00')
        self.capturar(t,'confirmada-edicao-remocao','#listaConfirmadaMobileTitulo')
        self.tocar(t,'#btnSalvarGerarCompraConfirmados')
        t.wait("location.pathname.includes('/compras/') && location.search.includes('continuar_itens=1')")
        compra=Compra.objects.get();self.assertEqual(compra.total,45);self.assertEqual(compra.itens.count(),1)
        self.produtos[0].refresh_from_db();self.assertEqual(self.produtos[0].quantidade,10)

    def test_remover_confirmado_nao_retorna_a_prelista(self):
        t=self.navegador();self.abrir(t);self.iniciar(t);self.pegar(t)
        self.tocar(t,'#itensListaMobileLista .sugestao-remover-lista-mobile')
        self.tocar(t,'#btnConfirmarRemoverItemListaMobile')
        self.resumo(t,1,0,'5,20','0,00');self.abrir(t);self.resumo(t,1,0,'5,20','0,00')

    def test_salvar_prelista_sem_confirmados_preserva_produtos_e_totais(self):
        t=self.navegador();self.abrir(t);self.iniciar(t)
        card=f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[1].pk}"]'
        self.tocar(t,card+' .sugestao-confirmar-card-mobile')
        t.evaluate("const c=document.querySelector("+json.dumps(card)+");c.querySelector('.sugestao-qtd-input').value='2';c.querySelector('.sugestao-qtd-input').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,card+' .sugestao-confirmar-card-mobile')
        self.resumo(t,2,0,'40,55','0,00')
        self.tocar(t,'#btnSalvarAlteracoesListaMobile')
        t.wait("location.pathname==="+json.dumps(reverse('estoque:compras_listas_fornecedor')))
        self.abrir(t);self.resumo(t,2,0,'40,55','0,00');self.sem_operacoes()

    def test_aba_desatualizada_nao_confirma_visualmente_nem_reinicia(self):
        t=self.navegador();self.abrir(t);self.iniciar(t)
        self.acao('mover',produto=str(self.produtos[1].pk),estado='pego')
        self.tocar(t,f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[0].pk}"] .sugestao-adicionar-lista-mobile')
        t.wait("document.querySelector('[data-coleta-aviso]').textContent.includes('Reabra')")
        self.assertFalse(t.evaluate("!!document.querySelector('#itensListaMobileLista [data-produto-id=\""+str(self.produtos[0].pk)+"\"]')"))
        self.abrir(t);self.resumo(t,1,1,'30,15','5,20');self.sem_operacoes()

    def test_arredondamento_meio_par_nas_duas_secoes(self):
        i=self.lista.itens.get(produto=self.produtos[0]);i.quantidade_final='0.005';i.preco_compra=1;i.total='0';i.save()
        t=self.navegador();self.abrir(t);self.iniciar(t)
        self.resumo(t,2,0,'5,20','0,00');self.pegar(t);self.resumo(t,1,1,'5,20','0,00')

    def test_desktop_visivel_igual_ao_template_publicado(self):
        t=self.navegador(1366);self.abrir(t)
        script="""(() => {const s=document.querySelector('.sugestao-page') || document.querySelector('main') || document.body;
          return [...s.querySelectorAll('input,button,select,h1,h2,td,th')].filter(e=>e.offsetWidth&&e.offsetHeight)
            .map(e=>{const r=e.getBoundingClientRect();return [e.tagName,e.id,e.value||e.textContent.trim(),Math.round(r.width),Math.round(r.height)]})})()"""
        atual=t.evaluate(script);self.assertFalse(t.evaluate("document.querySelector('[data-coleta-aviso]').offsetHeight>0"))
        card=TEMPLATE.parent/'includes/lista_fornecedor_produto_card.html'
        aprovados={p:p.read_bytes() for p in (TEMPLATE,card)}
        try:
            for p in aprovados:
                p.write_bytes(subprocess.check_output(['git','show','c2f6169:'+str(p.relative_to(Path(__file__).parent.parent)).replace('\\','/')]))
            t.call('Page.navigate',dict(url=self.live_server_url+self.url+'?comparar=1'))
            t.wait("document.readyState==='complete' && !!document.querySelector('#btnGravarListaFornecedor')")
            self.assertEqual(t.evaluate(script),atual)
        finally:
            for p,conteudo in aprovados.items():p.write_bytes(conteudo)
