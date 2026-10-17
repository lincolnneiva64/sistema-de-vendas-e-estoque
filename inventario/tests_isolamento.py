from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from estoque.models import Funcionario, MovimentacaoEstoqueManual
from . import services
from .models import MovimentoObservado
from .tests import produto


class IsolamentoTests(TestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser("gestor-isolamento", "", "teste")
        self.produto = produto()
        self.funcionario = Funcionario.objects.create(nome="Operador fictício", pode_operar_sistema=True)
        self.inventario = services.criar_inventario(self.gestor, "Isolamento", "manual", [self.produto.pk])
        self.rodada = self.inventario.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inventario.pk, self.funcionario.pk, [self.rodada.pk])

    def test_sessao_limitada_bloqueia_todos_endpoints_comerciais_e_admin(self):
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.post(reverse("inventario:convite", args=[self.token])).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        urls = [reverse("estoque:conferencia_estoque_produto", args=[self.produto.pk]),
            reverse("estoque:conferencia_estoque_confirmar", args=[self.produto.pk]),
            reverse("estoque:conferencia_estoque_corrigir", args=[self.produto.pk]),
            "/vendas/", "/verificar-produto/", "/api/offline/snapshot/comercial/",
            "/vendas/gravar/", "/compras/nova/", "/admin/",
            reverse("estoque:conferencia_estoque_autorizar"),
            reverse("estoque:conferencia_estoque_autorizada"),
            reverse("inventario:detalhe", args=[self.inventario.pk]),
            reverse("inventario:relatorio", args=[self.inventario.pk])]
        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    resposta = getattr(self.client, method)(url, {"operador": self.funcionario.nome,
                        "novo_estoque": "99", "motivo": "Não deve executar"}, HTTP_X_OPERADOR_VENDA=self.funcionario.nome)
                    self.assertEqual(resposta.status_code, 403)
                    self.assertNotIn("esperado", resposta.content.decode())
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())

    def test_vendas_sem_login_continua_disponivel_fora_da_sessao_limitada(self):
        self.assertEqual(Client().get("/vendas/").status_code, 200)

    def test_revogacao_nao_devolve_acesso_comercial_a_sessao_limitada(self):
        self.client.post(reverse("inventario:convite", args=[self.token]))
        services.revogar_acesso(self.gestor, self.acesso.pk)
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 403)
        self.assertEqual(self.client.get("/vendas/").status_code, 403)
