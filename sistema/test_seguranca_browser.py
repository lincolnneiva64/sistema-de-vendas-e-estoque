"""Chrome login/logout preserves local offline records; synthetic identities only."""
import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome
from offline.tests_browser import CHROME


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class SegurancaBrowserTests(StaticLiveServerTestCase):
    def test_login_mobile_desktop_e_logout_preservam_fila_e_indicador(self):
        password = secrets.token_urlsafe(24)
        user = get_user_model().objects.create_superuser('administrador-browser-ficticio', '', password)
        with TemporaryDirectory(prefix='seguranca-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Page.navigate', {'url': self.live_server_url + '/offline/'})
                tab.wait("location.pathname==='/offline/' && document.readyState==='complete'")
                pending = tab.evaluate("""(async()=>{
                    const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');
                    const r=new Repository(await openDB());
                    await r.put('operations',{operation_id:crypto.randomUUID(),actor_id:'ficticio',
                        environment_id:'ficticio',status:'pendente',payload:{observacao:'Preservar'}});
                    return JSON.stringify(await r.all('operations'));
                })()""")
                tab.call('Page.navigate', {'url': self.live_server_url + '/vendas/?origem=atalho'})
                tab.wait("location.pathname==='/entrar/' && !!document.querySelector('input[name=username]')")
                for width in [1440, 390, 320]:
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                tab.evaluate('document.querySelector("input[name=username]").value=' + json.dumps(user.username) +
                    ';document.querySelector("input[name=password]").value=' + json.dumps(password) +
                    ';document.querySelector("form").requestSubmit();true')
                tab.wait("location.pathname==='/vendas/' && !!document.getElementById('offline-global')")
                self.assertEqual(tab.evaluate('location.search'), '?origem=atalho')
                tab.wait("document.getElementById('offline-global').dataset.state==='online'")
                self.assertEqual(tab.evaluate('(await (await fetch("/api/offline/session/")).json()).authenticated'), True)
                tab.evaluate("""document.querySelector('form[action="/sair/"]').requestSubmit();true""")
                tab.wait("location.pathname==='/entrar/' && !!document.querySelector('input[name=username]')")
                self.assertFalse(tab.evaluate('(await (await fetch("/api/offline/session/")).json()).authenticated'))
                after = tab.evaluate("""(async()=>{
                    const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');
                    return JSON.stringify(await new Repository(await openDB()).all('operations'));
                })()""")
                self.assertEqual(after, pending)
            finally:
                chrome.stop()
