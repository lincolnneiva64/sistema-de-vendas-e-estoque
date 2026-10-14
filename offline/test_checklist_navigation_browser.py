import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from .browser_support import Chrome
from .tests import fixtures

CHROME = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')


@skipUnless(Path(CHROME).is_file(), 'Chrome unavailable')
class ChecklistNavigationTests(StaticLiveServerTestCase):
    def test_online_navigation_and_sidebar_initialization(self):
        user, _, _, _ = fixtures()
        client = Client()
        client.force_login(user)
        with TemporaryDirectory(prefix='checklist-navigation-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source': 'window.errors=[];window.addEventListener("error",e=>errors.push(e.message));'})
                # Hold module downloads: DOMContentLoaded cannot initialize sales yet.
                tab.call('Fetch.enable', {'patterns': [{'urlPattern': '*offline/assets/*', 'requestStage': 'Request'}]})
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/'})
                tab.wait('document.readyState==="interactive" && !!document.getElementById("btnAtalhosVenda")', timeout=25)
                tab.evaluate('document.getElementById("btnAtalhosVenda").click();true')
                self.assertEqual(tab.evaluate('document.getElementById("btnAtalhosVenda").getAttribute("aria-expanded")'), 'true', str(tab.evaluate('errors')))
                tab.evaluate('document.getElementById("btnLocacoesOperacionaisVenda").click();true')
                self.assertEqual(tab.evaluate('document.getElementById("btnLocacoesOperacionaisVenda").getAttribute("aria-expanded")'), 'true')
                self.assertEqual(tab.evaluate('document.getElementById("btnAtalhosVenda").getAttribute("aria-expanded")'), 'false')
                tab.evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape"}));true')
                self.assertEqual(tab.evaluate('document.getElementById("btnLocacoesOperacionaisVenda").getAttribute("aria-expanded")'), 'false')
                tab.call('Fetch.disable')
                tab.wait('document.readyState==="complete"', timeout=25)
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                tab.wait('!!navigator.serviceWorker.controller && location.pathname==="/offline/"')
                worker = chrome.worker(self.live_server_url + '/service-worker.js')
                worker.evaluate('self.originalFetch=fetch;self.fetch=(request,options)=>new URL(request.url || request,self.location.origin).pathname==="/locacoes/checklist-operacional/"?new Promise(resolve=>setTimeout(resolve,6000)).then(()=>originalFetch(request,options)):originalFetch(request,options);true')
                tab.call('Page.navigate', {'url': self.live_server_url + '/locacoes/checklist-operacional/'})
                tab.wait('location.pathname==="/locacoes/checklist-operacional/" && !!document.getElementById("offline-global")', timeout=20)
                self.assertEqual(tab.evaluate('document.title'), 'Entregas e Recolhimentos')
            finally:
                chrome.stop()

    def test_integrated_worker_refreshes_old_modules_preserving_indexeddb(self):
        user, _, _, _ = fixtures()
        self.client.force_login(user)
        with TemporaryDirectory(prefix='integrated-cache-upgrade-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url + '/vendas/'})
                tab.wait('window.salesDraftUI?.ready === true')
                tab.evaluate('const core=await import("/offline/assets/2-8f/core.js");window.upgradeRepo=new core.Repository(await core.openDB());await upgradeRepo.put("metadata",{key:"cache-upgrade-draft",text:"rascunho preservado"});const old=await caches.open("offline-pilot-shell-v37-pickup");await old.put("/offline/assets/2-8g-close/sales-drafts.js",new Response("outdated-draft-module"));const separate=await caches.open("preserved-test-cache");await separate.put("/keep",new Response("preserved"));true')
                tab.evaluate('await navigator.serviceWorker.register("/service-worker.js",{scope:"/"});await navigator.serviceWorker.ready;true')
                tab.wait('!!navigator.serviceWorker.controller')
                self.assertTrue(tab.evaluate('(await (await fetch("/offline/assets/2-8g-close/sales-drafts.js")).text()).includes("legacyDraftKey")'))
                self.assertEqual(tab.evaluate('await upgradeRepo.get("metadata","cache-upgrade-draft")'), {'key':'cache-upgrade-draft', 'text':'rascunho preservado'})
                self.assertEqual(tab.evaluate('await (await (await caches.open("preserved-test-cache")).match("/keep")).text()'), 'preserved')
                self.assertTrue(tab.evaluate('(await caches.keys()).includes("offline-pilot-shell-v38-integrated-pickup")'))
            finally:
                chrome.stop()
