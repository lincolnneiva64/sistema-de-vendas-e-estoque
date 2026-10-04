from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from . import tests as fixtures
from .models import (
    TarefaOperacionalLocacao, ConferenciaEntregaLocacao,
    ConferenciaRecolhimentoLocacao, MovimentoEstoqueLocacao,
    PagamentoLocacao, ConfiguracaoLocacao, FaixaPrecoLocacao,
)
from .services import obter_ou_criar_tarefa_operacional, painel_operacional_rapido_locacoes
from estoque.models import MovimentoFinanceiro


class ResolucaoAdministrativaTests(TestCase):
    def setUp(self):
        FaixaPrecoLocacao.objects.get_or_create(
            codigo=FaixaPrecoLocacao.CENTRO_PERTO,
            defaults={"nome": "Centro/perto", "preco_jogo_diaria": 8},
        )
        fixtures.LocacoesChecklistOperacionalTests.setUp(self)
    dados_base = fixtures.LocacoesChecklistOperacionalTests.dados_base
    criar_locacao = fixtures.LocacoesChecklistOperacionalTests.criar_locacao
    colocar_na_rua = fixtures.LocacoesChecklistOperacionalTests.colocar_na_rua

    def painel(self, hora=time(7, 30)):
        return painel_operacional_rapido_locacoes(
            self.factory.get("/"), self.hoje,
            timezone.make_aware(datetime.combine(self.hoje, hora)),
        )

    def tarefa(self, tipo="entrega", **extras):
        locacao = self.criar_locacao(**extras)
        if tipo == "recolhimento":
            locacao = self.colocar_na_rua(locacao)
        return obter_ou_criar_tarefa_operacional(locacao, tipo)

    def test_pendente_aparece(self):
        tarefa = self.tarefa()
        self.assertIn(tarefa.pk, [i["tarefa"].pk for i in self.painel()["itens"]])

    def test_resolucao_preserva_tarefa_evento_e_nao_altera_operacao(self):
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                tarefa = self.tarefa(tipo)
                locacao = tarefa.locacao
                antes = {
                    "locacao": type(locacao).objects.filter(pk=locacao.pk).values().get(),
                    "itens": list(locacao.itens.values()),
                    "configuracao": list(ConfiguracaoLocacao.objects.values()),
                    "estoque": list(MovimentoEstoqueLocacao.objects.values()),
                    "financeiro": list(MovimentoFinanceiro.objects.values()),
                    "pagamentos": list(PagamentoLocacao.objects.values()),
                    "entregas": ConferenciaEntregaLocacao.objects.count(),
                    "recolhimentos": ConferenciaRecolhimentoLocacao.objects.count(),
                }
                tarefa.resolver_administrativamente("Operacao realizada; registro esquecido", "Roseli")
                self.assertTrue(TarefaOperacionalLocacao.objects.filter(pk=tarefa.pk).exists())
                self.assertEqual(tarefa.status, "resolvida_admin")
                self.assertFalse(tarefa.pendente_operacional)
                self.assertIsNone(tarefa.confirmado_em)
                self.assertEqual(tarefa.resolvida_por, "Roseli")
                self.assertIsNotNone(tarefa.resolvida_em)
                evento = locacao.eventos.get(tipo="tarefa_resolvida_admin")
                for texto in (str(tarefa.pk), tipo, "pendente", "Roseli", "registro esquecido"):
                    self.assertIn(texto, evento.descricao)
                self.assertEqual(evento.responsavel, "Roseli")
                self.assertEqual(antes["locacao"], type(locacao).objects.filter(pk=locacao.pk).values().get())
                self.assertEqual(antes["itens"], list(locacao.itens.values()))
                self.assertEqual(antes["configuracao"], list(ConfiguracaoLocacao.objects.values()))
                self.assertEqual(antes["estoque"], list(MovimentoEstoqueLocacao.objects.values()))
                self.assertEqual(antes["financeiro"], list(MovimentoFinanceiro.objects.values()))
                self.assertEqual(antes["pagamentos"], list(PagamentoLocacao.objects.values()))
                self.assertEqual(antes["entregas"], ConferenciaEntregaLocacao.objects.count())
                self.assertEqual(antes["recolhimentos"], ConferenciaRecolhimentoLocacao.objects.count())
                self.assertNotIn(tarefa.pk, [i["tarefa"].pk for i in self.painel()["itens"]])
                obter_ou_criar_tarefa_operacional(locacao, tipo).refresh_from_db()
                tarefa.refresh_from_db()
                self.assertEqual(tarefa.status, "resolvida_admin")
                self.assertFalse(locacao.pode_excluir())

    def test_endpoint_identifica_usuario_e_rejeita_repeticao(self):
        tarefa = self.tarefa()
        usuario = get_user_model().objects.create_user(username="roseli", password="teste")
        self.client.force_login(usuario)
        url = reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk])
        self.assertEqual(self.client.get(url, secure=True).status_code, 405)
        self.assertEqual(self.client.post(url, {"motivo": " "}, secure=True).status_code, 400)
        self.assertEqual(self.client.post(url, {"motivo": "x" * 501}, secure=True).status_code, 400)
        self.assertEqual(self.client.post(url, {"motivo": "Ja realizada"}, secure=True).status_code, 200)
        tarefa.refresh_from_db()
        self.assertEqual(tarefa.resolvida_por, "roseli")
        self.assertEqual(self.client.post(url, {"motivo": "Repetida"}, secure=True).status_code, 400)
        self.assertEqual(tarefa.locacao.eventos.filter(tipo="tarefa_resolvida_admin").count(), 1)

    def test_confirmada_nao_reaparece_nem_pode_ser_resolvida(self):
        tarefa = self.tarefa()
        tarefa.confirmar("Roseli")
        with self.assertRaises(ValidationError):
            tarefa.resolver_administrativamente("Ja entregue")
        self.assertNotIn(tarefa.pk, [i["tarefa"].pk for i in self.painel()["itens"]])

    def test_resolvida_nao_pode_ser_reaberta_por_acoes_antigas(self):
        tarefa = self.tarefa()
        tarefa.resolver_administrativamente("Ja realizada")
        with self.assertRaises(ValidationError):
            tarefa.registrar_nao_possivel("Outro motivo")
        with self.assertRaises(ValidationError):
            tarefa.confirmar()

    def test_recolhimento_parcial_antigo_permanece_no_painel(self):
        tarefa = self.tarefa("recolhimento", data_entrega=self.hoje - timedelta(days=10),
                             data_prevista_devolucao=self.hoje - timedelta(days=8))
        tarefa.status = "parcial"
        tarefa.save(update_fields=["status"])
        painel = self.painel()
        item = next(i for i in painel["itens"] if i["tarefa"].pk == tarefa.pk)
        self.assertFalse(item["entrega_antiga"])
        self.assertTrue(item["vencida"])
        self.assertEqual(painel["total_recolhimentos"], 1)

    def test_entregas_antigas_ficam_acessiveis_fora_do_contador_recente(self):
        antigas = self.tarefa(data_entrega=self.hoje - timedelta(days=3))
        for dias in (0, 1, 2):
            self.tarefa(data_entrega=self.hoje - timedelta(days=dias))
        painel = self.painel()
        self.assertEqual(painel["total"], 3)
        self.assertEqual(painel["total_entregas_antigas"], 1)
        item = next(i for i in painel["itens"] if i["tarefa"].pk == antigas.pk)
        self.assertTrue(item["entrega_antiga"])
        antigas.resolver_administrativamente("Ja realizada")
        self.assertEqual(self.painel()["total_entregas_antigas"], 0)

    def test_proxima_exige_hoje_e_preserva_janela_de_uma_hora(self):
        tarefa = self.tarefa(horario_entrega=time(8, 0))
        for hora, proxima, vencida in ((time(6, 59), False, False),
                                      (time(7, 0), True, False),
                                      (time(7, 30), True, False),
                                      (time(8, 0), False, True),
                                      (time(8, 1), False, True)):
            with self.subTest(hora=hora):
                item = next(i for i in self.painel(hora)["itens"] if i["tarefa"].pk == tarefa.pk)
                self.assertEqual(item["proxima"], proxima)
                self.assertEqual(item["vencida"], vencida)

    def test_futura_nao_recebe_atencao_apenas_pela_hora(self):
        tarefa = self.tarefa(data_entrega=self.hoje + timedelta(days=1), horario_entrega=time(8, 0))
        item = next(i for i in self.painel()["itens"] if i["tarefa"].pk == tarefa.pk)
        self.assertFalse(item["proxima"])
        self.assertFalse(item["vencida"])
        self.assertEqual(item["data_label"], "Amanhã")

    def test_resolucao_exige_csrf(self):
        from django.test import Client
        tarefa = self.tarefa()
        resposta = Client(enforce_csrf_checks=True).post(
            reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk]),
            {"motivo": "Ja realizada"}, secure=True,
        )
        self.assertEqual(resposta.status_code, 403)
        tarefa.refresh_from_db()
        self.assertEqual(tarefa.status, "pendente")

    def test_template_tem_resolucao_e_acesso_as_entregas_antigas(self):
        from django.template.loader import render_to_string
        tarefa = self.tarefa(data_entrega=self.hoje - timedelta(days=5))
        html = render_to_string("estoque/vendas_layout_teste.html", {
            "locacoes_operacionais": self.painel(),
        })
        self.assertIn("Dar como resolvida", html)
        self.assertIn('data-entrega-antiga="1"', html)
        self.assertIn('data-locacoes-filtro="antigas"', html)
        self.assertIn(reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk]), html)


