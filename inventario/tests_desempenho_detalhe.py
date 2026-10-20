"""Read-only detail projection and explicit documentary preview; fictitious data."""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import ajustes, services, views
from .models import AplicacaoAjuste, RodadaContagem
from .tests_ajustes import AjustesFixture
from .tests_conciliacao_documental import ConciliacaoDocumentalTests


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class DetalheLeveTests(AjustesFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])

    def test_get_comum_nao_prepara_concilia_assina_nem_escreve(self):
        # Establish the session first; no permission/session cache writes may
        # disguise a stock or audit write in the actual request under test.
        with patch.object(ajustes, 'preparar_resultado', side_effect=AssertionError('Heavy GET')), \
                patch('inventario.conciliacao_documental.conciliar', side_effect=AssertionError('Documentary GET')), \
                patch.object(ajustes, 'token', side_effect=AssertionError('Signed GET')), \
                CaptureQueriesContext(connection) as queries:
            resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertContains(resposta, 'Requer prévia')
        self.assertNotContains(resposta, 'Apto para atualizar')
        self.assertFalse(resposta.context['previa_lote'])
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_projecao_duas_consultas_sem_json_de_evidencia(self):
        self.segunda()
        linhas = views.linhas_revisao(self.inv)
        with CaptureQueriesContext(connection) as queries:
            resultado = ajustes.projetar_resultado(linhas)
        self.assertEqual(len(queries), 2)
        self.assertEqual(resultado['resumo']['candidatos'], 2)
        self.assertFalse(resultado['aptas'])
        self.assertTrue(all('"evidencia"' not in q['sql'] for q in queries))

    def test_previa_descobre_bloqueio_documental_e_nao_permite_confirmar(self):
        ConciliacaoDocumentalTests.vender(self, quantidade=1)
        from estoque.models import EventoVenda
        from estoque.models import Venda
        EventoVenda.objects.create(venda=Venda.objects.latest('pk'),
            tipo_evento='edicao_desconhecida', descricao='Alteracao ficticia sem evidencia')
        inicial = self.client.get(self.url)
        self.assertEqual(inicial.context['atualizacao']['resumo']['candidatos'], 1)
        self.assertNotContains(inicial, 'Apto para atualizar')
        from .conciliacao_documental import conciliar
        with patch('inventario.conciliacao_documental.conciliar', wraps=conciliar) as completa:
            previa = self.client.get(self.url, {'atualizar': 'prever'})
        self.assertTrue(completa.called)
        self.assertEqual(previa.context['atualizacao']['resumo']['bloqueados'], 1)
        motivo = previa.context['atualizacao']['linhas'][0]['bloqueio']
        self.assertTrue(motivo)
        self.assertContains(previa, motivo)
        self.assertNotContains(previa, 'id="confirmar-atualizacao-resultado"')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_previa_exige_permissao_mesmo_com_parametro(self):
        usuario = get_user_model().objects.create_user('somente-leitura')
        usuario.user_permissions.add(Permission.objects.get(
            content_type__app_label='inventario', codename='revisar_inventario'))
        self.client.force_login(usuario)
        with patch.object(ajustes, 'preparar_resultado', side_effect=AssertionError('Unauthorized preview')):
            resposta = self.client.get(self.url, {'atualizar': 'prever'})
        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, 'id="revisar-atualizacao"')
        self.assertFalse(resposta.context['previa_lote'])

    def test_finalizada_com_aplicado_bloqueado_e_pendente_preserva_saldos(self):
        previa = self.client.get(self.url, {'atualizar': 'prever'}).context['previa_lote']
        ajustes.atualizar(self.usuario, self.inv.pk, previa, confirmacao=True)
        bloqueada = self.segunda()
        services.sinalizar_fisico(bloqueada.pk, 'Evidencia fisica pendente',
            str(uuid4()), usuario=self.usuario)
        pendente = self.segunda()
        self.inv.refresh_from_db()
        for acesso in self.inv.acessos.all():
            self.inv.refresh_from_db()
            services.finalizar_conferencia(acesso.pk, self.inv.versao)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        with patch.object(ajustes, 'preparar_resultado', side_effect=AssertionError('Historical GET')):
            pagina = self.client.get(self.url)
        linhas = {l['rodada'].pk: l for l in pagina.context['atualizacao']['linhas']}
        self.assertEqual(linhas[self.rodada.pk]['saldo_aplicado'], 8)
        self.assertEqual(linhas[bloqueada.pk]['estado'], 'Bloqueado')
        self.assertEqual(linhas[pendente.pk]['estado'], 'Requer prévia')
        self.assertContains(pagina, 'Saldo registrado: 8 CX')
        self.assertEqual(pagina.context['atualizacao']['resumo']['aplicados'], 1)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)

    def test_bloqueio_local_sem_confirmacao_original_nao_oferece_atualizacao(self):
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(contado_base=7)
        pagina = self.client.get(self.url)
        self.assertEqual(pagina.context['atualizacao']['resumo']['bloqueados'], 1)
        self.assertNotContains(pagina, 'id="revisar-atualizacao"')
