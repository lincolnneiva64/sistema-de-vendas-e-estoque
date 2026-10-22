from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connections, connection
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario, Fornecedor, ProdutoFornecedor, MovimentacaoEstoqueManual
from . import services, ajustes
from .models import (Inventario, RodadaContagem, ConfirmacaoContagem, AtribuicaoContagem,
                     EventoInventario, PropostaAjuste, DecisaoAjuste, MovimentoObservado, ItemInventario)
from .selecao import salvar_selecao
from .tests import produto


class Fixture:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('gestor-cancelamento', '', 'teste')
        self.produto = produto()
        self.fornecedor = Fornecedor.objects.create(nome='Fornecedor sintetico cancelamento')
        ProdutoFornecedor.objects.create(produto=self.produto, fornecedor=self.fornecedor)
        self.inv = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk)
        self.rodada = RodadaContagem.objects.get(item__inventario=self.inv)
        self.funcionario = Funcionario.objects.create(nome='Funcionario sintetico')
        self.url = reverse('inventario:cancelar', args=[self.inv.pk])
        self.detalhe = reverse('inventario:detalhe', args=[self.inv.pk])

    def acesso(self):
        return services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodada.pk])

    def cancelar(self):
        return services.cancelar_conferencia(self.usuario, self.inv.pk)

    def snapshot(self):
        return {m.__name__: list(m.objects.order_by('pk').values()) for m in (
            ItemInventario, RodadaContagem, ConfirmacaoContagem, AtribuicaoContagem, PropostaAjuste,
            DecisaoAjuste, MovimentacaoEstoqueManual, MovimentoObservado)}


