"""Only closing workflows; synthetic data in the guarded PostgreSQL runner."""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Produto, MovimentacaoEstoqueManual
from . import ajustes, encerramento, services, contagem_admin
from .models import (Inventario, RodadaContagem, ConfirmacaoContagem, DecisaoAjuste,
    AplicacaoAjuste, EventoAjuste, EventoInventario, MovimentoObservado)
from .tests_ajustes import AjustesFixture
from .tests_marco_fisico import Operacoes


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class EncerramentoPendenciasTests(Operacoes, AjustesFixture, TestCase):
    def ambigua(self, rodada=None):
        r = self.fresca((rodada or self.rodada).pk)
        original = r.confirmacoes.order_by('-versao_entrada').first()
        c = ConfirmacaoContagem.objects.create(rodada=r, versao_entrada=r.versao,
            embalagens=r.embalagens, avulsos=r.avulsos, contado=r.contado,
            contado_base=r.contado_base, esperado=r.esperado, sequencia_ok=r.sequencia_ok,
            ok_em=timezone.now(), acesso=None, responsavel_nome=self.usuario.username)
        r.ok_em = c.ok_em
        r.versao += 1
        r.save()
        EventoInventario.objects.create(inventario=self.inv, rodada=r, usuario=self.usuario,
            acao=contagem_admin.ACAO, dados=dict(confirmacao_id=c.pk, confirmacao_anterior_id=original.pk))
        return r

    def preservado(self):
        return [list(m.objects.order_by('pk').values()) for m in (
            Produto, RodadaContagem, ConfirmacaoContagem, DecisaoAjuste,
            AplicacaoAjuste, EventoAjuste, MovimentoObservado, MovimentacaoEstoqueManual)]

    def previa(self):
        self.inv.refresh_from_db()
        return encerramento.previa(self.usuario, self.inv)

    def test_formulario_reaparece_para_confirmacao_administrativa_antiga_ambigua(self):
        r = self.ambigua()
        self.assertEqual(ajustes.avaliar(self.fresca())['estado'], 'Bloqueado')
        antes = self.preservado()
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(pagina.status_code, 200)
        p = pagina.context['encerramento_previa']
        self.assertIsNotNone(p)
        self.assertEqual((p['conferidos'], p['confirmados'], p['revisao']), (1, 0, 1))
        self.assertEqual(len(p['pendencias']), 1)
        self.assertEqual(p['pendencias'][0]['rodada'], r.pk)
        self.assertIn('administrativa', p['pendencias'][0]['motivo'])
        self.assertContains(pagina, 'Encerrar administrativamente com pendências')
        self.assertContains(pagina, 'name="confirmacao" value="sim" required')
        self.assertContains(pagina, 'sem resolução')
        self.assertEqual(antes, self.preservado())

    def test_encerra_com_ambiguidade_preserva_dados_motivo_e_bloqueio_de_aplicacao(self):
        r = self.ambigua()
        bloqueio = ajustes.avaliar(self.fresca())['bloqueio']
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        p = pagina.context['encerramento_previa']
        self.assertIsNotNone(p)
        antes = self.preservado()
        with patch('inventario.ajustes.atualizar', side_effect=AssertionError('Não aplicar no encerramento')):
            resposta = self.client.post(reverse('inventario:finalizar_administrativamente', args=[self.inv.pk]),
                dict(previa=p['token'], confirmacao='sim'))
        self.assertRedirects(resposta, reverse('inventario:lista'))
        self.assertEqual(antes, self.preservado())
        self.assertEqual(ajustes.avaliar(self.fresca())['bloqueio'], bloqueio)
        self.assertFalse(ajustes.preparar_resultado(self.inv)['aptas'])
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        evento = self.inv.eventos.get(acao='conferencia_finalizada')
        pendencia = evento.dados['pendencias'][0]
        self.assertEqual(pendencia['confirmacao'], r.confirmacoes.order_by('-versao_entrada').first().pk)
        self.assertEqual(pendencia['unidade'], r.item.cadastro['base'])
        self.assertEqual(pendencia['ok_em'], r.ok_em.isoformat())
        self.assertEqual(pendencia['motivo'], p['pendencias'][0]['motivo'])
        self.assertFalse(evento.dados['estoque_alterado'])
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(pagina, 'Pendências preservadas no encerramento administrativo')
        self.assertContains(pagina, pendencia['motivo'])
        self.assertTrue(encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')['ja_finalizada'])
        self.assertEqual(self.inv.eventos.filter(acao='conferencia_finalizada').count(), 1)
        self.assertEqual(antes, self.preservado())

    def test_sem_checkbox_nao_encerra_mesmo_com_previa_valida(self):
        self.ambigua()
        p = self.previa()
        antes = self.preservado()
        url = reverse('inventario:finalizar_administrativamente', args=[self.inv.pk])
        resposta = self.client.post(url, dict(previa=p['token']))
        self.assertEqual(resposta.status_code, 302)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)
        self.assertEqual(antes, self.preservado())
        self.assertFalse(self.inv.eventos.filter(acao='conferencia_finalizada').exists())

    def test_misto_mostra_aplicado_aguardando_e_revisao_individualmente(self):
        self.aplicar_marco()
        ambigua = self.segunda()
        self.ambigua(ambigua)
        aguardando = self.segunda()
        p = self.previa()
        self.assertEqual((p['conferidos'], p['confirmados'], p['aguardando'], p['revisao']), (3, 1, 1, 1))
        self.assertEqual({x['rodada'] for x in p['pendencias']}, {ambigua.pk, aguardando.pk})
        self.assertTrue(all(x['motivo'] for x in p['pendencias']))
        antes = self.preservado()
        encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')
        self.assertEqual(antes, self.preservado())
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)

    def test_falha_na_auditoria_reverte_somente_o_encerramento(self):
        self.ambigua()
        p = self.previa()
        antes = self.preservado()
        inv = Inventario.objects.values().get(pk=self.inv.pk)
        with patch('inventario.encerramento.services.evento', side_effect=RuntimeError('Falha fictícia')):
            with self.assertRaises(RuntimeError):
                encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')
        self.assertEqual(inv, Inventario.objects.values().get(pk=self.inv.pk))
        self.assertEqual(antes, self.preservado())

    def test_permissao_csrf_e_token_obsoleto_continuam_obrigatorios(self):
        from django.contrib.auth import get_user_model
        from django.core.exceptions import PermissionDenied
        self.ambigua()
        p = self.previa()
        antes = self.preservado()
        outro = get_user_model().objects.create_user('sem-autorizacao')
        with self.assertRaises(PermissionDenied):
            encerramento.finalizar(outro, self.inv.pk, p['token'], 'sim')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(reverse('inventario:finalizar_administrativamente', args=[self.inv.pk]),
            dict(previa=p['token'], confirmacao='sim')).status_code, 403)
        Inventario.objects.filter(pk=self.inv.pk).update(versao=self.inv.versao + 1)
        with self.assertRaisesMessage(ValueError, 'mudou'):
            encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')
        self.assertEqual(antes, self.preservado())

    def test_encerramento_normal_com_pendencia_nao_aplica_ajustes(self):
        self.ambigua()
        acesso = self.fresca().atribuicao.acesso
        self.inv.refresh_from_db()
        antes = self.preservado()
        services.finalizar_conferencia(acesso.pk, self.inv.versao)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(antes, self.preservado())
        self.assertEqual(ajustes.avaliar(self.fresca())['estado'], 'Bloqueado')

    def test_sem_ok_nao_existe_atalho_para_encerrar(self):
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(ok_em=None)
        antes = self.preservado()
        with self.assertRaises(ValueError):
            self.previa()
        self.assertEqual(antes, self.preservado())
