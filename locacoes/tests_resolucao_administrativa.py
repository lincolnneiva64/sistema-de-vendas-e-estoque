from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from . import tests as fixtures
from .models import (
    TarefaOperacionalLocacao, ConferenciaEntregaLocacao,
    ConferenciaRecolhimentoLocacao, MovimentoEstoqueLocacao,
    PagamentoLocacao, ConfiguracaoLocacao, FaixaPrecoLocacao, Locacao,
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
        self.assertEqual(self.client.post(url, {"motivo": "Ja realizada"}, secure=True).status_code, 400)
        self.assertEqual(self.client.post(url, {"motivo": "Criada por engano", "resultado": "tarefa_engano"}, secure=True).status_code, 200)
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

    def test_aba_lateral_janela_de_trinta_minutos(self):
        from django.template.loader import render_to_string
        self.tarefa(horario_entrega=time(10, 30))
        antiga = self.tarefa(data_entrega=self.hoje - timedelta(days=1))
        antiga.resolver_administrativamente("Já realizada")
        for hora, estado in ((time(9, 59), "normal"), (time(10, 0), "advertencia"),
                             (time(10, 12), "advertencia"), (time(10, 29), "advertencia"),
                             (time(10, 30), "alerta"), (time(10, 31), "alerta")):
            with self.subTest(hora=hora):
                painel = self.painel(hora)
                self.assertEqual(painel["alerta"], estado == "alerta")
                self.assertEqual(painel["advertencia_lateral"], estado == "advertencia")
                html = render_to_string("estoque/vendas_layout_teste.html", {"locacoes_operacionais": painel})
                import re
                classes = re.search(r'<aside class="([^"]+)" id="locacoesOperacionaisVenda"', html)[1].split()
                self.assertEqual("alerta" in classes, estado == "alerta")
                self.assertEqual("advertencia" in classes, estado == "advertencia")
                self.assertNotIn(antiga.pk, [i["tarefa"].pk for i in painel["itens"]])

    def test_aba_lateral_futura_nao_adverte(self):
        self.tarefa(data_entrega=self.hoje + timedelta(days=1), horario_entrega=time(10, 30))
        painel = self.painel(time(10, 12))
        self.assertFalse(painel["alerta"])
        self.assertFalse(painel["advertencia_lateral"])

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


class RegularizacaoEntregaTests(TestCase):
    setUp = ResolucaoAdministrativaTests.setUp
    dados_base = ResolucaoAdministrativaTests.dados_base
    criar_locacao = ResolucaoAdministrativaTests.criar_locacao
    colocar_na_rua = ResolucaoAdministrativaTests.colocar_na_rua
    tarefa = ResolucaoAdministrativaTests.tarefa
    painel = ResolucaoAdministrativaTests.painel

    def regularizar(self, tarefa, resultado="retirada_cliente", **extras):
        dados = {"resultado": resultado, "data_efetiva": self.hoje}
        dados.update(extras)
        return tarefa.resolver_administrativamente("Registro esquecido", "Equipe ficticia", **dados)

    def test_entrega_equipe_e_retirada_preservam_dados_e_recolhimento(self):
        for resultado in ("entrega_equipe", "retirada_cliente"):
            with self.subTest(resultado=resultado):
                tarefa = self.tarefa(jogos=0, mesas=6, cadeiras=16,
                                     data_prevista_devolucao=self.hoje)
                locacao = tarefa.locacao
                recolhimento = obter_ou_criar_tarefa_operacional(locacao, "recolhimento")
                locacao.registrar_pagamento("1.00", PagamentoLocacao.FORMA_PIX,
                                           responsavel="Caixa ficticio")
                antes = {
                    "itens": list(locacao.itens.values()),
                    "config": list(ConfiguracaoLocacao.objects.values()),
                    "movimentos": list(MovimentoEstoqueLocacao.objects.values()),
                    "financeiro": list(MovimentoFinanceiro.objects.values()),
                    "pagamentos": list(PagamentoLocacao.objects.values()),
                    "total": locacao.total, "saldo": locacao.saldo_devedor,
                    "historico": list(locacao.eventos.values()),
                }
                self.regularizar(tarefa, resultado)
                locacao.refresh_from_db()
                recolhimento.refresh_from_db()
                self.assertEqual(locacao.status, Locacao.STATUS_ENTREGUE)
                self.assertEqual(recolhimento.status, "pendente")
                self.assertEqual(recolhimento.data_agendada, self.hoje)
                self.assertEqual(locacao.tarefas_operacionais.count(), 2)
                self.assertIsNone(tarefa.confirmado_em)
                self.assertFalse(locacao.conferencias_entrega.exists())
                self.assertFalse(locacao.conferencias_recolhimento.exists())
                self.assertEqual(antes["itens"], list(locacao.itens.values()))
                self.assertEqual(antes["config"], list(ConfiguracaoLocacao.objects.values()))
                self.assertEqual(antes["movimentos"], list(MovimentoEstoqueLocacao.objects.values()))
                self.assertEqual(antes["financeiro"], list(MovimentoFinanceiro.objects.values()))
                self.assertEqual(antes["pagamentos"], list(PagamentoLocacao.objects.values()))
                self.assertEqual((locacao.total, locacao.saldo_devedor), (antes["total"], antes["saldo"]))
                self.assertEqual(antes["historico"], list(locacao.eventos.filter(
                    pk__in=[evento["id"] for evento in antes["historico"]]
                ).values()))
                evento = locacao.eventos.get(tipo="entregue")
                self.assertIn("sem checklist fisico", evento.descricao)
                self.assertIn(self.hoje.isoformat(), evento.descricao)
                self.assertIn(resultado, evento.descricao)
                self.assertIn(recolhimento.pk, [i["tarefa"].pk for i in self.painel()["itens"]])
                quantidade_eventos = locacao.eventos.count()
                with self.assertRaises(ValidationError):
                    self.regularizar(tarefa, resultado)
                self.assertEqual(locacao.eventos.count(), quantidade_eventos)

    def test_caso_87_ficticio_regulariza_posteriormente_sem_liberar_materiais(self):
        # Somente fixtures no SQLite descartavel; nenhuma leitura de registros reais.
        criar = Locacao.objects.create
        with patch.object(Locacao.objects, "create", side_effect=lambda **kw: criar(id=87, **kw)):
            locacao = self.criar_locacao(
                jogos=0, mesas=6, cadeiras=16, pessoa_avulsa_nome="Rubens Rocha",
                data_entrega=date(2026, 10, 10), data_prevista_devolucao=date(2026, 10, 12),
            )
        tarefa = obter_ou_criar_tarefa_operacional(locacao, "entrega")
        tarefa.resolver_administrativamente("Encerramento antigo", "Admin ficticio")
        registro_antigo = (tarefa.resolvida_em, tarefa.resolvida_por, tarefa.motivo_resolucao)
        with patch("django.utils.timezone.localdate", return_value=date(2026, 10, 12)):
            html = self.client.get(reverse("locacoes:detalhe", args=[87]), secure=True)
            self.assertContains(html, "Regularizar entrega encerrada administrativamente")
            self.assertContains(html, reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk]))
            resposta = self.client.post(reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk]), {
                "resultado": "retirada_cliente", "data_efetiva": "2026-10-10",
                "responsavel": "Responsavel ficticio", "motivo": "Retirada sem registro", "origem": "detalhe",
            }, secure=True)
        self.assertRedirects(resposta, reverse("locacoes:detalhe", args=[87]), fetch_redirect_response=False)
        locacao.refresh_from_db()
        tarefa.refresh_from_db()
        self.assertEqual(locacao.status, Locacao.STATUS_ENTREGUE)
        self.assertEqual(registro_antigo, (tarefa.resolvida_em, tarefa.resolvida_por, tarefa.motivo_resolucao))
        self.assertEqual(locacao.eventos.filter(tipo="tarefa_resolvida_admin").count(), 2)
        recolhimento = locacao.tarefas_operacionais.get(tipo="recolhimento")
        self.assertEqual((recolhimento.status, recolhimento.data_agendada), ("pendente", date(2026, 10, 12)))
        disponibilidade = Locacao.disponibilidade_periodo(date(2026, 10, 13), date(2026, 10, 14))
        self.assertEqual(disponibilidade["disponivel_mesas"], 24)
        self.assertEqual(disponibilidade["disponivel_cadeiras"], 104)
        self.assertFalse(locacao.conferencias_entrega.exists())

    def test_recolhimento_nao_regulariza_entrega_nem_libera_estoque(self):
        tarefa = self.tarefa("recolhimento")
        antes = Locacao.disponibilidade_periodo(self.hoje, self.hoje)
        for resultado in ("entrega_equipe", "retirada_cliente"):
            with self.assertRaises(ValidationError):
                self.regularizar(tarefa, resultado)
        tarefa.refresh_from_db()
        self.assertEqual(tarefa.status, "pendente")
        tarefa.resolver_administrativamente("Tarefa criada por engano", resultado="tarefa_engano")
        self.assertEqual(Locacao.disponibilidade_periodo(self.hoje, self.hoje), antes)
        self.assertFalse(tarefa.locacao.conferencias_recolhimento.exists())

    def test_dados_invalidos_nao_alteram_reserva(self):
        tarefa = self.tarefa()
        for extras in ({"data_efetiva": None}, {"data_efetiva": self.hoje + timedelta(days=1)},
                       {"resultado": "invalido"}):
            with self.subTest(extras=extras), self.assertRaises(ValidationError):
                self.regularizar(tarefa, **extras)
        with self.assertRaises(ValidationError):
            tarefa.resolver_administrativamente("Motivo", "", resultado="retirada_cliente", data_efetiva=self.hoje)
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_RESERVADA)

    def test_confirmacao_anterior_e_recolhimento_encerrado_impedem_regularizacao(self):
        tarefa = self.tarefa()
        tarefa.locacao.eventos.create(tipo="entregue", descricao="Entrega previa")
        with self.assertRaises(ValidationError):
            self.regularizar(tarefa)
        outra = self.tarefa()
        recolhimento = obter_ou_criar_tarefa_operacional(outra.locacao, "recolhimento")
        recolhimento.resolver_administrativamente("Encerramento antigo")
        with self.assertRaises(ValidationError):
            self.regularizar(outra)

    def test_falha_na_transicao_reverte_status_eventos_e_tarefa(self):
        tarefa = self.tarefa()
        eventos = tarefa.locacao.eventos.count()
        with patch.object(Locacao, "confirmar_entrega", side_effect=ValidationError("Falha simulada")):
            with self.assertRaises(ValidationError):
                self.regularizar(tarefa)
        tarefa.refresh_from_db()
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.status, "pendente")
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_RESERVADA)
        self.assertEqual(tarefa.locacao.eventos.count(), eventos)
        self.assertFalse(tarefa.locacao.tarefas_operacionais.filter(tipo="recolhimento").exists())

    def test_regulariza_saida_sem_duplicar_evento_de_saida(self):
        tarefa = self.tarefa()
        tarefa.locacao.marcar_saiu_para_entrega("Equipe ficticia")
        self.regularizar(tarefa, "entrega_equipe")
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_ENTREGUE)
        self.assertEqual(tarefa.locacao.eventos.filter(tipo="saiu_para_entrega").count(), 1)

    def test_endpoint_valida_data_responsavel_e_repeticao(self):
        tarefa = self.tarefa()
        url = reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk])
        dados = {"resultado": "entrega_equipe", "motivo": "Registro esquecido",
                 "responsavel": "Equipe ficticia", "data_efetiva": self.hoje.isoformat()}
        for invalido in ({"data_efetiva": "2026-02-30"}, {"data_efetiva": "texto"},
                         {"responsavel": " "}, {"resultado": "invalido"}):
            with self.subTest(invalido=invalido):
                self.assertEqual(self.client.post(url, {**dados, **invalido}, secure=True).status_code, 400)
        self.assertEqual(self.client.post(url, dados, secure=True).status_code, 200)
        self.assertEqual(self.client.post(url, dados, secure=True).status_code, 400)

    def test_regularizacao_posterior_nao_aceita_saida_ou_cancelamento(self):
        for status in (Locacao.STATUS_SAIU_PARA_ENTREGA, Locacao.STATUS_CANCELADA):
            with self.subTest(status=status):
                tarefa = self.tarefa()
                tarefa.resolver_administrativamente("Encerramento antigo")
                Locacao.objects.filter(pk=tarefa.locacao_id).update(status=status)
                with self.assertRaises(ValidationError):
                    self.regularizar(tarefa)

    def test_formulario_posterior_fica_na_secao_operacional_sem_mensagem_html(self):
        tarefa = self.tarefa()
        tarefa.resolver_administrativamente("Encerramento antigo")
        resposta = self.client.get(reverse("locacoes:detalhe", args=[tarefa.locacao_id]), secure=True)
        self.assertContains(resposta, "Regularizar entrega encerrada administrativamente")
        html = resposta.content.decode()
        self.assertLess(html.index("Entrega e recolhimento"), html.index("regularizacaoResultado"))
        self.assertFalse(list(resposta.context["messages"]))
        self.assertEqual(resposta.context["regularizacao_tarefa"].pk, tarefa.pk)
        from django.template.loader import render_to_string
        vendas = render_to_string("estoque/vendas_layout_teste.html", {"regularizacao_tarefa": tarefa})
        self.assertNotIn("Regularizar entrega encerrada administrativamente", vendas)

    def test_formulario_posterior_oculto_para_historicos_incompativeis(self):
        for cenario in ("pendente", "saida", "cancelada", "entregue", "evento_entrega",
                        "evento_checklist", "confirmado_em", "recolhimento_encerrado"):
            with self.subTest(cenario=cenario):
                tarefa = self.tarefa()
                if cenario != "pendente":
                    tarefa.resolver_administrativamente("Encerramento antigo")
                status = {"saida": Locacao.STATUS_SAIU_PARA_ENTREGA,
                          "cancelada": Locacao.STATUS_CANCELADA, "entregue": Locacao.STATUS_ENTREGUE}
                if cenario in status:
                    Locacao.objects.filter(pk=tarefa.locacao_id).update(status=status[cenario])
                if cenario in {"evento_entrega", "evento_checklist"}:
                    tarefa.locacao.eventos.create(
                        tipo="entregue" if cenario == "evento_entrega" else "checklist_entrega_confirmada",
                        descricao="Confirmacao anterior ficticia",
                    )
                if cenario == "confirmado_em":
                    TarefaOperacionalLocacao.objects.filter(pk=tarefa.pk).update(confirmado_em=timezone.now())
                if cenario == "recolhimento_encerrado":
                    recolhimento = obter_ou_criar_tarefa_operacional(tarefa.locacao, "recolhimento")
                    recolhimento.resolver_administrativamente("Encerrado")
                resposta = self.client.get(reverse("locacoes:detalhe", args=[tarefa.locacao_id]), secure=True)
                self.assertNotContains(resposta, "Regularizar entrega encerrada administrativamente")
                self.assertIsNone(resposta.context["regularizacao_tarefa"])

    def test_interface_oferece_tres_resultados_para_entrega(self):
        from django.template.loader import render_to_string
        self.tarefa()
        html = render_to_string("estoque/vendas_layout_teste.html", {"locacoes_operacionais": self.painel()})
        for resultado in ("entrega_equipe", "retirada_cliente", "tarefa_engano"):
            self.assertIn(f'value="{resultado}"', html)
        self.assertIn('name="data_efetiva"', html)
        self.assertIn('name="funcionario_registro"', html)


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

