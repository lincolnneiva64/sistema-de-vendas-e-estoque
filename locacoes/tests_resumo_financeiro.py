"""Resumo somente leitura; datas de cadastro e pagamento independentes."""
import base64
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
import re
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente, MovimentoFinanceiro
from estoque.views import _criar_operacao_recebimento_cliente, _desfazer_operacao_recebimento_cliente
from offline.browser_support import Chrome
from .consulta import resumo_financeiro_locacoes
from .models import ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao


class DadosResumo:
    def setUp(self):
        ConfiguracaoLocacao.obter()
        self.faixa, _ = FaixaPrecoLocacao.objects.get_or_create(
            codigo=FaixaPrecoLocacao.CENTRO_PERTO,
            defaults={"nome": "Centro fictício", "preco_jogo_diaria": 10},
        )

    def instante(self, texto):
        valor = datetime.fromisoformat(texto)
        return timezone.make_aware(valor) if timezone.is_naive(valor) else valor

    def criar(self, cadastro="2026-10-10", total=100, **campos):
        dados = dict(tipo_pessoa="avulsa", pessoa_avulsa_nome="Cliente fictício",
                     endereco_entrega="", faixa_preco=self.faixa,
                     data_entrega=date(2026, 10, 25), horario_entrega=time(9),
                     data_evento=date(2026, 10, 25), horario_evento=time(18),
                     data_prevista_devolucao=date(2026, 10, 26), total=total, saldo_devedor=total)
        dados.update(campos)
        locacao = Locacao.objects.create(**dados)
        Locacao.objects.filter(pk=locacao.pk).update(criado_em=self.instante(cadastro))
        return locacao

    def pagar(self, locacao, valor, quando="2026-10-15", **campos):
        return locacao.registrar_pagamento(Decimal(str(valor)), "pix", data_hora=self.instante(quando), **campos)

    def consultar(self, **campos):
        return self.client.get(reverse("locacoes:lista"), {"resumo_inicio": "2026-10-01", "resumo_fim": "2026-10-31", **campos})


