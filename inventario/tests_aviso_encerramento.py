"""Opening an unfinished count must not look like a refused closure."""
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario, Produto, MovimentacaoEstoqueManual
from . import encerramento, services
from .models import RodadaContagem
from .tests import produto


@override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class AvisoEncerramentoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('gestor-aviso', '', 'teste')
        produtos = [produto(nome=f'Produto sintético {i}') for i in range(2)]
        self.inv = services.criar_inventario(self.usuario, 'Contagem sintética', 'manual',
                                             [p.pk for p in produtos])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk'))
        funcionario = Funcionario.objects.create(nome='Contador sintético')
        self.acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, funcionario.pk,
                                             [r.pk for r in self.rodadas])
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.client.force_login(self.usuario)

    def confirmar(self, rodada):
        services.salvar_contagem(self.acesso.pk, rodada.pk, rodada.versao, '8', '0', True)

    def estado(self):
        modelos = [*apps.get_app_config('inventario').get_models(), Produto, MovimentacaoEstoqueManual]
        return {m._meta.label: list(m.objects.order_by('pk').values()) for m in modelos}

    def abrir_sem_previa(self):
        antes = self.estado()
        with patch.object(encerramento, 'previa', wraps=encerramento.previa) as previa, \
                CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        previa.assert_not_called()
        self.assertIsNone(resposta.context['encerramento_previa'])
        self.assertEqual(resposta.context['encerramento_erro'], '')
        self.assertNotContains(resposta, 'encerramento-impedido')
        self.assertNotContains(resposta, 'O encerramento foi recusado')
        self.assertContains(resposta, 'Aguardando envio das atribuições')
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))
        self.assertEqual(antes, self.estado())

    def test_conferencia_nova_ciclo_1_sem_aviso_ou_previa(self):
        self.assertEqual(self.inv.ciclo, 1)
        self.abrir_sem_previa()

    def test_contagem_parcial_sem_aviso_ou_previa(self):
        self.confirmar(self.rodadas[0])
        self.abrir_sem_previa()

    def test_todos_oks_validos_preparam_encerramento_sem_alterar_registros(self):
        for rodada in self.rodadas:
            self.confirmar(rodada)
        antes = self.estado()
        with patch.object(encerramento, 'previa', wraps=encerramento.previa) as previa:
            resposta = self.client.get(self.url)
        previa.assert_called_once()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context['encerramento_previa']['total'], 2)
        self.assertTrue(resposta.context['encerramento_previa']['token'])
        self.assertEqual(resposta.context['encerramento_erro'], '')
        self.assertContains(resposta, 'id="encerramento-administrativo"')
        self.assertEqual(antes, self.estado())

    def test_post_real_recusa_ok_removido_apos_previa_sem_alterar_registros(self):
        for rodada in self.rodadas:
            self.confirmar(rodada)
        previa = self.client.get(self.url).context['encerramento_previa']['token']
        # Synthetic stale preview: exercise the real POST and resumo validation.
        RodadaContagem.objects.filter(pk=self.rodadas[0].pk).update(ok_em=None)
        antes = self.estado()
        with patch.object(encerramento, 'resumo', wraps=encerramento.resumo) as resumo:
            resposta = self.client.post(reverse('inventario:finalizar_administrativamente',
                args=[self.inv.pk]), {'previa': previa, 'confirmacao': 'sim'})
        resumo.assert_called_once()
        self.assertRedirects(resposta, self.url, fetch_redirect_response=False)
        self.assertTrue(any('falta OK valido' in str(m) for m in resposta.wsgi_request._messages))
        self.assertEqual(antes, self.estado())

    def test_recontagem_pendente_nao_usa_ok_da_rodada_anterior(self):
        for rodada in self.rodadas:
            self.confirmar(rodada)
        services.recontar(self.usuario, self.rodadas[0].pk, 'Recontagem sintética')
        self.abrir_sem_previa()

    def test_oks_presentes_nao_ocultam_contagem_inconsistente(self):
        for rodada in self.rodadas:
            self.confirmar(rodada)
        RodadaContagem.objects.filter(pk=self.rodadas[0].pk).update(contado_base=7)
        antes = self.estado()
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.context['encerramento_previa'])
        self.assertIn('contagem inconsistente', resposta.context['encerramento_erro'])
        self.assertContains(resposta, 'encerramento-impedido')
        self.assertEqual(antes, self.estado())
