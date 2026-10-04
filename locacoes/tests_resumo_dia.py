from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import tests as fixtures
from .models import EventoLocacao, Locacao, TarefaOperacionalLocacao
from .services import historico_operacional_do_dia, obter_ou_criar_tarefa_operacional


class ResumoOperacionalDiaTests(TestCase):
    dados_base = fixtures.LocacoesChecklistOperacionalTests.dados_base
    criar_locacao = fixtures.LocacoesChecklistOperacionalTests.criar_locacao
    colocar_na_rua = fixtures.LocacoesChecklistOperacionalTests.colocar_na_rua

    def setUp(self):
        fixtures.LocacoesChecklistOperacionalTests.setUp(self)
        self.hoje = date(2026, 10, 4)

    def instante(self, dia=None, hora=time(10, 30)):
        return timezone.make_aware(datetime.combine(dia or self.hoje, hora))

    def consultar(self, dia=None):
        return self.client.get(reverse("locacoes:checklist_operacional"),
                               {"data": (dia or self.hoje).isoformat()}, secure=True)

    def concluir(self, tipo="entrega", dia=None):
        dia = dia or self.hoje
        locacao = self.criar_locacao(
            pessoa_avulsa_nome="Cliente concluído",
            data_entrega=dia if tipo == "entrega" else dia - timedelta(days=1),
            data_prevista_devolucao=dia + timedelta(days=1) if tipo == "entrega" else dia,
        )
        with patch("django.utils.timezone.now", return_value=self.instante(dia)):
            if tipo == "recolhimento":
                self.colocar_na_rua(locacao)
            tarefa = obter_ou_criar_tarefa_operacional(locacao, tipo)
            tarefa.confirmar(responsavel="Lincoln")
        return tarefa

    def test_entrega_concluida_sem_pendencias_tem_historico(self):
        tarefa = self.concluir()
        response = self.consultar()
        self.assertEqual(response.context["resumo_dia"]["entregas_concluidas"], 1)
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 0)
        self.assertEqual(response.context["historico_dia"]["fisicas"][0]["locacao"].pk, tarefa.locacao_id)
        for texto in ("Histórico do dia", "Cliente concluído", "Entregue", "10:30", "1 jogo"):
            self.assertContains(response, texto)

    def test_sem_recolhimentos_tem_texto_especifico(self):
        self.assertContains(self.consultar(), "Nenhum recolhimento pendente.")

    def test_recolhimento_concluido_tem_historico(self):
        tarefa = self.concluir("recolhimento")
        response = self.consultar()
        self.assertEqual(response.context["resumo_dia"]["recolhimentos_concluidos"], 1)
        self.assertTrue(any(i["locacao"].pk == tarefa.locacao_id and i["tipo"] == "recolhimento"
                            for i in response.context["historico_dia"]["fisicas"]))
        self.assertContains(response, "Recolhido")

    def test_pendencia_atual_preserva_cartao_e_acoes(self):
        locacao = self.criar_locacao(pessoa_avulsa_nome="Entrega pendente")
        response = self.consultar()
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 1)
        self.assertEqual(response.context["checklist"]["grupos"]["entregas"][0]["locacao"].pk, locacao.pk)
        self.assertContains(response, "Entrega pendente")
        self.assertContains(response, "data-envio-operacional")

    def test_devolucao_atrasada_permanece(self):
        locacao = self.colocar_na_rua(self.criar_locacao(
            data_entrega=self.hoje - timedelta(days=3),
            data_prevista_devolucao=self.hoje - timedelta(days=1),
        ))
        response = self.consultar()
        self.assertEqual(response.context["resumo_dia"]["devolucoes_atrasadas"], 1)
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 1)
        self.assertEqual(response.context["checklist"]["grupos"]["devolucoes_atrasadas"][0]["locacao"].pk, locacao.pk)

    def test_resolvida_admin_separada_de_conclusoes_fisicas(self):
        tarefa = obter_ou_criar_tarefa_operacional(self.criar_locacao(), "entrega")
        with patch("django.utils.timezone.now", return_value=self.instante()):
            tarefa.resolver_administrativamente("Duplicidade conferida", "Roseli")
        response = self.consultar()
        self.assertEqual(response.context["historico_dia"]["fisicas"], [])
        self.assertEqual(response.context["resumo_dia"]["entregas_concluidas"], 0)
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 0)
        for texto in ("Resoluções administrativas", "Resolvida administrativamente", "Duplicidade conferida", "Roseli"):
            self.assertContains(response, texto)

    def test_dia_sem_movimento_tem_mensagem_clara(self):
        self.assertContains(self.consultar(), "Não há operações registradas nem pendências para este dia.")

    def test_historico_respeita_data_da_confirmacao_e_fuso(self):
        tarefa = self.concluir()
        ontem = self.hoje - timedelta(days=1)
        TarefaOperacionalLocacao.objects.filter(pk=tarefa.pk).update(
            data_agendada=ontem, confirmado_em=self.instante(hora=time(23, 30)),
        )
        self.assertEqual(self.consultar().context["resumo_dia"]["entregas_concluidas"], 1)
        self.assertEqual(self.consultar(ontem).context["historico_dia"]["fisicas"], [])
        self.assertEqual(self.consultar(self.hoje + timedelta(days=1)).context["historico_dia"]["fisicas"], [])

    def test_dia_futuro_preserva_prevista_sem_historico(self):
        amanha = self.hoje + timedelta(days=1)
        self.criar_locacao(data_entrega=amanha)
        response = self.consultar(amanha)
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 1)
        self.assertEqual(response.context["historico_dia"]["fisicas"], [])

    def test_historico_passado_com_pendencia_do_dia(self):
        ontem = self.hoje - timedelta(days=1)
        self.concluir(dia=ontem)
        self.criar_locacao(data_entrega=ontem)
        response = self.consultar(ontem)
        self.assertEqual(response.context["resumo_dia"]["entregas_concluidas"], 1)
        self.assertEqual(response.context["resumo_dia"]["pendencias"], 1)

    def test_entrega_antiga_por_evento_sem_duplicar_confirmacao(self):
        with patch("django.utils.timezone.now", return_value=self.instante()):
            self.colocar_na_rua(self.criar_locacao())
        self.concluir()
        self.assertEqual(self.consultar().context["resumo_dia"]["entregas_concluidas"], 2)

    def test_devolucao_antiga_completa_por_evento(self):
        locacao = self.colocar_na_rua(self.criar_locacao(data_entrega=self.hoje - timedelta(days=1)))
        with patch("django.utils.timezone.now", return_value=self.instante()):
            locacao.registrar_devolucao({i.pk: {"devolvida_boa": i.quantidade} for i in locacao.itens.all()})
        self.assertEqual(self.consultar().context["resumo_dia"]["recolhimentos_concluidos"], 1)

    def test_devolucao_parcial_antiga_nao_conta_como_conclusao(self):
        locacao = self.colocar_na_rua(self.criar_locacao(jogos=2))
        with patch("django.utils.timezone.now", return_value=self.instante()):
            locacao.registrar_devolucao({i.pk: {"devolvida_boa": 1} for i in locacao.itens.all()})
        self.assertEqual(self.consultar().context["resumo_dia"]["recolhimentos_concluidos"], 0)

    def test_consulta_historico_nao_altera_registros(self):
        self.concluir()
        antes = [list(model.objects.values()) for model in (Locacao, TarefaOperacionalLocacao, EventoLocacao)]
        historico_operacional_do_dia(self.hoje)
        self.assertEqual(antes, [list(model.objects.values()) for model in (Locacao, TarefaOperacionalLocacao, EventoLocacao)])