@override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class CancelamentoTests(Fixture, TestCase):
    def test_sem_contagens_preserva_saldos_custos_e_historico(self):
        antes = self.snapshot()
        produto_antes = self.produto.__class__.objects.values().get(pk=self.produto.pk)
        self.cancelar()
        self.assertEqual(antes, self.snapshot())
        self.assertEqual(produto_antes, self.produto.__class__.objects.values().get(pk=self.produto.pk))
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.cancelada_em)
        self.assertIsNone(self.inv.finalizada_em)
        self.assertEqual(self.inv.cancelada_por, self.usuario)
        ev = self.inv.eventos.get(acao='conferencia_cancelada')
        self.assertEqual(ev.usuario, self.usuario)
        self.assertEqual(ev.dados['situacao_anterior'], 'em_andamento')
        self.assertEqual(ev.dados['acessos_revogados'], 0)

    def test_rascunho_e_confirmacao_antiga_preservados(self):
        acesso, _ = self.acesso()
        services.salvar_contagem(acesso.pk, self.rodada.pk, 0, '2', '0', True)
        services.salvar_contagem(acesso.pk, self.rodada.pk, 1, '3', '0')
        antes = self.snapshot()
        eventos = list(self.inv.eventos.values())
        self.cancelar()
        self.assertEqual(antes, self.snapshot())
        self.assertEqual(eventos, list(self.inv.eventos.exclude(acao='conferencia_cancelada').values()))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)

    def test_links_revogados_inclusive_sessao_aberta(self):
        acesso, token = self.acesso()
        celular = Client()
        convite = reverse('inventario:convite', args=[token])
        self.assertEqual(celular.post(convite).status_code, 302)
        self.cancelar()
        acesso.refresh_from_db()
        self.assertIsNotNone(acesso.revogado_em)
        with self.assertRaises(PermissionDenied): services.acesso_token(token)
        with self.assertRaises(PermissionDenied): services.salvar_contagem(acesso.pk, self.rodada.pk, 0, '2', '0', True)
        with self.assertRaises(PermissionDenied): services.finalizar_conferencia(acesso.pk, 0)
        for url in [reverse('inventario:mobile'), convite]:
            self.assertNotEqual(celular.get(url).status_code, 200)
        self.assertIn(celular.post(reverse('inventario:contar', args=[self.rodada.pk]), {}).status_code, (400, 403))

    def test_todos_acessos_revogados_e_idempotencia(self):
        a, _ = self.acesso()
        services.recontar(self.usuario, self.rodada.pk, 'Nova rodada sintetica')
        nova = RodadaContagem.objects.order_by('-pk').first()
        b, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [nova.pk])
        services.revogar_acesso(self.usuario, a.pk)
        primeiro = self.cancelar()
        segundo = self.cancelar()
        self.assertEqual(primeiro.cancelada_em, segundo.cancelada_em)
        self.assertFalse(self.inv.acessos.filter(revogado_em__isnull=True).exists())
        self.assertEqual(self.inv.eventos.filter(acao='conferencia_cancelada').count(), 1)
        self.assertEqual(self.inv.eventos.get(acao='conferencia_cancelada').dados['acessos_revogados'], 1)

    def test_rollback_se_auditoria_falhar(self):
        acesso, _ = self.acesso()
        with patch('inventario.services.evento', side_effect=RuntimeError('falha sintetica')):
            with self.assertRaises(RuntimeError): self.cancelar()
        self.inv.refresh_from_db(); acesso.refresh_from_db()
        self.assertIsNone(self.inv.cancelada_em)
        self.assertIsNone(acesso.revogado_em)
        self.assertFalse(self.inv.eventos.filter(acao='conferencia_cancelada').exists())

    def test_ajuste_aplicado_bloqueia_sem_reverter(self):
        acesso, _ = self.acesso()
        services.salvar_contagem(acesso.pk, self.rodada.pk, 0, '2', '0', True)
        confirmacao = ConfirmacaoContagem.objects.get()
        PropostaAjuste.objects.create(rodada=self.rodada, confirmacao=confirmacao,
            assinatura='a'*64, evidencia={}, justificativa='sintetico', estado='aplicado', autorizado_por=self.usuario)
        antes = self.snapshot()
        with self.assertRaisesMessage(ValueError, 'aplicados'): self.cancelar()
        self.assertEqual(antes, self.snapshot())
        self.inv.refresh_from_db(); acesso.refresh_from_db()
        self.assertIsNone(self.inv.cancelada_em); self.assertIsNone(acesso.revogado_em)

    def test_bloqueio_de_servicos_e_urls_administrativos(self):
        acesso, _ = self.acesso()
        self.cancelar()
        for operacao in [
            lambda: services.renovar_acesso(self.usuario, acesso.pk),
            lambda: services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodada.pk]),
            lambda: services.recontar(self.usuario, self.rodada.pk, 'sintetico'),
            lambda: services.reabrir_conferencia(self.usuario, self.inv.pk, 'sintetico'),
            lambda: services.conciliar(self.usuario, self.rodada.pk, 1, 'sintetico'),
            lambda: services.revisar(self.usuario, self.rodada.pk, 'sintetico'),
            lambda: services.sinalizar_fisico(self.rodada.pk, 'sintetico', uuid4(), usuario=self.usuario),
            lambda: salvar_selecao(self.usuario, self.inv.pk, [self.rodada.item_id], 1, 'sintetico'),
        ]:
            with self.subTest(operacao=operacao):
                with self.assertRaises(PermissionDenied): operacao()
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(PermissionDenied): ajustes.bloquear(self.inv.pk, [self.rodada.pk])
        self.client.force_login(self.usuario)
        self.assertEqual(self.client.post(self.detalhe, {'acao': 'renovar', 'acesso': acesso.pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse('inventario:ajustes', args=[self.inv.pk])).status_code, 403)

    def test_permissoes_post_csrf_e_confirmacao(self):
        self.client.force_login(self.usuario)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'confirmacao':'sim'}).status_code, 401)
        usuario = get_user_model().objects.create_user('sem-permissao')
        self.client.force_login(usuario)
        self.assertEqual(self.client.post(self.url, {'confirmacao':'sim'}).status_code, 403)
        self.client.force_login(self.usuario)
        self.assertEqual(self.client.post(self.url, {}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(self.url, {'confirmacao':'sim'}).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'confirmacao':'sim'}).status_code, 302)

    def test_historico_consulta_sem_formularios_e_nova_independente(self):
        self.acesso(); self.cancelar(); self.client.force_login(self.usuario)
        resposta = self.client.get(self.detalhe)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Cancelada em')
        self.assertNotContains(resposta, '<form')
        lista = self.client.get(reverse('inventario:lista'))
        self.assertNotIn(self.inv.pk, [i.pk for i in lista.context['em_andamento']])
        self.assertIn(self.inv.pk, [i.pk for i in lista.context['finalizados']])
        self.assertEqual(lista.context['resumo']['conferencias'], 0)
        nova = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk)
        self.assertNotEqual(nova.pk, self.inv.pk)
        self.assertFalse(nova.acessos.exists())
        rodada = RodadaContagem.objects.get(item__inventario=nova)
        self.assertIsNone(rodada.contado); self.assertIsNone(rodada.ok_em)
        self.assertFalse(rodada.confirmacoes.exists()); self.assertFalse(hasattr(rodada, 'atribuicao'))
        self.assertFalse(nova.eventos.filter(acao__in=['produto_retirado','produto_reincluido']).exists())

    def test_migration_sincronizada(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('makemigrations', 'inventario', check=True, dry_run=True, stdout=StringIO())

    def test_csrf_valido_permite_cancelamento_e_get_nao_cancela(self):
        from django.middleware.csrf import get_token
        from django.test import RequestFactory
        self.client.force_login(self.usuario)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.inv.refresh_from_db(); self.assertIsNone(self.inv.cancelada_em)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.usuario)
        request = RequestFactory().get('/')
        token = get_token(request)
        csrf.cookies['csrftoken'] = request.META['CSRF_COOKIE']
        self.assertEqual(csrf.post(self.url, {'confirmacao':'sim', 'csrfmiddlewaretoken':token}).status_code, 302)
        self.inv.refresh_from_db(); self.assertIsNotNone(self.inv.cancelada_em)

    def test_finalizada_permanece_inalterada(self):
        self.inv.finalizada_em = timezone.now(); self.inv.save(update_fields=['finalizada_em'])
        antes = Inventario.objects.values().get(pk=self.inv.pk)
        with self.assertRaisesMessage(ValueError, 'finalizada'): self.cancelar()
        self.assertEqual(antes, Inventario.objects.values().get(pk=self.inv.pk))


