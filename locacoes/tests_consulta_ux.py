from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao


class ConsultaLocacoesUxTests(TestCase):
    hoje = date(2026, 10, 4)

    def setUp(self):
        self.relogio = patch("locacoes.views.timezone.localdate", return_value=self.hoje)
        self.relogio.start()
        self.addCleanup(self.relogio.stop)
        ConfiguracaoLocacao.obter()
        self.faixa = FaixaPrecoLocacao.objects.first()
        self.passada = self.criar(self.hoje - timedelta(days=1))
        self.atual = self.criar(self.hoje)
        self.futura = self.criar(self.hoje + timedelta(days=1))
        self.distante = self.criar(date(2026, 12, 20))

    def criar(self, data, horario=time(9), **extras):
        return Locacao.objects.create(
            tipo_pessoa=Locacao.TIPO_PESSOA_AVULSA,
            pessoa_avulsa_nome="Cliente Consulta",
            endereco_entrega="Rua Consulta, 10",
            faixa_preco=self.faixa,
            data_entrega=data,
            horario_entrega=horario,
            data_evento=data,
            horario_evento=time(18),
            data_prevista_devolucao=data + timedelta(days=1),
            **extras,
        )

    def consultar(self, **filtros):
        return self.client.get(reverse("locacoes:lista"), filtros, secure=True)

    def ids(self, **filtros):
        return [locacao.pk for locacao in self.consultar(**filtros).context["locacoes"]]

    def test_abertura_mostra_hoje_e_futuras(self):
        self.assertEqual(self.ids(), [self.atual.pk, self.futura.pk, self.distante.pk])

    def test_passadas_fora_da_visao_padrao(self):
        self.assertNotIn(self.passada.pk, self.ids())

    def test_futura_em_dezembro_sem_limite_de_mes(self):
        self.assertIn(self.distante.pk, self.ids())

    def test_data_especifica(self):
        alvo = self.criar(date(2026, 11, 8))
        self.assertEqual(self.ids(data_inicio="2026-11-08", data_fim="2026-11-08"), [alvo.pk])

    def test_intervalo_inclusivo(self):
        self.assertEqual(self.ids(data_inicio="2026-10-03", data_fim="2026-10-05"),
                         [self.passada.pk, self.atual.pk, self.futura.pk])

    def test_historico_com_datas_no_passado(self):
        self.assertEqual(self.ids(data_inicio="2026-10-03", data_fim="2026-10-03"), [self.passada.pk])

    def test_apenas_data_final_permite_historico(self):
        self.assertEqual(self.ids(data_fim="2026-10-03"), [self.passada.pk])

    def test_apenas_data_inicial_sem_limite_superior(self):
        self.assertEqual(self.ids(data_inicio="2026-10-05"), [self.futura.pk, self.distante.pk])

    def test_status_preserva_visao_padrao(self):
        alvo = self.criar(self.hoje, status=Locacao.STATUS_ENTREGUE)
        self.criar(self.hoje - timedelta(days=2), status=Locacao.STATUS_ENTREGUE)
        self.assertEqual(self.ids(status=Locacao.STATUS_ENTREGUE), [alvo.pk])

    def test_financeiro_preserva_visao_padrao(self):
        alvo = self.criar(self.hoje, saldo_devedor=Decimal("10"))
        self.criar(self.hoje - timedelta(days=2), saldo_devedor=Decimal("10"))
        self.assertEqual(self.ids(financeiro="com_saldo"), [alvo.pk])
        self.assertNotIn(alvo.pk, self.ids(financeiro="quitadas"))

    def test_limpar_filtros_retorna_visao_padrao(self):
        response = self.consultar(status="entregue", financeiro="com_saldo", data_inicio="2026-10-03")
        self.assertContains(response, f'href="{reverse("locacoes:lista")}" id="limpar-filtros-locacoes"')
        self.assertEqual(self.ids(status="", financeiro="", data_inicio="", data_fim=""), self.ids())

    def test_ordem_cronologica(self):
        self.assertEqual(self.ids(), [self.atual.pk, self.futura.pk, self.distante.pk])

    def test_mesmo_dia_ordena_por_horario(self):
        tarde = self.criar(self.hoje, time(16))
        cedo = self.criar(self.hoje, time(7))
        self.assertEqual(self.ids()[:3], [cedo.pk, self.atual.pk, tarde.pk])


class FiltrosConsultaFrontendTests(SimpleTestCase):
    def test_eventos_automaticos_debounce_e_limpeza_no_browser(self):
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        chrome_windows = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        if not chrome and chrome_windows.exists():
            chrome = str(chrome_windows)
        if not chrome:
            self.skipTest("Chrome/Chromium necessário para executar o JavaScript")
        html = '''<!doctype html><form id="filtros-locacoes">
          <select name="status"><option value="">Todos</option><option>entregue</option></select>
          <select name="financeiro"><option value="">Todas</option><option>quitadas</option></select>
          <input type="date" name="data_inicio"><input type="date" name="data_fim">
        </form><a id="limpar-filtros-locacoes" href="#">Limpar filtros</a><pre id="resultado">PENDENTE</pre>'''
        html += render_to_string("locacoes/includes/filtros_consulta.html")
        html += '''<script>
        (async function () {
          const form = document.getElementById('filtros-locacoes');
          const resultado = document.getElementById('resultado');
          let submits = 0;
          form.requestSubmit = function () { submits++; };
          const esperar = () => new Promise(resolve => setTimeout(resolve, 450));
          function mudar(nome, valor) {
            const campo = form.elements[nome]; campo.value = valor;
            campo.dispatchEvent(new Event('input', {bubbles: true}));
            campo.dispatchEvent(new Event('change', {bubbles: true}));
          }
          try {
            for (const [nome, valor] of [['status','entregue'], ['financeiro','quitadas'],
                 ['data_inicio','2026-11-08'], ['data_fim','2026-11-08']]) {
              const antes = submits; mudar(nome, valor); await esperar();
              if (submits !== antes + 1) throw Error(nome + ': submit automático/debounce');
            }
            mudar('data_inicio', '2026-11-09'); mudar('data_fim', '2026-11-10');
            await esperar();
            if (submits !== 5) throw Error('debounce entre campos');
            mudar('status', '');
            document.getElementById('limpar-filtros-locacoes').click(); await esperar();
            if (submits !== 5) throw Error('limpar deve cancelar submit pendente');
            resultado.textContent = 'PASSOU';
          } catch (error) { resultado.textContent = 'FALHOU: ' + error.message; }
        })();
        </script>'''
        with tempfile.TemporaryDirectory(prefix="locacoes-ux-") as pasta:
            pagina = Path(pasta) / "filtros.html"
            pagina.write_text(html, encoding="utf-8")
            processo = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                 f"--user-data-dir={Path(pasta) / 'perfil'}", "--dump-dom",
                 "--virtual-time-budget=4000", pagina.as_uri()],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertIn('<pre id="resultado">PASSOU</pre>', processo.stdout, processo.stderr[-2000:])