class OperacoesDiretasLocacaoTests(TestCase):
    setUp_base = ResolucaoAdministrativaTests.setUp
    dados_base = ResolucaoAdministrativaTests.dados_base
    criar_locacao = ResolucaoAdministrativaTests.criar_locacao
    colocar_na_rua = ResolucaoAdministrativaTests.colocar_na_rua
    tarefa = ResolucaoAdministrativaTests.tarefa
    painel = ResolucaoAdministrativaTests.painel
    dados_conferencia_entrega = fixtures.LocacoesChecklistOperacionalTests.dados_conferencia_entrega
    dados_conferencia_recolhimento = fixtures.LocacoesChecklistOperacionalTests.dados_conferencia_recolhimento

    def setUp(self):
        from estoque.models import Funcionario
        self.setUp_base()
        self.conferente = Funcionario.objects.create(nome="Roseli", ativo=True, pode_operar_sistema=True)
        self.transportador = self.entregador_checklist
        self.inativo = Funcionario.objects.create(nome="Inativo", ativo=False, pode_operar_sistema=True)

    def identificacao(self, modalidade, **extras):
        dados = {"modalidade": modalidade, "data_efetiva": self.hoje.isoformat(),
                 "funcionario_conferente": str(self.conferente.pk),
                 "transportador": str(self.transportador.pk) if modalidade.endswith("_equipe") else ""}
        dados.update(extras)
        return dados

    def entrega(self, modalidade="retirada_cliente", **extras):
        tarefa = self.tarefa()
        dados = {**self.dados_conferencia_entrega(), **self.identificacao(modalidade), **extras}
        url = reverse("locacoes:conferencia_entrega", args=[tarefa.pk]) + "?embutido=1"
        return tarefa, self.client.post(url, dados, secure=True)

    def recolher(self, tarefa, modalidade="devolucao_cliente", **extras):
        dados = {**self.dados_conferencia_recolhimento(), **self.identificacao(modalidade), **extras}
        url = reverse("locacoes:conferencia_recolhimento", args=[tarefa.pk]) + "?embutido=1"
        return self.client.post(url, dados, secure=True)

    def test_cliente_retira_no_deposito_conferente_fica_identificado(self):
        tarefa, resposta = self.entrega()
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        conferencia = tarefa.locacao.conferencias_entrega.get()
        self.assertEqual(conferencia.responsavel, "Roseli")
        self.assertIn("Cliente, no deposito", conferencia.observacao)
        self.assertIn("Conferiu/registrou: Roseli", conferencia.observacao)
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_ENTREGUE)

    def test_equipe_transportador_diferente_do_conferente(self):
        tarefa, resposta = self.entrega("entrega_equipe")
        self.assertTrue(resposta.json()["ok"])
        conferencia = tarefa.locacao.conferencias_entrega.get()
        self.assertEqual(conferencia.responsavel, self.conferente.nome)
        self.assertIn("Transportou: " + self.transportador.nome, conferencia.observacao)

    def test_cliente_devolve_conferencia_real_preserva_financeiro(self):
        tarefa = self.tarefa("recolhimento")
        tarefa.locacao.registrar_pagamento("1.00", PagamentoLocacao.FORMA_PIX)
        antes = list(MovimentoFinanceiro.objects.values())
        pagamentos = list(PagamentoLocacao.objects.values())
        resposta = self.recolher(tarefa)
        self.assertTrue(resposta.json()["ok"])
        conferencia = tarefa.locacao.conferencias_recolhimento.get()
        self.assertEqual(conferencia.responsavel, self.conferente.nome)
        self.assertEqual((conferencia.boa_mesas, conferencia.boa_cadeiras), (1, 4))
        self.assertIn("Cliente, no deposito", conferencia.observacao)
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_DEVOLVIDA)
        self.assertEqual(list(MovimentoFinanceiro.objects.values()), antes)
        self.assertEqual(list(PagamentoLocacao.objects.values()), pagamentos)
        self.assertEqual(Locacao.material_pendente_na_rua(tarefa.locacao), {"mesas": 0, "cadeiras": 0})

    def test_recolhimento_sem_quantidades_nao_libera_materiais(self):
        tarefa = self.tarefa("recolhimento")
        antes = Locacao.disponibilidade_periodo(self.hoje, self.hoje)
        resposta = self.recolher(tarefa, boa_mesas="0", boa_cadeiras="0")
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.context["form"].errors)
        self.assertFalse(tarefa.locacao.conferencias_recolhimento.exists())
        self.assertEqual(Locacao.disponibilidade_periodo(self.hoje, self.hoje), antes)

    def test_avaria_exige_observacao_real_sem_metadados_mascararem_validacao(self):
        tarefa = self.tarefa("recolhimento")
        resposta = self.recolher(tarefa, boa_mesas="0", quebrada_mesas="1", observacao="")
        self.assertIn("observacao", resposta.context["form"].errors)
        self.assertFalse(tarefa.locacao.conferencias_recolhimento.exists())
        resposta = self.recolher(tarefa, boa_mesas="0", quebrada_mesas="1", observacao="Mesa com avaria conferida")
        self.assertTrue(resposta.json()["ok"])
        conferencia = tarefa.locacao.conferencias_recolhimento.get()
        self.assertEqual(conferencia.quebrada_mesas, 1)

    def test_recolhimento_parcial_preserva_pendencia(self):
        tarefa = self.tarefa("recolhimento")
        resposta = self.recolher(
            tarefa, boa_mesas="1", boa_cadeiras="2", justificativa_parcial="Duas cadeiras ainda no cliente",
            previsao_conclusao=(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertTrue(resposta.json()["parcial"])
        tarefa.refresh_from_db()
        tarefa.locacao.refresh_from_db()
        self.assertEqual(tarefa.status, "parcial")
        self.assertEqual(tarefa.locacao.status, Locacao.STATUS_PENDENTE_DEVOLUCAO)
        self.assertEqual(Locacao.material_pendente_na_rua(tarefa.locacao)["cadeiras"], 2)
        self.assertEqual(Locacao.disponibilidade_periodo(self.hoje, self.hoje)["disponivel_cadeiras"], 118)

    def test_funcionario_inativo_e_falta_de_transportador_impedem_salvar(self):
        for extras in ({"funcionario_conferente": str(self.inativo.pk)}, {"transportador": ""},
                       {"data_efetiva": (self.hoje + timedelta(days=1)).isoformat()}):
            with self.subTest(extras=extras):
                tarefa, resposta = self.entrega("entrega_equipe", **extras)
                self.assertTrue(resposta.context["form"].errors)
                self.assertFalse(tarefa.locacao.conferencias_entrega.exists())
                tarefa.locacao.refresh_from_db()
                self.assertEqual(tarefa.locacao.status, Locacao.STATUS_RESERVADA)

    def test_regularizacao_seleciona_registrador_e_audita_transporte_sem_checklist(self):
        tarefa = self.tarefa()
        resposta = self.client.post(reverse("locacoes:resolver_tarefa_operacional", args=[tarefa.pk]), {
            "resultado": "retirada_cliente", "motivo": "Registro esquecido", "data_efetiva": self.hoje.isoformat(),
            "funcionario_registro": str(self.conferente.pk),
        }, secure=True)
        self.assertTrue(resposta.json()["ok"])
        evento = tarefa.locacao.eventos.get(tipo="transporte_regularizado")
        self.assertIn("Cliente, no deposito", evento.descricao)
        self.assertEqual(evento.responsavel, "Roseli")
        self.assertFalse(tarefa.locacao.conferencias_entrega.exists())

    @override_settings(X_FRAME_OPTIONS="DENY")
    def test_checklists_embutidos_permitidos_somente_na_mesma_origem(self):
        for tipo in ("entrega", "recolhimento"):
            with self.subTest(tipo=tipo):
                tarefa = self.tarefa(tipo)
                url = reverse("locacoes:conferencia_" + tipo, args=[tarefa.pk]) + "?embutido=1"
                resposta = self.client.get(url, secure=True)
                self.assertEqual(resposta["X-Frame-Options"], "SAMEORIGIN")
                self.assertContains(resposta, "funcionario_conferente")
                self.assertContains(resposta, "locacao-conferida")

    def test_indicadores_consulta_e_ordenacao_por_atraso(self):
        from .services import indicador_operacional_locacao
        antiga = self.tarefa(data_entrega=self.hoje - timedelta(days=1))
        self.assertTrue(indicador_operacional_locacao(antiga.locacao)["atraso"])
        hoje = self.tarefa(horario_entrega=time(23, 59))
        futura = self.tarefa(data_entrega=self.hoje + timedelta(days=1))
        itens = self.painel()["itens"]
        ids = [item["tarefa"].pk for item in itens]
        self.assertLess(ids.index(antiga.pk), ids.index(hoje.pk))
        self.assertLess(ids.index(hoje.pk), ids.index(futura.pk))
        recolhimento = self.tarefa("recolhimento", data_prevista_devolucao=self.hoje + timedelta(days=2))
        self.assertEqual(indicador_operacional_locacao(recolhimento.locacao)["texto"], "Aguardando recolhimento")
        resposta = self.client.get(reverse("locacoes:lista"), {"data_inicio": (self.hoje - timedelta(days=2)).isoformat()}, secure=True)
        self.assertContains(resposta, "Atraso na entrega")
        self.recolher(recolhimento)
        recolhimento.locacao.refresh_from_db()
        self.assertEqual(indicador_operacional_locacao(recolhimento.locacao)["texto"], "Devolucao concluida")

    def test_recolhimento_do_painel_prepara_checklist_sem_alterar_materiais(self):
        tarefa = self.tarefa("recolhimento")
        antes = Locacao.disponibilidade_periodo(self.hoje, self.hoje)
        resposta = self.client.get(reverse("locacoes:conferencia_recolhimento", args=[tarefa.pk]),
            {"embutido": "1", **self.identificacao("devolucao_cliente"), "observacao": "Cliente trouxe materiais"}, secure=True)
        self.assertEqual(resposta.context["form"].initial["modalidade"], "devolucao_cliente")
        self.assertEqual(resposta.context["form"].initial["funcionario_conferente"], str(self.conferente.pk))
        self.assertFalse(tarefa.locacao.conferencias_recolhimento.exists())
        self.assertEqual(Locacao.disponibilidade_periodo(self.hoje, self.hoje), antes)
        from django.template.loader import render_to_string
        html = render_to_string("estoque/vendas_layout_teste.html", {"locacoes_operacionais": self.painel()})
        self.assertIn('value="devolucao_cliente"', html)
        self.assertIn('value="recolhimento_equipe"', html)
        self.assertIn('data-checklist-frame', html)
