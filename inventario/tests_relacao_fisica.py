from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, Client
from django.urls import reverse
from estoque.models import Funcionario, Produto
from . import services, contagem_admin, conversao_fisica
from .tests import produto
from .tests_browser import CHROME,capturar
from offline.browser_support import Chrome


class Fixture:
    def setUp(self):
        self.usuario=get_user_model().objects.create_superuser('relacao-teste','','teste')
        self.produto=produto(nome='Coca Cola Lta 12/350Ml',vende_fracionado=False,
            unidade_compra='PCT',unidade_venda_1='PCT',unidade_venda_2='',fator_conversao=0)
        self.produto.refresh_from_db()
        self.inv=services.criar_inventario(self.usuario,'Ficticio','manual',[self.produto.pk])
        self.rodada=self.inv.itens.get().rodadas.get()
        self.funcionario=Funcionario.objects.create(nome='Contador ficticio')
        self.acesso,self.token=services.gerar_acesso(self.usuario,self.inv.pk,self.funcionario.pk,[self.rodada.pk])
        self.client=Client();self.client.force_login(self.usuario)

    def confirmar(self,fator='12',token=None):
        self.inv.refresh_from_db();self.rodada.refresh_from_db()
        return conversao_fisica.confirmar(self.usuario,self.inv.pk,self.rodada.pk,
            token or contagem_admin.autorizacao(self.usuario,self.inv,self.rodada),fator)


class RelacaoTests(Fixture,TestCase):
    def test_confirma_uma_vez_audita_e_nao_altera_cadastro(self):
        antes=Produto.objects.values().get(pk=self.produto.pk)
        snapshot=dict(self.rodada.item.cadastro)
        self.confirmar();self.confirmar()
        e=self.inv.eventos.get(acao=conversao_fisica.ACAO)
        self.assertEqual(e.usuario,self.usuario);self.assertEqual(e.dados['unidades_por_embalagem'],'12')
        self.assertEqual(e.dados['item_id'],self.rodada.item_id);self.assertIsNotNone(e.criado_em)
        self.rodada.item.refresh_from_db();self.assertEqual(snapshot,self.rodada.item.cadastro)
        self.assertEqual(antes,Produto.objects.values().get(pk=self.produto.pk))
        with self.assertRaises(ValueError):self.confirmar('24')

    def test_rejeita_sem_prova_fatores_invalidos_e_token_invalido(self):
        with self.assertRaises(ValueError):services.salvar_contagem(self.acesso.pk,self.rodada.pk,0,'1','5',True)
        for valor in ['',0,1,'2.5','NaN',-12]:
            with self.subTest(valor=valor),self.assertRaises(ValueError):self.confirmar(valor)
        with self.assertRaises(ValueError):self.confirmar(token='falso')
        self.assertFalse(self.inv.eventos.filter(acao=conversao_fisica.ACAO).exists())

    def test_permissao_csrf_e_conferencia_isolada(self):
        outro=get_user_model().objects.create_user('sem-permissao')
        with self.assertRaises(PermissionDenied):
            conversao_fisica.confirmar(outro,self.inv.pk,self.rodada.pk,'falso','12')
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(reverse('inventario:confirmar_conversao_fisica',args=[self.inv.pk,self.rodada.pk]),{}).status_code,403)
        self.confirmar()
        outra=services.criar_inventario(self.usuario,'Outra','manual',[self.produto.pk])
        self.assertFalse(conversao_fisica.cadastro_contagem(outra.itens.get()).get('avulsos_fisicos'))

    def test_17_un_exatas_persistidas_ajuste_arredondado_bloqueado(self):
        antes=Produto.objects.values().get(pk=self.produto.pk)
        self.confirmar()
        r=services.salvar_contagem(self.acesso.pk,self.rodada.pk,0,'1','5',True)
        c=r.confirmacoes.get();self.assertEqual((c.embalagens,c.avulsos),(1,5))
        e=self.inv.eventos.get(acao='produto_ok')
        self.assertEqual(Decimal(e.dados['contagem_fisica']['total_unidades']),17)
        with self.assertRaisesMessage(ValueError,'Ajuste automático indisponível'):conversao_fisica.exigir_representavel(r)
        resposta=self.client.get(reverse('inventario:detalhe',args=[self.inv.pk]))
        self.assertEqual(resposta.status_code,200);self.assertEqual(resposta.context['linhas'][0]['contado_texto'],'1 PCT + 5 UN (17 UN)')
        self.assertContains(resposta,'Ajuste automático indisponível')
        self.assertEqual(antes,Produto.objects.values().get(pk=self.produto.pk))

    def test_equivalencia_exata_e_recontagem_reutilizam_relacao(self):
        self.confirmar()
        r=services.salvar_contagem(self.acesso.pk,self.rodada.pk,0,'1','6',True)
        conversao_fisica.exigir_representavel(r)
        services.recontar(self.usuario,r.pk,'Nova contagem ficticia')
        item=self.inv.itens.get()
        self.assertEqual(conversao_fisica.cadastro_contagem(item)['fator'],'12')
        self.assertEqual(self.inv.eventos.filter(acao=conversao_fisica.ACAO).count(),1)


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
class RelacaoBrowserTests(Fixture,StaticLiveServerTestCase):
    def test_confirmar_relacao_uma_vez_mobile_desktop_e_contar(self):
        with TemporaryDirectory(prefix='relacao-chrome-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                url=self.live_server_url+reverse('inventario:detalhe',args=[self.inv.pk])
                tab.call('Page.navigate',{'url':url});tab.wait('window.inventarioContagemAdminPronta === true')
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin]').click();document.querySelector('dialog .unidades-avulsas summary').click();true")
                for width in [320,390,430,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'confirmar-relacao-'+str(width)+'.png')
                tab.evaluate("document.querySelector('[name=fator_fisico]').value='12';document.querySelector('form[data-relacao-fisica] button').click();true")
                tab.wait("window.inventarioContagemAdminPronta === true && !document.querySelector('form[data-relacao-fisica]')")
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin]').click();document.querySelector('dialog .unidades-avulsas summary').click();document.querySelector('[name=embalagens]').value='1';document.querySelector('[name=avulsos]').value='5';document.querySelector('[name=avulsos]').dispatchEvent(new Event('input',{bubbles:true}));true")
                tab.wait("document.querySelector('[data-total-fisico]').textContent.includes('17 UN')")
                capturar(tab,'relacao-17-un-admin.png')
                tab.evaluate("document.querySelector('form[data-edicao-admin] button[type=submit]').click();true")
                tab.wait("window.inventarioContagemAdminPronta === true && document.querySelector('.resumo-linha').textContent.includes('17 UN')")
                # Count with the employee link: the confirmed relation must be reused.
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:convite',args=[self.token])})
                tab.wait("!!document.querySelector('form button')");tab.evaluate("document.querySelector('form button').click();true")
                tab.wait('window.inventarioContagemPronta === true')
                self.assertEqual(tab.evaluate("document.querySelector('[name=avulsos]').value"),'5')
                self.assertFalse(tab.evaluate("document.body.textContent.includes('Solicite ao responsável confirmar')"))
                for width in [320,390,430,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'relacao-17-un-contagem-'+str(width)+'.png')
                self.assertEqual(self.inv.eventos.filter(acao=conversao_fisica.ACAO).count(),1)
            finally:chrome.stop()