class CancelamentoConcorrenciaTests(Fixture, TransactionTestCase):
    def test_dois_cancelamentos_simultaneos(self):
        self.assertEqual(connection.vendor, 'postgresql')
        self.acesso()
        barreira = Barrier(2)
        def cancelar(_):
            try:
                usuario = get_user_model().objects.get(pk=self.usuario.pk)
                barreira.wait(timeout=10)
                return services.cancelar_conferencia(usuario, self.inv.pk).cancelada_em
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(cancelar, range(2)))
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(self.inv.eventos.filter(acao='conferencia_cancelada').count(), 1)
        self.assertFalse(self.inv.acessos.filter(revogado_em__isnull=True).exists())

    def test_ok_concorrente_com_cancelamento(self):
        acesso, _ = self.acesso()
        barreira = Barrier(2)
        def executar(acao):
            try:
                usuario = get_user_model().objects.get(pk=self.usuario.pk)
                barreira.wait(timeout=10)
                try:
                    if acao == 'cancelar': services.cancelar_conferencia(usuario, self.inv.pk)
                    else: services.salvar_contagem(acesso.pk, self.rodada.pk, 0, '2', '0', True)
                    return acao
                except PermissionDenied:
                    return 'bloqueado'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(executar, ['cancelar', 'ok']))
        self.assertEqual(resultados[0], 'cancelar')
        self.inv.refresh_from_db(); self.assertIsNotNone(self.inv.cancelada_em)
        self.produto.refresh_from_db(); self.assertEqual(self.produto.quantidade, 10)
        self.assertEqual(ConfirmacaoContagem.objects.count(), int(resultados[1] == 'ok'))
        with self.assertRaises(PermissionDenied): services.salvar_contagem(acesso.pk, self.rodada.pk, 1, '3', '0', True)


