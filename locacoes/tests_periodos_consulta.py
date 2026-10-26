"""Calendário de Belém e atalhos nos campos de data já existentes."""
import base64
from datetime import datetime, timezone as utc
from pathlib import Path
import re
import tempfile
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from offline.browser_support import Chrome
from .periodos import periodos_consulta
from . import tests_resumo_financeiro as resumo


class CalendarioPeriodosTests(SimpleTestCase):
    def calcular(self, dia):
        with patch("locacoes.periodos.timezone.now", return_value=datetime.fromisoformat(dia + "T15:00:00+00:00")):
            return periodos_consulta()

    def test_somente_seis_atalhos_e_escolha_manual_na_interface(self):
        self.assertEqual(list(self.calcular("2026-10-26")), ["hoje", "ontem", "semana", "fim_semana", "mes", "mes_passado"])

    def test_hoje_ontem_virada_do_ano(self):
        p = self.calcular("2027-01-01")
        self.assertEqual((p["hoje"]["inicio"], p["hoje"]["fim"]), ("2027-01-01", "2027-01-01"))
        self.assertEqual((p["ontem"]["inicio"], p["ontem"]["fim"]), ("2026-12-31", "2026-12-31"))

    def test_semana_segunda_ate_hoje_inclusive(self):
        for dia, inicio in (("2026-03-02", "2026-03-02"), ("2026-03-01", "2026-02-23"), ("2027-01-01", "2026-12-28")):
            with self.subTest(dia=dia):
                p = self.calcular(dia)["semana"]
                self.assertEqual((p["inicio"], p["fim"]), (inicio, dia))

    def test_fim_de_semana_passado_e_em_andamento(self):
        for dia, inicio, fim in (("2026-02-28", "2026-02-28", "2026-02-28"),
                                 ("2026-03-01", "2026-02-28", "2026-03-01"),
                                 ("2026-03-02", "2026-02-28", "2026-03-01"),
                                 ("2026-03-06", "2026-02-28", "2026-03-01"),
                                 ("2027-01-01", "2026-12-26", "2026-12-27")):
            with self.subTest(dia=dia):
                p = self.calcular(dia)["fim_semana"]
                self.assertEqual((p["inicio"], p["fim"]), (inicio, fim))

    def test_mes_passado_fevereiro_bissexto_e_ano_anterior(self):
        for dia, inicio, fim in (("2024-03-01", "2024-02-01", "2024-02-29"),
                                 ("2026-03-01", "2026-02-01", "2026-02-28"),
                                 ("2027-01-01", "2026-12-01", "2026-12-31")):
            with self.subTest(dia=dia):
                p = self.calcular(dia)
                self.assertEqual((p["mes_passado"]["inicio"], p["mes_passado"]["fim"]), (inicio, fim))
                self.assertEqual(p["mes"]["inicio"], dia[:8] + "01")
                self.assertEqual(p["mes"]["fim"], dia)

    def test_usa_belem_mesmo_com_outro_fuso_ativo_e_dia_utc_diferente(self):
        instante = datetime(2026, 3, 1, 2, 59, tzinfo=utc.utc)
        with timezone.override("Asia/Tokyo"), patch("locacoes.periodos.timezone.now", return_value=instante):
            self.assertEqual(periodos_consulta()["hoje"]["inicio"], "2026-02-28")


