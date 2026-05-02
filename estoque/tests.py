from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .forms import FuncionarioForm
from .models import Funcionario


class FuncionarioTests(TestCase):
    def test_exige_whatsapp_para_receber_checklist(self):
        funcionario = Funcionario(
            nome="Joao Entregador",
            pode_receber_checklist=True,
        )

        with self.assertRaises(ValidationError):
            funcionario.full_clean()

    def test_habilitados_para_checklist_filtra_ativos_e_marcados(self):
        habilitado = Funcionario.objects.create(
            nome="Maria Entregadora",
            telefone_whatsapp="85999990000",
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            telefone_whatsapp="85999991111",
            ativo=False,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Checklist",
            telefone_whatsapp="85999992222",
            pode_receber_checklist=False,
        )

        self.assertEqual(list(Funcionario.habilitados_para_checklist()), [habilitado])

    def test_form_valida_whatsapp_quando_checklist_marcado(self):
        form = FuncionarioForm(data={
            "nome": "Carlos Entregador",
            "telefone_whatsapp": "",
            "pode_receber_checklist": "on",
            "ativo": "on",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("telefone_whatsapp", form.errors)

    def test_view_cria_busca_edita_e_alterna_status(self):
        url = reverse("estoque:funcionarios")

        resposta_criar = self.client.post(url, data={
            "nome": "Ana Entregadora",
            "telefone_whatsapp": "85999990000",
            "pode_receber_checklist": "on",
            "ativo": "on",
            "observacoes": "Rota centro",
        }, secure=True)
        self.assertEqual(resposta_criar.status_code, 302)

        funcionario = Funcionario.objects.get(nome="Ana Entregadora")
        self.assertTrue(funcionario.ativo)
        self.assertTrue(funcionario.pode_receber_checklist)
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85999990000")

        resposta_busca = self.client.get(url, {"q": "99999"}, secure=True)
        self.assertContains(resposta_busca, "Ana Entregadora")

        resposta_editar = self.client.post(url, data={
            "funcionario_id": funcionario.id,
            "nome": "Ana Silva",
            "telefone_whatsapp": "85988887777",
            "pode_receber_checklist": "on",
            "ativo": "on",
        }, secure=True)
        self.assertEqual(resposta_editar.status_code, 302)
        funcionario.refresh_from_db()
        self.assertEqual(funcionario.nome, "Ana Silva")
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85988887777")

        resposta_status = self.client.post(url, data={
            "acao": "alternar_status",
            "funcionario_id": funcionario.id,
            "ativo": "0",
        }, secure=True)
        self.assertEqual(resposta_status.status_code, 302)
        funcionario.refresh_from_db()
        self.assertFalse(funcionario.ativo)
        self.assertFalse(funcionario.pode_receber_checklist)
