from decimal import Decimal
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from estoque.models import Produto, Funcionario, MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from estoque.views import _baixar_estoque_produto
from . import services, views
from .models import MovimentoObservado, SinalizacaoFisica, EventoInventario
from .tests import produto


class AperfeicoamentosTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser("gestor-melhorias", "", "teste")
        self.funcionario = Funcionario.objects.create(nome="Contador melhorias")
        self.produto = produto()
        self.inventario = services.criar_inventario(self.usuario, "Melhorias", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def ok(self):
        return services.salvar_contagem(self.acesso.pk, self.rodada.pk, 0, "9", "6", True)

    def sinalizar(self, chave=None, descricao="Retirada física ainda não lançada"):
        return services.sinalizar_fisico(self.rodada.pk, descricao, chave or uuid4(), acesso_id=self.acesso.pk, token_hash=self.acesso.token_hash)

    def test_ajustes_antes_e_depois_do_ok_tem_origem_e_corte(self):
        conferir_ou_ajustar_estoque(self.produto.pk, "ajuste", novo_estoque=12, usuario=self.usuario)
        rodada = self.ok()
        conferir_ou_ajustar_estoque(self.produto.pk, "ajuste", novo_estoque=13, usuario=self.usuario)
        movimentos = list(services.posteriores(rodada))
        self.assertEqual((rodada.esperado, rodada.sequencia_ok), (12, 1))
        self.assertEqual(len(movimentos), 1)
        self.assertEqual(movimentos[0].origem, "ajuste_manual")
        self.assertTrue(movimentos[0].referencia.startswith("Movimentação manual #"))

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_origem_desconhecida_bloqueia_conciliacao_revisao_e_aprovacao(self):
        rodada = self.ok()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=11)
        movimento = services.posteriores(rodada).get()
        self.assertEqual(movimento.origem, "nao_identificada")
        with self.assertRaisesMessage(ValueError, "Origem não identificada"):
            services.conciliar(self.usuario, rodada.pk, movimento.pk, "Texto não supre origem ausente")
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Revisar")
        with self.assertRaises(ValueError):
            services.aprovar(self.usuario, rodada.pk, "Aprovar")

    def test_sinalizacao_idempotente_depois_ok_nao_edita_contagem_ou_saldo(self):
        rodada = self.ok()
        chave = uuid4()
        primeira, segunda = self.sinalizar(chave), self.sinalizar(chave)
        self.assertEqual(primeira.pk, segunda.pk)
        self.assertEqual(EventoInventario.objects.filter(acao="fisico_sinalizado").count(), 1)
        rodada.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual((rodada.contado_base, rodada.versao, self.produto.quantidade), (Decimal("9.500"), 1, 10))
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, rodada.pk, "Revisar")
        with self.assertRaises(ValueError):
            self.sinalizar(chave, "Descrição alterada em reenvio")

    def test_sinalizacao_pendente_continua_bloqueando_recontagem_ate_resolucao(self):
        sinal = self.sinalizar()
        nova = services.recontar(self.usuario, self.rodada.pk, "Conferir novamente")
        acesso, _ = services.gerar_acesso(self.usuario, self.inventario.pk, self.funcionario.pk, [nova.pk])
        services.salvar_contagem(acesso.pk, nova.pk, 0, "10", "0", True)
        with self.assertRaises(ValueError):
            services.revisar(self.usuario, nova.pk, "Corte")
        services.resolver_sinalizacao(self.usuario, sinal.pk, "Retirada cancelada e quantidade física reconferida")
        self.assertIsNotNone(services.revisar(self.usuario, nova.pk, "Verificação física documentada").revisada_em)

    def test_sinalizacao_exige_atribuicao_token_e_permissao(self):
        outro = services.criar_inventario(self.usuario, "Outra", "manual", [self.produto.pk]).itens.get().rodadas.get()
        with self.assertRaises(PermissionDenied):
            services.sinalizar_fisico(outro.pk, "Dúvida", uuid4(), acesso_id=self.acesso.pk, token_hash=self.acesso.token_hash)
        with self.assertRaises(PermissionDenied):
            services.sinalizar_fisico(self.rodada.pk, "Dúvida", uuid4(), acesso_id=self.acesso.pk, token_hash="antigo")
        usuario = get_user_model().objects.create_user("sem-permissao-melhorias")
        with self.assertRaises(PermissionDenied):
            services.resolver_sinalizacao(usuario, self.sinalizar().pk, "Sem autoridade")

    def test_resumo_com_unidades_valor_status_e_formulario_unico(self):
        self.ok()
        linha = views.linhas_revisao(self.inventario)[0]
        self.assertEqual((linha["diferenca_texto"], linha["valor"], linha["situacao"]), ("-6 UN", Decimal("-5.00"), "Falta"))
        self.client.force_login(self.usuario)
        pagina = self.client.get(reverse("inventario:detalhe", args=[self.inventario.pk]))
        self.assertContains(pagina, 'class="resumo-linha falta"')
        self.assertContains(pagina, 'value="recontar"', count=1)
        self.assertContains(pagina, 'value="revisar"', count=1)
        self.assertContains(pagina, 'class="card detalhe-produto"')

    def test_movimentos_conhecidos_aparecem_automaticamente_sem_avisos(self):
        self.ok()
        with transaction.atomic():
            _baixar_estoque_produto(self.produto.pk, 1, unidade="CX")
        linha = views.linhas_revisao(self.inventario)[0]
        self.assertEqual(linha["situacao"], "Revisão necessária")
        self.assertEqual(linha["movimentos"][0]["origem"], "Venda")
        self.assertEqual(linha["movimentos"][0]["quantidade"], "-12 UN")
        self.assertFalse(SinalizacaoFisica.objects.exists())

    def test_inteiros_e_kg_payload_cego(self):
        rodada = self.ok()
        self.assertEqual((views.payload_cego(rodada)["embalagens"], views.payload_cego(rodada)["avulsos"]), ("9", "6"))
        kg = produto(nome="Peso melhorias", unidade_compra="KG", vende_fracionado=False)
        inventario = services.criar_inventario(self.usuario, "KG", "manual", [kg.pk])
        r = inventario.itens.get().rodadas.get()
        a, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [r.pk])
        r = services.salvar_contagem(a.pk, r.pk, 0, "2.375", "", True)
        self.assertEqual(views.payload_cego(r)["embalagens"], "2.375")

    def test_consultas_nao_gravam_nem_conciliam_automaticamente(self):
        self.ok()
        self.sinalizar()
        antes = [list(model.objects.order_by("pk").values()) for model in (Produto, SinalizacaoFisica, MovimentoObservado, EventoInventario)]
        self.client.force_login(self.usuario)
        for nome in ("detalhe", "relatorio"):
            self.assertEqual(self.client.get(reverse("inventario:" + nome, args=[self.inventario.pk])).status_code, 200)
        self.assertEqual(antes, [list(model.objects.order_by("pk").values()) for model in (Produto, SinalizacaoFisica, MovimentoObservado, EventoInventario)])

    def test_endpoint_fisico_nao_devolve_dados_administrativos(self):
        self.client.post(reverse("inventario:convite", args=[self.token]))
        resposta = self.client.post(reverse("inventario:sinalizar", args=[self.rodada.pk]), {"descricao": "Entrada não lançada", "chave": str(uuid4())})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(set(resposta.json()), {"mensagem"})
