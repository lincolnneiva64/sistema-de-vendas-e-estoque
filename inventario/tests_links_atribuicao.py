from datetime import timedelta
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario
from . import services
from .models import AcessoInventario, AtribuicaoContagem, ConfirmacaoContagem, EventoInventario
from .tests import produto


class Elementos(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.ids = {}
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids[attrs['id']] = attrs


class LinksAtribuicaoTests(TestCase):
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-links', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Responsável fictício')
        self.produtos = [produto(nome=f'Produto fictício {n}') for n in range(6)]
        self.inv = services.criar_inventario(self.gestor, 'Conferência fictícia', 'manual', [p.pk for p in self.produtos])
        self.rodadas = list(self.inv.itens.order_by('pk').values_list('rodadas__pk', flat=True))
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.client.force_login(self.gestor)

    def atribuir(self):
        return services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, self.rodadas)

    def test_criacao_apresenta_link_sem_redirect_e_sem_persistir_token(self):
        response = self.client.post(self.url, {'acao': 'acesso', 'versao': self.inv.versao, 'funcionario': self.funcionario.pk,
                                             'rodadas': self.rodadas, 'horas': '24'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response)
        self.assertContains(response, 'Atribuição concluída.')
        self.assertContains(response, 'id="compartilhar-acesso" tabindex="-1"')
        self.assertContains(response, 'Copiar link')
        token = response.context['link'].rstrip('/').split('/')[-1]
        acesso = services.acesso_token(token)
        self.assertNotEqual(acesso.token_hash, token)
        self.assertEqual(acesso.atribuicoes.count(), 6)
        self.assertNotContains(self.client.get(self.url), 'id="link-funcionario"')

    def test_renovacao_via_post_preserva_progresso_e_historico(self):
        acesso, antigo = self.atribuir()
        services.salvar_contagem(acesso.pk, self.rodadas[0], 0, '1', '0', True)
        atribuicoes = list(AtribuicaoContagem.objects.values_list('pk', 'rodada_id', 'acesso_id'))
        rodadas = list(self.inv.itens.order_by('pk').values('rodadas__pk', 'rodadas__ok_em', 'rodadas__contado_base', 'rodadas__versao'))
        historico = list(ConfirmacaoContagem.objects.values())
        response = self.client.post(self.url, {'acao': 'renovar', 'acesso': acesso.pk})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response)
        self.assertContains(response, 'Acesso renovado.')
        self.assertContains(response, 'id="link-funcionario"')
        novo = response.context['link'].rstrip('/').split('/')[-1]
        self.assertNotEqual(novo, antigo)
        with self.assertRaises(PermissionDenied):
            services.acesso_token(antigo)
        self.assertEqual(services.acesso_token(novo).pk, acesso.pk)
        self.assertEqual(list(AtribuicaoContagem.objects.values_list('pk', 'rodada_id', 'acesso_id')), atribuicoes)
        self.assertEqual(list(self.inv.itens.order_by('pk').values('rodadas__pk', 'rodadas__ok_em', 'rodadas__contado_base', 'rodadas__versao')), rodadas)
        self.assertEqual(list(ConfirmacaoContagem.objects.values()), historico)
        self.assertEqual(EventoInventario.objects.filter(acao='acesso_renovado').count(), 1)
        self.assertEqual(AcessoInventario.objects.count(), 1)
        for p in self.produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)

    def test_renovacao_de_acesso_expirado(self):
        acesso, _ = self.atribuir()
        AcessoInventario.objects.filter(pk=acesso.pk).update(expira_em=timezone.now() - timedelta(hours=1))
        response = self.client.post(self.url, {'acao': 'renovar', 'acesso': acesso.pk})
        self.assertContains(response, 'Acesso renovado.')
        acesso.refresh_from_db()
        self.assertGreater(acesso.expira_em, timezone.now() + timedelta(hours=23))

    def test_erro_renovacao_visivel_sem_alterar_acesso(self):
        acesso, _ = self.atribuir()
        antes = AcessoInventario.objects.values().get(pk=acesso.pk)
        self.funcionario.ativo = False
        self.funcionario.save(update_fields=['ativo'])
        response = self.client.post(self.url, {'acao': 'renovar', 'acesso': acesso.pk})
        self.assertContains(response, 'Funcionário inativo.')
        self.assertContains(response, 'id="erro-acao" tabindex="-1"')
        self.assertNotContains(response, 'id="link-funcionario"')
        self.assertEqual(AcessoInventario.objects.values().get(pk=acesso.pk), antes)
        self.assertFalse(EventoInventario.objects.filter(acao='acesso_renovado').exists())

    def test_sem_disponiveis_bloqueia_selecao_e_confirmacao(self):
        self.atribuir()
        response = self.client.get(self.url)
        campos = Elementos(response.content.decode()).ids
        self.assertNotIn('selecionar-todos', campos)
        self.assertNotIn('confirmar-atribuicao', campos)
        self.assertContains(response, 'Todos os produtos já estão atribuídos.')
        self.assertContains(response, 'name="acao" value="renovar"')
        self.assertContains(response, 'data-renovar-acesso')

    def test_selecao_vazia_e_validade_invalida_sao_diferentes(self):
        response = self.client.post(self.url, {'acao': 'acesso', 'versao': self.inv.versao, 'funcionario': self.funcionario.pk, 'horas': '24'})
        self.assertContains(response, 'Selecione pelo menos um produto disponível para atribuir.')
        for horas in ('', 'abc', '0', '169', '1.5'):
            with self.subTest(horas=horas):
                response = self.client.post(self.url, {'acao': 'acesso', 'versao': self.inv.versao, 'funcionario': self.funcionario.pk,
                                                     'rodadas': self.rodadas, 'horas': horas})
                self.assertContains(response, 'Informe uma validade inteira entre 1 e 168 horas.')
        self.assertFalse(AcessoInventario.objects.exists())