from .tests_ajustes import AjustesFixture


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class CancelamentoAjustesTests(AjustesFixture, TestCase):
    def test_aplicacao_real_sintetica_impede_cancelamento(self):
        proposta = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        ajustes.aplicar(self.usuario, self.inv.pk, previa, True)
        antes = list(MovimentacaoEstoqueManual.objects.values())
        self.produto.refresh_from_db(); saldo = self.produto.quantidade
        with self.assertRaisesMessage(ValueError, 'aplicados'):
            services.cancelar_conferencia(self.usuario, self.inv.pk)
        self.produto.refresh_from_db()
        self.assertEqual(saldo, self.produto.quantidade)
        self.assertEqual(antes, list(MovimentacaoEstoqueManual.objects.values()))

    def test_proposta_aprovada_preservada_mas_aplicacao_bloqueada(self):
        proposta = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        avaliacao = self.avaliar()
        atualizacao = ajustes.previa_atualizacao(self.inv.pk, [avaliacao])
        antes = list(PropostaAjuste.objects.values())
        services.cancelar_conferencia(self.usuario, self.inv.pk)
        with self.assertRaises(PermissionDenied): ajustes.aplicar(self.usuario, self.inv.pk, previa, True)
        with self.assertRaises(PermissionDenied): ajustes.atualizar(self.usuario, self.inv.pk, atualizacao, True)
        with self.assertRaises(PermissionDenied): ajustes.decidir(self.usuario, self.inv.pk, self.rodada.pk, avaliacao['token'], 'sintetico', confirmacao=True)
        self.assertEqual(antes, list(PropostaAjuste.objects.values()))
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class CancelamentoAjustesConcorrenciaTests(AjustesFixture, TransactionTestCase):
    def test_aplicacao_e_cancelamento_concorrentes(self):
        proposta = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        barreira = Barrier(2)
        def executar(acao):
            try:
                usuario = get_user_model().objects.get(pk=self.usuario.pk)
                barreira.wait(timeout=10)
                try:
                    if acao == 'cancelar': services.cancelar_conferencia(usuario, self.inv.pk)
                    else: ajustes.aplicar(usuario, self.inv.pk, previa, True)
                    return acao
                except (ValueError, PermissionDenied):
                    return 'bloqueado'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(executar, ['cancelar', 'aplicar']))
        self.inv.refresh_from_db()
        self.assertEqual(resultados.count('bloqueado'), 1)
        self.assertEqual(bool(self.inv.cancelada_em), not DecisaoAjuste.objects.filter(item__inventario=self.inv).exists())


from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome
from .tests_browser import CHROME


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponivel')
class CancelamentoBrowserTests(Fixture, StaticLiveServerTestCase):
    def test_confirmacao_voltar_cancelamento_e_historico(self):
        self.client.force_login(self.usuario)
        with TemporaryDirectory(prefix='cancelamento-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value, 'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url + self.detalhe})
                tab.wait('window.inventarioRevisaoPronta === true')
                tab.evaluate("document.querySelector('form[action$=\"/cancelar/\"]').closest('details').querySelector('summary').click();true")
                self.inv.refresh_from_db(); self.assertIsNone(self.inv.cancelada_em)
                self.assertIn('Cancelar esta', tab.evaluate("document.querySelector('form[action$=\"/cancelar/\"]').closest('details').textContent"))
                tab.evaluate("document.querySelector('form[action$=\"/cancelar/\"]').closest('details').querySelector('a').click();true")
                tab.wait('window.inventarioRevisaoPronta === true')
                self.inv.refresh_from_db(); self.assertIsNone(self.inv.cancelada_em)
                tab.evaluate("document.querySelector('form[action$=\"/cancelar/\"]').closest('details').open=true;document.querySelector('form[action$=\"/cancelar/\"] button').click();true")
                tab.wait("document.body.textContent.includes('Cancelada em')")
                self.assertEqual(tab.evaluate("document.querySelectorAll('form').length"), 0)
                self.inv.refresh_from_db(); self.assertIsNotNone(self.inv.cancelada_em)
                tab.call('Page.navigate', {'url':self.live_server_url + reverse('inventario:lista')})
                tab.wait("!!document.querySelector('#historico-conferencias [data-conferencia]')")
                self.assertIn('Cancelada', tab.evaluate("document.querySelector('#historico-conferencias').textContent"))
            finally:
                chrome.stop()
