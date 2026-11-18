"""Templates, autorizacao e navegacao em DOM isolado; conexoes de banco proibidas."""
import html
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory, gettempdir
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parents[1]
BASE = "a4379c97477b3f29a17398e04f7dc4755ffdbf96"
BROWSER = os.environ.get("OFFLINE_TEST_CHROME", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PAGES = ("locacoes/lista.html", "estoque/compras_listas_fornecedor.html")
BOOTSTRAP = Path(os.environ.get("ATALHOS_TEST_BOOTSTRAP_CSS", str(Path(gettempdir()) / "atalhos-bootstrap-5.3.3.css")))
SCRIPT = ROOT / "static/estoque/atalhos-consultas.js"


class AtalhosConsultasBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ["OFFLINE_TEST_DATABASE_URL"] = ""
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "offline.test_settings")
        from django.db.backends.base.base import BaseDatabaseWrapper
        cls.no_database = patch.object(BaseDatabaseWrapper, "ensure_connection", side_effect=AssertionError("Banco proibido neste teste"))
        cls.no_database.start()
        import django
        django.setup()
        from django.template import Engine, engines
        from django.template.backends.django import get_installed_libraries
        cls.engine = engines["django"].engine
        original = {}
        for name, path in [
            ("estoque/base.html", "estoque/templates/estoque/base.html"),
            ("estoque/includes/sessao_usuario.html", "estoque/templates/estoque/includes/sessao_usuario.html"),
            ("estoque/compras_listas_fornecedor.html", "estoque/templates/estoque/compras_listas_fornecedor.html"),
            ("estoque/vendas_layout_teste.html", "estoque/templates/estoque/vendas_layout_teste.html"),
            ("locacoes/lista.html", "locacoes/templates/locacoes/lista.html"),
        ]:
            original[name] = subprocess.check_output(["git", "show", BASE + ":" + path], cwd=ROOT).decode("utf-8")
        cls.published = Engine(loaders=[("django.template.loaders.locmem.Loader", original),
                                       "django.template.loaders.app_directories.Loader"], libraries=get_installed_libraries())

    @classmethod
    def tearDownClass(cls):
        cls.no_database.stop()

    def request(self, path="/locacoes/", authenticated=True, superuser=False, permissions=False):
        from django.test import RequestFactory
        request = RequestFactory().get(path)
        request.session = {}
        request.user = SimpleNamespace(
            is_authenticated=authenticated, is_active=True, is_superuser=superuser, is_staff=superuser, pk=1,
            get_username=lambda: "Operador Teste", has_perm=lambda p: permissions or superuser,
            has_perms=lambda p: permissions or superuser,
        )
        return request

    def render(self, name, published=False, authenticated=True, path="/locacoes/"):
        from django.template import Context
        request = self.request(path, authenticated)
        context = Context({"request": request, "csrf_token": "isolado", "locacoes": [], "listas": []})
        return (self.published if published else self.engine).get_template(name).render(context)

    def links(self, markup):
        return [(html.unescape(href), re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip())
                for href, text in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup, re.S)]


