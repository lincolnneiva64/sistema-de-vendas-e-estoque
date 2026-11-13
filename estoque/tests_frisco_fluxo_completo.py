"""Critérios A/B: salvar, reabrir e vincular Friscos sintéticos sem reajuste."""
import base64
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse
from estoque import tests_frisco_vinculo as anterior
from estoque import tests_decisoes_precos_browser as interacao
from estoque.models import Produto, Categoria, Unidade, MembroGrupoProduto, AlteracaoPrecoVinculado
from estoque.grupos_produtos import criar_grupo
from estoque.services.precos_vinculados import diagnosticar_grupo, regularizar_grupo
from offline.browser_support import Chrome

CAMPOS = ('pk','unidade_compra','unidade_venda_1','unidade_venda_2','vende_fracionado','fator_conversao',
    'quantidade','estoque_minimo','preco_compra','preco_compra_fracionado','preco_vista','preco_prazo',
    'preco_vista_fracionado','preco_prazo_fracionado','preco_venda','autoria_precos','precos_canonicos_adotados')


class FriscoCompletoFixture:
    def setUp(self):
        super().setUp()
        self.operador=get_user_model().objects.create_superuser('frisco-completo','','teste')
        self.client.force_login(self.operador)
        Categoria.objects.get_or_create(nome='Bebidas')
        for sigla in ('CX','UN'):
            Unidade.objects.get_or_create(sigla=sigla,defaults={'nome':sigla})
        self.produtos=[]
        for index,nome in enumerate(('Laranja','Maracuja','Morango','Uva','Abacaxi','Incompativel')):
            p=Produto.objects.create(nome='Frisco completo '+nome,categoria='Bebidas',
                unidade_compra='CX',unidade_venda_1='CX',unidade_venda_2='UN',vende_fracionado=True,
                estoque_minimo=0,fator_conversao=12 if index==5 else 18,quantidade=4 if index==0 else index+1,
                preco_compra=Decimal('11.70'),preco_compra_fracionado=Decimal('.65'),
                preco_vista=Decimal('15.80')+index,preco_prazo=Decimal('16.00')+index,
                preco_vista_fracionado=Decimal('1.00')+Decimal(index)/100,
                preco_prazo_fracionado=Decimal('1.10')+Decimal(index)/100)
            self.produtos.append(p)
        self.grupo=criar_grupo([p.pk for p in self.produtos[:3]],'Frisco completo 18/18G')
        regularizar_grupo(self.grupo.pk,diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],confirmar=True,operador=self.operador)
        self.url=reverse('estoque:grupos_produtos_detalhe',args=[self.grupo.pk])

    def estado(self):
        return list(Produto.objects.order_by('pk').values(*CAMPOS))


