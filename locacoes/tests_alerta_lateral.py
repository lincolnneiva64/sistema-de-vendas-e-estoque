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
        funcao = re.search(r"    function atualizarAlertaLateral\(\) \{.*?\n    \}", template, re.S)[0].replace("{{ fuso_operacional|escapejs }}", "America/Belem")
        script = """
        const RealDate = Date;
        let instante;
        Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [instante])); } };
        const estados = new Set();
        const locacoesOperacionaisVenda = {classList: {toggle(nome, ativo) {
            if (ativo) estados.add(nome); else estados.delete(nome);
        }}};
        const classesCard = new Set();
        const card = {isConnected: true, querySelector() { return null; }, classList: {
            add(...nomes) { nomes.forEach(nome => classesCard.add(nome)); },
            remove(...nomes) { nomes.forEach(nome => classesCard.delete(nome)); }
        }, dataset: {entregaAntiga: "0",
            tipo: "recolhimento", dataLateral: "2026-10-04", horarioLateral: String(new RealDate("2026-10-04T10:30:00-03:00").getTime())}};
        const cards = [card];
        """ + funcao + """
        function verificar(hora, esperado) {
            instante = new RealDate("2026-10-04T" + hora + ":00-03:00").getTime();
            atualizarAlertaLateral();
            const atual = estados.has("alerta") ? "alerta" : estados.has("advertencia") ? "advertencia" : "normal";
            if (atual !== esperado) throw Error(hora + ": " + atual + " != " + esperado);
            if (card.isConnected) {
                const estadoCard = classesCard.has("atrasada") ? "alerta" : classesCard.has("proxima") ? "advertencia" : "normal";
                if (estadoCard !== esperado) throw Error(hora + ": card " + estadoCard + " != " + esperado);
            }
        }
        try {
            for (const [hora, estado] of [["09:59", "normal"], ["10:00", "advertencia"],
                ["10:12", "advertencia"], ["10:26", "advertencia"], ["10:29", "advertencia"],
                ["10:30", "alerta"], ["10:31", "alerta"]]) verificar(hora, estado);
            card.dataset.dataLateral = "2026-10-05";
            verificar("10:12", "normal");
            card.dataset.dataLateral = "2026-10-03";
            card.isConnected = false;
            verificar("10:12", "normal");
            card.isConnected = true;
            card.dataset.tipo = "entrega";
            card.dataset.dataLateral = "2026-10-04";
            function entrega(hora, estado, moldura = true) {
                instante = new RealDate("2026-10-04T" + hora + ":00-03:00").getTime();
                atualizarAlertaLateral();
                const atual = estados.has("alerta") ? "alerta" : estados.has("advertencia") ? "advertencia" : "normal";
                if (atual !== estado) throw Error("entrega " + hora + ": " + atual);
                if (classesCard.has("entrega-hoje") !== moldura) throw Error("moldura " + hora);
            }
            entrega("07:00", "advertencia");
            entrega("09:29", "advertencia");
            entrega("09:30", "alerta");
            entrega("10:30", "alerta");
            entrega("11:30", "alerta");
            card.dataset.dataLateral = "2026-10-05";
            entrega("11:30", "normal", false);
            card.dataset.tipo = "recolhimento";
            card.dataset.dataLateral = "2026-10-04";
            entrega("07:00", "normal", false);
            card.dataset.tipo = "entrega";
            card.isConnected = false;
            entrega("11:30", "normal", false);
            // O calendario usa o fuso do sistema, mesmo apos meia-noite UTC.
            card.isConnected = true;
            instante = new RealDate("2026-10-05T01:30:00Z").getTime();
            atualizarAlertaLateral();
            if (!classesCard.has("entrega-hoje")) throw Error("fuso local");
            instante = new RealDate("2026-10-05T03:01:00Z").getTime();
            atualizarAlertaLateral();
            if (classesCard.has("entrega-hoje")) throw Error("virada do dia");
            card.isConnected = false;
            cards.push({isConnected: true, dataset: {tipo: "recolhimento", entregaAntiga: "0",
                dataLateral: "2026-10-03", horarioLateral: "0"},
                classList: {add() {}, remove() {}}, querySelector() { return null; }});
            atualizarAlertaLateral();
            if (!estados.has("alerta")) throw Error("preservar outro alerta");
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