class AtalhosConsultasTests(AtalhosConsultasBase):
    def test_shared_destinations_preserve_sales_links_and_ids(self):
        from django.urls import reverse, resolve
        before = self.render("estoque/vendas_layout_teste.html", published=True, path="/vendas/")
        after = self.render("estoque/vendas_layout_teste.html", path="/vendas/")
        pattern = r'<div class="atalhos-venda-lista">(.*?)<button type="button" id="btnNovaVenda"'
        existing = self.links(re.search(pattern, before, re.S)[1])
        self.assertEqual(self.links(re.search(pattern, after, re.S)[1]), existing)
        self.assertEqual(after.count('id="btnContasReceberVenda"'), 1)
        self.assertEqual(after.count('id="btnConferenciaEstoqueVendas"'), 1)
        for page in PAGES:
            markup = self.render(page)
            menu = re.search(r'<nav[^>]*class="atalhos-cabecalho-painel rc-atalhos-painel"[^>]*>(.*?)</nav>', markup, re.S)[1]
            links = self.links(menu)
            expected = self.links(re.search(pattern, self.render("estoque/vendas_layout_teste.html"), re.S)[1])
            self.assertEqual(links, [(reverse("estoque:vendas"), "Vendas")] + expected)
            for href, _ in links:
                self.assertIsNotNone(resolve(urlsplit(href).path))
            self.assertNotIn('id="btnContasReceberVenda"', menu)
            self.assertNotIn('id="btnNovaVenda"', menu)

    def test_consultation_content_and_default_session_are_unchanged(self):
        def without_navigation(markup):
            markup = re.sub(r'<div class="consulta-atalhos-faixa"[^>]*>.*?<!-- fim-atalhos-consultas -->', '', markup, flags=re.S)
            markup = re.sub(r'<link[^>]*atalhos-consultas\.css[^>]*>', '', markup)
            markup = re.sub(r'<script[^>]*atalhos-consultas\.js[^>]*>.*?</script>', '', markup, flags=re.S)
            markup = markup.replace(' data-consulta-atalhos', '')
            return '\n'.join(line for line in markup.splitlines() if line.strip())

        for name in PAGES:
            before, after = self.render(name, published=True), self.render(name)
            main = r'<main\b[^>]*>.*?</main>'
            self.assertEqual(without_navigation(re.search(main, after, re.S)[0]),
                             without_navigation(re.search(main, before, re.S)[0]))
            logout = r'<form data-auth-session.*?</form>'
            self.assertEqual(re.search(logout, after, re.S)[0], re.search(logout, before, re.S)[0])
            self.assertEqual(after.count('data-atalhos-cabecalho'), 1)
        before = self.render("estoque/includes/sessao_usuario.html", published=True)
        after = self.render("estoque/includes/sessao_usuario.html")
        self.assertEqual(re.sub(r"\s+", " ", after).strip(), re.sub(r"\s+", " ", before).strip())
        for page in PAGES:
            self.assertNotIn('data-atalhos-cabecalho', self.render(page, authenticated=False))

    def test_destinations_keep_original_authentication_and_permissions(self):
        from django.urls import resolve
        from django.core.exceptions import PermissionDenied
        from sistema.acesso import AcessoComercialMiddleware
        from django.test import override_settings
        middleware = AcessoComercialMiddleware(lambda request: None)
        menu = re.search(r'<nav[^>]*class="atalhos-cabecalho-painel rc-atalhos-painel"[^>]*>(.*?)</nav>', self.render(PAGES[0]), re.S)[1]
        with override_settings(LOCAL_SALES_LOOPBACK=False):
            for href, _ in self.links(menu):
                path = urlsplit(href).path
                match = resolve(path)
                for authenticated in (False, True):
                    request = self.request(path, authenticated=authenticated)
                    request.resolver_match = match
                    response = middleware.process_view(request, match.func, match.args, match.kwargs)
                    if match.view_name == 'inventario:lista':
                        if authenticated:
                            with self.assertRaises(PermissionDenied):
                                match.func(request, *match.args, **match.kwargs)
                        else:
                            self.assertEqual(match.func(request, *match.args, **match.kwargs).status_code, 302)
                    else:
                        self.assertEqual(response.status_code, 403 if authenticated else 302, href)
                request = self.request(path, superuser=True)
                request.resolver_match = match
                self.assertIsNone(middleware.process_view(request, match.func, match.args, match.kwargs))


