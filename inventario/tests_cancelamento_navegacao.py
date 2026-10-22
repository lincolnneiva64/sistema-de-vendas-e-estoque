from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Inventario, AcessoInventario, EventoInventario
from .tests_cancelamento import Fixture


class CancelamentoNavegacaoTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.usuario)
        self.lista = reverse('inventario:lista')
        self.destino_cancelada = self.lista
        self.auditoria = reverse('inventario:auditoria_cancelada', args=[self.inv.pk])

    def historico_snapshot(self):
        return (self.snapshot(), list(Inventario.objects.values()),
                list(AcessoInventario.objects.values()), list(EventoInventario.objects.values()))

    def test_cancelamento_redireciona_lista_com_mensagem_sem_criar_conferencia(self):
        self.acesso()
        resposta = self.client.post(self.url, {'confirmacao': 'sim'}, follow=True)
        self.assertEqual(resposta.redirect_chain, [(self.lista, 302)])
        self.assertContains(resposta, f'Conferência nº {self.inv.pk} cancelada.')
        self.assertContains(resposta, 'Histórico preservado e estoque inalterado.')
        self.assertContains(resposta, 'href="?nova=1#nova-conferencia"')
        self.assertEqual(Inventario.objects.count(), 1)
        self.assertEqual(list(resposta.context['em_andamento']), [])
        self.assertEqual(list(resposta.context['finalizados']), [])
        self.assertNotContains(resposta, f'data-conferencia="{self.inv.pk}"')
        self.assertNotContains(resposta, f'href="{self.detalhe}"')
        self.assertNotContains(resposta, '57 produtos nesta conferência')

    def test_url_antiga_redireciona_lista_sem_alterar_registros(self):
        self.acesso(); self.cancelar()
        antes = self.historico_snapshot()
        for suffix in ('', '?auditoria=1', '?atualizar=prever'):
            with self.subTest(suffix=suffix):
                resposta = self.client.get(self.detalhe + suffix, follow=True)
                self.assertEqual(resposta.redirect_chain, [(self.destino_cancelada, 302)])
                self.assertContains(resposta, f'A Conferência nº {self.inv.pk} foi cancelada.')
                self.assertContains(resposta, 'estoque permaneceu inalterado pelo cancelamento')
                self.assertContains(resposta, 'consulta administrativa autorizada')
                self.assertNotContains(resposta, f'data-conferencia="{self.inv.pk}"')
                self.assertNotContains(resposta, 'class="card detalhe-produto"')
        self.assertEqual(antes, self.historico_snapshot())

    def test_auditoria_administrativa_explicita_somente_leitura(self):
        self.acesso(); self.cancelar()
        antes = self.historico_snapshot()
        resposta = self.client.get(self.auditoria)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Consulta administrativa de auditoria')
        self.assertContains(resposta, 'Auditoria da conferência')
        self.assertContains(resposta, 'conferencia_cancelada')
        self.assertNotContains(resposta, '<form')
        self.assertEqual(self.client.post(self.auditoria, {}).status_code, 405)
        self.assertEqual(self.client.post(self.detalhe, {'acao': 'renovar'}).status_code, 403)
        self.assertEqual(antes, self.historico_snapshot())

    def test_consulta_administrativa_usa_permissao_existente(self):
        self.cancelar()
        revisor = get_user_model().objects.create_user('revisor-navegacao')
        revisor.user_permissions.add(Permission.objects.get(content_type__app_label='inventario', codename='revisar_inventario'))
        self.client.force_login(revisor)
        self.assertRedirects(self.client.get(self.detalhe), self.destino_cancelada, fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.auditoria).status_code, 403)
        self.assertNotContains(self.client.get(self.lista), 'Consultar auditoria')
        self.client.logout()
        self.assertNotEqual(self.client.get(self.auditoria).status_code, 200)

    def test_nova_conferencia_abre_formulario_sem_criar_dados(self):
        self.cancelar()
        antes = self.historico_snapshot()
        resposta = self.client.get(self.lista + '?nova=1')
        self.assertContains(resposta, 'id="nova-conferencia" open')
        self.assertEqual(antes, self.historico_snapshot())

    def test_abertas_e_finalizadas_mantem_detalhe_e_link(self):
        for finalizada in (False, True):
            with self.subTest(finalizada=finalizada):
                if finalizada:
                    self.inv.finalizada_em = timezone.now()
                    self.inv.save(update_fields=['finalizada_em'])
                resposta = self.client.get(self.detalhe)
                self.assertEqual(resposta.status_code, 200)
                self.assertContains(self.client.get(self.lista), f'href="{self.detalhe}"')
                self.assertRedirects(self.client.get(self.auditoria), self.detalhe, fetch_redirect_response=False)

    def test_cancelamento_recusado_permanece_no_detalhe(self):
        self.inv.finalizada_em = timezone.now()
        self.inv.save(update_fields=['finalizada_em'])
        resposta = self.client.post(self.url, {'confirmacao': 'sim'}, follow=True)
        self.assertEqual(resposta.redirect_chain, [(self.detalhe, 302)])
        self.assertContains(resposta, 'já finalizada')
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.cancelada_em)

    def test_repeticao_cancelamento_volta_lista_sem_duplicar_evento(self):
        self.cancelar()
        antes = self.historico_snapshot()
        self.assertRedirects(self.client.post(self.url, {'confirmacao': 'sim'}), self.lista)
        self.assertEqual(antes, self.historico_snapshot())

    def test_listas_operacionais_excluem_canceladas_e_separam_ativas_finalizadas(self):
        self.acesso(); self.cancelar()
        ativa = Inventario.objects.create(titulo='Ativa sintética', criterio='manual', criado_por=self.usuario)
        finalizada = Inventario.objects.create(titulo='Finalizada sintética', criterio='manual',
            criado_por=self.usuario, finalizada_em=timezone.now())
        for cancelada_com_finalizacao in (False, True):
            with self.subTest(cancelada_com_finalizacao=cancelada_com_finalizacao):
                if cancelada_com_finalizacao:
                    Inventario.objects.filter(pk=self.inv.pk).update(finalizada_em=timezone.now())
                antes = self.historico_snapshot()
                for query in ('', '?nova=1'):
                    resposta = self.client.get(self.lista + query)
                    self.assertEqual([i.pk for i in resposta.context['em_andamento']], [ativa.pk])
                    self.assertEqual([i.pk for i in resposta.context['finalizados']], [finalizada.pk])
                    self.assertNotContains(resposta, f'data-conferencia="{self.inv.pk}"')
                    self.assertNotContains(resposta, self.inv.titulo)
                    self.assertNotContains(resposta, f'href="{self.auditoria}"')
                self.assertEqual(antes, self.historico_snapshot())
                self.assertEqual(self.client.get(self.auditoria).status_code, 200)
