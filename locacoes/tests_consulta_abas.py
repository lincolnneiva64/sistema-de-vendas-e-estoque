from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente
from offline.browser_support import Chrome
from .consulta import MARCO_OPERACIONAL
from .models import (
    ConferenciaEntregaLocacao, ConfiguracaoLocacao, FaixaPrecoLocacao,
    Locacao, TarefaOperacionalLocacao,
)


class ConsultaAbasTests(TestCase):
    def setUp(self):
        ConfiguracaoLocacao.obter()
        self.faixa, _ = FaixaPrecoLocacao.objects.get_or_create(
            codigo=FaixaPrecoLocacao.CENTRO_PERTO,
            defaults={"nome": "Centro teste", "preco_jogo_diaria": 10},
        )
        self.relogio = patch("locacoes.views.timezone.localdate", return_value=date(2026, 10, 16))
        self.relogio.start()
        self.addCleanup(self.relogio.stop)

    def criar(self, data=date(2026, 10, 2), **extras):
        dados = dict(
            tipo_pessoa=Locacao.TIPO_PESSOA_AVULSA, pessoa_avulsa_nome="Cliente Consulta",
            endereco_entrega="Rua ficticia", faixa_preco=self.faixa,
            data_entrega=data, horario_entrega=time(9), data_evento=data,
            horario_evento=time(18), data_prevista_devolucao=data + timedelta(days=1),
        )
        dados.update(extras)
        return Locacao.objects.create(**dados)

    def tarefa(self, locacao, tipo="recolhimento", **extras):
        dados = dict(locacao=locacao, tipo=tipo, data_agendada=locacao.data_prevista_devolucao)
        dados.update(extras)
        return TarefaOperacionalLocacao.objects.create(**dados)

    def consultar(self, **filtros):
        return self.client.get(reverse("locacoes:lista"), filtros)

    def ids(self, **filtros):
        return [item.pk for item in self.consultar(**filtros).context["locacoes"]]

    def conferencia(self, locacao, quando):
        registro = ConferenciaEntregaLocacao.objects.create(
            locacao=locacao, previsto_mesas=1, previsto_cadeiras=4,
            entregue_mesas=1, entregue_cadeiras=4, acumulado_mesas=1, acumulado_cadeiras=4,
            pendente_mesas=0, pendente_cadeiras=0, situacao="completa",
            recebedor_nome="Teste", recebedor_relacao="cliente", estado_material="bom", responsavel="Teste",
        )
        ConferenciaEntregaLocacao.objects.filter(pk=registro.pk).update(criado_em=quando)

    def test_marco_fixo_inclusivo_e_nao_janela_movel(self):
        self.assertEqual(MARCO_OPERACIONAL, date(2026, 10, 1))
        antiga = self.criar(date(2026, 9, 30))
        limite = self.criar(MARCO_OPERACIONAL)
        with patch("locacoes.views.timezone.localdate", return_value=date(2027, 2, 1)):
            self.assertEqual(self.ids(), [limite.pk])
            self.assertEqual(self.ids(aba="historico"), [limite.pk, antiga.pk])

    def test_abas_particionam_canceladas_antigas_ativas_e_concluidas(self):
        antiga = self.criar(date(2026, 9, 28), status="entregue")
        cancelada = self.criar(date(2026, 11, 8), status="cancelada")
        ativa = self.criar(status="entregue")
        concluida = self.criar(status="devolvida")
        self.assertEqual(self.ids(aba="recolhimentos"), [ativa.pk])
        self.assertEqual(self.ids(aba="concluidas"), [concluida.pk])
        self.assertEqual(set(self.ids(aba="historico")), {antiga.pk, cancelada.pk, ativa.pk, concluida.pk})

    def test_canceladas_70_71_com_tarefas_pendentes_so_no_historico(self):
        for pk in [70, 71]:
            locacao = self.criar(pk=pk, status="cancelada")
            self.tarefa(locacao, tipo="entrega")
        for aba in ["andamento", "concluidas"]:
            self.assertEqual(self.ids(aba=aba, status="todos"), [])
            self.assertEqual(self.ids(aba=aba, status="cancelada"), [])
        self.assertEqual(set(self.ids(aba="historico")), {70, 71})

    def test_reserva_futura_e_pendencias_recentes_atrasadas(self):
        futura = self.criar(date(2027, 1, 1))
        atrasada = self.criar(MARCO_OPERACIONAL, status="entregue")
        parcial = self.criar(status="pendente_devolucao")
        self.tarefa(parcial, status="parcial")
        self.assertEqual(self.ids(), [futura.pk, parcial.pk, atrasada.pk])
        self.assertEqual(self.ids(aba="recolhimentos"), [atrasada.pk, parcial.pk])

    def test_entrega_parcial_e_tentativa_nao_realizada_continuam_abertas(self):
        for status in ["parcial", "nao_possivel"]:
            locacao = self.criar(status="saiu_para_entrega")
            self.tarefa(locacao, tipo="entrega", status=status)
            self.assertIn(locacao.pk, self.ids())

    def test_devolvida_com_avaria_concluida_mesmo_com_divida(self):
        for status in ["devolvida", "devolvida_com_avaria"]:
            locacao = self.criar(status=status, saldo_devedor=Decimal("50.00"))
            self.assertIn(locacao.pk, self.ids(aba="concluidas", financeiro="com_saldo"))
            self.assertIn(locacao.pk, self.ids())

    def test_quitada_nao_equivale_a_devolvida(self):
        ativa = self.criar(status="entregue", saldo_devedor=0)
        self.assertEqual(self.ids(aba="recolhimentos", financeiro="quitadas"), [ativa.pk])
        self.assertEqual(self.ids(aba="concluidas"), [])

    def test_encerramento_administrativo_nao_afirma_devolucao_fisica(self):
        locacao = self.criar(status="entregue")
        tarefa = self.tarefa(locacao, status="resolvida_admin")
        self.assertEqual(self.ids(), [locacao.pk])
        self.assertEqual(self.ids(aba="concluidas"), [])
        response = self.consultar(aba="historico")
        self.assertContains(response, "Encerramento administrativo")
        self.assertNotContains(response, "Atraso na devolucao")
        tarefa.refresh_from_db();locacao.refresh_from_db()
        self.assertEqual(tarefa.status, "resolvida_admin")
        self.assertEqual(locacao.status, "entregue")

    def test_entrega_sem_checklist_nao_encerra_recolhimento(self):
        locacao = self.criar(status="entregue")
        self.tarefa(locacao, tipo="entrega", status="confirmada_sem_checklist")
        self.assertEqual(self.ids(aba="recolhimentos"), [locacao.pk])

    def test_antiga_so_com_status_e_tarefa_criada_nao_e_excecao(self):
        locacao = self.criar(date(2026, 9, 27), status="entregue")
        self.tarefa(locacao, data_agendada=date(2026, 10, 20))
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.ids(aba="historico"), [locacao.pk])

    def test_antiga_com_checklist_atual_e_pendencia_aparece_sinalizada(self):
        locacao = self.criar(date(2026, 9, 28), status="entregue")
        self.conferencia(locacao, timezone.make_aware(datetime(2026, 10, 3, 9)))
        response = self.consultar(aba="recolhimentos")
        self.assertEqual([item.pk for item in response.context["locacoes"]], [locacao.pk])
        self.assertContains(response, "Anterior ao marco: atividade operacional registrada")
        self.assertEqual(self.ids(aba="historico"), [locacao.pk])

    def test_antiga_com_checklist_antigo_nao_e_excecao(self):
        locacao = self.criar(date(2026, 9, 28), status="entregue")
        self.conferencia(locacao, timezone.make_aware(datetime(2026, 9, 28, 9)))
        self.assertEqual(self.ids(), [])

    def test_antiga_resolvida_admin_nao_reabre_por_checklist_recente(self):
        locacao = self.criar(date(2026, 9, 28), status="entregue")
        self.conferencia(locacao, timezone.now())
        self.tarefa(locacao, status="resolvida_admin")
        self.assertEqual(self.ids(), [locacao.pk])
        self.assertEqual(self.ids(aba="historico"), [locacao.pk])

    def test_tentativa_documentada_atual_preserva_pendencia_antiga(self):
        locacao = self.criar(date(2026, 9, 28))
        self.tarefa(locacao, tipo="entrega", status="nao_possivel", tentativa_em=timezone.now())
        self.assertEqual(self.ids(), [locacao.pk])

    def test_cliente_cadastrado_e_contratante_avulso(self):
        cliente = Cliente.objects.create(nome="Cliente Cadastrado")
        cadastrado = self.criar(tipo_pessoa="cliente", cliente=cliente, pessoa_avulsa_nome="")
        avulso = self.criar(pessoa_avulsa_nome="Contratante Avulso")
        self.assertEqual(self.ids(cliente="cadastrado"), [cadastrado.pk])
        self.assertEqual(self.ids(cliente="AVULSO"), [avulso.pk])

    def test_cliente_e_datas_nao_atravessam_limites_da_aba(self):
        antiga = self.criar(date(2026, 9, 28), pessoa_avulsa_nome="Antiga")
        self.assertEqual(self.ids(cliente="Antiga", data_fim="2026-09-30"), [])
        self.assertEqual(self.ids(aba="historico", cliente="Antiga", data_fim="2026-09-30"), [antiga.pk])

    def test_limpar_e_pesquisar_preservam_aba(self):
        for aba in ["entregas", "recolhimentos"]:
            response = self.consultar(aba=aba, cliente="Teste", financeiro="com_saldo")
            self.assertContains(response, f'name="aba" value="{aba}"')
            self.assertContains(response, f'href="?aba={aba}" id="limpar-filtros-locacoes"')
            for item in response.context["abas"]:
                params = parse_qs(urlsplit(item["url"]).query)
                self.assertEqual(params["cliente"], ["Teste"])
                self.assertEqual(params["financeiro"], ["com_saldo"])
                self.assertEqual(params["aba"], [item["valor"]])

    def test_aba_invalida_usa_entregas(self):
        self.assertEqual(self.consultar(aba="invalida").context["aba"], "entregas")

    def test_sem_tarefa_link_prepara_acao_sem_criar_registro(self):
        entrega = self.criar()
        recolhimento = self.criar(status="entregue")
        for locacao, tipo in [(entrega, "entrega"), (recolhimento, "recolhimento")]:
            response = self.consultar(aba="entregas" if tipo == "entrega" else "recolhimentos")
            self.assertContains(response, reverse("locacoes:abrir_tarefa_operacional", args=[locacao.pk, tipo]))
        self.assertFalse(TarefaOperacionalLocacao.objects.exists())

    def test_todas_as_abas_sao_somente_leitura_inclusive_agendamentos(self):
        self.criar()
        self.criar(status="devolvida_com_avaria")
        self.criar(date(2026, 9, 28), status="entregue")
        self.criar(status="cancelada")
        locacao = self.criar(status="entregue")
        self.tarefa(locacao, status="parcial", data_agendada=date(2026, 10, 25))
        models = list(apps.get_app_config("locacoes").get_models())
        antes = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        def sem_escrita(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(sem_escrita):
            for aba in ["entregas", "recolhimentos", "historico"]:
                self.assertEqual(self.consultar(aba=aba).status_code, 200)
        depois = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        self.assertEqual(antes, depois)


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


@skipUnless(CHROME.exists(), "Chrome necessario para validar a consulta")
class ConsultaAbasBrowserTests(StaticLiveServerTestCase):
    setUp = ConsultaAbasTests.setUp
    criar = ConsultaAbasTests.criar

    def fluxo(self, largura):
        antiga = self.criar(date(2026, 9, 28), pessoa_avulsa_nome="Antiga Pesquisa")
        cancelada = self.criar(status="cancelada", pessoa_avulsa_nome="Cancelada")
        ativa = self.criar(status="entregue")
        concluida = self.criar(status="devolvida", saldo_devedor=10)
        reserva = self.criar()
        models = list(apps.get_app_config("locacoes").get_models())
        antes = {m.__name__: list(m.objects.order_by("pk").values()) for m in models}
        with tempfile.TemporaryDirectory(prefix="consulta-locacoes-chrome-") as perfil:
            chrome = Chrome(str(CHROME), perfil).start()
            try:
                tab = chrome.tab()
                tab.call("Network.enable")
                tab.call("Network.setBlockedURLs", {"urls": ["https://cdn.jsdelivr.net/*"]})
                tab.call("Emulation.setDeviceMetricsOverride", {
                    "width": largura, "height": 900, "deviceScaleFactor": 1, "mobile": largura < 600,
                })
                tab.call("Page.navigate", {"url": self.live_server_url + reverse("locacoes:lista")})
                def esperar(aba, ids):
                    import json
                    tab.wait('document.querySelector("input[name=aba]")?.value === ' + json.dumps(aba))
                    tab.wait('JSON.stringify(Array.from(document.querySelectorAll("tbody td:first-child a")).map(a=>Number(a.textContent.trim().slice(1)))) === ' + json.dumps(json.dumps(ids, separators=(",", ":"))))
                    tab.wait('document.readyState === "complete"')
                esperar("entregas", [reserva.pk])
                tab.evaluate('document.querySelector("nav a[href*=recolhimentos]").click();true')
                esperar("recolhimentos", [ativa.pk])
                self.assertIn("recolhimento", tab.evaluate('document.querySelector("tbody").textContent').lower())
                tab.evaluate('const situacao=document.querySelector("select[name=situacao]");situacao.value="realizadas";situacao.dispatchEvent(new Event("change",{bubbles:true}));true')
                esperar("recolhimentos", [concluida.pk])
                tab.evaluate('document.querySelector("a[href*=visao]").click();true')
                esperar("recolhimentos", [reserva.pk, concluida.pk, ativa.pk, cancelada.pk, antiga.pk])
                tab.evaluate('const campo=document.querySelector("input[name=cliente]");campo.value="Antiga";campo.dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.wait('new URLSearchParams(location.search).get("cliente")==="Antiga"')
                esperar("recolhimentos", [antiga.pk])
                tab.call("Page.reload")
                esperar("recolhimentos", [antiga.pk])
                self.assertEqual(tab.evaluate('document.querySelector("input[name=cliente]").value'), "Antiga")
                tab.evaluate('document.getElementById("limpar-filtros-locacoes").click();true')
                tab.wait('new URLSearchParams(location.search).get("cliente")===null')
                esperar("recolhimentos", [reserva.pk, concluida.pk, ativa.pk, cancelada.pk, antiga.pk])
                self.assertEqual(tab.evaluate('document.querySelector("input[name=cliente]").value'), "")
            finally:
                chrome.stop()
        depois = {m.__name__: list(m.objects.order_by("pk").values()) for m in models}
        self.assertEqual(antes, depois)

    def test_fluxo_desktop(self):
        self.fluxo(1366)

    def test_fluxo_mobile(self):
        self.fluxo(390)
