from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Inventario
from .services import criar_inventario
from .tests import produto


class ExclusaoPilotoTests(TestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser("gestor-piloto", "", "teste")
        self.excluido = produto(nome="Produto fora do piloto")
        self.valido = produto(nome="Produto liberado no piloto")
        config = override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=(self.excluido.pk,))
        config.enable()
        self.addCleanup(config.disable)

    def test_categoria_exclui_produto_sem_alterar_cadastro_ou_saldo(self):
        antes = type(self.excluido).objects.filter(pk=self.excluido.pk).values().get()
        inventario = criar_inventario(self.gestor, "Piloto", "categoria", self.valido.categoria)
        self.assertEqual(list(inventario.itens.values_list("produto_id", flat=True)), [self.valido.pk])
        self.assertEqual(type(self.excluido).objects.filter(pk=self.excluido.pk).values().get(), antes)

    def test_requisicao_manual_nao_contorna_exclusao(self):
        with self.assertRaisesMessage(ValueError, "indisponíveis"):
            criar_inventario(self.gestor, "Piloto", "manual", [self.excluido.pk, self.valido.pk])
        self.assertFalse(Inventario.objects.exists())

    def test_interface_nao_oferece_produto_excluido(self):
        self.client.force_login(self.gestor)
        response = self.client.get(reverse("inventario:lista"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.excluido.nome)
        self.assertContains(response, self.valido.nome)
