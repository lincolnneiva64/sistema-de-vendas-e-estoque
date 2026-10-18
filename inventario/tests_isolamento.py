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

    def test_contagem_preserva_login_comercial_sem_liberar_administracao_inventario(self):
        self.client.force_login(self.gestor)
        sessao_antes = self.client.session.session_key
        self.assertEqual(self.client.post(reverse("inventario:convite", args=[self.token])).status_code, 302)
        self.assertEqual(self.client.session.session_key, sessao_antes)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.gestor.pk)
        urls = [reverse("inventario:lista"), reverse("inventario:detalhe", args=[self.inventario.pk]),
            reverse("inventario:relatorio", args=[self.inventario.pk])]
        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    resposta = getattr(self.client, method)(url, {"operador": self.funcionario.nome,
                        "novo_estoque": "99", "motivo": "Não deve executar"}, HTTP_X_OPERADOR_VENDA=self.funcionario.nome)
                    self.assertEqual(resposta.status_code, 403)
                    self.assertNotIn("esperado", resposta.content.decode())
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 200)
        for url in ("/vendas/", "/compras/", "/", "/locacoes/"):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())

    def test_vendas_sem_login_continua_disponivel_fora_da_sessao_limitada(self):
        self.assertEqual(Client().get("/vendas/").status_code, 200)

    def test_revogacao_bloqueia_contagem_sem_bloquear_vendas(self):
        self.client.post(reverse("inventario:convite", args=[self.token]))
        services.revogar_acesso(self.gestor, self.acesso.pk)
        self.assertEqual(self.client.get(reverse("inventario:mobile")).status_code, 403)
        self.assertEqual(self.client.get("/vendas/").status_code, 200)
