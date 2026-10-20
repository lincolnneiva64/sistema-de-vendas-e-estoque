from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from estoque.models import Funcionario, MovimentacaoEstoqueManual
from .tests import produto
from .models import Inventario, RodadaContagem, ConfirmacaoContagem, EventoInventario, DecisaoAjuste
from . import services


class DistribuicaoTests(TestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-distribuicao', '', 'teste')
        self.ana = Funcionario.objects.create(nome='Ana', telefone_whatsapp='11999999999')
        self.bruno = Funcionario.objects.create(nome='Bruno')
        self.produtos = [produto(nome=nome, unidade_compra='UN', vende_fracionado=False) for nome in ('Arroz', 'Feijao')]
        self.inv = services.criar_inventario(self.gestor, 'Deposito', 'manual', [p.pk for p in self.produtos])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk'))

    def atribuir(self, funcionario, rodadas):
        return services.gerar_acesso(self.gestor, self.inv.pk, funcionario.pk, [r.pk for r in rodadas])

    def enviar(self, acesso):
        self.inv.refresh_from_db()
        return services.finalizar_conferencia(acesso.pk, self.inv.versao)

    def test_criacao_nao_cadastra_nem_atribui_funcionario_automaticamente(self):
        self.assertEqual(Funcionario.objects.count(), 2)
        self.assertFalse(self.inv.acessos.exists())

    def test_divisao_envios_correcoes_e_conclusao_sem_ajustes(self):
        a, _ = self.atribuir(self.ana, self.rodadas[:1])
        with self.assertRaises(ValueError):
            self.atribuir(self.bruno, self.rodadas)
        self.assertEqual(self.inv.acessos.count(), 1)
        b, _ = self.atribuir(self.bruno, self.rodadas[1:])
        with self.assertRaises(PermissionDenied):
            services.salvar_contagem(a.pk, self.rodadas[1].pk, 0, '8', '', True)
        services.salvar_contagem(a.pk, self.rodadas[0].pk, 0, '6', '', True)
        services.salvar_contagem(a.pk, self.rodadas[0].pk, 1, '8', '', True)
        self.assertIsNone(self.enviar(a).finalizada_em)
        with self.assertRaises(ValueError):
            self.enviar(b)
        with self.assertRaises(ValueError):
            services.salvar_contagem(a.pk, self.rodadas[0].pk, 2, '9', '', True)
        services.salvar_contagem(b.pk, self.rodadas[1].pk, 0, '9', '', True)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)  # OK alone never submits another assignment.
        self.assertIsNotNone(self.enviar(b).finalizada_em)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 3)
        self.assertEqual(EventoInventario.objects.filter(acao='atribuicao_enviada').count(), 2)
        self.assertFalse(DecisaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        for p in self.produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)

    def test_produto_sem_atribuicao_impede_conclusao_geral(self):
        a, _ = self.atribuir(self.ana, self.rodadas[:1])
        services.salvar_contagem(a.pk, self.rodadas[0].pk, 0, '8', '', True)
        self.assertIsNone(self.enviar(a).finalizada_em)
        b, _ = self.atribuir(self.bruno, self.rodadas[1:])
        services.salvar_contagem(b.pk, self.rodadas[1].pk, 0, '9', '', True)
        self.assertIsNotNone(self.enviar(b).finalizada_em)

    def test_link_compartilhavel_e_renovacao_invalida_token_antigo(self):
        self.client.force_login(self.gestor)
        url = reverse('inventario:detalhe', args=[self.inv.pk])
        resposta = self.client.post(url, {'acao': 'acesso', 'versao': self.inv.versao, 'funcionario': self.ana.pk,
            'horas': 24, 'rodadas': [r.pk for r in self.rodadas]})
        self.assertContains(resposta, 'Copiar link')
        self.assertContains(resposta, 'Enviar pelo WhatsApp')
        self.assertIn('phone=5511999999999', resposta.context['whatsapp_url'])
        token = resposta.context['link'].rstrip('/').split('/')[-1]
        acesso = services.acesso_token(token)
        _, novo = services.renovar_acesso(self.gestor, acesso.pk)
        with self.assertRaises(PermissionDenied):
            services.acesso_token(token)
        services.acesso_token(novo)
        services.revogar_acesso(self.gestor, acesso.pk)
        with self.assertRaises(PermissionDenied):
            services.acesso_token(novo)
