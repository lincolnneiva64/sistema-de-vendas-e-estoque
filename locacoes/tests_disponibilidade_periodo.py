from datetime import date, datetime, time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Locacao
from . import tests as fixtures
from .views import _avaliar_disponibilidade_dinamica


class DisponibilidadePeriodoTests(TestCase):
    setUp = fixtures.LocacoesReservasTests.setUp
    dados_base = fixtures.LocacoesReservasTests.dados_base
    itens = fixtures.LocacoesReservasTests.itens

    def setUp(self):
        fixtures.LocacoesReservasTests.setUp(self)
        agora = timezone.make_aware(datetime(2030, 10, 17, 9))
        self.relogio = patch("django.utils.timezone.now", return_value=agora)
        self.relogio.start()
        self.addCleanup(self.relogio.stop)

    def reserva(self, dia=17, hora=10, devolucao=18, retorno=10, jogos=5):
        return Locacao.criar_reserva(self.dados_base(
            data_entrega=date(2030, 10, dia), horario_entrega=time(hora),
            data_evento=date(2030, 10, dia),
            data_prevista_devolucao=date(2030, 10, devolucao),
            horario_previsto_devolucao=time(retorno)), self.itens(jogos=jogos))

    def disponivel(self, dia=18, hora=11, fim=19, retorno=10):
        return Locacao.disponibilidade_periodo(date(2030, 10, dia), date(2030, 10, fim),
            horario_entrega=time(hora), horario_previsto_devolucao=time(retorno))

    def test_futura_reutiliza_material_com_devolucao_anterior(self):
        self.reserva(jogos=10)
        self.assertEqual(self.disponivel()["disponivel_mesas"], 10)
        self.reserva(dia=18, hora=11, devolucao=19, jogos=10)

    def test_horario_conflitante_bloqueia_e_informa_quantidade(self):
        self.reserva(jogos=8, retorno=12)
        with self.assertRaisesMessage(ValidationError, "2 disponiveis"):
            self.reserva(dia=18, hora=11, devolucao=19, jogos=3)

    def test_limite_exato_devolucao_libera_antes_da_entrega(self):
        self.reserva(jogos=10, retorno=11)
        self.assertEqual(self.disponivel()["disponivel_cadeiras"], 40)

    def test_pico_simultaneo_nao_soma_reservas_sequenciais(self):
        self.reserva(dia=18, hora=8, devolucao=18, retorno=10, jogos=6)
        self.reserva(dia=18, hora=12, devolucao=18, retorno=14, jogos=6)
        self.assertEqual(self.disponivel(dia=18, hora=7, fim=18, retorno=16)["disponivel_mesas"], 4)
        with self.assertRaises(ValidationError):
            self.reserva(dia=18, hora=7, devolucao=18, retorno=16, jogos=5)

    def test_na_rua_previsao_futura_e_atraso_sem_confirmacao(self):
        locacao = self.reserva(jogos=10)
        locacao.marcar_saiu_para_entrega()
        self.assertEqual(self.disponivel()["disponivel_mesas"], 10)
        with patch("django.utils.timezone.now", return_value=timezone.make_aware(datetime(2030, 10, 18, 10, 1))):
            self.assertEqual(self.disponivel()["disponivel_mesas"], 0)
            with self.assertRaises(ValidationError):
                self.reserva(dia=18, hora=11, devolucao=19, jogos=1)

    def test_sem_horario_nao_libera_no_meio_do_dia(self):
        locacao = self.reserva(jogos=10)
        Locacao.objects.filter(pk=locacao.pk).update(horario_previsto_devolucao=None)
        self.assertEqual(self.disponivel()["disponivel_mesas"], 0)

    def test_avulsos_e_jogos_disputam_mesmo_patrimonio(self):
        self.reserva(jogos=8, retorno=12)
        dados = self.dados_base(data_entrega=date(2030, 10, 18), horario_entrega=time(11),
            data_prevista_devolucao=date(2030, 10, 19))
        with self.assertRaises(ValidationError):
            Locacao.criar_reserva(dados, self.itens(jogos=2, cadeiras=1))

    def test_edicao_exclui_propria_reserva_e_revalida(self):
        self.reserva(jogos=8, retorno=12)
        locacao = self.reserva(dia=18, hora=13, devolucao=19, jogos=5)
        dados = self.dados_base(data_entrega=date(2030, 10, 18), horario_entrega=time(13),
            data_prevista_devolucao=date(2030, 10, 19), horario_previsto_devolucao=time(9))
        locacao.atualizar_reserva(dados, self.itens(jogos=10))
        locacao.refresh_from_db()
        self.assertEqual(locacao.horario_previsto_devolucao, time(9))
        dados["horario_entrega"] = time(11)
        with self.assertRaises(ValidationError):
            locacao.atualizar_reserva(dados, self.itens(jogos=10))

    def test_dinamica_recalcula_horario_e_quantidade(self):
        self.reserva(jogos=8, retorno=12)
        for hora, quantidade, esperado in [(11, 3, "indisponivel"), (13, 3, "disponivel"), (13, 11, "indisponivel")]:
            resultado = _avaliar_disponibilidade_dinamica(date(2030, 10, 18), date(2030, 10, 19),
                jogos=quantidade, horario_entrega=time(hora))
            self.assertEqual(resultado["status"], esperado)

    def test_intervalo_invertido_rejeitado(self):
        with self.assertRaisesMessage(ValidationError, "posterior"):
            self.reserva(dia=18, hora=11, devolucao=18, retorno=10)

    def test_cenario_75_mesas_303_cadeiras_permite_15_jogos_futuros(self):
        self.configuracao.total_mesas = 75
        self.configuracao.total_cadeiras = 303
        type(self.configuracao).objects.filter(pk=self.configuracao.pk).update(total_mesas=75, total_cadeiras=303)
        locacao = self.reserva(jogos=65)
        locacao.marcar_saiu_para_entrega()
        agora = self.disponivel(dia=17, hora=10, fim=17, retorno=23)
        self.assertEqual((agora["disponivel_mesas"], agora["disponivel_cadeiras"]), (10, 43))
        self.reserva(dia=18, hora=11, devolucao=19, jogos=15)

    def test_datas_invertidas_e_horarios_invalidos_na_previa(self):
        for inicio, fim, hora in [(19, 18, "11:00"), (18, 19, "invalido")]:
            resultado = _avaliar_disponibilidade_dinamica(date(2030, 10, inicio), date(2030, 10, fim),
                                                        jogos=1, horario_entrega=hora)
            self.assertEqual(resultado["status"], "incompleto")
