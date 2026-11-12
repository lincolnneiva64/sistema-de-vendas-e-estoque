"""Physical anchors across old and new conferences; synthetic guarded PostgreSQL."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Produto, MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from . import ajustes, contagem_admin, services, views
from .marco_fisico import ok_referencia, referencias_estoque, recibos
from .models import (AplicacaoAjuste, ConfirmacaoContagem, DecisaoAjuste, EventoAjuste,
                     EventoInventario, MovimentoObservado, PropostaAjuste)
from .tests_ajustes import AjustesFixture
from .tests_marco_fisico import Operacoes, Tabela


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ReferenciaFisicaGeralTests(Operacoes, AjustesFixture, TestCase):
    def saldo(self):
        self.produto.refresh_from_db()
        return referencias_estoque([self.produto])[1][self.produto.pk]

    def estado_auditoria(self):
        return [list(m.objects.order_by('pk').values()) for m in (
            Produto, ConfirmacaoContagem, AplicacaoAjuste, EventoAjuste,
            EventoInventario, MovimentoObservado, MovimentacaoEstoqueManual)]

    def test_primeira_contagem_apos_historico_desconhecido_estabelece_referencia(self):
        self.assertEqual(self.saldo()['estado'], 'provavel')
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=35)
        r = services.recontar(self.usuario, self.rodada.pk, 'Contagem física realizada pelo funcionário agora.')
        acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [r.pk])
        services.salvar_contagem(acesso.pk, r.pk, 0, '7', '0', True)
        r = self.fresca(r.pk)
        self.assertEqual(ok_referencia(r).responsavel_nome, self.funcionario.nome)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        marco = self.aplicar_marco(r)
        self.assertEqual((marco.antes, marco.depois, marco.diferenca), (35, 7, -28))
        self.assertEqual(self.saldo()['estado'], 'aplicado')

    def test_contagem_antiga_pendente_preserva_ok_e_venda_posterior(self):
        antiga = services.criar_inventario(self.usuario, 'Contagem antiga fictícia', 'manual', [self.produto.pk])
        instante = timezone.now() - timedelta(days=120)
        with patch('inventario.services.timezone.now', return_value=instante):
            r = self.confirmar(antiga, 8)
        original = ConfirmacaoContagem.objects.values().get(rodada=r)
        self.documento(False, 1)
        antes = self.estado_auditoria()
        preparada = ajustes.preparar_resultado(antiga)
        self.assertEqual(len(preparada['aptas']), 1)
        self.assertEqual(preparada['aptas'][0]['resultante'], 7)
        self.assertEqual(self.saldo()['estado'], 'provavel')
        self.assertEqual(antes, self.estado_auditoria())
        token = ajustes.previa_atualizacao(antiga.pk, preparada['aptas'])
        a = ajustes.atualizar(self.usuario, antiga.pk, token, True)[0]
        self.assertEqual((a.antes, a.depois, a.diferenca), (9, 7, -2))
        self.assertEqual(original, ConfirmacaoContagem.objects.values().get(pk=original['id']))
        self.assertEqual(self.saldo()['estado'], 'aplicado')

    def test_diferenca_zero_antiga_formaliza_sem_movimento_ficticio(self):
        antiga = services.criar_inventario(self.usuario, 'Zero antigo fictício', 'manual', [self.produto.pk])
        with patch('inventario.services.timezone.now', return_value=timezone.now() - timedelta(days=90)):
            r = self.confirmar(antiga, 10)
        self.assertFalse(recibos(PropostaAjuste.objects.filter(rodada=r)))
        self.assertEqual(self.saldo()['estado'], 'provavel')
        marco = self.aplicar_marco(r)
        self.assertTrue(marco.sem_ajuste)
        self.assertEqual((marco.antes, marco.depois, marco.diferenca), (10, 10, 0))
        self.assertEqual(self.saldo()['estado'], 'aplicado')
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())

    def test_referencia_aplicada_depois_saldo_atualizado_por_movimentos(self):
        marco = self.aplicar_marco()
        self.assertEqual(self.saldo()['rotulo'], 'Referência física aplicada')
        self.documento(True, 5)
        self.documento(False, 2)
        antes = self.estado_auditoria()
        with patch('inventario.marco_fisico.comprovar', side_effect=AssertionError('Não reconstruir antes da referência')):
            ref = self.saldo()
        self.assertEqual(ref['estado'], 'posterior')
        self.assertEqual(ref['marco'].pk, marco.pk)
        self.assertEqual(self.produto.quantidade, 11)
        self.assertEqual(antes, self.estado_auditoria())
        p = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(p, 'Saldo atualizado por movimentações posteriores')
        tabela = Tabela()
        tabela.feed(p.content.decode())
        self.assertEqual(tabela.colunas, 7)
        self.assertFalse(tabela.abertos)

    def test_correcao_administrativa_reutiliza_ok_original_sem_criar_novo_corte(self):
        original = ok_referencia(self.fresca())
        token = contagem_admin.autorizacao(self.usuario, self.inv, self.fresca())
        self.documento(False, 1)
        corrigida = contagem_admin.confirmar(self.usuario, self.inv.pk, self.rodada.pk, token, '7', '0')
        escolhida = ok_referencia(corrigida)
        self.assertNotEqual(escolhida.pk, original.pk)
        self.assertEqual((escolhida.ok_em, escolhida.sequencia_ok, escolhida.esperado),
            (original.ok_em, original.sequencia_ok, original.esperado))
        self.assertEqual(self.saldo()['estado'], 'provavel')
        marco = self.aplicar_marco(self.fresca())
        self.assertEqual((marco.antes, marco.depois), (9, 6))

    def test_administracao_antiga_sem_prova_fisica_nao_cria_referencia(self):
        r = self.fresca()
        anterior = r.confirmacoes.get()
        # Construct the old administrative format as a fixture, never rewrite an immutable OK.
        novo_horario = timezone.now()
        c = ConfirmacaoContagem.objects.create(rodada=r, versao_entrada=r.versao,
            embalagens=r.embalagens, avulsos=r.avulsos, contado=r.contado,
            contado_base=r.contado_base, esperado=r.esperado, sequencia_ok=r.sequencia_ok,
            ok_em=novo_horario, acesso=None, responsavel_nome=self.usuario.username)
        r.ok_em = novo_horario
        r.versao += 1
        r.save()
        EventoInventario.objects.create(inventario=self.inv, rodada=r, usuario=self.usuario,
            acao=contagem_admin.ACAO, dados=dict(confirmacao_id=c.pk,
                confirmacao_anterior_id=anterior.pk))
        with self.assertRaisesMessage(ValueError, 'administrativa'):
            ok_referencia(r)
        self.assertEqual(ajustes.avaliar(self.fresca())['estado'], 'Bloqueado')
        self.assertEqual(self.saldo()['estado'], 'provavel')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_nova_referencia_em_outra_conferencia_substitui_apenas_a_selecao(self):
        antigo = self.aplicar_marco()
        originais = list(ConfirmacaoContagem.objects.values())
        nova = services.criar_inventario(self.usuario, 'Outra referência física', 'manual', [self.produto.pk])
        r = self.confirmar(nova, 6)
        novo = self.aplicar_marco(r)
        ref = self.saldo()
        self.assertEqual(ref['marco'].pk, novo.pk)
        self.assertNotEqual(novo.pk, antigo.pk)
        self.assertEqual(originais, list(ConfirmacaoContagem.objects.filter(pk__in=[c['id'] for c in originais]).values()))
        antes = self.estado_auditoria()
        p = self.client.get(reverse('inventario:historico_produto', args=[self.inv.pk, self.rodada.pk]), {'periodo': 'completo'})
        self.assertContains(p, 'substituída')
        self.assertEqual(antes, self.estado_auditoria())
        linhas = views.linhas_revisao(self.inv)
        self.assertEqual(linhas[0]['marco'].pk, antigo.pk)
        self.assertEqual(linhas[0]['referencia_estoque']['marco'].pk, novo.pk)

    def test_estado_aplicado_sem_recibo_nao_confirma_nem_reaplica(self):
        a = self.avaliar()
        PropostaAjuste.objects.create(rodada=self.rodada, confirmacao=ok_referencia(self.fresca()),
            assinatura=a['assinatura'], evidencia=a['evidencia'], justificativa='Fixture incompleta',
            estado='aplicado', autorizado_por=self.usuario)
        self.assertEqual(self.saldo()['estado'], 'provavel')
        p = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(p.context['linhas'][0]['confirmacao_estoque'], 'Revisão necessária')
        self.assertContains(p, 'sem recibo compatível')
        self.assertFalse(ajustes.preparar_resultado(self.inv)['aptas'])

    def test_aplicacao_antiga_sem_versao_nova_e_reconhecida_sem_reescrita(self):
        a = self.avaliar()
        dados = deepcopy(a['evidencia'])
        dados.pop('regra')
        dados.pop('diferenca_ok')
        p = PropostaAjuste.objects.create(rodada=self.rodada, confirmacao=ok_referencia(self.fresca()),
            assinatura=ajustes.assinatura(dados), evidencia=dados, justificativa='Aplicação antiga fictícia',
            estado='aplicado', autorizado_por=self.usuario)
        with transaction.atomic():
            resultado = conferir_ou_ajustar_estoque(self.produto.pk, MovimentacaoEstoqueManual.TIPO_AJUSTE,
                novo_estoque=8, motivo=p.justificativa, usuario=self.usuario)
            d = DecisaoAjuste.objects.create(item=self.rodada.item, rodada=self.rodada,
                movimentacao=resultado['historico'], diferenca=-2, justificativa=p.justificativa,
                aprovado_por=self.usuario)
            aplicada = AplicacaoAjuste.objects.create(proposta=p, decisao=d, usuario=self.usuario,
                lote=uuid4(), antes=10, depois=8, diferenca=-2, unidade='CX',
                evidencia=dict(preparacao=dados, autorizado_por=self.usuario.pk, justificativa=p.justificativa))
        antes = self.estado_auditoria()
        self.assertEqual(self.saldo()['marco'].pk, aplicada.pk)
        token = ajustes.token(dict(tipo='atualizacao', inventario=self.inv.pk,
            rodadas=[dict(rodada=self.rodada.pk, assinatura=p.assinatura)]))
        self.assertEqual(ajustes.atualizar(self.usuario, self.inv.pk, token, True)[0].pk, aplicada.pk)
        self.assertEqual(antes, self.estado_auditoria())

    def test_consulta_de_referencias_em_lote_sem_escrita_ou_consultas_por_produto(self):
        self.aplicar_marco()
        outra = self.segunda()
        self.aplicar_marco(outra)
        produtos = list(Produto.objects.filter(pk__in=[self.produto.pk, outra.item.produto_id]))
        with CaptureQueriesContext(connection) as consultas:
            _, saldos = referencias_estoque(produtos)
        self.assertEqual(len(saldos), 2)
        self.assertEqual(len(consultas), 4)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))

    def test_consulta_carrega_so_referencia_recente_e_rodada_historica_pedida(self):
        antiga = self.aplicar_marco()
        intermediaria = services.criar_inventario(self.usuario, 'Referência intermediária', 'manual', [self.produto.pk])
        meio = self.aplicar_marco(self.confirmar(intermediaria, 6))
        ultima = services.criar_inventario(self.usuario, 'Última referência', 'manual', [self.produto.pk])
        recente = self.aplicar_marco(self.confirmar(ultima, 4))
        self.produto.refresh_from_db()
        marcos, saldos = referencias_estoque([self.produto], rodada_ids=[self.rodada.pk])
        self.assertEqual({m.pk for m in marcos.values()}, {antiga.pk, recente.pk})
        self.assertNotIn(meio.pk, {m.pk for m in marcos.values()})
        self.assertEqual(saldos[self.produto.pk]['marco'].pk, recente.pk)

    def test_unidade_atual_incompativel_nao_apaga_referencia_historica(self):
        marco = self.aplicar_marco()
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN')
        ref = self.saldo()
        self.assertEqual(ref['estado'], 'provavel')
        self.assertEqual(ref['marco'].pk, marco.pk)
        self.assertIn('unidade', ref['motivo'])
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)

    def test_encerramento_reconhece_recibo_antigo_sem_exigir_nova_prova_administrativa(self):
        from . import encerramento
        r = self.fresca()
        dados = deepcopy(self.avaliar()['evidencia'])
        anterior = ok_referencia(r)
        horario = timezone.now()
        c = ConfirmacaoContagem.objects.create(rodada=r, versao_entrada=r.versao,
            embalagens=r.embalagens, avulsos=r.avulsos, contado=r.contado,
            contado_base=r.contado_base, esperado=r.esperado, sequencia_ok=r.sequencia_ok,
            ok_em=horario, acesso=None, responsavel_nome=self.usuario.username)
        r.ok_em, r.versao = horario, r.versao + 1
        r.save()
        EventoInventario.objects.create(inventario=self.inv, rodada=r, usuario=self.usuario,
            acao=contagem_admin.ACAO, dados=dict(confirmacao_id=c.pk, confirmacao_anterior_id=anterior.pk))
        dados.update(confirmacao=c.pk, versao=r.versao, ok_em=horario.isoformat())
        dados.pop('regra')
        dados.pop('diferenca_ok')
        p = PropostaAjuste.objects.create(rodada=r, confirmacao=c,
            assinatura=ajustes.assinatura(dados), evidencia=dados, justificativa='Aplicação antiga fictícia',
            estado='aplicado', autorizado_por=self.usuario)
        resultado = conferir_ou_ajustar_estoque(self.produto.pk, MovimentacaoEstoqueManual.TIPO_AJUSTE,
            novo_estoque=8, motivo=p.justificativa, usuario=self.usuario)
        d = DecisaoAjuste.objects.create(item=r.item, rodada=r, movimentacao=resultado['historico'],
            diferenca=-2, justificativa=p.justificativa, aprovado_por=self.usuario)
        AplicacaoAjuste.objects.create(proposta=p, decisao=d, usuario=self.usuario, lote=uuid4(),
            antes=10, depois=8, diferenca=-2, unidade='CX',
            evidencia=dict(preparacao=dados, autorizado_por=self.usuario.pk, justificativa=p.justificativa))
        modelos = (Produto, ConfirmacaoContagem, AplicacaoAjuste, EventoAjuste,
            MovimentoObservado, MovimentacaoEstoqueManual)
        antes = [list(m.objects.order_by('pk').values()) for m in modelos]
        with self.assertRaises(ValueError):
            ok_referencia(r)  # Still blocked for a pending application; receipt changes no history.
        self.inv.refresh_from_db()
        with patch('inventario.encerramento.converter', side_effect=AssertionError('Não recalcular contagem já aplicada')):
            previa = encerramento.previa(self.usuario, self.inv)
            self.assertEqual(previa['confirmados'], 1)
            encerramento.finalizar(self.usuario, self.inv.pk, previa['token'], 'sim')
        self.assertEqual(antes, [list(m.objects.order_by('pk').values()) for m in modelos])


from tempfile import TemporaryDirectory
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome
from .tests_browser import CHROME
from .tests_matematica_tabela import MatematicaTabelaTests


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class VerificacaoFinalReferenciaBrowserTests(StaticLiveServerTestCase):
    setUp = MatematicaTabelaTests.setUp
    conferir = MatematicaTabelaTests.conferir
    movimentar = MatematicaTabelaTests.movimentar

    def abrir(self, tab):
        tab.call('Page.navigate', dict(url=self.live_server_url + reverse('inventario:detalhe', args=[self.inv.pk])))
        tab.wait('window.inventarioContagemAdminPronta===true && window.inventarioAjustesModalPronto===true')

    def test_verificacao_interativa_chopp_sacola_recibos_e_revisao(self):
        with TemporaryDirectory(prefix='verificacao-referencia-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value,
                    url=self.live_server_url))
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=1366, height=900,
                    deviceScaleFactor=1, mobile=False))
                self.conferir('1', '1')
                original = ConfirmacaoContagem.objects.values().get(rodada=self.r)
                self.movimentar('.3', atribuir=True)
                self.abrir(tab)
                self.assertIn('1,000 MIL', tab.evaluate("document.querySelector('.contado-coluna').innerText"))
                self.assertIn('0,700 MIL', tab.evaluate("document.querySelector('.estoque-atual').innerText"))
                self.assertNotIn('Sobra', tab.evaluate("document.querySelector('[data-label=Situação]').innerText"))
                tab.evaluate("document.querySelector('[data-revisar-estoque]').click();true")
                tab.wait("!!document.querySelector('[data-confirmar-ajustes-modal]')")
                self.assertRegex(tab.evaluate("document.querySelector('[data-conteudo-previa]').innerText"),
                    r'Saldo anterior:\s*0[.,]700 MIL → 0[.,]700 MIL')
                tab.evaluate("document.querySelector('[data-confirmar-ajustes-modal] button[type=submit]').click();true")
                tab.wait("document.querySelector('.situacao-atualizacao').innerText.includes('Confirmado')")
                tab.wait("document.querySelector('#modal-sucesso-estoque').open")
                self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
                self.assertEqual(MovimentoObservado.objects.count(), 1)
                self.assertEqual(ConfirmacaoContagem.objects.values().get(pk=original['id']), original)
                self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, D('.7'))
                self.assertEqual(EventoAjuste.objects.filter(acao='marco_sem_ajuste').count(), 1)
                tab.call('Page.navigate', dict(url=self.live_server_url + reverse('inventario:ajustes', args=[self.inv.pk])))
                tab.wait("!!document.querySelector('.resumo-ajustes')")
                self.assertIn('Confirmado', tab.evaluate("document.querySelector('.estado-ajuste').innerText"))
                self.abrir(tab)
                self.assertIn('1 referências físicas confirmadas',
                    tab.evaluate("document.querySelector('#encerramento-administrativo').innerText"))
                self.assertIn('Referência física aplicada', tab.evaluate("document.querySelector('.estoque-atual').innerText"))

                # An unproved subsequent movement preserves the original physical numbers.
                self.conferir('1', '.5')
                Produto.objects.filter(pk=self.produto.pk).update(quantidade=2)
                self.abrir(tab)
                for width in (1366, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900,
                        deviceScaleFactor=1, mobile=width < 600))
                    self.assertIn('0,500 MIL', tab.evaluate("document.querySelector('.contado-coluna').innerText"))
                    self.assertIn('−1,500 MIL', tab.evaluate("document.querySelector('.diferenca').innerText"))
                    self.assertIn('Comparação com contagem anterior', tab.evaluate("document.querySelector('.diferenca').innerText"))
                    self.assertIn('Revisão necessária', tab.evaluate("document.querySelector('.situacao-atualizacao').innerText"))
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    tab.evaluate("document.querySelector('.ver-detalhes-conferencia').click();true")
                    self.assertTrue(tab.evaluate("document.querySelector('.detalhes-conferencia').matches(':popover-open')"))
                    self.assertIn('não autoriza', tab.evaluate("document.querySelector('.detalhes-conferencia').innerText"))
                    tab.evaluate("document.querySelector('.detalhes-conferencia [popovertargetaction=hide]').click();true")
                tab.evaluate("document.querySelector('[data-revisar-estoque]').click();true")
                tab.wait("!!document.querySelector('[data-bloqueados-modal]')")
                self.assertFalse(tab.evaluate("!!document.querySelector('[data-confirmar-ajustes-modal]')"))
                tab.evaluate("document.querySelector('[data-cancelar-ajustes]').click();true")
                self.assertFalse(MovimentacaoEstoqueManual.objects.filter(produto=self.produto).exists())
                self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, 2)
            finally:
                chrome.stop()
