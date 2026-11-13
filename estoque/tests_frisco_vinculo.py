"""Produtos Frisco sintéticos; sem dados operacionais."""
import json
import os
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse
from estoque.models import Produto, Categoria, Unidade, MembroGrupoProduto
from estoque.grupos_produtos import criar_grupo
from estoque.services.precos_vinculados import diagnosticar_grupo, regularizar_grupo
from offline.browser_support import Chrome


CHROME = os.environ.get('COMPRA_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


class FriscoFixture:
    def setUp(self):
        super().setUp()
        self.operador = get_user_model().objects.create_superuser('frisco-sintetico', '', 'teste')
        self.client.force_login(self.operador)
        Categoria.objects.get_or_create(nome='Bebidas')
        for unidade in ('CX','UN'):
            Unidade.objects.get_or_create(sigla=unidade, defaults={'nome':unidade})
        self.produtos = [Produto.objects.create(nome='Frisco teste '+sabor, categoria='Bebidas',
            unidade_compra='CX', unidade_venda_1='CX', vende_fracionado=sabor!='Uva',
            unidade_venda_2='UN', fator_conversao=18, preco_compra=12,
            preco_compra_fracionado=Decimal('.67'), preco_vista=Decimal('14.70'), preco_prazo=Decimal('15.50'),
            preco_vista_fracionado=Decimal('.90'), preco_prazo_fracionado=Decimal('.95'), quantidade=10)
            for sabor in ('Laranja','Maracuja','Morango','Uva','Abacaxi')]
        self.grupo = criar_grupo([p.pk for p in self.produtos[:3]], 'Frisco teste 18/18G')
        regularizar_grupo(self.grupo.pk, diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],
            confirmar=True, operador=self.operador)
        self.url = reverse('estoque:grupos_produtos_detalhe', args=[self.grupo.pk])


