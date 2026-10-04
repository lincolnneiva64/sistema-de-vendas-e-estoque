from datetime import date, time
from unittest.mock import patch

from django.test import TestCase

from .models import ItemLocacao, Locacao
from .services import obter_ou_criar_tarefa_operacional
from . import tests as fixtures
from .views import _avaliar_disponibilidade_dinamica


class QuadroDisponibilidadeTests(TestCase):
    setUp = fixtures.LocacoesOperacaoTests.setUp
    dados_base = fixtures.LocacoesOperacaoTests.dados_base
    criar_locacao = fixtures.LocacoesOperacaoTests.criar_locacao
    criar_locacao_com_datas = fixtures.LocacoesOperacaoTests.criar_locacao_com_datas
    hoje = date(2026, 9, 1)

    def quadro(self, fim=None):
        with patch("locacoes.models.timezone.localdate", return_value=self.hoje):
            return Locacao.quadro_disponibilidade(self.hoje, data_prevista_devolucao=fim)

    def test_reserva_de_hoje_reduz_disponiveis(self):
        self.criar_locacao(jogos=2)
        self.assertEqual(self.quadro()["estoque_agora"], {"jogos": 3, "mesas": 3, "cadeiras": 12})

    def test_entrega_de_hoje_e_horario(self):
        locacao = self.criar_locacao(jogos=2)
        Locacao.objects.filter(pk=locacao.pk).update(horario_entrega=time(10, 30))
        self.assertEqual(self.quadro()["entregar_hoje"], {
            "jogos": 2, "mesas": 2, "cadeiras": 8,
            "quantidade_entregas": 1, "proxima_entrega": "10:30",
        })

    def test_varias_entregas_somam_componentes_e_proxima(self):
        primeira = self.criar_locacao(jogos=1, cadeiras=2)
        segunda = self.criar_locacao(jogos=2, mesas=1)
        Locacao.objects.filter(pk=primeira.pk).update(horario_entrega=time(14))
        Locacao.objects.filter(pk=segunda.pk).update(horario_entrega=time(10, 30))
        entrega = self.quadro()["entregar_hoje"]
        self.assertEqual(entrega, {"jogos": 3, "mesas": 4, "cadeiras": 14,
                                  "quantidade_entregas": 2, "proxima_entrega": "10:30"})

    def test_recolhimento_atrasado_separado_sem_liberacao(self):
        locacao = self.criar_locacao(jogos=2)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()
        self.hoje = date(2026, 9, 5)
        quadro = self.quadro()
        self.assertEqual(quadro["estoque_agora"]["jogos"], 3)
        self.assertEqual(quadro["previsto_recolher"]["jogos"], 2)
        self.assertEqual(quadro["entregar_hoje"]["jogos"], 0)
        self.assertEqual(quadro["potencial_data"]["jogos"], 5)

    def test_cadeiras_avulsas_completam_jogos_na_projecao(self):
        locacao = self.criar_locacao(jogos=0, cadeiras=8)
        locacao.marcar_saiu_para_entrega()
        self.hoje = date(2026, 9, 2)
        quadro = self.quadro()
        self.assertEqual(quadro["estoque_agora"]["jogos"], 3)
        self.assertEqual(quadro["previsto_recolher"]["jogos"], 0)
        self.assertEqual(quadro["potencial_data"], {"jogos": 5, "mesas": 5, "cadeiras": 20})

    def test_resolucao_admin_nao_libera_material(self):
        locacao = self.criar_locacao(jogos=2)
        locacao.marcar_saiu_para_entrega()
        locacao.confirmar_entrega()
        self.hoje = date(2026, 9, 5)
        antes = self.quadro()
        tarefa = obter_ou_criar_tarefa_operacional(locacao, "recolhimento")
        tarefa.resolver_administrativamente("Registro antigo", "Operador")
        self.assertEqual(self.quadro(), antes)
        self.assertFalse(locacao.conferencias_recolhimento.exists())

    def test_cabecalho_validacao_e_endpoint_mesmo_periodo(self):
        self.criar_locacao_com_datas(date(2026, 9, 3), date(2026, 9, 4), jogos=2)
        fim = date(2026, 9, 4)
        validacao = Locacao.validar_disponibilidade(self.hoje, fim, [
            {"tipo": ItemLocacao.TIPO_JOGO, "quantidade": 1},
        ])
        resultado = _avaliar_disponibilidade_dinamica(self.hoje, fim, jogos=1)
        for componente in ("mesas", "cadeiras"):
            self.assertEqual(resultado["quadro"]["estoque_agora"][componente], validacao["disponivel_" + componente])
            self.assertEqual(resultado["disponivel_" + componente], validacao["disponivel_" + componente])
        self.assertEqual(resultado["quadro"]["estoque_agora"]["jogos"], 3)

    def test_entregas_canceladas_e_outros_dias_nao_entram(self):
        self.criar_locacao().cancelar()
        self.criar_locacao_com_datas(date(2026, 9, 3), date(2026, 9, 4))
        self.assertEqual(self.quadro()["entregar_hoje"]["quantidade_entregas"], 0)

    def test_template_apresenta_rotulos_e_condicao(self):
        from django.template.loader import render_to_string
        html = render_to_string("locacoes/nova.html")
        for texto in ("Disponíveis agora", "A entregar hoje", "Total previsto livre",
                      "se retornarem em bom estado", "Próxima entrega:"):
            self.assertIn(texto, html)
