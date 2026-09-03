from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase
from django.urls import reverse

from .models import ContaPagar, ContaReceber, Fornecedor
from .services import sincronizacao_firebird as sync


def previa_fake(area="estoque"):
    return {
        "area": area,
        "titulo": sync.AREAS[area],
        "resumo": {
            "novos": 0,
            "alterados": 0,
            "sem_alteracao": 0,
            "fora_escopo": 0,
            "problemas": 0,
            "possiveis_fechamentos": 0,
        },
        "secoes": [],
        "etapas": [],
        "dados_aplicacao": {},
    }


class SincronizacaoFirebirdViewTests(TestCase):
    def setUp(self):
        self.url = reverse("estoque:sincronizacao_firebird")

    @patch("estoque.views.sincronizacao_aplicar_com_releitura")
    @patch("estoque.views.sincronizacao_gerar_previa")
    def test_get_nao_executa_previa_nem_aplicacao(self, gerar_previa, aplicar):
        resposta = self.client.get(self.url)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Atualizacao do sistema antigo")
        gerar_previa.assert_not_called()
        aplicar.assert_not_called()

    @patch("estoque.views.sincronizacao_aplicar_com_releitura")
    @patch("estoque.views.sincronizacao_gerar_previa")
    def test_previa_nao_aplica_banco(self, gerar_previa, aplicar):
        gerar_previa.return_value = previa_fake("estoque")

        resposta = self.client.post(self.url, {"acao": "preview", "area": "estoque"})

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Atualizar Estoque")
        gerar_previa.assert_called_once_with("estoque")
        aplicar.assert_not_called()

    @patch("estoque.views.sincronizacao_aplicar_com_releitura")
    def test_aplicacao_exige_post_confirmacao_e_token_valido(self, aplicar):
        resposta = self.client.post(self.url, {"acao": "aplicar", "area": "estoque"})

        self.assertEqual(resposta.status_code, 400)
        aplicar.assert_not_called()

    @patch("estoque.views.sincronizacao_aplicar_com_releitura")
    @patch("estoque.views.sincronizacao_gerar_previa")
    def test_aplicacao_confirmada_rele_e_aplica(self, gerar_previa, aplicar):
        gerar_previa.return_value = previa_fake("receber")
        aplicar.return_value = SimpleNamespace(titulo="Atualizar Contas a Receber")

        resposta_previa = self.client.post(self.url, {"acao": "preview", "area": "receber"})
        token = resposta_previa.context["preview_token"]
        resposta = self.client.post(
            self.url,
            {
                "acao": "aplicar",
                "area": "receber",
                "preview_token": token,
                "confirmacao": sync.CONFIRMACAO_TEXTO,
            },
        )

        self.assertEqual(resposta.status_code, 302)
        aplicar.assert_called_once_with("receber")

    @patch("estoque.views.sincronizacao_aplicar_com_releitura")
    @patch("estoque.views.sincronizacao_gerar_previa")
    def test_duplo_envio_nao_reaplica_token_consumido(self, gerar_previa, aplicar):
        gerar_previa.return_value = previa_fake("pagar")
        aplicar.return_value = SimpleNamespace(titulo="Atualizar Contas a Pagar")

        resposta_previa = self.client.post(self.url, {"acao": "preview", "area": "pagar"})
        token = resposta_previa.context["preview_token"]
        payload = {
            "acao": "aplicar",
            "area": "pagar",
            "preview_token": token,
            "confirmacao": sync.CONFIRMACAO_TEXTO,
        }

        primeira = self.client.post(self.url, payload)
        segunda = self.client.post(self.url, payload)

        self.assertEqual(primeira.status_code, 302)
        self.assertEqual(segunda.status_code, 409)
        aplicar.assert_called_once_with("pagar")

    def test_template_e_home_expoem_rota_sem_regressao_evidente(self):
        resposta_home = self.client.get(reverse("estoque:home"))
        resposta_sync = self.client.get(self.url)

        self.assertEqual(resposta_home.status_code, 200)
        self.assertContains(resposta_home, reverse("estoque:sincronizacao_firebird"))
        self.assertEqual(resposta_sync.status_code, 200)
        self.assertContains(resposta_sync, "Gerar previa")

    def test_cards_de_previa_tem_post_action_csrf_e_area(self):
        resposta = self.client.get(self.url)
        html = resposta.content.decode("utf-8")
        action = f'action="{self.url}"'

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(html.count('name="acao" value="preview"'), 4)
        self.assertEqual(html.count('method="post"'), 4)
        self.assertEqual(html.count(action), 4)
        self.assertEqual(html.count('name="csrfmiddlewaretoken"'), 4)
        for area in ("estoque", "receber", "pagar", "tudo"):
            self.assertIn(f'name="area" value="{area}"', html)

    def test_post_preview_das_quatro_areas_retorna_bloqueio_de_fonte_invalida(self):
        resultado = SimpleNamespace(returncode=0, stdout="824\n", stderr="")

        for area in ("estoque", "receber", "pagar", "tudo"):
            with self.subTest(area=area):
                with patch.object(sync.subprocess, "run", return_value=resultado):
                    resposta = self.client.post(self.url, {"acao": "preview", "area": area})

                self.assertEqual(resposta.status_code, 200)
                self.assertContains(
                    resposta,
                    "produtos ativos Firebird: 824; esperado: 748",
                )