class FriscoDiagnosticoTests(FriscoFixture, TestCase):
    def test_regra_entrada_aponta_fracionamento_real(self):
        for produto, bloqueado in ((self.produtos[3], True),(self.produtos[4],False)):
            previa = self.client.post(self.url, {'acao':'previa_adicao','produto_ids':[produto.pk]}).json()['precos_vinculados']
            self.assertEqual(bool(previa['bloqueios']), bloqueado)
            if bloqueado:
                self.assertIn('vende_fracionado', {i['campo'] for i in previa['incompatibilidades']})
            self.assertFalse(MembroGrupoProduto.objects.filter(produto=produto).exists())

    def test_entrada_conjunta_incompativel_nao_vincula_abacaxi_parcialmente(self):
        ids = [p.pk for p in self.produtos[3:]]
        antes = list(Produto.objects.order_by('pk').values())
        previa = self.client.post(self.url, {'acao':'previa_adicao','produto_ids':ids}).json()['precos_vinculados']
        resposta = self.client.post(self.url, {'acao':'adicionar','produto_ids':ids,'confirmar':'1','versao_observada':previa['versao_observada']})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self.grupo.produtos.count(),3)
        self.assertEqual(list(Produto.objects.order_by('pk').values()), antes)

    def editor_payload(self, produto):
        resposta = self.client.get(reverse('estoque:produto_editar',args=[produto.pk]))
        form = resposta.context['form']
        dados = {k:v for k,v in form.initial.items() if v is not None}
        dados.pop('fornecedores', None)
        return dados

    def test_abacaxi_compativel_entra_sem_novo_salvamento(self):
        abacaxi = self.produtos[4]
        campos = ('pk','quantidade','preco_compra','preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado','fator_conversao','vende_fracionado')
        antes = list(Produto.objects.order_by('pk').values_list(*campos))
        previa = self.client.post(self.url, {'acao':'previa_adicao','produto_ids':[abacaxi.pk]}).json()['precos_vinculados']
        self.assertFalse(previa['bloqueios'])
        resposta = self.client.post(self.url, {'acao':'adicionar','produto_ids':[abacaxi.pk],'confirmar':'1','versao_observada':previa['versao_observada']})
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertEqual(self.grupo.produtos.count(),4)
        self.assertEqual(list(Produto.objects.order_by('pk').values_list(*campos)), antes)

    def test_corrigir_configuracao_uva_salvar_e_vincular_ambos(self):
        uva = self.produtos[3]
        dados = self.editor_payload(uva)
        dados.update(vende_fracionado='True', fator_conversao='18.00', unidade_venda_2='UN')
        resposta = self.client.post(reverse('estoque:produto_editar',args=[uva.pk]), dados)
        self.assertEqual(resposta.status_code, 302, resposta.context and resposta.context['form'].errors)
        uva.refresh_from_db()
        self.assertEqual((uva.unidade_compra,uva.unidade_venda_1,uva.unidade_venda_2,uva.fator_conversao,uva.vende_fracionado), ('CX','CX','UN',Decimal('18'),True))
        campos = ('pk','quantidade','preco_compra','preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado')
        antes = list(Produto.objects.order_by('pk').values_list(*campos))
        ids = [p.pk for p in self.produtos[3:]]
        previa = self.client.post(self.url, {'acao':'previa_adicao','produto_ids':ids}).json()['precos_vinculados']
        self.assertFalse(previa['bloqueios'])
        resposta = self.client.post(self.url, {'acao':'adicionar','produto_ids':ids,'confirmar':'1','versao_observada':previa['versao_observada']})
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertEqual(self.grupo.produtos.count(),5)
        self.assertEqual(list(Produto.objects.order_by('pk').values_list(*campos)), antes)

    def test_mudanca_real_apresentacao_e_preco_continua_bloqueada(self):
        p = self.produtos[0]
        dados = self.editor_payload(p)
        dados.update(fator_conversao='20',preco_vista='15.20')
        antes = list(Produto.objects.order_by('pk').values())
        resposta = self.client.post(reverse('estoque:produto_editar',args=[p.pk]),dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertIn('Fator de conversão: 18.00 → 20', str(resposta.context['form'].errors))
        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)

    def test_formulario_invalido_nao_persiste_configuracao_visual(self):
        p = self.produtos[3]
        dados = self.editor_payload(p)
        dados.update(vende_fracionado='True',fator_conversao='18.00',unidade_venda_2='UN',categoria='')
        resposta = self.client.post(reverse('estoque:produto_editar',args=[p.pk]),dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.context['form'].errors)
        p.refresh_from_db()
        self.assertFalse(p.vende_fracionado)
        previa = self.client.post(self.url, {'acao':'previa_adicao','produto_ids':[p.pk]}).json()['precos_vinculados']
        self.assertTrue(previa['bloqueios'])


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class FriscoDiagnosticoBrowserTests(FriscoFixture, StaticLiveServerTestCase):
    def verificar_editor(self, largura):
        with TemporaryDirectory(prefix='frisco-browser-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(CHROME, perfil).start()
            try:
                t = browser.tab()
                t.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
                t.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                t.call('Page.navigate', {'url':self.live_server_url+reverse('estoque:produto_editar',args=[self.produtos[0].pk])})
                t.wait("document.readyState==='complete' && !!document.querySelector('#fator_visual')")
                dados=t.evaluate("({real:document.querySelector('#id_fator_conversao').value,atributo:document.querySelector('#id_fator_conversao').getAttribute('value'),visual:document.querySelector('#fator_visual').value,vista:document.querySelector('#id_preco_vista').value,prazo:document.querySelector('#id_preco_prazo').value,fracionado:document.querySelector('#id_vende_fracionado').value,observada:JSON.parse(document.querySelector('#produto-apresentacao-observada').textContent)})")
                print('FRISCO_EDITOR '+json.dumps(dados),flush=True)
                self.assertEqual(Decimal(dados['real'].replace(',','.')), Decimal('18'))
                self.assertEqual(dados['visual'], '18,00')
                self.assertEqual(Decimal(dados['vista']), Decimal('14.70'))
                self.assertEqual(Decimal(dados['prazo']), Decimal('15.50'))
                t.evaluate("document.querySelector('#fator_visual').scrollIntoView({block:'center',behavior:'instant'});true")
                self.foto(t, str(largura)+'-configuracao')
                t.evaluate("document.querySelector('#fator_visual').value='18,00';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));true")
                self.assertEqual(t.evaluate("document.querySelector('#id_fator_conversao').value"), '18.00')
                t.evaluate("document.querySelector('#id_preco_vista').value='15.20';document.querySelector('#id_preco_vista').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#form-produto').requestSubmit();true")
                t.wait("!!document.querySelector('.ps-confirm[open]')")
                self.assertFalse(t.evaluate("[...document.querySelectorAll('.precos-seletivos-feedback')].some(e=>e.textContent.includes('Salve primeiro'))"))
                t.evaluate("[...document.querySelectorAll('.ps-confirm[open] button')].find(b=>b.textContent.includes('Confirmar selecionados')).click();true")
                try:
                    t.wait("location.pathname==='/' && document.readyState==='complete'")
                except AssertionError:
                    self.fail(str(t.evaluate("({url:location.href,erros:document.querySelector('#alerta-erros-produto')?.innerText,avisos:[...document.querySelectorAll('.precos-seletivos-feedback')].map(e=>e.textContent),invalidos:[...document.querySelectorAll('#form-produto :invalid')].map(e=>[e.name,e.value,e.validationMessage]),fator:document.querySelector('#id_fator_conversao')?.value})")))
                self.produtos[0].refresh_from_db()
                self.assertEqual(self.produtos[0].fator_conversao, Decimal('18'))
                self.assertEqual(self.produtos[0].preco_vista, Decimal('15.20'))
                self.assertEqual(list(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[1:3]]).values_list('preco_vista',flat=True)), [Decimal('14.70')]*2)
            finally:
                browser.stop()

    def foto(self, t, nome):
        pasta=Path('amostras/frisco-vinculo')
        pasta.mkdir(parents=True,exist_ok=True)
        t.evaluate("(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()")
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot',{'format':'png'})['data']))

    def test_editor_desktop_sem_salvamento_redundante(self):
        self.verificar_editor(1280)

    def test_editor_mobile_sem_salvamento_redundante(self):
        self.verificar_editor(390)

    def test_previa_visual_bloqueia_uva_e_permite_abacaxi_sem_salvar(self):
        with TemporaryDirectory(prefix='frisco-grupo-browser-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(CHROME,perfil).start()
            try:
                t=browser.tab()
                t.call('Emulation.setDeviceMetricsOverride',dict(width=1280,height=900,deviceScaleFactor=1,mobile=False))
                t.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                t.call('Page.navigate',{'url':self.live_server_url+'/'})
                t.wait("document.readyState==='complete' && !!document.querySelector('#grupoProdutoGerenciar')")
                t.evaluate("document.querySelector('#grupoProdutoGerenciar').click();true")
                t.wait("!!document.querySelector('[data-grupo-id] button')")
                t.evaluate("document.querySelector('[data-grupo-id] button').click();true")
                t.wait("document.querySelector('#grupoProdutoModal').open")
                t.evaluate("document.querySelector('#grupoProdutoAdicionar').click();true")
                t.wait("document.querySelectorAll('#grupoProdutoElegiveis input').length===2")
                campos=('pk','quantidade','preco_compra','preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado')
                antes=list(Produto.objects.order_by('pk').values_list(*campos))
                t.evaluate("document.querySelectorAll('#grupoProdutoElegiveis input').forEach(c=>c.click());document.querySelector('#grupoProdutoConfirmarAdicao').click();true")
                t.wait("document.querySelector('#grupoProdutoErro').textContent.includes('Entrada bloqueada')")
                self.assertEqual(self.grupo.produtos.count(),3)
                self.assertIn('Venda fracionada',t.evaluate("document.querySelector('#grupoProdutoIncompatibilidades').textContent"))
                self.assertIn('Uva',t.evaluate("document.querySelector('#grupoProdutoIncompatibilidades').textContent"))
                t.evaluate("document.querySelector('#grupoProdutoIncompatibilidades').scrollIntoView({block:'center',behavior:'instant'});true")
                self.foto(t,'previa-bloqueada')
                t.evaluate("document.querySelector('#grupoProdutoElegiveis input[value=\""+str(self.produtos[3].pk)+"\"]').click();document.querySelector('#grupoProdutoConfirmarAdicao').click();true")
                t.wait("!document.querySelector('#grupoProdutoAplicarAdicao').hidden")
                self.assertEqual(self.grupo.produtos.count(),3)
                t.evaluate("document.querySelector('#grupoProdutoAdicaoCompatibilidade').click();document.querySelector('#grupoProdutoAplicarAdicao').click();true")
                t.wait("document.querySelector('#grupoProdutoSucesso').textContent.includes('adicionados')")
                self.assertEqual(self.grupo.produtos.count(),4)
                self.assertTrue(MembroGrupoProduto.objects.filter(produto=self.produtos[4],grupo=self.grupo).exists())
                self.assertFalse(MembroGrupoProduto.objects.filter(produto=self.produtos[3]).exists())
                self.assertEqual(list(Produto.objects.order_by('pk').values_list(*campos)),antes)
            finally:
                browser.stop()
