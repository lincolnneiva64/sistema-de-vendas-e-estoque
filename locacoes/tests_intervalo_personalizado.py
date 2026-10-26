"""Aplicação explícita do intervalo financeiro sem interferência na tabela."""
import base64
from pathlib import Path
import re
import tempfile
from unittest import skipUnless

from django.conf import settings
from django.template.loader import render_to_string
from django.test import TestCase

from offline.browser_support import Chrome
from . import tests_resumo_financeiro as resumo


class IntervaloPersonalizadoTests(resumo.DadosResumo, TestCase):
    def test_intervalos_livres_calculos_e_preservacao(self):
        for cadastro, valor in (("2025-12-31", 10), ("2026-01-15", 20), ("2026-06-30", 30), ("2026-12-31", 40)):
            self.pagar(self.criar(cadastro), valor, cadastro)
        for inicio, fim, quantidade, recebido in (("2026-01-01", "2026-06-30", 2, 50),
                                                  ("2026-01-01", "2026-12-31", 3, 90),
                                                  ("2025-12-01", "2026-01-31", 2, 30),
                                                  ("2026-01-15", "2026-01-15", 1, 20)):
            with self.subTest(inicio=inicio, fim=fim):
                resposta = self.consultar(resumo_periodo="escolha", resumo_inicio=inicio, resumo_fim=fim,
                                          data_inicio="2026-10-25", data_fim="2026-10-26", cliente="fictício")
                r = resposta.context["resumo_financeiro"]
                self.assertEqual((r["quantidade"], r["contratado"], r["recebido"], r["saldo"]),
                                 (quantidade, quantidade * 100, recebido, quantidade * 100 - recebido))
                self.assertEqual(resposta.context["resumo_inicio"], inicio)
                self.assertEqual(resposta.context["resumo_fim"], fim)
                self.assertEqual(len(resposta.context["locacoes"]), 4)
                self.assertContains(resposta, 'id="aplicar-periodo"')

    def test_datas_invalidas_sem_totais_e_sem_afetar_tabela(self):
        self.criar()
        for inicio, fim, mensagem in (("2026-07-01", "2026-06-30", "A data inicial deve ser anterior ou igual à data final."),
                                     ("2026-02-30", "2026-06-30", "Informe datas válidas")):
            resposta = self.consultar(resumo_inicio=inicio, resumo_fim=fim)
            self.assertIsNone(resposta.context["resumo_financeiro"])
            self.assertContains(resposta, mensagem)
            self.assertNotContains(resposta, 'class="resumo-financeiro-blocos"')
            self.assertEqual(len(resposta.context["locacoes"]), 1)


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@skipUnless(CHROME.exists(), "Chrome necessário")
class IntervaloPersonalizadoBrowserTests(resumo.DadosResumo, TestCase):
    def test_intervalo_aberto_aplicar_preservar_e_voltar_ao_predefinido(self):
        self.pagar(self.criar("2026-01-15"), 20, "2026-01-15")
        self.pagar(self.criar("2026-06-30"), 30, "2026-06-30")
        resposta = self.consultar(resumo_periodo="escolha", resumo_inicio="2026-01-01", resumo_fim="2026-06-30",
                                  data_inicio="2026-10-25", data_fim="2026-10-26")
        html = resposta.content.decode()
        html = re.sub(r'<link[^>]+href="https://[^>]+>', '', html)
        html = re.sub(r'<script(?![^>]*type="application/json")\b[^>]*>.*?</script>', '', html, flags=re.S)
        html = html.replace('<link rel="stylesheet" href="/static/locacoes/consulta-operacional.css">', '<style>' + (settings.BASE_DIR / "static/locacoes/consulta-operacional.css").read_text(encoding="utf-8") + '</style>')
        html = html.replace('</body>', render_to_string("locacoes/includes/filtros_consulta.html") + '</body>')
        destino = settings.BASE_DIR / "amostras/previa-resumo-financeiro"
        destino.mkdir(parents=True, exist_ok=True)
        destino = Path(tempfile.mkdtemp(prefix="personalizado-", dir=destino))
        with tempfile.TemporaryDirectory(prefix="financeiro-escolha-", ignore_cleanup_errors=True) as perfil:
            chrome = Chrome(str(CHROME), perfil).start()
            try:
                tab = chrome.tab()
                for largura in (1366, 1051, 390, 320):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 1100, "deviceScaleFactor": 1, "mobile": largura < 600})
                    tab.call("Page.setDocumentContent", {"frameId": tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"], "html": html})
                    tab.wait('!!document.getElementById("aplicar-periodo")')
                    self.assertFalse(tab.evaluate('document.getElementById("datas-resumo").hidden'))
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    if largura > 820:
                        controles = tab.evaluate('["periodo-locacoes","resumo-inicio","resumo-fim","aplicar-periodo"].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return {top:r.top,height:r.height,left:r.left,right:r.right}})')
                        self.assertLessEqual(max(c["top"] for c in controles) - min(c["top"] for c in controles), 1)
                        self.assertTrue(all(c["height"] == 38 for c in controles))
                        for anterior, proximo in zip(controles, controles[1:]):
                            self.assertAlmostEqual(proximo["left"] - anterior["right"], 8, delta=1)
                    self.assertEqual(tab.evaluate('document.querySelector("label[for=resumo-inicio]").textContent'), "Data inicial")
                    self.assertEqual(tab.evaluate('document.querySelector("label[for=resumo-fim]").textContent'), "Data final")
                    tab.evaluate('document.querySelector(".resumo-financeiro").scrollIntoView({block:"start"});true')
                    clip = tab.evaluate('(()=>{const r=document.querySelector(".resumo-financeiro").getBoundingClientRect();return {x:0,y:Math.max(0,r.top+scrollY-8),width:innerWidth,height:r.height+16,scale:1}})()')
                    imagem = tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})
                    (destino / f"intervalo-{largura}.png").write_bytes(base64.b64decode(imagem["data"]))
                    tab.evaluate('''window.envios=[];const f=document.getElementById('filtros-resumo');f.requestSubmit=function(){window.envios.push(Object.fromEntries(new FormData(this)))};f.addEventListener('submit',function(e){e.preventDefault();e.stopImmediatePropagation();window.envios.push(Object.fromEntries(new FormData(this)))},true);f.elements.resumo_fim.value='2026-12-31';f.elements.resumo_fim.dispatchEvent(new Event('change',{bubbles:true}));true''')
                    tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,450));return true})()')
                    self.assertEqual(tab.evaluate('window.envios.length'), 0)
                    tab.evaluate('document.getElementById("aplicar-periodo").click();true')
                    tab.wait('window.envios.length===1')
                    enviados = tab.evaluate('window.envios[0]')
                    self.assertEqual((enviados["resumo_inicio"], enviados["resumo_fim"]), ("2026-01-01", "2026-12-31"))
                    self.assertEqual((enviados["data_inicio"], enviados["data_fim"]), ("2026-10-25", "2026-10-26"))
                    atualizada = self.consultar(**enviados)
                    self.assertEqual(atualizada.context["resumo_fim"], "2026-12-31")
                    self.assertEqual(atualizada.context["resumo_financeiro"]["recebido"], 50)
                    tab.evaluate('''const s=document.getElementById('periodo-locacoes');s.value='ontem';s.dispatchEvent(new Event('change',{bubbles:true}));true''')
                    tab.wait('window.envios.length===2')
                    self.assertTrue(tab.evaluate('document.getElementById("datas-resumo").hidden'))
                    previstos = resposta.context["periodos_consulta"]["ontem"]
                    self.assertEqual(tab.evaluate('window.envios[1].resumo_inicio'), previstos["inicio"])
                    self.assertEqual(tab.evaluate('window.envios[1].resumo_fim'), previstos["fim"])
            finally:
                chrome.stop()
