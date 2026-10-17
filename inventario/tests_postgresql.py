from concurrent.futures import ThreadPoolExecutor
from threading import Event, Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections, transaction
from django.test import TransactionTestCase, override_settings

from estoque.models import Funcionario, Produto, MovimentacaoEstoqueManual
from estoque.views import _baixar_estoque_produto
from . import services
from .models import RodadaContagem, MovimentoObservado, DecisaoAjuste
from .tests import produto


@skipUnless(connection.vendor == "postgresql", "Exige bloqueios reais de PostgreSQL descartável")
class ConcorrenciaPostgresqlTests(TransactionTestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-pg", "", "teste")
        self.produto = produto()
        self.funcionario = Funcionario.objects.create(nome="Contador PG")
        self.inventario = services.criar_inventario(self.usuario, "Concorrência", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, _ = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def executar(self, funcao):
        close_old_connections()
        try:
            return funcao()
        finally:
            close_old_connections()

    def test_distribuicoes_concorrentes_nao_atribuem_mesma_rodada(self):
        nova = services.criar_inventario(self.usuario, "Divisao concorrente", "manual", [self.produto.pk])
        rodada = nova.itens.get().rodadas.get()
        barreira = Barrier(2)
        def atribuir():
            barreira.wait(10)
            try:
                services.gerar_acesso(self.usuario, nova.pk, self.funcionario.pk, [rodada.pk])
                return "criada"
            except ValueError:
                return "conflito"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.executar, atribuir) for _ in range(2)]
            resultados = [future.result(15) for future in futures]
        self.assertCountEqual(resultados, ["criada", "conflito"])
        self.assertEqual(nova.acessos.count(), 1)

    def test_envios_concorrentes_concluem_apenas_apos_ambas_atribuicoes(self):
        from .models import Inventario, EventoInventario
        outro = produto(nome="Outro contador")
        inv = services.criar_inventario(self.usuario, "Envios concorrentes", "manual", [self.produto.pk, outro.pk])
        rodadas = list(RodadaContagem.objects.filter(item__inventario=inv).order_by('pk'))
        acessos = [services.gerar_acesso(self.usuario, inv.pk, self.funcionario.pk, [r.pk])[0] for r in rodadas]
        for acesso, rodada in zip(acessos, rodadas):
            services.salvar_contagem(acesso.pk, rodada.pk, 0, "8", "0", True)
        inv.refresh_from_db()
        barreira = Barrier(2)
        def enviar(acesso):
            barreira.wait(10)
            try:
                services.finalizar_conferencia(acesso.pk, inv.versao)
                return "enviada"
            except ValueError:
                return "atualizar"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.executar, lambda a=a: enviar(a)) for a in acessos]
            resultados = [f.result(15) for f in futures]
        self.assertCountEqual(resultados, ["enviada", "atualizar"])
        inv.refresh_from_db()
        self.assertIsNone(inv.finalizada_em)
        pendente = inv.acessos.get(enviada_em__isnull=True)
        services.finalizar_conferencia(pendente.pk, Inventario.objects.get(pk=inv.pk).versao)
        self.assertEqual(EventoInventario.objects.filter(inventario=inv, acao="conferencia_finalizada").count(), 1)

    def test_venda_antes_do_ok_referencia_aguarda_commit(self):
        travado, liberar, iniciou_ok, terminou_ok = Event(), Event(), Event(), Event()
        def venda():
            with transaction.atomic():
                _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
                travado.set()
                if not liberar.wait(10):
                    raise AssertionError("Liberação não recebida")
        def ok():
            iniciou_ok.set()
            rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "9", "0", True)
            terminou_ok.set()
            return rodada
        with ThreadPoolExecutor(max_workers=2) as executor:
            primeira = executor.submit(self.executar, venda)
            self.assertTrue(travado.wait(10))
            segunda = executor.submit(self.executar, ok)
            try:
                self.assertTrue(iniciou_ok.wait(10))
                self.assertFalse(terminou_ok.wait(.25))
            finally:
                liberar.set()
            primeira.result(15)
            rodada = segunda.result(15)
        self.assertEqual(rodada.esperado, 9)
        self.assertEqual(rodada.sequencia_ok, 1)

    def test_venda_apos_ok_registra_sequencia_posterior(self):
        capturado, liberar, iniciou_venda, terminou_venda = Event(), Event(), Event(), Event()
        original = services.evento
        def segurar_evento(*args, **kwargs):
            if args[1] == "produto_ok":
                capturado.set()
                if not liberar.wait(10):
                    raise AssertionError("Liberação não recebida")
            return original(*args, **kwargs)
        def ok():
            return services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "0", True)
        def venda():
            iniciou_venda.set()
            with transaction.atomic():
                _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
            terminou_venda.set()
        with patch("inventario.services.evento", side_effect=segurar_evento), ThreadPoolExecutor(max_workers=2) as executor:
            primeira = executor.submit(self.executar, ok)
            self.assertTrue(capturado.wait(10))
            segunda = executor.submit(self.executar, venda)
            try:
                self.assertTrue(iniciou_venda.wait(10))
                self.assertFalse(terminou_venda.wait(.25))
            finally:
                liberar.set()
            rodada = primeira.result(15)
            segunda.result(15)
        self.assertEqual(rodada.esperado, 10)
        movimento = services.posteriores(rodada).get()
        self.assertEqual((movimento.sequencia, movimento.antes, movimento.depois), (1, 10, 9))

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_dupla_aprovacao_concorrente_idempotente(self):
        rodada = services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "8", "0", True)
        services.revisar(self.usuario, rodada.pk, "Corte físico comprovado")
        barreira = Barrier(2)
        def aprovar():
            usuario = get_user_model().objects.get(pk=self.usuario.pk)
            barreira.wait(10)
            return services.aprovar(usuario, rodada.pk, "Falta aprovada").pk
        with ThreadPoolExecutor(max_workers=2) as executor:
            resultados = [executor.submit(self.executar, aprovar) for _ in range(2)]
            ids = [r.result(20) for r in resultados]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(DecisaoAjuste.objects.count(), 1)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 8)

    def test_sinalizacao_concorrente_idempotente_preserva_saldo(self):
        chave, barreira = uuid4(), Barrier(2)
        def sinalizar():
            barreira.wait(10)
            return services.sinalizar_fisico(self.rodada.pk, "Retirada ainda não lançada", chave,
                acesso_id=self.acesso.pk, token_hash=self.acesso.token_hash).pk
        with ThreadPoolExecutor(max_workers=2) as executor:
            resultados = [executor.submit(self.executar, sinalizar) for _ in range(2)]
            ids = [r.result(20) for r in resultados]
        self.assertEqual(ids[0], ids[1])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentoObservado.objects.exists())
