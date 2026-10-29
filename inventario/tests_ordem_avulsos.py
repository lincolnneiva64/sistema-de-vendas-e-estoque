from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .tests_relacao_fisica import Fixture
from .tests_browser import CHROME,capturar
from .models import ConfirmacaoContagem
from . import conversao_fisica, services
from offline.browser_support import Chrome


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
class OrdemAvulsosBrowserTests(Fixture,StaticLiveServerTestCase):
    def test_pacotes_primeiro_vazio_amarelo_zero_5_un_17_un_e_reabertura(self):
        with TemporaryDirectory(prefix='ordem-avulsos-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:detalhe',args=[self.inv.pk])})
                tab.wait('window.inventarioContagemAdminPronta === true')
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin]').click();true")
                self.assertFalse(tab.evaluate("document.querySelector('dialog .unidades-avulsas').open"))
                self.assertTrue(tab.evaluate("document.querySelector('[name=embalagens]').getBoundingClientRect().top < document.querySelector('dialog summary').getBoundingClientRect().top"))
                tab.evaluate("document.querySelector('form[data-edicao-admin] button[type=submit]').click();true")
                tab.wait("document.querySelector('[name=embalagens]').getAttribute('aria-invalid') === 'true'")
                self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('[name=embalagens]')).backgroundColor"),'rgb(255, 248, 214)')
                self.assertEqual(ConfirmacaoContagem.objects.count(),0)
                capturar(tab,'pct-vazio-amarelo-390.png')
                tab.evaluate("document.querySelector('[name=embalagens]').value='0';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('dialog summary').click();document.querySelector('[name=fator_fisico]').value='12';document.querySelector('[name=avulsos]').value='5';document.querySelector('form[data-relacao-fisica] button').click();true")
                tab.wait("window.inventarioContagemAdminPronta === true && !document.querySelector('[data-relacao-fisica]') && document.querySelector('dialog').open")
                self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"),'0')
                self.assertEqual(tab.evaluate("document.querySelector('[name=avulsos]').value"),'5')
                self.assertFalse(tab.evaluate("document.querySelector('dialog .unidades-avulsas').open"))
                tab.wait("document.querySelector('[data-total-fisico]').textContent.includes('0 PCT + 5 UN')")
                for width in [320,390,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,'0-pct-5-un-'+str(width)+'.png')
                tab.evaluate("document.querySelector('form[data-edicao-admin] button[type=submit]').click();true")
                tab.wait("window.inventarioContagemAdminPronta === true && document.querySelector('.contado-principal')?.textContent === '0 PCT + 5 UN' && document.querySelector('.contado-total')?.textContent.endsWith('5 UN')")
                self.rodada.refresh_from_db();self.assertEqual((self.rodada.embalagens,self.rodada.avulsos),(0,5))
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin]').click();true")
                self.assertFalse(tab.evaluate("document.querySelector('dialog .unidades-avulsas').open"))
                self.assertEqual(tab.evaluate("document.querySelector('[name=avulsos]').value"),'5')
                tab.evaluate("document.querySelector('[name=embalagens]').value='1';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));true")
                tab.wait("document.querySelector('[data-total-fisico]').textContent.includes('1 PCT + 5 UN') && document.querySelector('[data-total-fisico]').textContent.includes('17 UN')")
                tab.evaluate("document.querySelector('dialog summary').click();true")
                for width in [390,1366]:
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<700})
                    capturar(tab,'1-pct-5-un-'+str(width)+'.png')
                tab.evaluate("document.querySelector('form[data-edicao-admin] button[type=submit]').click();true")
                tab.wait("window.inventarioContagemAdminPronta === true && document.querySelector('.contado-principal')?.textContent === '1 PCT + 5 UN' && document.querySelector('.contado-total')?.textContent.endsWith('17 UN')")
                self.rodada.refresh_from_db();self.assertEqual((self.rodada.embalagens,self.rodada.avulsos),(1,5))
                self.assertEqual(self.inv.eventos.filter(acao=conversao_fisica.ACAO).count(),1)
                self.assertEqual(ConfirmacaoContagem.objects.filter(rodada=self.rodada).count(),2)
                self.rodada=services.recontar(self.usuario,self.rodada.pk,'Recontagem autorizada para teste mobile')
                _, novo_token=services.gerar_acesso(self.usuario,self.inv.pk,self.funcionario.pk,[self.rodada.pk])
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:convite',args=[novo_token])})
                tab.wait("!!document.querySelector('form button')");tab.evaluate("document.querySelector('form button').click();true")
                tab.wait('window.inventarioContagemPronta === true')
                self.assertFalse(tab.evaluate("document.querySelector('.unidades-avulsas').open"))
                tab.evaluate("document.querySelector('[name=embalagens]').value='';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('[name=embalagens]').getAttribute('aria-invalid') === 'true'")
                tab.evaluate("document.querySelector('.unidades-avulsas summary').click();document.querySelector('[name=avulsos]').value='5';document.querySelector('[name=avulsos]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[name=embalagens]').value='0';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('form.contagem').dataset.confirmado === 'true'")
                self.rodada.refresh_from_db();self.assertEqual((self.rodada.embalagens,self.rodada.avulsos),(0,5))
                self.produto.refresh_from_db();self.assertEqual(self.produto.quantidade,10)
            finally:chrome.stop()
