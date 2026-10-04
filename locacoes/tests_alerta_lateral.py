"""Executa no Chrome a função real da aba, com relógio controlado."""
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class AlertaLateralBrowserTests(SimpleTestCase):
    def test_relogio_cores_e_tarefas_desconectadas(self):
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        if not chrome.exists():
            self.skipTest("Chrome necessário para executar o JavaScript da aba")
        template = (settings.BASE_DIR / "estoque/templates/estoque/vendas_layout_teste.html").read_text(encoding="utf-8")
        funcao = re.search(r"    function atualizarAlertaLateral\(\) \{.*?\n    \}", template, re.S)[0]
        script = """
        const RealDate = Date;
        let instante;
        Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [instante])); } };
        const estados = new Set();
        const locacoesOperacionaisVenda = {classList: {toggle(nome, ativo) {
            if (ativo) estados.add(nome); else estados.delete(nome);
        }}};
        const card = {isConnected: true, dataset: {entregaAntiga: "0",
            dataLateral: "2026-10-04", horarioLateral: String(new RealDate("2026-10-04T10:30:00-03:00").getTime())}};
        const cards = [card];
        """ + funcao + """
        function verificar(hora, esperado) {
            instante = new RealDate("2026-10-04T" + hora + ":00-03:00").getTime();
            atualizarAlertaLateral();
            const atual = estados.has("alerta") ? "alerta" : estados.has("advertencia") ? "advertencia" : "normal";
            if (atual !== esperado) throw Error(hora + ": " + atual + " != " + esperado);
        }
        try {
            for (const [hora, estado] of [["09:59", "normal"], ["10:00", "advertencia"],
                ["10:12", "advertencia"], ["10:29", "advertencia"],
                ["10:30", "alerta"], ["10:31", "alerta"]]) verificar(hora, estado);
            card.dataset.dataLateral = "2026-10-05";
            verificar("10:12", "normal");
            card.dataset.dataLateral = "2026-10-03";
            card.isConnected = false;
            verificar("10:12", "normal");
            document.body.textContent = "PASSOU";
        } catch (erro) { document.body.textContent = "FALHOU: " + erro.message; }
        """
        with tempfile.TemporaryDirectory(prefix="alerta-lateral-") as pasta:
            html = Path(pasta) / "teste.html"
            html.write_text("<body><script>" + script + "</script></body>", encoding="utf-8")
            resultado = subprocess.run(
                [str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                 "--user-data-dir=" + str(Path(pasta) / "perfil"), "--dump-dom", html.as_uri()],
                capture_output=True, text=True, timeout=30,
            )
            self.assertIn("<body>PASSOU</body>", resultado.stdout, resultado.stderr[-2000:])
