"""Existing purchase modal in a real browser, only synthetic fixtures."""
import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from offline.browser_support import Chrome
from .models import Compra, MovimentoFinanceiro, Produto, ItemCompra, ContaFinanceira
from .tests_decisoes_precos_compra import DecisoesFixture

CHROME = os.environ.get('COMPRA_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class DecisoesBrowserTests(DecisoesFixture, StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        MovimentoFinanceiro.objects.create(conta=ContaFinanceira.objects.get(tipo='banco'),
            tipo='entrada',valor=2000,data='2026-10-29',descricao='Saldo exclusivamente fictício')
        self.movimentos_iniciais=1

    def navegador(self):
        profile = TemporaryDirectory(prefix='decisoes-browser-', ignore_cleanup_errors=True)
        self.addCleanup(profile.cleanup)
        chrome = Chrome(CHROME, profile.name).start()
        self.addCleanup(chrome.stop)
        tab = chrome.tab()
        tab.call('Page.addScriptToEvaluateOnNewDocument',dict(source="window.traceTeste=[];const fetchTeste=fetch;window.fetch=async(...a)=>{const r=await fetchTeste(...a);traceTeste.push(['fetch',String(a[0]),a[1]?.method||'GET',r.status]);return r};document.addEventListener('click',e=>traceTeste.push(['bubble',e.target.id]));addEventListener('click',e=>traceTeste.push(['click',e.target.id]),true);addEventListener('invalid',e=>traceTeste.push(['invalid',e.target.name,e.target.form?.id]),true);addEventListener('submit',e=>traceTeste.push(['submit',e.target.id]),true);window.errosTeste=[];addEventListener('error',e=>errosTeste.push(e.message));addEventListener('unhandledrejection',e=>errosTeste.push(String(e.reason)));") )
        tab.call('Network.enable')
        tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value,
                                          url=self.live_server_url))
        tab.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=True))
        tab.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
        return tab

    def tocar(self,t,seletor):
        ponto=t.evaluate("(async () => {const e=document.querySelector("+json.dumps(seletor)+
            ");if(!e||e.disabled)return null;e.scrollIntoView({block:'center',behavior:'instant'});"
            "await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const r=e.getBoundingClientRect();"
            "if(r.width<=0||r.height<=0)return null;const x=r.x+r.width/2,y=r.y+r.height/2;"
            "if(y<0||y>innerHeight||!e.contains(document.elementFromPoint(x,y)))return null;return {x,y}})()")
        if ponto is None:
            self.fail('Acao inacessivel: '+seletor+' '+str(t.evaluate("(() => {const e=document.querySelector("+json.dumps(seletor)+");const r=e?.getBoundingClientRect();return {rect:r?.toJSON(),disabled:e?.disabled,viewport:[innerWidth,innerHeight],cobre:r?document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.outerHTML:null}})()")))
        if t.evaluate("matchMedia('(max-width:820px)').matches"):
            t.call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[ponto]))
            t.call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
        else:
            for tipo in ('mousePressed','mouseReleased'):
                t.call('Input.dispatchMouseEvent',dict(type=tipo,button='left',clickCount=1,**ponto))

    def abrir(self, t, nome='compra_editar'):
        url = reverse('estoque:'+nome, args=[self.compra.pk])
        t.call('Page.navigate', dict(url=self.live_server_url+url))
        t.wait("document.readyState==='complete' && !!document.querySelector('#btnSalvarCompra')")

    def revisar(self, t, total='555,00'):
        self.tocar(t,'#btnSalvarCompra')
        t.wait("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')")
        t.evaluate("document.querySelector('#origemReservaCompra').value='0,00';document.querySelector('#origemBancoCompra').value="+json.dumps(total)+";document.querySelector('#origemBancoCompra').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,'#btnConfirmarFechamentoCompra')
        t.wait("document.querySelector('#modalConferenciaPrecosCompra').classList.contains('visivel')")

    def conferir_final(self, t):
        try:
            t.wait("location.pathname==='/compras/'")
        except AssertionError:
            self.fail(str(t.evaluate("({financeiro:[...document.querySelectorAll('#origemDinheiroCompra input')].map(e=>[e.name,e.value]),confirmarDesabilitado:document.querySelector('#btnConfirmarFechamentoCompra')?.disabled,trace:traceTeste,erros:errosTeste,erro:document.querySelector('#erroSalvarPreRevisaoCompra')?.textContent,modo:document.querySelector('#decisaoPrecosCompra')?.value,decisoes:document.querySelector('#decisoesPrecosItensCompra')?.value,modal:document.querySelector('#modalConferenciaPrecosCompra')?.className,form:JSON.stringify(document.querySelector('#formCompra')?.dataset),invalidos:[...document.querySelectorAll('#formCompra :invalid')].map(c=>c.name)})")))
        self.produto.refresh_from_db()
        compra = Compra.objects.get(itens__produto=self.produto, status='finalizada')
        self.assertEqual(self.produto.quantidade, 25)
        self.assertEqual(self.produto.preco_compra, 37)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 1)
        return compra

    def test_mobile_manter_sem_alteracao_automatica(self):
        t=self.navegador();self.abrir(t);self.revisar(t)
        self.capturar(t,'escolha-mobile')
        self.produto.refresh_from_db();self.assertEqual(self.produto.preco_compra,36)
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        self.capturar(t,'revisar-mobile')
        self.assertIn('36,00',t.evaluate("document.querySelector('#listaConferenciaPrecosCompra').innerText"))
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        compra=self.conferir_final(t)
        self.assertFalse(compra.revisao_precos_pendente)
        self.assertEqual(self.produto.preco_vista,45)

    def test_mobile_alterar_apenas_campo_selecionado(self):
        t=self.navegador();self.abrir(t,'compra_finalizar');self.revisar(t)
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        t.evaluate("document.querySelectorAll('#listaConferenciaPrecosCompra .compras-preco-simulador').forEach(s=>{const vista=s.dataset.campoPreco==='preco_vista';s.querySelector('.marcarAtualizarVendaCompra').checked=vista;if(vista)s.querySelector('.campoNovoPrecoVendaCompra').value='47,00'});true")
        self.tocar(t,'#btnAtualizarPrecosContinuarCompra')
        self.conferir_final(t)
        self.assertEqual(self.produto.preco_vista,47)
        self.assertEqual(self.produto.preco_prazo,46)

    def test_mobile_adiar_e_manter_na_revisao_posterior(self):
        t=self.navegador();self.abrir(t);self.revisar(t)
        self.tocar(t,'#btnFinalizarRevisarDepoisCompra')
        compra=self.conferir_final(t)
        self.assertTrue(compra.revisao_precos_pendente)
        self.assertEqual(self.produto.preco_vista,45)
        t.call('Page.navigate',dict(url=self.live_server_url+reverse('estoque:vendas')))
        t.wait("document.querySelector('#revisaoPrecosPosterior')?.classList.contains('visivel')")
        self.tocar(t,'#rpAgora')
        t.wait("!document.querySelector('#rpSalvar').hidden")
        self.capturar(t,'posterior-mobile')
        self.tocar(t,'#rpManter')
        try:
            t.wait("!document.querySelector('#revisaoPrecosPosterior').classList.contains('visivel')")
        except AssertionError:
            self.fail(str(t.evaluate("({erro:document.querySelector('#rpErro').innerText,erros:errosTeste,trace:traceTeste,offline:JSON.stringify(document.querySelector('#offline-global')?.dataset),banner:document.querySelector('#sales-offline-notice')?.textContent,manter:document.querySelector('#rpManter').outerHTML})")))
        compra.refresh_from_db();self.produto.refresh_from_db()
        self.assertFalse(compra.revisao_precos_pendente)
        self.assertEqual(self.produto.preco_vista,45)
        self.assertTrue(compra.itens.get().revisao_preco_concluida)
        self.assertEqual(t.evaluate("document.querySelector('#operadorVenda').value"),'')
        self.tocar(t,'#btnGravarVenda')
        t.wait("document.querySelector('#modal-operador-obrigatorio')?.offsetHeight>0")

    def test_mobile_previa_desatualizada_preserva_formulario_e_permite_nova_revisao(self):
        t=self.navegador();self.abrir(t);self.revisar(t)
        marco=t.evaluate('performance.timeOrigin')
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        t.evaluate("document.querySelectorAll('#listaConferenciaPrecosCompra .compras-preco-simulador').forEach(s=>{const vista=s.dataset.campoPreco==='preco_vista';s.querySelector('.marcarAtualizarVendaCompra').checked=vista;if(vista)s.querySelector('.campoNovoPrecoVendaCompra').value='47,00'});true")
        self.produto.preco_prazo=49;self.produto.save(update_fields=['preco_prazo'])
        self.tocar(t,'#btnAtualizarPrecosContinuarCompra')
        t.wait("!document.querySelector('#erroSalvarPreRevisaoCompra').hidden && !document.querySelector('#btnSalvarCompra').disabled")
        self.assertEqual(t.evaluate('performance.timeOrigin'),marco)
        self.assertEqual(t.evaluate("document.querySelector('#origemBancoCompra').value"),'555,00')
        self.assertIn('decisão',t.evaluate("document.querySelector('#erroSalvarPreRevisaoCompra').innerText"))
        self.sem_efeitos()
        self.capturar(t,'dados-desatualizados-mobile')
        self.tocar(t,'#btnSalvarCompra')
        try:
            t.wait("document.querySelector('#modalConferenciaPrecosCompra').classList.contains('visivel')")
        except AssertionError:
            self.fail(str(t.evaluate("({trace:traceTeste,erros:errosTeste,form:JSON.stringify(document.querySelector('#formCompra').dataset),erro:document.querySelector('#erroSalvarPreRevisaoCompra').innerText})")))
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        self.assertEqual(t.evaluate("document.querySelector('#listaConferenciaPrecosCompra [data-campo-preco=preco_vista] .campoNovoPrecoVendaCompra').value"),'47,00')
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        self.conferir_final(t)
        self.assertEqual(self.produto.preco_prazo,49)

    def test_mobile_compra_nova_mesma_janela(self):
        t=self.navegador()
        t.call('Page.navigate',dict(url=self.live_server_url+reverse('estoque:compras_nova')+'?continuar_itens=1'))
        t.wait("document.readyState==='complete' && !!document.querySelector('#btnSalvarCompra')")
        t.evaluate("document.querySelector('[name=fornecedor_id]').value="+json.dumps(str(self.fornecedor.pk))+";document.querySelector('[name=fornecedor_busca]').value='Fornecedor ficticio';document.querySelector('#tipoPagamentoCompra').value='avista';document.querySelector('#tipoPagamentoCompra').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#tbodyAdicionarProdutoMobile [name=\"produto_busca[]\"]').focus();true")
        t.call('Input.insertText',dict(text=self.produto.nome))
        t.wait("!!document.querySelector('#tbodyAdicionarProdutoMobile .produto-sugestao')")
        t.evaluate("document.querySelector('#tbodyAdicionarProdutoMobile .produto-sugestao').click();const l=document.querySelector('#tbodyAdicionarProdutoMobile .linha-item');l.querySelector('[name=\"quantidade[]\"]').value='15';l.querySelector('.campo-preco-unitario-compra').value='37,00';l.querySelector('.campo-preco-unitario-compra').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#btnAdicionarItem').click();true")
        self.revisar(t)
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        t.evaluate("true")
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        self.conferir_final(t)
        self.assertEqual(self.produto.preco_vista,45)

    def test_mobile_dois_produtos_decisoes_individuais(self):
        outro=Produto.objects.create(nome='Outro fictício',quantidade=10,preco_compra=20,
            preco_vista=30,preco_prazo=31,unidade_compra='CX',unidade_venda_1='CX')
        ItemCompra.objects.create(compra=self.compra,produto=outro,quantidade=1,unidade='CX',preco_unitario=21,valor_total=21)
        self.compra.total=576;self.compra.save(update_fields=['total'])
        t=self.navegador();self.abrir(t);self.revisar(t,'576,00')
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        t.evaluate("true")
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        self.compra.refresh_from_db();self.assertEqual(self.compra.status,'rascunho')
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        self.conferir_final(t);outro.refresh_from_db()
        self.assertEqual(outro.preco_compra,21)
        self.assertEqual(outro.preco_vista,30)

    def test_desktop_rascunho_mesma_decisao(self):
        t=self.navegador()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=1366,height=900,deviceScaleFactor=1,mobile=False))
        self.abrir(t)
        self.tocar(t,'#btnSalvarCompra')
        t.wait("document.querySelector('#modalConferenciaPrecosCompra').classList.contains('visivel')")
        self.capturar(t,'revisar-desktop')
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        t.wait("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')")
        t.evaluate("document.querySelector('#origemCaixaCompra').value='0,00';document.querySelector('#origemReservaCompra').value='0,00';document.querySelector('#origemBancoCompra').value='555,00';document.querySelector('#origemBancoCompra').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,'#btnConfirmarFechamentoCompra')
        self.conferir_final(t)
        self.assertEqual(self.produto.preco_vista,45)

    def test_mobile_catalogo_passou_a_igualar_custo_nao_cria_etapa_extra(self):
        t=self.navegador();self.abrir(t)
        self.produto.preco_compra=37;self.produto.save(update_fields=['preco_compra'])
        self.tocar(t,'#btnSalvarCompra')
        t.wait("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')")
        t.evaluate("document.querySelector('#origemReservaCompra').value='0,00';document.querySelector('#origemBancoCompra').value='555,00';document.querySelector('#origemBancoCompra').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,'#btnConfirmarFechamentoCompra')
        self.conferir_final(t)
        self.assertEqual(self.produto.preco_vista,45)

    def test_desktop_revisar_depois_preserva_etapa_financeira(self):
        t=self.navegador()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=1366,height=900,deviceScaleFactor=1,mobile=False))
        self.abrir(t)
        self.tocar(t,'#btnSalvarCompra')
        t.wait("document.querySelector('#modalConferenciaPrecosCompra').classList.contains('visivel')")
        self.tocar(t,'#btnFinalizarRevisarDepoisCompra')
        t.wait("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')")
        self.compra.refresh_from_db();self.assertEqual(self.compra.status,'rascunho')
        t.evaluate("document.querySelector('#origemCaixaCompra').value='0,00';document.querySelector('#origemReservaCompra').value='0,00';document.querySelector('#origemBancoCompra').value='555,00';document.querySelector('#origemBancoCompra').dispatchEvent(new Event('input',{bubbles:true}));true")
        self.tocar(t,'#btnConfirmarFechamentoCompra')
        compra=self.conferir_final(t)
        self.assertTrue(compra.revisao_precos_pendente)
        self.assertEqual(self.produto.preco_vista,45)

    def test_resposta_perdida_nao_reenvia_nem_duplica_finalizacao(self):
        t=self.navegador();self.abrir(t);self.revisar(t)
        self.tocar(t,'#btnRevisarPrecosAgoraCompra')
        t.evaluate("window.fetchAntesPerda=fetch;window.fetch=async(...args)=>{const resposta=await fetchAntesPerda(...args);if(args[1]?.headers?.['X-Compra-Decisao']==='1')throw new TypeError('Resposta fictícia perdida');return resposta};true")
        self.tocar(t,'#btnContinuarSemAtualizarPrecosCompra')
        t.wait("!document.querySelector('#erroSalvarPreRevisaoCompra').hidden && !document.querySelector('#btnSalvarCompra').disabled")
        self.compra.refresh_from_db();self.produto.refresh_from_db()
        self.assertEqual(self.compra.status,'finalizada')
        self.assertEqual(self.produto.quantidade,25)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)
        self.assertIn('Sem confirmação',t.evaluate("document.querySelector('#erroSalvarPreRevisaoCompra').textContent"))
        t.evaluate("window.fetch=fetchAntesPerda;true")
        self.tocar(t,'#btnSalvarCompra')
        t.wait("document.querySelector('#erroSalvarPreRevisaoCompra').textContent.includes('finalizada')")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade,25)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)

    @staticmethod
    def capturar(t,nome):
        pasta=Path('amostras/decisoes-precos');pasta.mkdir(parents=True,exist_ok=True)
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot',dict(format='png'))['data']))
