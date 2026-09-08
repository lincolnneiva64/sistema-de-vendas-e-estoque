import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from estoque.models import Produto
from estoque.services.precos_antigo_snapshot import gerar_contexto_conferencia_precos


def _produto(**kwargs):
    dados = {
        "nome": "Produto Teste",
        "codigo_legado": "1.00.0001",
        "preco_compra": Decimal("10.00"),
        "preco_vista": Decimal("15.00"),
        "preco_prazo": Decimal("16.00"),
        "unidade_compra": "CX",
        "unidade_venda_1": "CX",
        "vende_fracionado": False,
    }
    dados.update(kwargs)
    for campo in ("preco_compra", "preco_vista", "preco_prazo"):
        if isinstance(dados.get(campo), str):
            dados[campo] = Decimal(dados[campo])
    return Produto.objects.create(**dados)


def _registro_antigo(
    codigo="1.00.0001",
    nome="Produto Teste",
    unv="CX",
    unf="UN",
    principal=None,
    fracionado=None,
):
    return {
        "codigo": codigo,
        "nome": nome,
        "st": "A",
        "unv": unv,
        "unf": unf,
        "convf": "12.00",
        "principal": principal or {"compra": "10.00", "vista": "15.00", "prazo": "16.00", "terceiro": "17.00"},
        "fracionado": fracionado or {"compra": "1.00", "vista": "1.50", "prazo": "1.60", "terceiro": "1.70"},
    }


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class ConferenciaPrecosAntigoSnapshotTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.snapshot_path = Path(self.tmpdir.name) / "precos.json"

    def _gravar_snapshot(self, produtos):
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "versao": 1,
                    "fonte_logica": "firebird.PRODUTOS.precos",
                    "gerado_em": "2026-09-07T10:00:00-03:00",
                    "quantidade_registros": len(produtos),
                    "produtos": produtos,
                }
            ),
            encoding="utf-8",
        )

    def _contexto(self, params=None):
        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            return gerar_contexto_conferencia_precos(params or {})

    def test_compara_por_codigo_legado_exato_com_unidade_principal(self):
        _produto(codigo_legado="1.00.0001", unidade_compra="CX")
        self._gravar_snapshot([_registro_antigo(codigo="1.00.0001", unv="CX", unf="UN")])

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["match_tipo"], "exato")
        self.assertEqual(linha["situacao"], "seguro")
        self.assertEqual(linha["avaliacao_unidade"]["conjunto"], "principal")
        self.assertEqual(linha["comparacao"]["vista"]["antigo_fmt"], "R$ 15,00")

    def test_compara_unidade_principal_nova_com_fracionado_antigo_quando_coincide_com_unf(self):
        _produto(codigo_legado="1.00.0002", unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.00.0002", unv="SC", unf="KG")])

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["situacao"], "seguro")
        self.assertEqual(linha["avaliacao_unidade"]["conjunto"], "fracionado")
        self.assertEqual(linha["comparacao"]["vista"]["antigo_fmt"], "R$ 1,50")

    def test_marca_unidade_duvidosa_sem_escolher_preco_por_valor(self):
        _produto(codigo_legado="1.00.0003", unidade_compra="PCT", unidade_venda_1="PCT")
        self._gravar_snapshot([_registro_antigo(codigo="1.00.0003", unv="CX", unf="UN")])

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["situacao"], "duvidoso")
        self.assertIsNone(linha["avaliacao_unidade"]["conjunto"])
        self.assertIsNone(linha["comparacao"])

    def test_match_provavel_apenas_quando_nome_normalizado_e_unico(self):
        _produto(codigo_legado="", nome="Cafe Do Sertao", unidade_compra="CX")
        self._gravar_snapshot([_registro_antigo(codigo="9.99.9999", nome="CAFE DO SERTAO", unv="CX")])

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["match_tipo"], "provavel")
        self.assertEqual(linha["situacao"], "seguro")

    def test_nome_normalizado_duplicado_fica_ambiguo(self):
        _produto(codigo_legado="", nome="Cafe Do Sertao", unidade_compra="CX")
        self._gravar_snapshot(
            [
                _registro_antigo(codigo="9.99.9998", nome="CAFE DO SERTAO", unv="CX"),
                _registro_antigo(codigo="9.99.9999", nome="CAFE DO SERTAO", unv="CX"),
            ]
        )

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["match_tipo"], "ambiguo")
        self.assertIsNone(linha["comparacao"])

    def test_nao_encontrado_quando_sem_codigo_e_sem_nome_unico(self):
        _produto(codigo_legado="1.00.4040", nome="Produto Sem Antigo")
        self._gravar_snapshot([])

        linha = self._contexto()["linhas"][0]

        self.assertEqual(linha["match_tipo"], "nao_encontrado")
        self.assertIsNone(linha["comparacao"])

    def test_view_e_somente_leitura_e_nao_chama_firebird(self):
        _produto(codigo_legado="1.00.0001")
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            with patch("subprocess.run", side_effect=AssertionError("Firebird nao deve ser chamado pela view")):
                resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Produto Teste")
        self.assertContains(resposta, "R$ 15,00")

    def test_post_na_tela_nao_altera_produto(self):
        produto = _produto(preco_vista="15.00")
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(reverse("estoque:conferencia_precos_antigo"), {"preco_vista": "99.00"})

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 405)
        self.assertEqual(str(produto.preco_vista), "15.00")

    def test_endpoint_altera_apenas_os_tres_precos_permitidos(self):
        produto = _produto(nome="Produto Restrito", categoria="Limpeza", preco_vista=Decimal("15.00"))

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "11.25",
                "preco_vista": "15.72",
                "preco_prazo": "17.30",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(str(produto.preco_compra), "11.25")
        self.assertEqual(str(produto.preco_vista), "15.72")
        self.assertEqual(str(produto.preco_prazo), "17.30")
        self.assertEqual(produto.categoria, "Limpeza")

    def test_campos_preco_conferido_iniciam_com_default_false(self):
        produto = _produto(nome="Produto Novo Padrao")

        self.assertFalse(produto.preco_conferido)
        self.assertIsNone(produto.preco_conferido_em)
        self.assertIsNone(produto.preco_conferido_por)

    def test_marcar_conferido_preenche_data_e_operador(self):
        usuario = get_user_model().objects.create_user(username="operador", password="senha")
        self.client.force_login(usuario)
        produto = _produto(nome="Produto Conferir", preco_vista=Decimal("15.00"))

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_marcar_conferido"),
            {
                "produto_id": produto.pk,
                "confirmacao": "CONFERIDO",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(produto.preco_conferido)
        self.assertIsNotNone(produto.preco_conferido_em)
        self.assertEqual(produto.preco_conferido_por, usuario)
        self.assertEqual(resposta.json()["preco_conferido_por"], "operador")

    def test_marcar_conferido_nao_altera_precos(self):
        produto = _produto(
            nome="Produto Conferido Sem Preco",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
        )

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_marcar_conferido"),
            {
                "produto_id": produto.pk,
                "confirmacao": "CONFERIDO",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(str(produto.preco_compra), "10.00")
        self.assertEqual(str(produto.preco_vista), "15.00")
        self.assertEqual(str(produto.preco_prazo), "16.00")

    def test_salvar_preco_alterado_limpa_status_conferido(self):
        usuario = get_user_model().objects.create_user(username="operador2", password="senha")
        produto = _produto(nome="Produto Altera Conferido", preco_vista=Decimal("15.00"))
        produto.preco_conferido = True
        produto.preco_conferido_em = "2026-09-08T10:00:00-03:00"
        produto.preco_conferido_por = usuario
        produto.save(update_fields=["preco_conferido", "preco_conferido_em", "preco_conferido_por", "atualizado_em"])

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "11.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(produto.preco_conferido)
        self.assertIsNone(produto.preco_conferido_em)
        self.assertIsNone(produto.preco_conferido_por)

    def test_salvar_mesmo_preco_nao_limpa_status_conferido(self):
        usuario = get_user_model().objects.create_user(username="operador3", password="senha")
        produto = _produto(nome="Produto Mantem Conferido", preco_vista=Decimal("15.00"))
        produto.preco_conferido = True
        produto.preco_conferido_em = "2026-09-08T10:00:00-03:00"
        produto.preco_conferido_por = usuario
        produto.save(update_fields=["preco_conferido", "preco_conferido_em", "preco_conferido_por", "atualizado_em"])

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "10.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(produto.preco_conferido)
        self.assertEqual(produto.preco_conferido_por, usuario)

    def test_endpoint_rejeita_campo_extra_indevido(self):
        produto = _produto(nome="Produto Campo Extra", categoria="Original")

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "11.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
                "nome": "Nome Indevido",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Campo nao permitido", resposta.json()["erro"])
        self.assertEqual(produto.nome, "Produto Campo Extra")

    def test_endpoint_aceita_decimal_com_virgula(self):
        produto = _produto(nome="Produto Decimal")

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "10,25",
                "preco_vista": "15,72",
                "preco_prazo": "16,90",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(str(produto.preco_vista), "15.72")
        self.assertEqual(resposta.json()["precos"]["preco_vista"], "15.72")

    def test_endpoint_faz_rollback_se_validacao_falhar(self):
        produto = _produto(preco_compra=Decimal("10.00"), preco_vista=Decimal("15.00"), preco_prazo=Decimal("16.00"))

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "20.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
            },
        )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(str(produto.preco_compra), "10.00")
        self.assertEqual(str(produto.preco_vista), "15.00")
        self.assertEqual(str(produto.preco_prazo), "16.00")

    def test_endpoint_produto_inexistente_retorna_404(self):
        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": 999999,
                "preco_compra": "10.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
            },
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(resposta.json()["campo"], "produto_id")

    def test_linha_duvidosa_permite_editar_novo_sem_alterar_referencia_antiga(self):
        produto = _produto(codigo_legado="1.00.0003", unidade_compra="PCT", unidade_venda_1="PCT")
        registro = _registro_antigo(codigo="1.00.0003", unv="CX", unf="UN")
        self._gravar_snapshot([registro])
        antes = self.snapshot_path.read_text(encoding="utf-8")

        resposta = self.client.post(
            reverse("estoque:conferencia_precos_antigo_salvar"),
            {
                "produto_id": produto.pk,
                "preco_compra": "11.00",
                "preco_vista": "15.00",
                "preco_prazo": "16.00",
            },
        )

        produto.refresh_from_db()
        depois = self.snapshot_path.read_text(encoding="utf-8")
        linha = self._contexto({"filtro": "duvidosos"})["linhas"][0]
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(str(produto.preco_compra), "11.00")
        self.assertEqual(antes, depois)
        self.assertEqual(linha["registro_antigo"]["principal"]["vista"], "15.00")
        self.assertEqual(linha["situacao"], "duvidoso")

    def test_template_tem_enter_shift_enter_esc_e_busca_enter(self):
        _produto(codigo_legado="1.00.0001")
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "data-price-input")
        self.assertContains(resposta, "event.key === \"Enter\"")
        self.assertContains(resposta, "event.shiftKey")
        self.assertContains(resposta, "event.key === \"Escape\"")
        self.assertContains(resposta, "data-search-form")
        self.assertContains(resposta, "function focusInput")
        self.assertContains(resposta, "input.select()")

    def test_template_nao_duplica_preco_novo_fora_do_input(self):
        _produto(codigo_legado="1.00.0001")
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'name="preco_compra"')
        self.assertContains(resposta, "Antigo:")
        self.assertContains(resposta, "Dif.:")
        self.assertNotContains(resposta, "Novo:")
        self.assertNotContains(resposta, "data-new-display")

    def test_view_e_endpoint_nao_chamam_firebird(self):
        produto = _produto(codigo_legado="1.00.0001")
        produto_sem_legado = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot(
            [
                _registro_antigo(),
                _registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf=""),
            ]
        )

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            with patch("subprocess.run", side_effect=AssertionError("Firebird nao deve ser chamado")):
                get_resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))
                post_resposta = self.client.post(
                    reverse("estoque:conferencia_precos_antigo_salvar"),
                    {
                        "produto_id": produto.pk,
                        "preco_compra": "11.00",
                        "preco_vista": "15.00",
                        "preco_prazo": "16.00",
                    },
                )
                confirmacao_resposta = self.client.post(
                    reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                    {
                        "produto_id": produto_sem_legado.pk,
                        "codigo_antigo": "1.01.0001",
                        "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                    },
                )

        self.assertEqual(get_resposta.status_code, 200)
        self.assertEqual(post_resposta.status_code, 200)
        self.assertEqual(confirmacao_resposta.status_code, 200)

    def test_snapshot_continua_read_only_ao_salvar(self):
        produto = _produto(codigo_legado="1.00.0001")
        produto_sem_legado = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot(
            [
                _registro_antigo(),
                _registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf=""),
            ]
        )
        antes = self.snapshot_path.read_text(encoding="utf-8")

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_salvar"),
                {
                    "produto_id": produto.pk,
                    "preco_compra": "11.00",
                    "preco_vista": "15.00",
                    "preco_prazo": "16.00",
                },
            )
            confirmacao_resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto_sem_legado.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        depois = self.snapshot_path.read_text(encoding="utf-8")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(confirmacao_resposta.status_code, 200)
        self.assertEqual(antes, depois)

    def test_sugere_candidato_forte_para_nao_encontrado(self):
        _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        linha = self._contexto({"filtro": "nao-encontrados"})["linhas"][0]

        self.assertEqual(linha["match_tipo"], "nao_encontrado")
        self.assertEqual(linha["sugestoes_correspondencia"][0]["codigo"], "1.01.0001")
        self.assertEqual(linha["sugestoes_correspondencia"][0]["nivel"], "forte")

    def test_confirmar_candidato_valido_preenche_codigo_legado(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(produto.codigo_legado, "1.01.0001")
        self.assertFalse(produto.preco_conferido)

    def test_confirmar_preenche_somente_codigo_legado_sem_alterar_precos_ou_outros_campos(self):
        produto = _produto(
            nome="Abacate Kg",
            codigo_legado=None,
            categoria="Frutas",
            unidade_compra="KG",
            unidade_venda_1="KG",
            preco_compra=Decimal("8.00"),
            preco_vista=Decimal("10.50"),
            preco_prazo=Decimal("11.00"),
        )
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(produto.codigo_legado, "1.01.0001")
        self.assertEqual(str(produto.preco_compra), "8.00")
        self.assertEqual(str(produto.preco_vista), "10.50")
        self.assertEqual(str(produto.preco_prazo), "11.00")
        self.assertEqual(produto.nome, "Abacate Kg")
        self.assertEqual(produto.categoria, "Frutas")
        self.assertEqual(produto.unidade_compra, "KG")

    def test_confirmar_bloqueia_codigo_ja_usado_por_outro_produto(self):
        alvo = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        usado = _produto(nome="Abacate Antigo", codigo_legado="1.01.0001", unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": alvo.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        alvo.refresh_from_db()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn(usado.nome, resposta.json()["erro"])
        self.assertIsNone(alvo.codigo_legado)

    def test_confirmar_bloqueia_candidato_inexistente_no_snapshot(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None)
        self._gravar_snapshot([])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 404)
        self.assertIsNone(produto.codigo_legado)

    def test_confirmar_bloqueia_produto_inexistente(self):
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": 999999,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )

        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(resposta.json()["campo"], "produto_id")

    def test_linha_passa_de_nao_encontrada_para_exata_apos_confirmacao(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        antes = self._contexto({"filtro": "nao-encontrados"})["linhas"][0]
        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                },
            )
        depois = self._contexto({"filtro": "exatos"})["linhas"][0]

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(antes["match_tipo"], "nao_encontrado")
        self.assertEqual(depois["match_tipo"], "exato")
        self.assertEqual(depois["situacao"], "seguro")
        self.assertEqual(depois["registro_antigo"]["codigo"], "1.01.0001")
        self.assertFalse(depois["produto"].preco_conferido)

    def test_match_exato_nao_marca_conferido_automaticamente(self):
        produto = _produto(codigo_legado="1.00.0001", unidade_compra="CX")
        self._gravar_snapshot([_registro_antigo(codigo="1.00.0001", unv="CX", unf="UN")])

        linha = self._contexto()["linhas"][0]
        produto.refresh_from_db()

        self.assertEqual(linha["match_tipo"], "exato")
        self.assertEqual(linha["situacao"], "seguro")
        self.assertFalse(produto.preco_conferido)

    def test_filtros_pendentes_e_conferidos(self):
        _produto(nome="Produto Pendente", codigo_legado="1.00.0001")
        conferido = _produto(nome="Produto Conferido", codigo_legado="1.00.0002")
        conferido.preco_conferido = True
        conferido.preco_conferido_em = "2026-09-08T10:00:00-03:00"
        conferido.save(update_fields=["preco_conferido", "preco_conferido_em", "atualizado_em"])
        self._gravar_snapshot(
            [
                _registro_antigo(codigo="1.00.0001", nome="Produto Pendente"),
                _registro_antigo(codigo="1.00.0002", nome="Produto Conferido"),
            ]
        )

        contexto_pendentes = self._contexto({"status_preco": "pendentes"})
        contexto_conferidos = self._contexto({"status_preco": "conferidos"})

        self.assertEqual(contexto_pendentes["resumo"]["pendentes"], 1)
        self.assertEqual(contexto_pendentes["resumo"]["conferidos"], 1)
        self.assertEqual([linha["produto"].nome for linha in contexto_pendentes["linhas"]], ["Produto Pendente"])
        self.assertEqual([linha["produto"].nome for linha in contexto_conferidos["linhas"]], ["Produto Conferido"])

    def test_linha_verde_somente_quando_preco_conferido_true(self):
        produto = _produto(codigo_legado="1.00.0001")
        produto.preco_conferido = True
        produto.preco_conferido_em = "2026-09-08T10:00:00-03:00"
        produto.save(update_fields=["preco_conferido", "preco_conferido_em", "atualizado_em"])
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "linha-conferida")
        self.assertContains(resposta, "badge-conferido")

    def test_fluxo_teclado_inclui_conferido_apos_salvar(self):
        _produto(codigo_legado="1.00.0001")
        self._gravar_snapshot([_registro_antigo()])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        self.assertContains(resposta, "data-mark-reviewed")
        self.assertContains(resposta, "focusReviewed")
        self.assertContains(resposta, "markReviewed")

    def test_indicador_visual_na_tela_de_vendas(self):
        produto = _produto(nome="Produto Venda Conferido", codigo_legado="1.00.0001")
        produto.preco_conferido = True
        produto.preco_conferido_em = "2026-09-08T10:00:00-03:00"
        produto.save(update_fields=["preco_conferido", "preco_conferido_em", "atualizado_em"])
        _produto(nome="Produto Venda Pendente", codigo_legado="1.00.0002")

        resposta = self.client.get(reverse("estoque:vendas"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'data-preco-conferido="true"')
        self.assertContains(resposta, 'data-preco-conferido="false"')
        self.assertContains(resposta, 'id="precoConferidoBadge"')
        self.assertContains(resposta, "preco-conferido-campo")
        self.assertContains(resposta, "preco-conferido-campo-badge")
        self.assertContains(resposta, 'preco.classList.toggle("preco-conferido-campo", precoConferido)')
        self.assertContains(resposta, 'precoConferidoBadge.classList.toggle("visivel", precoConferido)')
        self.assertContains(resposta, "Preço conferido")

    def test_nao_e_este_nao_grava_no_banco(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None, unidade_compra="KG", unidade_venda_1="KG")
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE", unv="KG", unf="")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.get(reverse("estoque:conferencia_precos_antigo"))

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "data-reject-candidate")
        self.assertIsNone(produto.codigo_legado)

    def test_confirmar_endpoint_rejeita_campo_extra(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None)
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                    "confirmacao": "CONFIRMAR_CORRESPONDENCIA",
                    "preco_vista": "1.00",
                },
            )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Campo nao permitido", resposta.json()["erro"])
        self.assertIsNone(produto.codigo_legado)

    def test_confirmar_endpoint_exige_confirmacao_explicita(self):
        produto = _produto(nome="Abacate Kg", codigo_legado=None)
        self._gravar_snapshot([_registro_antigo(codigo="1.01.0001", nome="ABACATE")])

        with override_settings(PRECOS_FIREBIRD_SNAPSHOT_PATH=str(self.snapshot_path)):
            resposta = self.client.post(
                reverse("estoque:conferencia_precos_antigo_confirmar_correspondencia"),
                {
                    "produto_id": produto.pk,
                    "codigo_antigo": "1.01.0001",
                },
            )

        produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 400)
        self.assertIsNone(produto.codigo_legado)