@skipUnless(Path(anterior.CHROME).is_file(),'Navegador indisponível')
class FriscoFluxoCompletoBrowserTests(FriscoCompletoFixture,StaticLiveServerTestCase):
    tocar = interacao.DecisoesBrowserTests.tocar
    def navegador(self,largura):
        pasta=TemporaryDirectory(prefix='frisco-completo-',ignore_cleanup_errors=True)
        self.addCleanup(pasta.cleanup)
        browser=Chrome(anterior.CHROME,pasta.name).start()
        self.addCleanup(browser.stop)
        t=browser.tab()
        t.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
        t.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
        return t

    def abrir(self,t,url):
        t.evaluate('window.anteriorFrisco=true;true')
        t.call('Page.navigate',{'url':self.live_server_url+url})
        t.wait("!window.anteriorFrisco && document.readyState==='complete'")

    def foto(self,t,nome):
        pasta=Path('amostras/frisco-completo');pasta.mkdir(parents=True,exist_ok=True)
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot',{'format':'png'})['data']))

    def salvar_sem_alterar(self,largura):
        t=self.navegador(largura)
        estado=self.estado()
        vinculos=list(MembroGrupoProduto.objects.order_by('pk').values_list('produto_id','grupo_id'))
        eventos=AlteracaoPrecoVinculado.objects.count()
        url=reverse('estoque:produto_editar',args=[self.produtos[0].pk])
        for tentativa in (1,2):
            self.abrir(t,url)
            t.wait("!!document.querySelector('#fator_visual')")
            dados=t.evaluate("({compra:document.querySelector('#id_unidade_compra').value,fator:document.querySelector('#id_fator_conversao').value,fracionado:document.querySelector('#id_vende_fracionado').value,unidade:document.querySelector('#id_unidade_venda_2').value,estoque:document.querySelector('#id_quantidade').value,visual:document.querySelector('#qtd_fracionada_visual').value,vista:document.querySelector('#id_preco_vista').value,prazo:document.querySelector('#id_preco_prazo').value})")
            self.assertEqual((dados['compra'],Decimal(dados['fator']),dados['fracionado'],dados['unidade']),('CX',18,'True','UN'))
            self.assertEqual(Decimal(dados['estoque'].replace(',','.')),4)
            self.assertEqual(Decimal(dados['visual'].replace(',','.')),72)
            self.assertEqual((Decimal(dados['vista']),Decimal(dados['prazo'])),(Decimal('15.80'),16))
            if tentativa == 2:
                t.evaluate("document.querySelector('#fator_visual').value='18,0';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));true")
            self.tocar(t,'.btn-salvar')
            try:
                t.wait("location.pathname==='/' && document.readyState==='complete'")
            except AssertionError:
                self.fail(str(t.evaluate("({url:location.pathname,modal:!!document.querySelector('.ps-confirm[open]'),erro:document.querySelector('#alerta-erros-produto')?.innerText,avisos:[...document.querySelectorAll('.precos-seletivos-feedback')].map(e=>e.textContent)})")))
            self.assertEqual(self.estado(),estado)
            self.assertEqual(AlteracaoPrecoVinculado.objects.count(),eventos)
            self.assertEqual(list(MembroGrupoProduto.objects.order_by('pk').values_list('produto_id','grupo_id')),vinculos)
        self.abrir(t,url)
        t.wait("!!document.querySelector('#fator_visual')")
        t.evaluate("document.querySelector('#fator_visual').scrollIntoView({block:'center',behavior:'instant'});true")
        self.foto(t,str(largura)+'-salvou-reabriu-duas-vezes')
        print('FRISCO_A '+json.dumps({'largura':largura,'salvamentos':2,'estado_preservado':True}),flush=True)
        return t

    def abrir_grupo(self,t):
        self.abrir(t,'/')
        t.wait("!!document.querySelector('#grupoProdutoGerenciar')")
        t.evaluate("document.querySelector('#grupoProdutoGerenciar').click();true")
        t.wait("!!document.querySelector('[data-grupo-id] button')")
        t.evaluate("document.querySelector('[data-grupo-id] button').click();true")
        t.wait("document.querySelector('#grupoProdutoModal').open && !!document.querySelector('#grupoProdutoMembros').textContent.trim()")

    def verificar_vinculo(self,t,largura):
        estado=self.estado()
        eventos=AlteracaoPrecoVinculado.objects.count()
        self.abrir_grupo(t)
        t.evaluate("document.querySelector('#grupoProdutoAdicionar').click();true")
        t.wait("document.querySelectorAll('#grupoProdutoElegiveis input').length===3")
        t.evaluate("document.querySelectorAll('#grupoProdutoElegiveis input').forEach(c=>c.click());document.querySelector('#grupoProdutoConfirmarAdicao').click();true")
        t.wait("document.querySelectorAll('#grupoProdutoSituacaoAdicao [data-produto-id]').length===3")
        self.assertEqual(t.evaluate("document.querySelectorAll('#grupoProdutoSituacaoAdicao [data-compativel=\"1\"]').length"),2)
        self.assertEqual(t.evaluate("document.querySelectorAll('#grupoProdutoSituacaoAdicao [data-compativel=\"0\"]').length"),1)
        self.assertIn('Fator',t.evaluate("document.querySelector('#grupoProdutoSituacaoAdicao [data-compativel=\"0\"]').textContent"))
        self.assertIn('Incompativel',t.evaluate("document.querySelector('#grupoProdutoErro').textContent"))
        self.assertEqual(self.grupo.produtos.count(),3)
        self.assertEqual(self.estado(),estado)
        t.evaluate("document.querySelector('#grupoProdutoSituacaoAdicao').scrollIntoView({block:'center',behavior:'instant'});true")
        self.foto(t,str(largura)+'-compativeis-e-impedido')
        seletor='#grupoProdutoElegiveis input[value="'+str(self.produtos[5].pk)+'"]'
        t.evaluate('document.querySelector('+json.dumps(seletor)+').click();document.querySelector("#grupoProdutoConfirmarAdicao").click();true')
        t.wait("!document.querySelector('#grupoProdutoAplicarAdicao').hidden")
        t.evaluate("document.querySelector('#grupoProdutoAdicaoCompatibilidade').click();document.querySelector('#grupoProdutoAplicarAdicao').click();true")
        t.wait("document.querySelector('#grupoProdutoSucesso').textContent.includes('adicionados')")
        self.assertEqual(self.grupo.produtos.count(),5)
        self.assertEqual(self.estado(),estado)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),eventos+1)
        evento=AlteracaoPrecoVinculado.objects.order_by('pk').last()
        self.assertEqual(evento.origem,'entrada_integrantes')
        self.assertFalse(any(evento.evidencia['alteracoes'].values()))
        # Nova requisição e nova consulta do grupo: não é apenas estado do modal.
        self.abrir_grupo(t)
        texto=t.evaluate("document.querySelector('#grupoProdutoMembros').textContent")
        self.assertIn('Uva',texto);self.assertIn('Abacaxi',texto)
        self.assertNotIn('Incompativel',texto)
        self.assertEqual(set(self.grupo.produtos.values_list('pk',flat=True)),{p.pk for p in self.produtos[:5]})
        self.assertEqual(self.estado(),estado)
        self.foto(t,str(largura)+'-vinculos-reabertos')
        p=self.produtos[5]
        previa=self.client.post(self.url,{'acao':'previa_adicao','produto_ids':[p.pk]}).json()
        self.assertFalse(previa['avaliacao_adicao'][0]['compativel'])
        resposta=self.client.post(self.url,{'acao':'adicionar','produto_ids':[p.pk],'confirmar':'1','versao_observada':previa['precos_vinculados']['versao_observada']})
        self.assertEqual(resposta.status_code,400)
        self.assertEqual(self.grupo.produtos.count(),5)
        self.assertEqual(self.estado(),estado)
        print('FRISCO_B '+json.dumps({'largura':largura,'vinculos_persistidos':5,'precos_e_saldos_preservados':True}),flush=True)

    def test_ab_desktop_salvar_reabrir_e_vincular(self):
        self.verificar_vinculo(self.salvar_sem_alterar(1280),1280)

    def test_ab_mobile_salvar_reabrir_e_vincular(self):
        self.verificar_vinculo(self.salvar_sem_alterar(390),390)
    def test_mudanca_real_informa_campo_e_permite_restaurar_sem_reabrir(self):
        t=self.navegador(1280)
        estado=self.estado()
        self.abrir(t,reverse('estoque:produto_editar',args=[self.produtos[0].pk]))
        t.wait("!!document.querySelector('#fator_visual')")
        t.evaluate("document.querySelector('#fator_visual').value='20';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#id_preco_vista').value='16.80';document.querySelector('#id_preco_vista').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#form-produto').requestSubmit();true")
        t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
        mensagem=t.evaluate("document.querySelector('#confirmarApresentacao').textContent")
        self.assertIn('Fator de conversão: 18.00 → 20',mensagem)
        self.assertIn('saldo ou o mínimo',mensagem)
        self.assertNotIn('Salve primeiro',mensagem)
        self.assertTrue(t.evaluate("document.querySelector('[data-confirmar-apresentacao]').disabled"))
        self.assertEqual(self.estado(),estado)
        self.tocar(t,'[data-cancelar-apresentacao]')
        t.evaluate("document.querySelector('#fator_visual').value='18,00';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#form-produto').requestSubmit();true")
        t.wait("!!document.querySelector('.ps-confirm[open]')")
        self.assertEqual(self.estado(),estado)
    def test_fator_vazio_nao_e_convertido_automaticamente_para_um(self):
        t=self.navegador(1280)
        estado=self.estado()
        self.abrir(t,reverse('estoque:produto_editar',args=[self.produtos[0].pk]))
        t.wait("!!document.querySelector('#fator_visual')")
        t.evaluate("document.querySelector('#fator_visual').value='';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#form-produto').requestSubmit();true")
        t.wait("document.querySelector('#apresentacaoPendente').textContent.includes('maior que zero')")
        self.assertEqual(t.evaluate("document.querySelector('#id_fator_conversao').value"),'')
        self.assertEqual(t.evaluate("document.querySelector('#fator_visual').value"),'')
        self.assertEqual(self.estado(),estado)
        dados=anterior.FriscoDiagnosticoTests.editor_payload(self,self.produtos[0])
        dados['fator_conversao']=''
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[0].pk]),dados)
        self.assertEqual(resposta.status_code,200)
        self.assertTrue(resposta.context['form'].errors)
        self.assertEqual(self.estado(),estado)