@unittest.skipUnless(Path(BROWSER).is_file(), "Configure OFFLINE_TEST_CHROME para testar o navegador")
class AtalhosConsultasBrowserTests(AtalhosConsultasBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temp = TemporaryDirectory(prefix="atalhos-consultas-", ignore_cleanup_errors=True)
        cls.browser = Chrome(BROWSER, str(Path(cls.temp.name) / "profile"))
        original_popen = subprocess.Popen
        with patch("offline.browser_support.subprocess.Popen", side_effect=lambda cmd, **kwargs:
                   original_popen([cmd[0], "--no-sandbox", *cmd[1:]], **kwargs)):
            cls.browser.start()

    @classmethod
    def tearDownClass(cls):
        cls.browser.stop()
        cls.temp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.tab = self.browser.tab()

    def tearDown(self):
        self.tab.call("Page.close")
        self.tab.socket.close()

    def prepare(self, page, width):
        from django.contrib.staticfiles import finders
        markup = self.render(page)
        markup = re.sub(r'<script\b[^>]*>.*?</script>', '', markup, flags=re.S)

        def stylesheet(match):
            path = urlsplit(html.unescape(match[1])).path
            if path.endswith('/bootstrap.min.css') and BOOTSTRAP.is_file():
                return '<style>' + BOOTSTRAP.read_text(encoding='utf-8') + '</style>'
            if not path.startswith('/static/'):
                return ''  # Os testes nao dependem de conexoes a CDNs.
            found = finders.find(path[len('/static/'):])
            return '<style>' + Path(found).read_text(encoding='utf-8') + '</style>' if found else ''

        markup = re.sub(r'<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>', stylesheet, markup)
        markup = re.sub(r'<link\b[^>]*href="([^"]+)"[^>]*rel="stylesheet"[^>]*>', stylesheet, markup)
        markup += '<script>window.submits=0;document.addEventListener("submit",e=>{e.preventDefault();submits++});</script>'
        path = Path(self.temp.name) / 'consulta.html'
        path.write_text(markup, encoding='utf-8')
        self.tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 800, 'deviceScaleFactor': 1, 'mobile': width <= 980})
        self.tab.call('Page.navigate', {'url': path.as_uri()})
        self.tab.wait("document.readyState==='complete' && !!document.querySelector('.rc-atalhos-toggle')")
        self.tab.evaluate(SCRIPT.read_text(encoding='utf-8'))

    @unittest.skipUnless(BOOTSTRAP.is_file(), "Informe ATALHOS_TEST_BOOTSTRAP_CSS com o CSS Bootstrap 5.3.3 usado pelo sistema")
    def test_reference_dimensions_and_typography_are_preserved(self):
        reference = (ROOT / 'estoque/templates/estoque/receber_cliente.html').read_text(encoding='utf-8')
        start = reference.index('(function () {\n  const atalhos = document.getElementById("atalhosReceberCliente")')
        script = reference[start:reference.index('</script>', start)]
        expression = """(()=>{const selectors=['.rc-atalhos-toggle','.rc-atalhos-painel','.rc-atalhos-titulo','.rc-atalhos-lista','.rc-atalhos-lista a'];
            const properties=['borderTopWidth','borderTopStyle','borderRadius','padding','fontFamily','fontSize','fontWeight','lineHeight','gap','overflowY'];
            return selectors.map(q=>{const s=getComputedStyle(document.querySelector(q));return Object.fromEntries(properties.map(p=>[p,s[p]]));});})()"""
        for width in (320, 390, 430, 1366):
            self.prepare('estoque/receber_cliente.html', width)
            self.tab.evaluate(script)
            self.tab.evaluate("document.querySelector('.rc-atalhos-toggle').click()")
            expected = self.tab.evaluate(expression)
            for page in PAGES:
                with self.subTest(page=page, width=width):
                    self.prepare(page, width)
                    self.tab.evaluate("document.querySelector('.rc-atalhos-toggle').click()")
                    actual = self.tab.evaluate(expression)
                    differences = [(i,k,v,expected[i][k]) for i,item in enumerate(actual) for k,v in item.items() if v != expected[i][k]]
                    self.maxDiff = None
                    self.assertEqual(differences, [])

    def key(self, key, code, value):
        for kind in ('keyDown', 'keyUp'):
            self.tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code,
                         'windowsVirtualKeyCode': value, 'text': ('\r' if key == 'Enter' else key if key == ' ' else '') if kind == 'keyDown' else ''})

    def test_open_close_keyboard_outside_scroll_and_layout(self):
        presentation = {}
        for page in PAGES:
            for width in (320, 390, 430, 1366):
                with self.subTest(page=page, width=width):
                    self.prepare(page, width)
                    position = self.tab.evaluate("""(()=>{const d=document.querySelector('[data-atalhos-cabecalho]'),b=d.querySelector('button'),main=d.closest('main'),h=main.querySelector('header'),s=getComputedStyle(b),r=b.getBoundingClientRect(),hr=h.getBoundingClientRect(),session=document.querySelector('[data-auth-session]');
                        return {inMain:!!main,inSession:session.contains(d),left:r.left,titleLeft:main.querySelector('h1').getBoundingClientRect().left,buttonBottom:r.bottom,headerTop:hr.top,
                            sessionBottom:session.getBoundingClientRect().bottom,
                            uncovered:[.05,.5,.95].every(x=>b.contains(document.elementFromPoint(r.left+r.width*x,r.top+r.height/2))),
                            presentation:[s.height,s.padding,s.font,s.border,s.color,s.backgroundColor]};})()""")
                    self.assertTrue(position['inMain'])
                    self.assertFalse(position['inSession'])
                    self.assertAlmostEqual(position['left'], position['titleLeft'], delta=1)
                    self.assertTrue(position['uncovered'])
                    self.assertGreaterEqual(position['headerTop'] - position['buttonBottom'], 4)
                    self.assertGreaterEqual(position['buttonBottom'] - 44, position['sessionBottom'])
                    self.assertEqual(position['presentation'][0], '44px')
                    if page not in presentation:
                        presentation[page] = position['presentation']
                    self.assertEqual(position['presentation'], presentation[page])
                    before = self.tab.evaluate("JSON.stringify(Array.from(document.querySelectorAll('main input,main select,main table,main header')).map(e=>e.getBoundingClientRect().toJSON()))")
                    self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]>button').focus()")
                    self.key('Enter', 'Enter', 13)
                    self.assertTrue(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"))
                    geometry = self.tab.evaluate("(()=>{const n=document.querySelector('.atalhos-cabecalho-painel'),r=n.getBoundingClientRect();return {left:r.left,right:r.right,bottom:r.bottom,links:n.querySelectorAll('a').length};})()")
                    self.assertGreaterEqual(geometry['left'], 0)
                    self.assertLessEqual(geometry['right'], width)
                    self.assertLessEqual(geometry['bottom'], 800)
                    self.assertEqual(geometry['links'], 11)
                    self.assertAlmostEqual(geometry['left'], position['left'], delta=1)
                    self.assertTrue(self.tab.evaluate("(()=>{const p=document.querySelector('.atalhos-cabecalho-painel').getBoundingClientRect(),h=document.querySelector('main header').getBoundingClientRect();return p.right<=h.right+1;})()"))
                    self.assertTrue(self.tab.evaluate("(()=>{const p=document.querySelector('.atalhos-cabecalho-painel'),r=p.getBoundingClientRect();return [.1,.5,.9].every(x=>[.1,.5,.9].every(y=>p.contains(document.elementFromPoint(r.left+r.width*x,r.top+r.height*y))));})()"))
                    self.assertEqual(before, self.tab.evaluate("JSON.stringify(Array.from(document.querySelectorAll('main input,main select,main table,main header')).map(e=>e.getBoundingClientRect().toJSON()))"))
                    self.assertEqual(self.tab.evaluate('submits'), 0)
                    self.assertTrue(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').closest('form')===null"))
                    self.key('Escape', 'Escape', 27)
                    self.assertFalse(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"))
                    self.assertTrue(self.tab.evaluate("document.activeElement.matches('[data-atalhos-cabecalho]>button')"))
                    self.key(' ', 'Space', 32)
                    self.assertTrue(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"))
                    self.tab.evaluate("document.querySelector('main h1').click()")
                    self.assertFalse(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"))
                    if width <= 980:
                        point = self.tab.evaluate("(()=>{const r=document.querySelector('[data-atalhos-cabecalho]>button').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
                        for expected in (True, False):
                            self.tab.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [point]})
                            self.tab.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                            self.assertEqual(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"), expected)
                    self.tab.evaluate(SCRIPT.read_text(encoding='utf-8'))  # Segunda carga nao duplica listeners.
                    self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]>button').click()")
                    self.assertTrue(self.tab.evaluate("document.querySelector('[data-atalhos-cabecalho]').classList.contains('aberto')"))
                    self.tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 350, 'deviceScaleFactor': 1, 'mobile': width <= 980})
                    self.assertTrue(self.tab.evaluate("(()=>{const p=document.querySelector('.atalhos-cabecalho-painel');p.scrollTop=p.scrollHeight;return p.scrollHeight>p.clientHeight&&p.scrollTop>0;})()"))
                    self.assertTrue(self.tab.evaluate("Array.from(document.querySelectorAll('[id]')).every(e=>document.querySelectorAll('[id=\"'+e.id+'\"]').length===1)"))


if __name__ == '__main__':
    unittest.main()
