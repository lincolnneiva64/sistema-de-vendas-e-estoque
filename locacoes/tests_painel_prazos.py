from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from . import tests_resolucao_administrativa as fixtures
from .services import prazo_operacional_label


class PainelTodasDatasTests(TestCase):
    setUp = fixtures.ResolucaoAdministrativaTests.setUp
    dados_base = fixtures.ResolucaoAdministrativaTests.dados_base
    criar_locacao = fixtures.ResolucaoAdministrativaTests.criar_locacao
    colocar_na_rua = fixtures.ResolucaoAdministrativaTests.colocar_na_rua
    tarefa = fixtures.ResolucaoAdministrativaTests.tarefa
    painel = fixtures.ResolucaoAdministrativaTests.painel

    def test_todas_datas_tipos_contadores_e_estados_terminais(self):
        tarefas = []
        for dias in (2, 24, 60):
            for tipo in ("entrega", "recolhimento"):
                tarefas.append(self.tarefa(tipo, data_entrega=self.hoje + timedelta(days=dias),
                    data_prevista_devolucao=self.hoje + timedelta(days=dias + 1)))
        encerrada = self.tarefa("entrega", data_entrega=self.hoje + timedelta(days=3))
        type(encerrada).objects.filter(pk=encerrada.pk).update(status="confirmada_sem_checklist")
        painel = self.painel()
        self.assertEqual({i["tarefa"].pk for i in painel["itens"]}, {t.pk for t in tarefas})
        self.assertEqual(painel["total_entregas"], 3)
        self.assertEqual(painel["total_recolhimentos"], 3)
        self.assertEqual(painel["total"], 6)
        datas = [i["tarefa"].data_agendada for i in painel["itens"]]
        self.assertEqual(datas, sorted(datas))
        self.assertEqual([i["tarefa"].pk for i in self.painel()["itens"]],
                         [i["tarefa"].pk for i in painel["itens"]])

    def test_prazos_minutos_horas_dias_e_sem_horario(self):
        for minutos, esperado in [(330,"5h30min"), (45,"45min"), (-20,"Atrasada 20min"),
                                  (-180,"Atrasada 3h"), (1440,"1d"), (0,"Agora")]:
            with self.subTest(minutos=minutos):
                self.assertEqual(prazo_operacional_label(100000000 + minutos*60000,100000000,
                    tarefa_data=self.hoje,hoje=self.hoje), esperado)
        self.assertEqual(prazo_operacional_label(None,0,tarefa_data=self.hoje,hoje=self.hoje),
                         "Sem horario definido")

    @override_settings(TIME_ZONE="America/Belem")
    def test_timestamp_usa_fuso_do_projeto_na_virada_do_dia(self):
        from datetime import datetime, time
        with timezone.override("America/Belem"):
            tarefa = self.tarefa("entrega", data_entrega=self.hoje + timedelta(days=1),
                                 horario_entrega=time(0,15))
            agora = timezone.make_aware(datetime.combine(self.hoje,time(23,30)))
            from .services import painel_operacional_rapido_locacoes
            item = next(i for i in painel_operacional_rapido_locacoes(
                self.factory.get("/"),self.hoje,agora)["itens"] if i["tarefa"].pk==tarefa.pk)
            self.assertEqual(item["prazo_label"],"45min")
            self.assertEqual(item["horario_lateral"],
                timezone.make_aware(datetime.combine(self.hoje+timedelta(days=1),time(0,15))).timestamp()*1000)

    def test_entrega_parcial_reagendada_nao_duplica(self):
        tarefa = self.tarefa("entrega",data_entrega=self.hoje + timedelta(days=4),
                             data_prevista_devolucao=self.hoje + timedelta(days=5))
        type(tarefa).objects.filter(pk=tarefa.pk).update(
            status="parcial", data_agendada=self.hoje-timedelta(days=1))
        itens = self.painel()["itens"]
        self.assertEqual([i["tarefa"].pk for i in itens].count(tarefa.pk),1)

    def test_contadores_com_antigas_futuras_e_encerradas(self):
        antigas = self.tarefa("entrega",data_entrega=self.hoje-timedelta(days=4))
        futura = self.tarefa("entrega",data_entrega=self.hoje+timedelta(days=20),
                             data_prevista_devolucao=self.hoje+timedelta(days=21))
        recolhimento = self.tarefa("recolhimento",data_entrega=self.hoje-timedelta(days=1),
                                   data_prevista_devolucao=self.hoje+timedelta(days=3))
        for tipo in ("entrega", "recolhimento"):
            for status in ("confirmada", "resolvida_admin", "confirmada_sem_checklist"):
                encerrada = self.tarefa(tipo,data_entrega=self.hoje+timedelta(days=10),
                    data_prevista_devolucao=self.hoje+timedelta(days=11))
                type(encerrada).objects.filter(pk=encerrada.pk).update(status=status)
        painel = self.painel()
        self.assertEqual({i["tarefa"].pk for i in painel["itens"]},
                         {antigas.pk, futura.pk, recolhimento.pk})
        self.assertEqual(painel["total_pendencias"],3)
        self.assertEqual(painel["total"],2)
        self.assertEqual(painel["total_entregas"],2)
        self.assertEqual(painel["total_recolhimentos"],1)
        self.assertEqual(painel["total_entregas_antigas"],1)
