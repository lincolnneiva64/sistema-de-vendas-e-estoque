"""Página completa e endpoint real; executar com sistema.settings_test_compras."""
import copy
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from unittest import skipUnless
from urllib.request import urlopen

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.utils import timezone

from offline.browser_support import DevTools
from .models import Cliente, ContaReceber


CHROME = Path(os.environ.get("OFFLINE_TEST_CHROME", "C:/Program Files/Google/Chrome/Application/chrome.exe"))


@contextmanager
def navegador_isolado():
    with TemporaryDirectory(prefix="previa-financeira-browser-", ignore_cleanup_errors=True) as profile:
        startup = subprocess.STARTUPINFO() if os.name == "nt" else None
        if startup:
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen([
            str(CHROME), "--headless", "--no-sandbox", "--in-process-gpu", "--disable-gpu",
            "--disable-background-networking", "--no-first-run", "--no-proxy-server",
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=" + profile, "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    port = int((Path(profile) / "DevToolsActivePort").read_text().splitlines()[0])
                    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        page = next(p for p in json.load(response) if p["type"] == "page")
                    break
                except (OSError, ValueError, StopIteration):
                    time.sleep(.05)
            else:
                raise AssertionError("Chrome isolado não iniciou")
            tab = DevTools(page["webSocketDebuggerUrl"])
            try:
                tab.call("Page.enable")
                tab.call("Network.enable")
                tab.call("Network.setBlockedURLs", {"urls": ["*onrender.com*", "*neon.tech*", "*googleapis.com*", "*cdn.jsdelivr.net*"]})
                yield tab
            finally:
                tab.socket.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@skipUnless(CHROME.is_file(), "Chrome indisponível")
class PreviaFinanceiraPaginaCompletaTests(StaticLiveServerTestCase):
    def setUp(self):
        # O settings isolado de compras não inclui o contexto de identidade da UI.
        templates = copy.deepcopy(settings.TEMPLATES)
        templates[0]["OPTIONS"]["context_processors"].append("estoque.context_processors.ambiente_sistema")
        self.override = override_settings(TEMPLATES=templates)
        self.override.enable()
        self.addCleanup(self.override.disable)
        user = get_user_model().objects.create_user("previa_ficticia", password="teste-isolado")
        user.user_permissions.add(Permission.objects.get(content_type__app_label="offline", codename="registrar_observacao"))
        self.lincoln = Cliente.objects.create(nome="Lincoln Neiva", whatsapp="5500000000000")
        Cliente.objects.create(nome="A Loja Ficticia", apelido_nome_conhecido="linco")
        Cliente.objects.create(nome="Beatriz Limpa")
        for valor in ["50.00"] * 9 + ["51.36"]:
            ContaReceber.objects.create(
                cliente=self.lincoln, data_emissao=timezone.localdate(),
                data_vencimento=timezone.localdate() - timedelta(days=1),
                valor_original=Decimal(valor), valor_em_aberto=Decimal(valor),
            )

    def test_multiplos_brutos_filtragem_detalhes_concorrencia_e_confirmacao(self):
        contas_antes = list(ContaReceber.objects.order_by("id").values())
        with navegador_isolado() as tab:
            # Segura somente a entrega da resposta real, sem fabricar seu conteúdo.
            tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": """
window.diag={errors:[],responses:[],hold:false,release:null};
addEventListener('error',e=>diag.errors.push(e.message));
addEventListener('unhandledrejection',e=>diag.errors.push(String(e.reason)));
const original=window.fetch;
window.fetch=async(...args)=>{
 const response=await original(...args),url=String(args[0]);
 if(url.includes('clientes/autocomplete')) {
  const data=await response.clone().json();diag.responses.push({url,data});
  if(url.includes('cliente_id=') && diag.hold)await new Promise(resolve=>diag.release=resolve);
 }
 return response;
};
"""})
            tab.call("Page.navigate", {"url": self.live_server_url + "/offline/login/?next=/vendas/"})
            tab.wait('!!document.getElementById("id_username")')
            tab.evaluate("document.getElementById('id_username').value='previa_ficticia';document.getElementById('id_password').value='teste-isolado';document.querySelector('form').requestSubmit();true")
            tab.wait('location.pathname==="/vendas/" && window.salesDraftUI?.ready===true', timeout=20)
            tab.evaluate("await(await import([...document.scripts].find(s=>s.src.includes('/app.js')).src)).initialized;true")
            time.sleep(.4)  # Aguarda somente a precarga inicial de sugestões.

            def digitar(texto):
                tab.evaluate('clienteBusca.focus();clienteBusca.value=' + json.dumps(texto) + ';clienteBusca.dispatchEvent(new Event("input",{bubbles:true}));true')

            def previa():
                tab.wait('clienteResumo.classList.contains("visivel") && clienteResumoFinanceiro.textContent.includes("501,36")')

            digitar("linco")
            previa()
            raw = tab.evaluate('diag.responses.find(r=>r.url.includes("q=linco")).data.clientes')
            self.assertEqual(len(raw), 2)
            self.assertEqual(raw[1]["id"], self.lincoln.pk)
            self.assertEqual(raw[1]["financeiro"]["contas_abertas_total"], "0.00")
            self.assertEqual(tab.evaluate('document.querySelectorAll(".cliente-sugestao-item").length'), 1)
            self.assertEqual(tab.evaluate('clienteId.value'), "")
            self.assertEqual(tab.evaluate('clienteResumoFinanceiro.querySelectorAll("a").length'), 0)
            self.assertEqual(tab.evaluate('clienteResumoFinanceiro.querySelectorAll("[aria-disabled=true]").length'), 3)

            # Dois resultados efetivamente visíveis impedem a prévia.
            Cliente.objects.create(nome="Lincoln Segundo")
            digitar("linco")
            tab.wait('document.querySelectorAll(".cliente-sugestao-item").length===2')
            self.assertFalse(tab.evaluate('clienteResumo.classList.contains("visivel")'))

            # A resposta financeira antiga não pode reaparecer após A-B-A.
            tab.evaluate('diag.hold=true;diag.release=null;true')
            digitar("Lincoln Neiva")
            tab.wait('!!diag.release')
            self.assertFalse(tab.evaluate('clienteResumo.classList.contains("visivel")'))
            digitar("Beatriz")
            digitar("Lincoln Neiva")
            tab.evaluate('diag.hold=false;diag.release();true')
            previa()

            # Limpeza durante consulta de detalhes não permite reaparecimento.
            tab.evaluate('diag.hold=true;diag.release=null;true')
            digitar("Lincoln Neiva")
            tab.wait('!!diag.release')
            digitar("")
            tab.evaluate('diag.hold=false;diag.release();true')
            time.sleep(.2)
            self.assertFalse(tab.evaluate('clienteResumo.classList.contains("visivel")'))
            self.assertEqual(tab.evaluate('clienteId.value'), "")

            for confirmar in [
                'clienteBusca.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))',
                'document.querySelector(".cliente-sugestao-item").click()',
            ]:
                digitar("Lincoln Neiva")
                previa()
                tab.evaluate(confirmar + ';true')
                tab.wait('!!clienteId.value && clienteResumoFinanceiro.querySelectorAll("a").length===3')
                self.assertEqual(tab.evaluate('clienteId.value'), str(self.lincoln.pk))

            # Confirmar enquanto a prévia está pendente não pode voltar ao modo desabilitado.
            tab.evaluate('diag.hold=true;diag.release=null;true')
            digitar("Lincoln Neiva")
            tab.wait('!!diag.release')
            tab.evaluate('diag.previewRelease=diag.release;diag.hold=false;document.querySelector(".cliente-sugestao-item").click();true')
            tab.wait('!!clienteId.value && clienteResumoFinanceiro.querySelectorAll("a").length===3')
            tab.evaluate('diag.previewRelease();true')
            time.sleep(.2)
            self.assertEqual(tab.evaluate('clienteResumoFinanceiro.querySelectorAll("[aria-disabled=true]").length'), 0)
            self.assertEqual(tab.evaluate('clienteId.value'), str(self.lincoln.pk))
            self.assertEqual(tab.evaluate('diag.errors'), [])
        self.assertEqual(list(ContaReceber.objects.order_by("id").values()), contas_antes)
