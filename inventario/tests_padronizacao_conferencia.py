"""Directed workflow tests; exclusively synthetic data in the guarded runner."""
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase, Client, override_settings
from django.urls import reverse

from estoque.models import Produto, MovimentacaoEstoqueManual
from . import ajustes, contagem_admin, encerramento, services
from .confirmacao_estoque import consultar
from .models import PropostaAjuste, EventoAjuste, MovimentoObservado, ConfirmacaoContagem
from .tests_ajustes import AjustesFixture
from .tests_marco_fisico import Tabela


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class PadronizacaoConferenciaTests(AjustesFixture, TestCase):
    def zero(self):
        self.inv.refresh_from_db()
        r = self.fresca()
        contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk,
            contagem_admin.autorizacao(self.usuario, self.inv, r), '10', '0')

    def modal(self, **dados):
        return self.client.post(reverse('inventario:ajustes_modal', args=[self.inv.pk]), dados)

    def previa(self):
        resposta = self.modal(acao='prever')
        self.assertEqual(resposta.status_code, 200)
        import re
        dados = resposta.json()
        token = re.search(r'name="previa" value="([^"]+)"', dados['html'])
        return dados, token[1] if token else ''

    def aplicar(self, token):
        return self.modal(acao='atualizar', previa=token, confirmacao='sim')

    def test_zero_aguarda_confirmacao_e_depois_confirma_sem_movimento(self):
        self.zero()
        antes = list(ConfirmacaoContagem.objects.values())
        resumo, token = self.previa()
        self.assertEqual(resumo['resumo']['aguardando'], 1)
        self.assertIn('inclusive com diferença zero', resumo['html'])
        self.assertEqual(consultar(self.inv), dict(conferidos=1, confirmados=0, aguardando=1, revisao=0))
        pagina = self.client.get(self.url)
        self.assertContains(pagina, 'Aguardando confirmação')
        self.assertEqual(pagina.context['resumo_confirmacao']['aguardando'], 1)
        resposta = self.aplicar(token)
        self.assertEqual(resposta.status_code, 200, resposta.content)
        self.assertEqual(resposta.json()['resumo']['aplicados'], 1)
        self.assertEqual(consultar(self.inv), dict(conferidos=1, confirmados=1, aguardando=0, revisao=0))
        pagina = self.client.get(self.url)
        self.assertContains(pagina, 'Confirmado')
        self.assertEqual(pagina.context['resumo_confirmacao']['confirmados'], 1)
        self.assertEqual(antes, list(ConfirmacaoContagem.objects.values()))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertEqual(self.aplicar(token).status_code, 200)
        self.assertEqual(EventoAjuste.objects.filter(acao='marco_sem_ajuste').count(), 1)

    def test_lote_exclui_impedido_individualmente_e_confirma_zero(self):
        self.zero()
        impedida = self.segunda()
        Produto.objects.filter(pk=impedida.item.produto_id).update(quantidade=21)
        dados, token = self.previa()
        self.assertEqual(dados['resumo']['aptos'], 1)
        self.assertEqual(dados['resumo']['revisao'], 1)
        self.assertIn(impedida.item.cadastro['nome'], dados['html'])
        self.assertTrue(dados['resumo']['bloqueados'][0]['motivo'])
        self.assertEqual(self.aplicar(token).status_code, 200)
        self.assertFalse(PropostaAjuste.objects.filter(rodada=impedida, estado='aplicado').exists())
        self.assertEqual(consultar(self.inv), dict(conferidos=2, confirmados=1, aguardando=0, revisao=1))

    def test_previa_obsoleta_recusa_lote_sem_confirmacao_parcial(self):
        self.zero()
        outra = self.segunda()
        _, token = self.previa()
        Produto.objects.filter(pk=outra.item.produto_id).update(quantidade=21)
        resposta = self.aplicar(token)
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(PropostaAjuste.objects.filter(estado='aplicado').exists())
        self.assertFalse(EventoAjuste.objects.filter(acao='marco_sem_ajuste').exists())

    def test_rollback_lote_misto_e_reenvio(self):
        self.zero()
        self.segunda()
        _, token = self.previa()
        with patch('inventario.ajustes.conferir_ou_ajustar_estoque', side_effect=ValueError('Falha sintética')):
            self.assertEqual(self.aplicar(token).status_code, 400)
        self.assertFalse(PropostaAjuste.objects.filter(estado='aplicado').exists())
        self.assertFalse(EventoAjuste.objects.filter(acao='marco_sem_ajuste').exists())
        self.assertEqual(self.aplicar(token).status_code, 200)
        self.assertEqual(self.aplicar(token).status_code, 200)
        self.assertEqual(consultar(self.inv)['confirmados'], 2)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)

    def test_encerramento_distingue_marco_pendente_de_diferenca_zero(self):
        self.zero()
        self.inv.refresh_from_db()
        p = encerramento.previa(self.usuario, self.inv)
        self.assertEqual((p['confirmados'], p['aguardando'], p['revisao'], p['diferencas_pendentes']), (0, 1, 0, 0))
        antes = list(Produto.objects.values())
        encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')
        self.assertEqual(antes, list(Produto.objects.values()))
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertTrue(encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')['ja_finalizada'])
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(pagina, 'Referências físicas aguardando confirmação')

    def test_encerramento_revalida_totais_apos_confirmacao_do_marco(self):
        self.zero()
        self.inv.refresh_from_db()
        p = encerramento.previa(self.usuario, self.inv)
        _, token = self.previa()
        self.assertEqual(self.aplicar(token).status_code, 200)
        with self.assertRaisesMessage(ValueError, 'mudou'):
            encerramento.finalizar(self.usuario, self.inv.pk, p['token'], 'sim')

    def test_token_da_tela_encerra_com_confirmado_aguardando_e_revisao_sem_aplicar(self):
        self.zero()
        _, token = self.previa()
        self.assertEqual(self.aplicar(token).status_code, 200)
        self.segunda()
        impedida = self.segunda()
        Produto.objects.filter(pk=impedida.item.produto_id).update(quantidade=21)
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        resumo = pagina.context['resumo_confirmacao']
        self.assertEqual(resumo, dict(conferidos=3, confirmados=1, aguardando=1, revisao=1))
        previa = pagina.context['encerramento_previa']
        self.assertEqual((previa['confirmados'], previa['aguardando'], previa['revisao']), (1, 1, 1))
        antes = list(Produto.objects.values())
        resposta = self.client.post(reverse('inventario:finalizar_administrativamente', args=[self.inv.pk]),
            dict(previa=previa['token'], confirmacao='sim'))
        self.assertEqual(resposta.status_code, 302)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(antes, list(Produto.objects.values()))
        self.assertEqual(PropostaAjuste.objects.filter(estado='aplicado').count(), 1)

    def test_tabela_sete_colunas_contagem_original_e_revisao_sem_erro_de_contagem(self):
        self.zero()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=9)
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(pagina.status_code, 200)
        linha = pagina.context['linhas'][0]
        self.assertEqual(linha['confirmacao_estoque'], 'Revisão necessária')
        self.assertEqual(linha['comparacao_atual']['diferenca'], 1)
        self.assertIsNone(linha['comparacao_operacional']['diferenca'])
        self.assertEqual(linha['diferenca'], 0)
        html = pagina.content.decode()
        contado = html.split('data-label="Contado"', 1)[1].split('</td>', 1)[0]
        from django.utils.html import strip_tags
        self.assertIn(linha['comparacao_atual']['contado_texto'], strip_tags(contado))
        self.assertNotIn('Não comparável', contado)
        self.assertIn('Contagem original no OK', contado)
        tabela = Tabela()
        tabela.feed(html)
        self.assertEqual(tabela.colunas, 7)
        self.assertFalse(tabela.abertos)
        self.assertContains(pagina, 'Confirmação do estoque')

    def test_normal_finaliza_sem_aplicar_e_informa_confirmados_e_pendentes(self):
        r = self.fresca()
        acesso = r.atribuicao.acesso
        self.inv.refresh_from_db()
        services.finalizar_conferencia(acesso.pk, self.inv.versao)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertEqual(consultar(self.inv), dict(conferidos=1, confirmados=0, aguardando=1, revisao=0))
        from .views import estado_conferencia
        estado = estado_conferencia(acesso)
        self.assertEqual(estado['confirmacao_estoque']['aguardando'], 1)

    def test_confirmacao_lote_exige_permissao_csrf_e_flag(self):
        self.zero()
        _, token = self.previa()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(reverse('inventario:ajustes_modal', args=[self.inv.pk]),
            dict(acao='atualizar', previa=token, confirmacao='sim')).status_code, 403)
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=False):
            self.assertEqual(self.aplicar(token).status_code, 400)
        self.usuario.is_superuser = False
        self.usuario.save(update_fields=['is_superuser'])
        self.assertEqual(self.aplicar(token).status_code, 403)
        self.assertFalse(PropostaAjuste.objects.exists())


class ClassificacaoRegistrosTests(SimpleTestCase):
    def linha(self, marco=None, estado='Requer prévia'):
        from types import SimpleNamespace
        return dict(rodada=SimpleNamespace(ok_em=True, encerrada_em=None),
            marco=marco, atualizacao=dict(estado=estado), comparacao_atual=dict(diferenca=0, limite=''))

    def test_47_ok_24_recibos_23_sem_marco_nao_sao_47_confirmados(self):
        from .confirmacao_estoque import classificar
        linhas = [self.linha(marco=object()) for _ in range(24)] + [self.linha() for _ in range(23)]
        self.assertEqual(classificar(linhas), dict(conferidos=47, confirmados=24, aguardando=23, revisao=0))

    def test_estado_aplicado_sem_recibo_nao_comprova_marco(self):
        from .confirmacao_estoque import classificar
        linha = self.linha(estado='Aplicado')
        self.assertEqual(classificar([linha]), dict(conferidos=1, confirmados=0, aguardando=0, revisao=1))