class ResolucaoAdministrativaRegressaoTests(TestCase):
    setUp = ResolucaoAdministrativaTests.setUp
    dados_base = ResolucaoAdministrativaTests.dados_base
    criar_locacao = ResolucaoAdministrativaTests.criar_locacao
    colocar_na_rua = ResolucaoAdministrativaTests.colocar_na_rua
    tarefa = ResolucaoAdministrativaTests.tarefa
    painel = ResolucaoAdministrativaTests.painel

    def test_resolvida_admin_nao_gera_acao(self):
        from .views import _acoes_locacao
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                tarefa = self.tarefa(tipo)
                tarefa.resolver_administrativamente("Ja realizada")
                acoes = _acoes_locacao(tarefa.locacao, self.factory.get("/"))
                self.assertIsNone(acoes[f"tarefa_{tipo}"])

    def test_resolvida_admin_nao_gera_whatsapp_operacional(self):
        from unittest.mock import patch
        from .views import _acoes_locacao, _whatsapp_recolhimento_context
        tarefa = self.tarefa("recolhimento")
        tarefa.resolver_administrativamente("Ja realizada")
        request = self.factory.get("/")
        self.assertEqual(_whatsapp_recolhimento_context(request, tarefa), {})
        with patch("locacoes.views._whatsapp_recolhimento_context") as contexto:
            self.assertEqual(_acoes_locacao(tarefa.locacao, request)["whatsapp_recolhimento"], {})
            contexto.assert_not_called()

    def test_acoes_preservam_demais_status(self):
        from .views import _acoes_locacao, _whatsapp_recolhimento_context
        for tipo in ("entrega", "recolhimento"):
            tarefa = self.tarefa(tipo)
            for status in ("pendente", "parcial", "nao_possivel", "confirmada"):
                with self.subTest(tipo=tipo, status=status):
                    tarefa.status = status
                    tarefa.save(update_fields=["status"])
                    request = self.factory.get("/")
                    self.assertEqual(_acoes_locacao(tarefa.locacao, request)[f"tarefa_{tipo}"].pk, tarefa.pk)
                    if tipo == "recolhimento":
                        self.assertEqual(bool(_whatsapp_recolhimento_context(request, tarefa)), status != "confirmada")

    def test_resolvida_admin_nao_reaparece_em_alerta(self):
        from estoque.views import _contexto_alertas_locacoes
        for tipo, dias in (("entrega", 0), ("recolhimento", 0), ("recolhimento", -1)):
            with self.subTest(tipo=tipo, dias=dias):
                tarefa = self.tarefa(tipo, data_entrega=self.hoje + timedelta(days=min(dias, 0)),
                                     data_prevista_devolucao=self.hoje + timedelta(days=dias))
                tarefa.resolver_administrativamente("Ja realizada")
        self.assertEqual(_contexto_alertas_locacoes(self.hoje)["locacoes_alertas_operacionais"], [])

    def test_entrega_resolvida_preserva_alerta_recolhimento_pendente(self):
        from estoque.views import _contexto_alertas_locacoes
        from .models import Locacao
        entrega = self.tarefa(data_prevista_devolucao=self.hoje)
        entrega.resolver_administrativamente("Entrega ja realizada")
        # Simula a evolucao posterior da locacao sem reabrir a entrega.
        Locacao.objects.filter(pk=entrega.locacao_id).update(status=Locacao.STATUS_ENTREGUE)
        entrega.locacao.refresh_from_db()
        recolhimento = obter_ou_criar_tarefa_operacional(entrega.locacao, "recolhimento")
        self.assertTrue(recolhimento.pendente_operacional)
        alertas = _contexto_alertas_locacoes(self.hoje)["locacoes_alertas_operacionais"]
        self.assertEqual([a["tipo"] for a in alertas], ["recolhimento"])

    def test_resolvida_admin_nao_determina_proxima_data(self):
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                tarefa = self.tarefa(tipo, data_entrega=self.hoje + timedelta(days=1),
                                     data_prevista_devolucao=self.hoje + timedelta(days=2))
                tarefa.resolver_administrativamente("Ja realizada")
        self.assertEqual(self.painel()["itens"], [])

    def test_proxima_operacao_ativa_apos_encerradas_aparece(self):
        for tipo in ("entrega", "recolhimento"):
            for status in ("resolvida_admin", "confirmada"):
                with self.subTest(tipo=tipo, status=status):
                    encerrada = self.tarefa(tipo, data_entrega=self.hoje + timedelta(days=1),
                                           data_prevista_devolucao=self.hoje + timedelta(days=1))
                    if status == "resolvida_admin":
                        encerrada.resolver_administrativamente("Ja realizada")
                    else:
                        encerrada.status = "confirmada"
                        encerrada.save(update_fields=["status"])
        ativa = self.tarefa("recolhimento", data_entrega=self.hoje + timedelta(days=3),
                            data_prevista_devolucao=self.hoje + timedelta(days=3))
        itens = self.painel()["itens"]
        self.assertEqual([i["tarefa"].pk for i in itens], [ativa.pk])
        self.assertEqual(itens[0]["tarefa"].data_agendada, self.hoje + timedelta(days=3))

    def test_locacao_futura_sem_tarefa_continua_considerada(self):
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                locacao = self.criar_locacao(data_entrega=self.hoje + timedelta(days=1),
                                             data_prevista_devolucao=self.hoje + timedelta(days=1))
                if tipo == "recolhimento":
                    locacao = self.colocar_na_rua(locacao)
                self.assertFalse(locacao.tarefas_operacionais.filter(tipo=tipo).exists())
                itens = self.painel()["itens"]
                self.assertIn((locacao.pk, tipo), [(i["locacao"].pk, i["tipo"]) for i in itens])