class PeriodosConsultaTests(resumo.DadosResumo, TestCase):
    def test_intervalo_livre_maior_que_ano_nao_e_limitado(self):
        resposta = self.consultar(resumo_periodo="escolha", resumo_inicio="2024-01-01", resumo_fim="2026-12-31", cliente="Exemplo", financeiro="com_saldo", aba="recolhimentos")
        self.assertEqual(resposta.context["periodo_selecionado"], "escolha")
        self.assertEqual(resposta.context["resumo_inicio"], "2024-01-01")
        self.assertEqual(resposta.context["resumo_fim"], "2026-12-31")
        self.assertContains(resposta, 'value="escolha" selected')
        for aba in resposta.context["abas"]:
            parametros = parse_qs(urlparse(aba["url"]).query)
            self.assertEqual(parametros["resumo_periodo"], ["escolha"])
            self.assertEqual(parametros["cliente"], ["Exemplo"])
            self.assertEqual(parametros["financeiro"], ["com_saldo"])
            self.assertEqual(parametros["resumo_inicio"], ["2024-01-01"])

    def test_datas_manuais_prevalecem_sobre_nome_do_atalho(self):
        resposta = self.consultar(resumo_periodo="hoje", resumo_inicio="2024-01-01", resumo_fim="2026-12-31")
        self.assertEqual(resposta.context["periodo_selecionado"], "escolha")
        self.assertEqual(resposta.context["resumo_inicio"], "2024-01-01")

    def test_periodo_e_filtros_preservados_na_paginacao_e_historico(self):
        for _ in range(26):
            self.criar()
        with patch("locacoes.periodos.timezone.now", return_value=datetime(2026, 10, 31, 15, tzinfo=utc.utc)):
            resposta = self.consultar(resumo_periodo="mes", status="reservada", financeiro="com_saldo")
        self.assertEqual(resposta.context["periodo_selecionado"], "mes")
        for url in (resposta.context["url_proxima"], resposta.context["url_historico"]):
            p = parse_qs(urlparse(url).query)
            self.assertEqual(p["resumo_periodo"], ["mes"])
            self.assertEqual(p["status"], ["reservada"])
            self.assertEqual(p["financeiro"], ["com_saldo"])


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@skipUnless(CHROME.exists(), "Chrome necessário")
class PeriodosBrowserTests(resumo.DadosResumo, TestCase):
    def test_atalhos_manual_filtros_e_layout(self):
        alvo = self.criar(total=3600)
        self.pagar(alvo, 2200)
        antiga = self.criar("2026-08-01", total=3000)
        self.pagar(antiga, 2000)
        with patch("locacoes.periodos.timezone.now", return_value=datetime(2026, 10, 26, 15, tzinfo=utc.utc)):
            resposta = self.consultar(resumo_periodo="mes", resumo_fim="2026-10-26", cliente="fictício", financeiro="com_saldo", status="reservada")
        html = resposta.content.decode()
        html = re.sub(r'<link[^>]+href="https://[^>]+>', '', html)
        html = re.sub(r'<script(?![^>]*type="application/json")\b[^>]*>.*?</script>', '', html, flags=re.S)
        html = html.replace('<link rel="stylesheet" href="/static/locacoes/consulta-operacional.css">', '<style>' + (settings.BASE_DIR / "static/locacoes/consulta-operacional.css").read_text(encoding="utf-8") + '</style>')
        html = html.replace('</body>', render_to_string("locacoes/includes/filtros_consulta.html") + '</body>')
        destino = settings.BASE_DIR / "amostras/previa-resumo-financeiro"
        destino.mkdir(parents=True, exist_ok=True)
        destino = Path(tempfile.mkdtemp(prefix="periodos-", dir=destino))
        with tempfile.TemporaryDirectory(prefix="periodos-locacoes-", ignore_cleanup_errors=True) as perfil:
            chrome = Chrome(str(CHROME), perfil).start()
            try:
                tab = chrome.tab()
                for largura in (1366, 1051, 390, 320):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 1200, "deviceScaleFactor": 1, "mobile": largura < 600})
                    tab.call("Page.setDocumentContent", {"frameId": tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"], "html": html})
                    tab.wait('!!document.getElementById("periodo-locacoes")')
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#periodo-locacoes option").length'), 7)
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#filtros-locacoes input[type=date]").length'), 2)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    tab.evaluate('window.scrollTo(0,0);true')
                    tamanho = tab.call("Page.getLayoutMetrics")["cssContentSize"]
                    imagem = tab.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": largura, "height": tamanho["height"], "scale": 1}})
                    (destino / f"periodos-{largura}.png").write_bytes(base64.b64decode(imagem["data"]))
                    tab.evaluate('''window.envios=[];document.getElementById('filtros-resumo').requestSubmit=function(){window.envios.push(Object.fromEntries(new FormData(this)))};document.getElementById('filtros-resumo').addEventListener('submit',function(e){e.preventDefault();e.stopImmediatePropagation();window.envios.push(Object.fromEntries(new FormData(this)))},true);true''')
                    for chave in resposta.context["periodos_consulta"]:
                        antes = tab.evaluate('window.envios.length')
                        tab.evaluate(f'''const seletor=document.getElementById('periodo-locacoes');seletor.value='{chave}';seletor.dispatchEvent(new Event('change',{{bubbles:true}}));true''')
                        tab.wait(f'window.envios.length === {antes + 1}')
                        valores = tab.evaluate('window.envios.at(-1)')
                        esperado = resposta.context["periodos_consulta"][chave]
                        self.assertEqual((valores["resumo_inicio"], valores["resumo_fim"]), (esperado["inicio"], esperado["fim"]))
                        self.assertTrue(tab.evaluate('document.getElementById("datas-resumo").hidden'))
                        self.assertEqual((valores["cliente"], valores["status"], valores["financeiro"], valores["aba"]), ("fictício", "reservada", "com_saldo", "entregas"))
                    antes = tab.evaluate('window.envios.length')
                    tab.evaluate('''const s=document.getElementById('periodo-locacoes');s.value='escolha';s.dispatchEvent(new Event('change',{bubbles:true}));true''')
                    self.assertEqual(tab.evaluate('window.envios.length'), antes)
                    self.assertFalse(tab.evaluate('document.getElementById("datas-resumo").hidden'))
                    self.assertEqual(tab.evaluate('document.querySelector("[name=resumo_inicio]").value'), esperado["inicio"])
                    tab.evaluate('''const f=document.getElementById('filtros-resumo');f.elements.resumo_inicio.value='2024-01-01';f.elements.resumo_inicio.dispatchEvent(new Event('input',{bubbles:true}));f.elements.resumo_fim.value='2026-12-31';f.elements.resumo_fim.dispatchEvent(new Event('change',{bubbles:true}));true''')
                    tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,450));return true})()')
                    self.assertEqual(tab.evaluate('window.envios.length'), antes)
                    tab.evaluate('document.getElementById("aplicar-periodo").click();true')
                    tab.wait(f'window.envios.length === {antes + 1}')
                    valores = tab.evaluate('window.envios.at(-1)')
                    self.assertEqual((valores["resumo_periodo"], valores["resumo_inicio"], valores["resumo_fim"]), ("escolha", "2024-01-01", "2026-12-31"))
                    self.assertEqual(valores["financeiro"], "com_saldo")
                    # Consulta real com os parâmetros enviados pelo navegador.
                    self.assertEqual(self.consultar(**valores).context["periodo_selecionado"], "escolha")
                    # A tabela conserva o período financeiro mesmo ao mudar seus
                    # campos depois de um preenchimento financeiro ainda no DOM.
                    tab.evaluate('''document.getElementById('filtros-locacoes').requestSubmit=function(){window.envios.push(Object.fromEntries(new FormData(this)))};const op=document.getElementById('filtros-locacoes');op.elements.cliente.value='Sem correspondencia';op.elements.cliente.dispatchEvent(new Event('input',{bubbles:true}));op.elements.data_inicio.value='2030-01-01';op.elements.data_inicio.dispatchEvent(new Event('change',{bubbles:true}));true''')
                    tab.wait(f'window.envios.length === {antes + 2}')
                    parametros = tab.evaluate('window.envios.at(-1)')
                    # Esta renderização ainda tem outubro como período aplicado:
                    # a captura do submit não navegou nem aplicou o rascunho.
                    self.assertEqual(parametros["resumo_inicio"], "2026-10-01")
                    self.assertEqual(parametros["resumo_fim"], "2026-10-26")
                    self.assertEqual(parametros["data_inicio"], "2030-01-01")
                    resposta_op = self.consultar(**parametros)
                    self.assertEqual(resposta_op.context["locacoes"], [])
                    self.assertEqual(resposta_op.context["resumo_financeiro"]["quantidade"], 1)
                    self.assertEqual(resposta_op.context["resumo_financeiro"]["recebido"], 4200)
            finally:
                chrome.stop()
