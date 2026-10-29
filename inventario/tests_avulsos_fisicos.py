from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.urls import reverse
from estoque.models import Funcionario, Produto
from . import services, contagem_admin
from .conversao import snapshot, converter, avulsos_fisicos, cadastro_compativel
from .models import ConfirmacaoContagem
from .tests import produto
from .tests_browser import CHROME, capturar
from .tests_historico import HistoricoFixture
from offline.browser_support import Chrome


class AvulsosFixture:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('fisico-teste', '', 'teste')
        self.produto = produto(nome='Coca Cola Lta 12/350Ml', unidade_compra='PCT',
            unidade_venda_1='PCT', vende_fracionado=False, unidade_venda_2='UN', fator_conversao=12)
        self.produto.refresh_from_db()
        self.inv = services.criar_inventario(self.usuario, 'Ficticio', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.funcionario = Funcionario.objects.create(nome='Contador ficticio')
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodada.pk])


class ConversaoTests(AvulsosFixture, TestCase):
    def test_pacote_unidades_e_componentes_exatos(self):
        cadastro = self.rodada.item.cadastro
        self.assertTrue(avulsos_fisicos(cadastro))
        a,b,total,base = converter(cadastro, '1', '5')
        self.assertEqual((a,b), (Decimal(1), Decimal(5)))
        self.assertEqual(a * Decimal(cadastro['fator']) + b, Decimal(17))
        self.assertEqual(total, Decimal('1.416666667'))
        self.assertEqual(base, Decimal('1.417'))

    def test_fatores_ausentes_ambiguos_e_unidade_nao_inferidos(self):
        for fator,secundaria in [(0,'UN'),(1,'UN'),('2.5','UN'),(12,''),(12,'KG')]:
            with self.subTest(fator=fator, secundaria=secundaria):
                self.produto.fator_conversao=Decimal(str(fator));self.produto.unidade_venda_2=secundaria
                cadastro=snapshot(self.produto)
                self.assertFalse(avulsos_fisicos(cadastro))
                with self.assertRaises(ValueError):converter(cadastro,'1','5')

    def test_nao_aceita_fracoes_negativos_ou_excesso(self):
        for avulso in ['-1','0.5','1.0001','NaN','1000000000']:
            with self.subTest(avulso=avulso), self.assertRaises(ValueError):
                converter(self.rodada.item.cadastro,'1',avulso)
        self.assertEqual(converter(self.rodada.item.cadastro,'1','')[1],0)

    def test_snapshot_antigo_nao_e_reinterpretado(self):
        antigo=dict(self.rodada.item.cadastro);antigo.pop('avulsos_fisicos');antigo['secundaria']=''
        self.assertTrue(cadastro_compativel(self.produto, antigo))
        self.assertFalse(avulsos_fisicos(antigo))
        with self.assertRaises(ValueError):converter(antigo,'1','5')
        self.produto.fator_conversao=24
        self.assertFalse(cadastro_compativel(self.produto, antigo))

    def test_dois_campos_existentes_preservados(self):
        self.produto.vende_fracionado=True
        cadastro=snapshot(self.produto)
        self.assertFalse(avulsos_fisicos(cadastro))
        self.assertEqual(converter(cadastro,'1','6')[3],Decimal('1.500'))
        with self.assertRaises(ValueError):converter(cadastro,'1','')

    def test_funcionario_grava_exatamente_sem_alterar_produto(self):
        antes=Produto.objects.values().get(pk=self.produto.pk)
        r=services.salvar_contagem(self.acesso.pk,self.rodada.pk,0,'1','5',True)
        r.refresh_from_db();self.assertEqual((r.embalagens,r.avulsos),(1,5))
        c=r.confirmacoes.get();self.assertEqual((c.embalagens,c.avulsos),(1,5))
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk),antes)

    def test_admin_grava_com_idempotencia_e_preserva_cadastro(self):
        antes=Produto.objects.values().get(pk=self.produto.pk)
        token=contagem_admin.autorizacao(self.usuario,self.inv,self.rodada)
        for _ in range(2):
            contagem_admin.confirmar(self.usuario,self.inv.pk,self.rodada.pk,token,'1','5')
        self.assertEqual(ConfirmacaoContagem.objects.filter(rodada=self.rodada).count(),1)
        self.assertEqual(Produto.objects.values().get(pk=self.produto.pk),antes)


class TituloTests(HistoricoFixture, TestCase):
    def test_titulo_e_periodo_ultima_compra(self):
        r=self.client.get(self.url)
        self.assertContains(r,'Movimentações do período — '+self.item.cadastro['nome'])
        self.assertEqual(r.context['compra'].pk,self.compra.pk)
        self.assertEqual(r.context['inicio'],self.inicio)


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
class AvulsosBrowserTests(AvulsosFixture, StaticLiveServerTestCase):
    def test_mobile_desktop_guardar_reabrir_e_edicao_admin(self):
        with TemporaryDirectory(prefix='avulsos-chrome-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:convite',args=[self.token])})
                tab.wait("!!document.querySelector('form button')")
                tab.evaluate("document.querySelector('form button').click();true")
                tab.wait("window.inventarioContagemPronta === true")
                self.assertFalse(tab.evaluate("document.querySelector('.unidades-avulsas').open"))
                tab.evaluate("document.querySelector('.unidades-avulsas summary').click();document.querySelector('[name=embalagens]').value='1';document.querySelector('[name=avulsos]').value='5';document.querySelector('[name=avulsos]').dispatchEvent(new Event('input',{bubbles:true}));true")
                tab.wait("document.querySelector('[data-total-fisico]').textContent.includes('17 UN')")
                for width in [320,390,430,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'avulsos-'+str(width)+'.png')
                tab.evaluate("document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('form.contagem').dataset.confirmado === 'true'")
                tab.call('Page.reload');tab.wait("window.inventarioContagemPronta === true")
                self.assertEqual(tab.evaluate("document.querySelector('[name=avulsos]').value"),'5')
                self.assertFalse(tab.evaluate("document.querySelector('.unidades-avulsas').open"))
                self.produto.refresh_from_db();self.assertEqual(self.produto.quantidade,10)
                client=Client();client.force_login(self.usuario)
                tab.call('Network.enable');tab.call('Network.setCookie',{'name':'sessionid','value':client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:detalhe',args=[self.inv.pk])})
                tab.wait('window.inventarioContagemAdminPronta === true')
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin]').click();true")
                tab.wait("document.querySelector('dialog[open] [data-total-fisico]').textContent.includes('17 UN')")
                for width in [320,390,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'admin-avulsos-'+str(width)+'.png')
                tab.evaluate("document.querySelector('[data-fechar-edicao-admin]').click();true")
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:historico_produto',args=[self.inv.pk,self.rodada.pk])})
                tab.wait("document.querySelector('#titulo-extrato')?.textContent.includes('Coca Cola Lta 12/350Ml')")
                for width in [320,390,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'historico-produto-'+str(width)+'.png')
            finally:chrome.stop()
