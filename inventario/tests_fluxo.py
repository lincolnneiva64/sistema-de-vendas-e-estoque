from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario, MovimentacaoEstoqueManual, Produto, MovimentoFinanceiro, Venda, Compra
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from .models import (RodadaContagem, EventoInventario, AcessoInventario, MovimentoObservado, DecisaoAjuste,
                     SequenciaEstoque)
from . import services
from .tests import produto


class FluxoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("responsavel", "", "teste")
        self.funcionario = Funcionario.objects.create(nome="Contador")
        self.produto = produto()
        self.inventario = services.criar_inventario(self.usuario, "Conferência", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def ok(self, principal="8", avulsos="0"):
        return services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, principal, avulsos, True)

    def entrar(self, client=None):
        client = client or self.client
        response = client.post(reverse("inventario:convite", args=[self.token]))
        self.assertEqual(response.status_code, 302)
        return client

    def test_envio_lista_e_get_nao_criam_referencia_ou_eventos(self):
        antes = EventoInventario.objects.count()
        self.client.get(reverse("inventario:convite", args=[self.token]))
        self.entrar()
        for _ in range(2):
            self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 200)
        self.rodada.refresh_from_db()
        self.assertIsNone(self.rodada.esperado)
        self.assertIsNone(self.rodada.ok_em)
        self.assertIsNone(self.rodada.sequencia_ok)
        self.assertEqual(EventoInventario.objects.count(), antes)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_progresso_retoma_versao_e_finalizacao_individual(self):
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "0", "0")
        self.assertEqual(rodada.contado_base, 0)
        self.assertIsNone(rodada.esperado)
        with self.assertRaises(ValueError):
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, "8", "0")
        rodada = services.salvar_contagem(self.acesso.pk, rodada.pk, 1, "8", "0", True)
        self.assertEqual(rodada.esperado, 10)
        self.assertIsNotNone(rodada.ok_em)
        rascunho = services.salvar_contagem(self.acesso.pk, rodada.pk, 2, "9", "0")
        self.assertIsNone(rascunho.ok_em)
        self.assertEqual(rascunho.confirmacoes.get().contado_base, 8)

    def test_progresso_parcial_preserva_campos_vazios(self):
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "2", "")
        self.assertEqual(rodada.embalagens, 2)
        self.assertIsNone(rodada.avulsos)
        self.assertIsNone(rodada.contado_base)
        with self.assertRaises(ValueError):
            services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, "2", "", True)

    def test_ok_repetido_apos_resposta_perdida_nao_duplica(self):
        primeiro = self.ok()
        segundo = self.ok()
        self.assertEqual(primeiro.ok_em, segundo.ok_em)
        self.assertEqual(EventoInventario.objects.filter(acao="produto_ok").count(), 1)
        with self.assertRaises(ValueError):
            self.ok("9")

    def test_referencia_usa_saldo_no_ok_e_relogio_servidor(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=12)
        self.entrar()
        response = self.client.post(reverse("inventario:contar", args=[self.rodada.pk]),
            {"acao": "ok", "versao": 0, "embalagens": 8, "avulsos": 0, "ok_em": "2000-01-01"})
        self.assertEqual(response.status_code, 200)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.esperado, 12)
        self.assertEqual(self.rodada.sequencia_ok, 1)
        self.assertGreater(self.rodada.ok_em, timezone.now() - timedelta(minutes=1))

    def test_produtos_tem_referencias_distintas(self):
        outro = produto(nome="Outro")
        inventario = services.criar_inventario(self.usuario, "Outra", "manual", [self.produto.pk, outro.pk])
        rodadas = list(RodadaContagem.objects.filter(item__inventario=inventario).order_by("item__produto_id"))
        acesso, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [r.pk for r in rodadas])
        primeiro = services.salvar_contagem(acesso.pk, rodadas[0].pk, 0, "10", "0", True)
        Produto.objects.filter(pk=outro.pk).update(quantidade=11)
        segundo = services.salvar_contagem(acesso.pk, rodadas[1].pk, 0, "11", "0", True)
        self.assertEqual((primeiro.esperado, segundo.esperado), (10, 11))
        self.assertLess(primeiro.ok_em, segundo.ok_em)

    def test_contagem_cega_html_json_e_bloqueio_administrativo(self):
        self.entrar()
        html = self.client.get(reverse("inventario:mobile"))
        self.assertContains(html, "OK — Produto conferido")
        for palavra in ("esperado", "preco", "preço", "custo_base", "divergencia", "sequencia_ok"):
            self.assertNotIn(palavra, html.content.decode().lower())
        response = self.client.post(reverse("inventario:contar", args=[self.rodada.pk]),
            {"acao": "ok", "versao": 0, "embalagens": 8, "avulsos": 0})
        self.assertEqual(set(response.json()["produto"]), {"id", "nome", "codigo", "base", "secundaria", "fracionado", "passo",
            "passo_avulsos", "embalagens", "avulsos", "versao", "conferido", "ok_em", "guardado", "ultimo_ok"})
        self.assertIsInstance(response.json()["produto"]["guardado"], bool)
        self.assertEqual(set(response.json()["produto"]["ultimo_ok"]), {"embalagens", "avulsos", "ok_em"})
        self.assertEqual(self.client.get(reverse("inventario:ajustes", args=[self.inventario.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("inventario:detalhe", args=[self.inventario.pk])).status_code, 403)
        self.assertIn("no-store", html["Cache-Control"])
        self.assertEqual(html["Referrer-Policy"], "same-origin")

    def test_token_revogado_vencido_invalido_e_funcionario_inativo(self):
        self.entrar()
        services.revogar_acesso(self.usuario, self.acesso.pk)
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventario:convite", args=[self.token])).status_code, 403)
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(revogado_em=None, expira_em=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PermissionDenied):
            services.acesso_token(self.token)
        with self.assertRaises(PermissionDenied):
            services.acesso_token("invalido")
        AcessoInventario.objects.filter(pk=self.acesso.pk).update(expira_em=timezone.now() + timedelta(hours=1))
        Funcionario.objects.filter(pk=self.funcionario.pk).update(ativo=False)
        with self.assertRaises(PermissionDenied):
            services.acesso_token(self.token)

    def test_acesso_nao_pode_gravar_outra_atribuicao(self):
        outro = services.criar_inventario(self.usuario, "Outra", "manual", [self.produto.pk])
        rodada = outro.itens.get().rodadas.get()
        with self.assertRaises(PermissionDenied):
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, "5", "0", True)

    def test_renovar_invalida_token_e_sessao_sem_perder_progresso(self):
        self.entrar()
        services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "2", "")
        hash_antigo = self.acesso.token_hash
        acesso, novo_token = services.renovar_acesso(self.usuario, self.acesso.pk)
        self.assertEqual(acesso.pk, self.acesso.pk)
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 403)
        with self.assertRaises(PermissionDenied):
            services.acesso_token(self.token)
        self.assertEqual(services.acesso_token(novo_token).pk, self.acesso.pk)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.embalagens, 2)
        with self.assertRaises(PermissionDenied):
            services.salvar_contagem(self.acesso.pk, self.rodada.pk, 1, "3", "0", token_hash=hash_antigo)

    def test_confirmacao_sem_observador_falha_sem_criar_ok(self):
        with patch("inventario.services.verificar_observador", side_effect=ValueError("Observador ausente")):
            with self.assertRaises(ValueError):
                self.ok()
        self.rodada.refresh_from_db()
        self.assertIsNone(self.rodada.ok_em)

    def test_csrf_e_permissoes(self):
        csrf = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf.post(reverse("inventario:convite", args=[self.token])).status_code, 403)
        usuario = get_user_model().objects.create_user("sem-permissao", password="teste")
        with self.assertRaises(PermissionDenied):
            services.criar_inventario(usuario, "Teste", "manual", [self.produto.pk])
        with self.assertRaises(PermissionDenied):
            services.aprovar(usuario, self.rodada.pk, "Motivo")
        usuario.user_permissions.add(Permission.objects.get(content_type__app_label="inventario", codename="revisar_inventario"))
        usuario = get_user_model().objects.get(pk=usuario.pk)
        with self.assertRaises(PermissionDenied):
            services.gerar_acesso(usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def test_recontagem_preserva_original_e_nao_vaza_contagem_anterior(self):
        original = self.ok()
        nova = services.recontar(self.usuario, original.pk, "Reabrir autorizado")
        original.refresh_from_db()
        self.assertEqual(original.contado_base, 8)
        self.assertIsNotNone(original.encerrada_em)
        self.assertIsNone(nova.contado_base)
        self.assertEqual(nova.numero, 2)
        self.entrar()
        self.assertNotContains(self.client.get(reverse("inventario:mobile")), "OK — Produto conferido")
        with self.assertRaises(ValueError):
            self.ok()

    def test_alteracao_cadastro_bloqueia_ok(self):
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=24)
        with self.assertRaises(ValueError):
            self.ok()
        self.assertFalse(EventoInventario.objects.filter(acao="produto_ok").exists())

    def test_observador_cobre_update_sql_e_saldo_nulo(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE estoque_produto SET quantidade = quantidade + 2 WHERE id = %s", [self.produto.pk])
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=None)
        movimentos = list(MovimentoObservado.objects.filter(produto=self.produto).order_by("sequencia"))
        self.assertEqual([m.sequencia for m in movimentos], [1, 2, 3])
        self.assertEqual((movimentos[1].antes, movimentos[1].depois), (11, 13))
        self.assertIsNone(movimentos[2].depois)

    def test_observador_rollback_e_update_sem_mudanca(self):
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=10)
        self.assertFalse(MovimentoObservado.objects.exists())
        try:
            with transaction.atomic():
                Produto.objects.filter(pk=self.produto.pk).update(quantidade=9)
                raise ValueError("rollback")
        except ValueError:
            pass
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertEqual(services.sequencia(self.produto.pk), 0)

    def test_ajuste_manual_existente_e_observado(self):
        self.ok()
        conferir_ou_ajustar_estoque(self.produto.pk, "ajuste", novo_estoque=9, usuario=self.usuario)
        self.assertEqual(services.posteriores(self.rodada_atual()).count(), 1)
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, self.rodada.pk, "Revisar")

    def rodada_atual(self):
        return RodadaContagem.objects.select_related("item").get(pk=self.rodada.pk)

    def test_movimento_posterior_bloqueia_ate_conciliacao(self):
        rodada = self.ok()
        with transaction.atomic():
            from estoque.views import _baixar_estoque_produto
            _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Conferido")
        services.conciliar(self.usuario, rodada.pk, services.posteriores(rodada).get().pk, "Venda teste: retirada física após o OK comprovada")
        revisada = services.revisar(self.usuario, rodada.pk, "Corte físico e lançamentos verificados")
        self.assertEqual(revisada.revisao_sequencia, 1)

    def test_telas_e_csv_sao_somente_leitura(self):
        self.ok()
        self.client.force_login(self.usuario)
        antes = (EventoInventario.objects.count(), MovimentacaoEstoqueManual.objects.count(), MovimentoObservado.objects.count())
        for nome, args in [("lista", []), ("detalhe", [self.inventario.pk]), ("relatorio", [self.inventario.pk])]:
            self.assertEqual(self.client.get(reverse("inventario:" + nome, args=args)).status_code, 200)
        self.assertEqual(antes, (EventoInventario.objects.count(), MovimentacaoEstoqueManual.objects.count(), MovimentoObservado.objects.count()))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_ajustes_bloqueados_por_padrao(self):
        self.ok()
        with self.assertRaisesMessage(ValueError, "desabilitada"):
            services.aprovar(self.usuario, self.rodada.pk, "Aprovado")
        self.assertFalse(DecisaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_legado_nao_contorna_aprovacao_separada_mesmo_com_revisao(self):
        rodada = self.ok()
        with self.assertRaises(ValueError):
            services.aprovar(self.usuario, rodada.pk, "Aprovar")
        with transaction.atomic():
            from estoque.views import _baixar_estoque_produto
            _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
        services.conciliar(self.usuario, rodada.pk, services.posteriores(rodada).get().pk, "Venda e retirada após OK documentadas")
        services.revisar(self.usuario, rodada.pk, "Corte físico verificado")
        for _ in range(2):
            with self.assertRaisesMessage(ValueError, "Fluxo antigo desativado"):
                services.aprovar(self.usuario, rodada.pk, "Falta aprovada")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 9)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 0)
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(Venda.objects.exists())
        self.assertFalse(Compra.objects.exists())

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_novo_movimento_invalida_revisao(self):
        rodada = self.ok()
        services.revisar(self.usuario, rodada.pk, "Corte físico")
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        with self.assertRaises(ValueError):
            services.aprovar(self.usuario, rodada.pk, "Aprovar")

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_legado_bloqueia_antes_de_criar_decisao(self):
        rodada = self.ok()
        services.revisar(self.usuario, rodada.pk, "Corte físico")
        with patch("inventario.services.DecisaoAjuste.objects.create", side_effect=RuntimeError("Falha simulada")):
            with self.assertRaisesMessage(ValueError, "Fluxo antigo desativado"):
                services.aprovar(self.usuario, rodada.pk, "Aprovar")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(DecisaoAjuste.objects.exists())

    def test_lacuna_no_historico_e_saldo_nulo_exigem_recontagem(self):
        rodada = self.ok()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        MovimentoObservado.objects.all().delete()  # Simulate an externally damaged audit trail.
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Corte físico")