class SincronizacaoFirebirdServiceTests(TestCase):
    def test_cada_area_de_previa_chama_somente_motor_correto(self):
        casos = {
            "estoque": ("_engine_estoque", "_engine_receber", "_engine_pagar"),
            "receber": ("_engine_receber", "_engine_estoque", "_engine_pagar"),
            "pagar": ("_engine_pagar", "_engine_estoque", "_engine_receber"),
        }

        for area, (correto, outro_a, outro_b) in casos.items():
            with self.subTest(area=area):
                motor = _motor_fake(area)
                with patch.object(sync, correto, return_value=motor) as patch_correto, \
                        patch.object(sync, outro_a) as patch_outro_a, \
                        patch.object(sync, outro_b) as patch_outro_b, \
                        patch.object(sync, "validar_fonte_firebird", return_value=748):
                    sync.gerar_previa(area)

                patch_correto.assert_called_once()
                patch_outro_a.assert_not_called()
                patch_outro_b.assert_not_called()

    def test_fonte_firebird_errada_bloqueia_as_quatro_previas(self):
        resultado = SimpleNamespace(returncode=0, stdout="824\n", stderr="")

        for area in ("estoque", "receber", "pagar", "tudo"):
            with self.subTest(area=area):
                with patch.object(sync.subprocess, "run", return_value=resultado), \
                        patch.object(sync, "_engine_estoque") as engine_estoque, \
                        patch.object(sync, "_engine_receber") as engine_receber, \
                        patch.object(sync, "_engine_pagar") as engine_pagar:
                    with self.assertRaisesMessage(ValueError, "produtos ativos Firebird: 824; esperado: 748"):
                        sync.gerar_previa(area)

                engine_estoque.assert_not_called()
                engine_receber.assert_not_called()
                engine_pagar.assert_not_called()

    def test_fonte_firebird_errada_bloqueia_aplicacao_antes_de_escrever(self):
        resultado = SimpleNamespace(returncode=0, stdout="824\n", stderr="")

        for area in ("estoque", "receber", "pagar", "tudo"):
            with self.subTest(area=area):
                with patch.object(sync.subprocess, "run", return_value=resultado), \
                        patch.object(sync, "_aplicar_previa") as aplicar:
                    with self.assertRaisesMessage(ValueError, "produtos ativos Firebird: 824; esperado: 748"):
                        sync.aplicar_com_releitura(area)

                aplicar.assert_not_called()

    def test_possiveis_fechamentos_receber_nao_sao_aplicados(self):
        motor = _motor_fake("receber")
        fechada = ContaReceber.objects.create(
            numero_legado="AR-FECHAR",
            data_emissao=date(2026, 1, 1),
            data_vencimento=date(2026, 1, 2),
            valor_original=Decimal("10.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaReceber.STATUS_ABERTA,
        )
        motor.comparar.return_value = ([], [], [], [fechada])

        with patch.object(sync, "_engine_receber", return_value=motor), \
                patch.object(sync, "validar_fonte_firebird", return_value=748):
            sync.aplicar_com_releitura("receber")

        args = motor.aplicar.call_args.args
        self.assertEqual(args[1], [])
        self.assertEqual(args[2], [])
        self.assertEqual(ContaReceber.objects.get(pk=fechada.pk).status, ContaReceber.STATUS_ABERTA)

    def test_possiveis_fechamentos_pagar_nao_sao_aplicados(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Legado")
        motor = _motor_fake("pagar")
        fechada = ContaPagar.objects.create(
            documento_legado="AP-FECHAR",
            fornecedor=fornecedor,
            data_emissao=date(2026, 1, 1),
            data_vencimento=date(2026, 1, 2),
            valor_original=Decimal("10.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaPagar.STATUS_ABERTA,
        )
        motor.carregar_django.return_value = ({fornecedor.id: fornecedor}, [], [], [], [fechada])

        with patch.object(sync, "_engine_pagar", return_value=motor), \
                patch.object(sync, "validar_fonte_firebird", return_value=748):
            sync.aplicar_com_releitura("pagar")

        args = motor.aplicar.call_args.args
        self.assertEqual(args[1], [])
        self.assertEqual(args[2], [])
        self.assertEqual(ContaPagar.objects.get(pk=fechada.pk).status, ContaPagar.STATUS_ABERTA)

    def test_falha_no_preflight_impede_escrita(self):
        motor = _motor_fake("estoque")
        motor.carregar_comparacao.side_effect = ValueError("preflight falhou")

        with patch.object(sync, "_engine_estoque", return_value=motor), \
                patch.object(sync, "validar_fonte_firebird", return_value=748):
            with self.assertRaises(ValueError):
                sync.aplicar_com_releitura("estoque")

        motor.aplicar.assert_not_called()

    def test_atualizar_tudo_nao_ignora_falha_de_etapa(self):
        preview = previa_fake("estoque")
        with patch.object(sync, "gerar_previa", side_effect=[preview, ValueError("AR falhou")]), \
                patch.object(sync, "_aplicar_previa") as aplicar:
            with self.assertRaises(ValueError):
                sync.aplicar_com_releitura("tudo")

        aplicar.assert_not_called()

    def test_atualizar_tudo_reverte_escrita_anterior_se_etapa_posterior_falhar(self):
        previews = [previa_fake("estoque"), previa_fake("receber"), previa_fake("pagar")]

        def aplicar_com_falha(preview, validar_preflight=True):
            if preview["area"] == "estoque":
                Fornecedor.objects.create(nome="Rollback Atualizar Tudo")
                return sync.ResultadoAplicacao(area="estoque", titulo="Atualizar Estoque", resumo=preview["resumo"])
            raise ValueError("etapa posterior falhou")

        with patch.object(sync, "gerar_previa", side_effect=previews), \
                patch.object(sync, "_validar_previa_antes_aplicar", return_value=None), \
                patch.object(sync, "_aplicar_previa", side_effect=aplicar_com_falha) as aplicar:
            with self.assertRaisesMessage(ValueError, "etapa posterior falhou"):
                sync.aplicar_com_releitura("tudo")

        self.assertEqual(aplicar.call_count, 2)
        self.assertFalse(Fornecedor.objects.filter(nome="Rollback Atualizar Tudo").exists())

    def test_cafe_santa_clara_bloqueia_ap_e_tudo_antes_de_aplicar_etapas(self):
        import atualizar_contas_pagar_firebird as motor_ap

        motor = _motor_fake("pagar")
        motor.validar_exclusoes_explicitamente_configuradas = motor_ap.validar_exclusoes_explicitamente_configuradas
        preview_pagar = previa_fake("pagar")
        preview_pagar["dados_aplicacao"] = {
            "contas": [],
            "novas": [],
            "alteradas": [],
            "ignoradas": _ignoradas_santa_clara_incompletas(),
        }

        with patch.object(sync, "gerar_previa", return_value=preview_pagar), \
                patch.object(sync, "_engine_pagar", return_value=motor):
            with self.assertRaisesMessage(ValueError, "5 contas / R$ 1.711,66"):
                sync.aplicar_com_releitura("pagar")

        motor.aplicar.assert_not_called()

        motor_tudo = _motor_fake("pagar")
        motor_tudo.validar_exclusoes_explicitamente_configuradas = (
            motor_ap.validar_exclusoes_explicitamente_configuradas
        )
        previews = [previa_fake("estoque"), previa_fake("receber"), preview_pagar]
        with patch.object(sync, "gerar_previa", side_effect=previews), \
                patch.object(sync, "_engine_pagar", return_value=motor_tudo), \
                patch.object(sync, "_aplicar_previa") as aplicar:
            with self.assertRaisesMessage(ValueError, "5 contas / R$ 1.711,66"):
                sync.aplicar_com_releitura("tudo")

        aplicar.assert_not_called()


class ContasPagarSantaClaraTests(TestCase):
    def test_cafe_santa_clara_permanece_excluido_da_sincronizacao_ap(self):
        import atualizar_contas_pagar_firebird as motor

        saida = (
            "27007-01/2-CA|00098|CAFE SANTA CLARA|2025-05-15|2025-05-30|322.45|0.00|322.45\n"
            "27008-02/2-CA|00098|CAFE SANTA CLARA|2025-05-16|2025-05-31|374.06|0.00|374.06\n"
            "27007-02/2-CA|00098|CAFE SANTA CLARA|2025-06-15|2025-06-30|322.44|0.00|322.44\n"
        )
        resultado = SimpleNamespace(returncode=0, stdout=saida, stderr="")

        with patch.object(motor.subprocess, "run", return_value=resultado):
            contas, ignoradas = motor.extrair_firebird("isql", "banco", "SYSDBA", "masterkey")

        self.assertEqual(contas, [])
        self.assertEqual(len(ignoradas), 3)
        self.assertEqual(motor.CONTAS_EXCLUIDAS_EXPLICITAMENTE_QTD_ESPERADA, 5)
        self.assertEqual(motor.CONTAS_EXCLUIDAS_EXPLICITAMENTE_TOTAL_ESPERADO, Decimal("1711.66"))
        self.assertEqual(
            {conta["documento"] for conta in ignoradas},
            {"27007-01/2-CA", "27008-02/2-CA", "27007-02/2-CA"},
        )
        self.assertTrue(all(
            conta["motivo_ignorada"] == "Exclusao explicita: Cafe Santa Clara removido da migracao"
            for conta in ignoradas
        ))
        self.assertEqual(sum((conta["valres"] for conta in ignoradas), Decimal("0.00")), Decimal("1018.95"))

        with self.assertRaisesMessage(ValueError, "5 contas / R$ 1.711,66"):
            motor.validar_exclusoes_explicitamente_configuradas(ignoradas)


def _motor_fake(area):
    motor = Mock()
    if area == "estoque":
        motor.extrair_firebird.return_value = []
        motor.carregar_comparacao.return_value = {
            "produtos_django": [],
            "produtos_firebird": [],
            "alterados": [],
            "iguais": [],
            "revisar": [],
            "revisao_manual": [],
            "bloqueados": [],
            "fora": {
                "firebird_sem_django": [],
                "django_sem_firebird": [],
                "django_sem_codigo": [],
            },
        }
    elif area == "receber":
        cliente = SimpleNamespace(id=1, nome="Cliente Legado")
        conta = {
            "numero_legado": "AR-1",
            "cliente_id": 1,
            "codigo": "C1",
            "nome": "Cliente Legado",
            "saldo": Decimal("10.00"),
        }
        motor.carregar_universo.return_value = ({1: cliente}, {"C1": 1})
        motor.extrair_firebird.return_value = ([conta], [])
        motor.comparar.return_value = ([], [], [(None, conta)], [])
    elif area == "pagar":
        fornecedor = SimpleNamespace(id=1, nome="Fornecedor Legado")
        conta = {
            "documento": "AP-1",
            "fornecedor_id": 1,
            "codi": "F1",
            "nome": "Fornecedor Legado",
            "valres": Decimal("10.00"),
        }
        motor.extrair_firebird.return_value = ([conta], [])
        motor.carregar_django.return_value = ({1: fornecedor}, [], [], [(None, conta)], [])
    return motor


def _ignoradas_santa_clara_incompletas():
    motivo = "Exclusao explicita: Cafe Santa Clara removido da migracao"
    return [
        {"documento": "27007-01/2-CA", "codi": "00098", "valres": Decimal("322.45"), "motivo_ignorada": motivo},
        {"documento": "27008-02/2-CA", "codi": "00098", "valres": Decimal("374.06"), "motivo_ignorada": motivo},
        {"documento": "27007-02/2-CA", "codi": "00098", "valres": Decimal("322.44"), "motivo_ignorada": motivo},
    ]
