"""Navigation-only checks in isolated browsers; queue records stay untouched."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase
from django.urls import reverse,resolve
from .browser_support import Chrome

CHROME=r'C:\Program Files\Google\Chrome\Application\chrome.exe'


class PilotNavigationRouteTests(SimpleTestCase):
    def test_routes_render_in_public_shell(self):
        response=self.client.get(reverse('offline_pilot'))
        self.assertContains(response,'href="'+reverse('estoque:vendas')+'"')
        self.assertContains(response,'href="'+reverse('estoque:home')+'"')
        self.assertContains(response,'Voltar para Vendas')
        self.assertContains(response,'Painel Central')
        self.assertNotContains(response,'{% url')
        self.assertEqual(resolve(reverse('estoque:vendas')).url_name,'vendas')
        self.assertEqual(resolve(reverse('estoque:home')).url_name,'home')


@skipUnless(Path(CHROME).is_file(),'Chrome unavailable')
class PilotNavigationBrowserTests(StaticLiveServerTestCase):
    def check_navigation(self,width):
        with TemporaryDirectory(prefix='pilot-nav-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<600})
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('offline_pilot')})
                tab.wait('document.getElementById("offline-return-sales") && document.getElementById("offline-device").textContent')
                tab.evaluate('window.core=await import("/offline/assets/2-8f/core.js");window.repo=new core.Repository(await core.openDB());await repo.put("operations",{operation_id:"navigation-pending",actor_id:"navigation-test",environment_id:"navigation-test",type:"criar_venda",status:"pendente"});await repo.put("operations",{operation_id:"navigation-closed",actor_id:"navigation-test",environment_id:"navigation-test",type:"criar_venda",status:"conflito"});await repo.put("metadata",{key:"navigation-admin-closure",operation_id:"navigation-closed",status:"encerrada_sem_venda"});await repo.put("history",{operation_id:"navigation-closed",status:"conflito"});true')
                before=tab.evaluate('({operations:await repo.all("operations"),history:await repo.all("history"),closure:await repo.get("metadata","navigation-admin-closure")})')
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth && document.getElementById("offline-return-sales").getBoundingClientRect().height>=44'))
                self.assertEqual(tab.evaluate('getComputedStyle(document.getElementById("offline-return-sales")).backgroundColor'),'rgb(29, 78, 216)')
                for button,name in [('offline-return-sales','estoque:vendas'),('offline-return-home','estoque:home')]:
                    # Let the isolated destination load its HTML without running
                    # business/offline scripts: this tests the anchor navigation.
                    target=reverse(name)
                    self.assertEqual(tab.evaluate('document.getElementById('+json.dumps(button)+').getAttribute("href")'),target)
                    tab.call('Emulation.setScriptExecutionDisabled',{'value':True})
                    tab.evaluate('document.getElementById('+json.dumps(button)+').click()')
                    tab.wait('location.pathname==='+json.dumps(target))
                    tab.call('Emulation.setScriptExecutionDisabled',{'value':False})
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('offline_pilot')})
                    tab.wait('document.getElementById("offline-return-sales") && document.getElementById("offline-device").textContent')
                    after=tab.evaluate('window.core=await import("/offline/assets/2-8f/core.js");window.repo=new core.Repository(await core.openDB());({operations:await repo.all("operations"),history:await repo.all("history"),closure:await repo.get("metadata","navigation-admin-closure")})')
                    self.assertEqual(after,before)
            finally:chrome.stop()

    def test_desktop_links_and_preserved_queue(self):self.check_navigation(1280)
    def test_mobile_links_and_preserved_queue(self):self.check_navigation(390)