class ResumoFinanceiroTests(DadosResumo, TestCase):
    def resumo(self, **campos):
        resposta = self.consultar(**campos)
        self.assertEqual(resposta.status_code, 200)
        return resposta.context["resumo_financeiro"]

    def test_cadastro_recebimento_parcelas_e_contrato_antigo(self):
        nova = self.criar()
        self.pagar(nova, 30)
        self.pagar(nova, 20)
        self.pagar(nova, 10, "2026-09-20")
        antiga = self.criar("2026-09-10", total=200)
        self.pagar(antiga, 40)
        self.criar("2026-11-01", total=300)
        r = self.resumo()
        self.assertEqual((r["quantidade"], r["contratado"], r["recebido"], r["saldo"]), (1, 100, 90, 40))

    def test_canceladas_com_e_sem_pagamentos_inclusive_antiga(self):
        ativa = self.criar()
        self.pagar(ativa, 20)
        cancelada = self.criar(total=50)
        self.pagar(cancelada, 15)
        cancelada.cancelar()
        self.criar(total=25, status="cancelada")
        antiga = self.criar("2026-08-01", total=60)
        self.pagar(antiga, 10)
        antiga.cancelar()
        r = self.resumo()
        self.assertEqual((r["quantidade"], r["contratado"], r["recebido"], r["saldo"]), (1, 100, 20, 80))
        self.assertEqual((r["canceladas_quantidade"], r["canceladas_contratado"], r["canceladas_recebido"]), (2, 75, 25))
        resposta = self.consultar(status="cancelada")
        self.assertNotContains(resposta, "Resumo financeiro — somente canceladas")
        self.assertContains(resposta, "Canceladas")
        r = resposta.context["resumo_financeiro"]
        self.assertFalse(r["somente_canceladas"])
        self.assertEqual(r["saldo"], 80)
        self.assertEqual(r["canceladas_recebido"], 25)

    def test_recebimento_unificado_e_desfazimento_reais_sem_dupla_subtracao(self):
        cliente = Cliente.objects.create(nome="Cliente unificado fictício", ativo=True)
        locacao = self.criar("2026-09-10", cliente=cliente, tipo_pessoa="cliente", total=200)
        resultado = _criar_operacao_recebimento_cliente(
            cliente, Decimal("30"), date(2026, 10, 15), "PIX",
            dividas_selecionadas=[f"locacao:{locacao.pk}"],
        )
        pagamento = locacao.pagamentos.get()
        self.assertIsNone(pagamento.movimento_financeiro_id)
        self.assertEqual(self.resumo()["recebido"], 30)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="recebimento_cliente").count(), 1)
        _desfazer_operacao_recebimento_cliente(resultado["operacao"].pk)
        self.assertEqual(self.resumo()["recebido"], 0)
        self.assertFalse(locacao.pagamentos.exists())
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="recebimento_cliente_estorno").count(), 1)

    def test_filtros_combinados_cliente_status_e_financeiro_atual(self):
        cliente = Cliente.objects.create(nome="Empresa fictícia", ativo=True)
        alvo = self.criar(cliente=cliente, tipo_pessoa="cliente", status="entregue")
        self.pagar(alvo, 25)
        self.criar(cliente=cliente, status="reservada")
        self.criar(pessoa_avulsa_nome="Outro", status="entregue")
        quitada = self.criar(cliente=cliente, status="entregue")
        self.pagar(quitada, 100)
        r = resumo_financeiro_locacoes(inicio=date(2026, 10, 1), fim=date(2026, 10, 31), cliente="Empresa", status="entregue", financeiro="com_saldo")
        self.assertEqual((r["quantidade"], r["contratado"], r["recebido"], r["saldo"]), (1, 100, 25, 75))
        self.assertEqual(len(r["restricoes"]), 3)
        r = resumo_financeiro_locacoes(inicio=date(2026, 10, 1), fim=date(2026, 10, 31), cliente="Empresa", status="entregue", financeiro="quitadas")
        self.assertEqual((r["quantidade"], r["recebido"], r["saldo"]), (1, 100, 0))
        self.assertEqual(self.resumo(cliente="Outro")["quantidade"], 4)

    def test_datas_inclusivas_no_fuso_com_limites_fora_do_intervalo(self):
        for instante, incluido in (("2026-09-30T23:59:59.999999", False),
                                   ("2026-10-01T00:00:00", True),
                                   ("2026-10-31T23:59:59.999999", True),
                                   ("2026-11-01T00:00:00", False)):
            locacao = self.criar(instante)
            self.pagar(locacao, 10, instante)
        r = self.resumo()
        self.assertEqual((r["quantidade"], r["contratado"], r["recebido"]), (2, 200, 20))
        # Dia UTC diferente do dia civil local: não entra em outubro.
        locacao = self.criar("2026-10-01T01:00:00+00:00")
        self.pagar(locacao, 10, "2026-10-01T01:00:00+00:00")
        self.assertEqual(self.resumo()["quantidade"], 2)
        self.assertEqual(self.resumo()["recebido"], 20)

    def test_sem_datas_e_limites_abertos_incluem_historico(self):
        self.criar("2026-01-01")
        self.criar("2026-10-01")
        self.criar("2027-01-01")
        self.assertEqual(self.resumo(resumo_inicio="", resumo_fim="")["quantidade"], 3)
        self.assertEqual(self.resumo(resumo_inicio="", resumo_fim="2026-10-01")["quantidade"], 2)
        self.assertEqual(self.resumo(resumo_inicio="2026-10-01", resumo_fim="")["quantidade"], 2)

    def test_datas_invalidas_nao_exibem_valores_nem_consultam_agregacoes(self):
        self.criar()
        for filtros in ({"resumo_inicio": "2026-02-30"}, {"resumo_fim": "invalida"}, {"resumo_inicio": "2026-11-01", "resumo_fim": "2026-10-01"}):
            with patch("locacoes.views.resumo_financeiro_locacoes") as calcular:
                resposta = self.consultar(**filtros)
                calcular.assert_not_called()
            self.assertIsNone(resposta.context["resumo_financeiro"])
            self.assertContains(resposta, "Resumo indisponível")
            self.assertNotContains(resposta, 'class="resumo-financeiro-blocos"')

    def test_paginacao_e_filas_nao_restringem_resumo(self):
        for _ in range(31):
            self.criar()
        alvo = self.criar(status="entregue")
        self.pagar(alvo, 10)
        padrao = self.resumo()
        self.assertEqual((padrao["quantidade"], padrao["contratado"]), (32, 3200))
        for filtros in ({"pagina": 2}, {"aba": "recolhimentos"}, {"situacao": "pendentes"}, {"situacao": "realizadas"}, {"visao": "historico"}):
            self.assertEqual(self.resumo(**filtros), padrao)
        self.assertEqual(len(self.consultar().context["locacoes"]), 25)

    def test_duas_consultas_sem_multiplicar_contratos_por_parcelas(self):
        for _ in range(12):
            locacao = self.criar()
            for _ in range(3):
                self.pagar(locacao, 10)
        with self.assertNumQueries(2):
            r = resumo_financeiro_locacoes(inicio=date(2026, 10, 1), fim=date(2026, 10, 31))
        self.assertEqual((r["quantidade"], r["contratado"], r["recebido"], r["saldo"]), (12, 1200, 360, 840))

    def test_consulta_somente_leitura(self):
        locacao = self.criar()
        self.pagar(locacao, 10)
        modelos = list(apps.get_app_config("locacoes").get_models()) + [MovimentoFinanceiro]
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        with CaptureQueriesContext(connection) as consultas:
            self.consultar()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in consultas))
        self.assertEqual(antes, {m: list(m.objects.order_by("pk").values()) for m in modelos})


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@skipUnless(CHROME.exists(), "Chrome necessário")
class ResumoFinanceiroBrowserTests(DadosResumo, TestCase):
    def test_previa_desktop_mobile_e_posicao(self):
        nova = self.criar(total=3600)
        self.pagar(nova, 2200)
        antiga = self.criar("2026-08-01", total=3000)
        self.pagar(antiga, 2000)
        cancelada = self.criar(total=500)
        self.pagar(cancelada, 120)
        cancelada.cancelar()
        html = self.consultar().content.decode()
        html = re.sub(r'<link[^>]+href="https://[^>]+>', '', html)
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
        html = html.replace('<link rel="stylesheet" href="/static/locacoes/consulta-operacional.css">', '<style>' + (settings.BASE_DIR / "static/locacoes/consulta-operacional.css").read_text(encoding="utf-8") + '</style>')
        destino = settings.BASE_DIR / "amostras/previa-resumo-financeiro"
        destino.mkdir(parents=True, exist_ok=True)
        destino = Path(tempfile.mkdtemp(prefix="resumo-", dir=destino))
        with tempfile.TemporaryDirectory(prefix="resumo-locacoes-", ignore_cleanup_errors=True) as perfil:
            chrome = Chrome(str(CHROME), perfil).start()
            try:
                tab = chrome.tab()
                for largura, colunas in ((1366, 4), (1051, 4), (390, 2), (320, 1)):
                    tab.call("Emulation.setDeviceMetricsOverride", {"width": largura, "height": 1100, "deviceScaleFactor": 1, "mobile": largura < 600})
                    tab.call("Page.setDocumentContent", {"frameId": tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"], "html": html})
                    tab.wait('!!document.querySelector(".resumo-financeiro-blocos")')
                    medidas = tab.evaluate('''(()=>{const r=document.querySelector('.resumo-financeiro'),b=r.querySelector('dl'),f=document.querySelector('#filtros-locacoes').closest('section'),n=document.querySelector('.consulta-operacional');return {colunas:getComputedStyle(b).gridTemplateColumns.split(' ').length,ordem:r.getBoundingClientRect().bottom<=f.getBoundingClientRect().top&&f.getBoundingClientRect().bottom<=n.getBoundingClientRect().top,semOverflow:document.documentElement.scrollWidth<=innerWidth,texto:r.textContent}})()''')
                    self.assertEqual(medidas["colunas"], colunas)
                    self.assertTrue(medidas["ordem"])
                    self.assertTrue(medidas["semOverflow"])
                    self.assertIn("4200", re.sub(r"\D", "", medidas["texto"]))
                    tab.evaluate('document.querySelector(".resumo-financeiro").scrollIntoView({block:"start"});true')
                    imagem = tab.call("Page.captureScreenshot", {"format": "png"})
                    (destino / f"resumo-{largura}.png").write_bytes(base64.b64decode(imagem["data"]))
            finally:
                chrome.stop()
