"""Comparison and temporal intent on fictitious, disposable PostgreSQL only."""
from decimal import Decimal as D
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import strip_tags

from estoque.models import Produto, MovimentacaoEstoqueManual
from . import ajustes, contagem_admin, services
from .models import ConfirmacaoContagem, MovimentoObservado, RodadaContagem, EventoInventario
from .tests_matematica_tabela import MatematicaTabelaTests
from .tests_marco_fisico import Tabela


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ComparacaoContagemTests(TestCase):
    setUp = MatematicaTabelaTests.setUp
    conferir = MatematicaTabelaTests.conferir
    movimentar = MatematicaTabelaTests.movimentar
    consultar = MatematicaTabelaTests.consultar

    def token(self):
        self.inv.refresh_from_db()
        self.r.refresh_from_db()
        return contagem_admin.autorizacao(self.user, self.inv, self.r)

    def admin(self, contado, *, tipo='corrigir', token=None):
        return contagem_admin.confirmar(self.user, self.inv.pk, self.r.pk,
            token or self.token(), contado, '0', tipo=tipo)

    def aplicar(self):
        resultado = ajustes.preparar_resultado(self.inv)
        self.assertEqual(len(resultado['aptas']), 1)
        token = ajustes.previa_atualizacao(self.inv.pk, resultado['aptas'])
        registros = ajustes.atualizar(self.user, self.inv.pk, token, True)
        return token, registros[0]

    def pagina(self):
        p = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(p.status_code, 200)
        tabela = Tabela()
        tabela.feed(p.content.decode())
        self.assertEqual(tabela.colunas, 7)
        self.assertFalse(tabela.abertos)
        return p

    def celula(self, pagina, rotulo):
        return strip_tags(pagina.content.decode().split(f'data-label="{rotulo}"', 1)[1].split('</td>', 1)[0])

    def test_sacola_estoque_dois_contagem_meio_exibe_menos_um_e_meio_sem_autorizar(self):
        self.conferir('1', '.5')
        preparada = ajustes.previa_atualizacao(self.inv.pk, ajustes.preparar_resultado(self.inv)['aptas'])
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=2)
        linha, resumo = self.consultar()
        c = linha['comparacao_atual']
        self.assertEqual((c['estoque'], c['contado'], c['diferenca']), (D('2'), D('.5'), D('-1.5')))
        self.assertEqual((c['percentual'], c['estimativa']), (D('-75'), D('-15')))
        self.assertEqual(c['base_percentual'], '2,000 MIL')
        self.assertIsNone(linha['comparacao_operacional']['diferenca'])
        self.assertEqual((resumo['anteriores'], resumo['faltas'], resumo['sobras']), (1, 0, 0))
        self.assertEqual(resumo['base_financeira'], 20)
        p = self.pagina()
        self.assertIn('2,000 MIL', self.celula(p, 'Estoque atual'))
        self.assertIn('0,500 MIL', self.celula(p, 'Contado'))
        self.assertIn('−1,500 MIL', self.celula(p, 'Diferença / %'))
        self.assertIn('Comparação com contagem anterior', self.celula(p, 'Diferença / %'))
        self.assertIn('Estimativa da comparação', self.celula(p, 'Estimativa ao custo'))
        self.assertEqual(p.context['linhas'][0]['confirmacao_estoque'], 'Revisão necessária')
        self.assertFalse(ajustes.preparar_resultado(self.inv)['aptas'])
        with self.assertRaises(ValueError):
            ajustes.atualizar(self.user, self.inv.pk, preparada, True)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_chopp_venda_posterior_projecao_zero_nao_e_sobra_atual(self):
        self.conferir('1', '1')
        original = list(ConfirmacaoContagem.objects.values())
        referencia = (self.r.ok_em, self.r.esperado, self.r.sequencia_ok, self.r.versao)
        self.movimentar('.3', atribuir=True)
        linha, _ = self.consultar()
        c, op = linha['comparacao_atual'], linha['comparacao_operacional']
        self.assertEqual((c['contado'], c['estoque'], c['diferenca']), (D('1'), D('.7'), D('.3')))
        self.assertEqual((op['contado'], op['diferenca'], op['comprovada']), (D('.7'), D('0'), True))
        self.assertEqual(linha['resultado_label'], 'Comparação com contagem anterior')
        self.assertEqual(c['destaque'], 'neutro')
        token = self.token()
        self.admin('1', token=token)
        self.admin('1', token=token)
        self.r.refresh_from_db()
        self.assertEqual((self.r.ok_em, self.r.esperado, self.r.sequencia_ok, self.r.versao), referencia)
        self.assertEqual(original, list(ConfirmacaoContagem.objects.values()))
        p = self.pagina()
        self.assertIn('1,000 MIL', self.celula(p, 'Contado'))
        self.assertNotIn('Sobra', self.celula(p, 'Situação'))
        self.assertContains(p, '0,700 MIL')

    def test_reconfirmacao_funcionario_tambem_nao_renova_ok(self):
        self.conferir('1', '1')
        acesso = self.r.atribuicao.acesso
        self.movimentar('.3', atribuir=True)
        antes = list(ConfirmacaoContagem.objects.values())
        versao = self.r.versao
        services.salvar_contagem(acesso.pk, self.r.pk, versao, '1', '0', True)
        self.r.refresh_from_db()
        self.assertEqual(self.r.versao, versao)
        self.assertEqual(antes, list(ConfirmacaoContagem.objects.values()))

    def test_corrigir_anterior_preserva_tempo_referencia_e_documentos_originais(self):
        self.conferir('1', '1')
        original = ConfirmacaoContagem.objects.get(rodada=self.r)
        antes = ConfirmacaoContagem.objects.values().get(pk=original.pk)
        self.movimentar('.3', atribuir=True)
        token = self.token()
        corrigida = self.admin('.9', token=token)
        self.assertEqual((corrigida.ok_em, corrigida.esperado, corrigida.sequencia_ok),
            (original.ok_em, original.esperado, original.sequencia_ok))
        self.assertEqual(antes, ConfirmacaoContagem.objects.values().get(pk=original.pk))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.admin('.9', token=token)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        linha, _ = self.consultar()
        self.assertEqual(linha['comparacao_operacional']['contado'], D('.6'))
        self.assertEqual(linha['comparacao_operacional']['diferenca'], D('-.1'))
        self.assertEqual(linha['comparacao_atual']['diferenca'], D('.2'))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, D('.7'))
        # The displayed +.2 comparison cannot turn into an operational +.2 write.
        token_aplicacao, registro = self.aplicar()
        self.assertEqual((registro.diferenca, registro.depois), (D('-.1'), D('.6')))
        ajustes.atualizar(self.user, self.inv.pk, token_aplicacao, True)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)

    def test_correcao_anterior_nao_apaga_impedimento_posterior(self):
        self.conferir('1', '1')
        corte = self.r.sequencia_ok
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('.7'))
        self.admin('.9')
        self.r.refresh_from_db()
        self.assertEqual(self.r.sequencia_ok, corte)
        self.assertFalse(ajustes.preparar_resultado(self.inv)['aptas'])
        self.assertTrue(self.consultar()[0]['comparacao_operacional']['limite'])

    def test_nova_contagem_explicita_usa_recontagem_e_reenvio_idempotente(self):
        self.conferir('1', '1')
        original = RodadaContagem.objects.values().get(pk=self.r.pk)
        historico = list(ConfirmacaoContagem.objects.values())
        self.movimentar('.3', atribuir=True)
        token = self.token()
        nova = self.admin('.7', tipo='nova', token=token)
        self.assertNotEqual(nova.pk, self.r.pk)
        self.assertEqual(nova.numero, self.r.numero + 1)
        self.assertGreater(nova.ok_em, self.r.ok_em)
        self.assertEqual((nova.esperado, nova.contado_base), (D('.7'), D('.7')))
        velha = RodadaContagem.objects.values().get(pk=self.r.pk)
        self.assertIsNotNone(velha.pop('encerrada_em'))
        original.pop('encerrada_em')
        self.assertEqual(velha, original)
        self.assertEqual(historico, list(ConfirmacaoContagem.objects.filter(pk__in=[h['id'] for h in historico]).values()))
        self.assertEqual(self.admin('.7', tipo='nova', token=token).pk, nova.pk)
        with self.assertRaises(ValueError):
            self.admin('.6', tipo='nova', token=token)
        self.assertEqual(RodadaContagem.objects.count(), 2)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_nova_contagem_mantem_idempotencia_ao_reabrir_ciclo_finalizado(self):
        self.conferir('1', '1')
        self.inv.refresh_from_db()
        services.finalizar_conferencia(self.r.atribuicao.acesso_id, self.inv.versao)
        token = self.token()
        nova = self.admin('1', tipo='nova', token=token)
        self.assertEqual(self.admin('1', tipo='nova', token=token).pk, nova.pk)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.ciclo, 2)
        self.assertEqual(RodadaContagem.objects.count(), 2)

    def test_saco_marco_aplicado_compra_venda_exibe_um_e_nove_sem_reconstruir(self):
        self.conferir('1', '.1')
        token, recibo = self.aplicar()
        original = list(ConfirmacaoContagem.objects.values())
        antes = recibo.evidencia
        self.movimentar('2', entrada=True, atribuir=True)
        self.movimentar('.2', atribuir=True)
        with patch('inventario.marco_fisico.comprovar', side_effect=AssertionError('Não reconstruir antes de marco aplicado')):
            p = self.pagina()
        linha = p.context['linhas'][0]
        c = linha['comparacao_atual']
        self.assertEqual((c['estoque'], c['contado'], c['diferenca']), (D('1.9'), D('.1'), D('-1.8')))
        self.assertEqual(linha['confirmacao_estoque'], 'Confirmado')
        self.assertIn('1,900 MIL', self.celula(p, 'Estoque atual'))
        self.assertIn('0,100 MIL', self.celula(p, 'Contado'))
        self.assertNotIn('Revisão necessária', self.celula(p, 'Confirmação do estoque'))
        self.assertEqual(len(ajustes.atualizar(self.user, self.inv.pk, token, True)), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, D('1.9'))
        recibo.refresh_from_db()
        self.assertEqual(recibo.evidencia, antes)
        self.assertEqual(original, list(ConfirmacaoContagem.objects.values()))
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        for tipo in ('corrigir', 'nova'):
            with self.assertRaises(ValueError):
                self.admin('.1', tipo=tipo)

    def test_zero_estoque_e_contagem_sem_correcao_e_marco_zero_sem_movimento(self):
        self.conferir('0', '0')
        linha, _ = self.consultar()
        c = linha['comparacao_atual']
        self.assertEqual(c['diferenca'], 0)
        self.assertIsNone(c['percentual'])
        self.assertEqual(c['estimativa'], 0)
        self.assertContains(self.pagina(), 'Sem diferença')
        self.assertNotContains(self.pagina(), 'Ajuste pendente')
        self.aplicar()
        self.assertEqual(self.pagina().context['linhas'][0]['confirmacao_estoque'], 'Confirmado')
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_unidade_distinta_sem_conversao_preserva_os_dois_numeros(self):
        self.conferir('1', '.5')
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN')
        p = self.pagina()
        self.assertIn('1 UN', self.celula(p, 'Estoque atual'))
        self.assertIn('0,500 MIL', self.celula(p, 'Contado'))
        self.assertIsNone(p.context['linhas'][0]['comparacao_atual']['diferenca'])
        self.assertIn('conversão', self.celula(p, 'Diferença / %'))

    def test_conversao_congelada_usa_mesma_base_para_diferenca_percentual_e_custo(self):
        self.conferir('2', '1', unidade='CX', fracionado=True, fator=12)
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN', quantidade=24)
        linha, resumo = self.consultar()
        c = linha['comparacao_atual']
        self.assertEqual((c['contado'], c['estoque'], c['diferenca']), (12, 24, -12))
        self.assertEqual((c['percentual'], c['estimativa'], resumo['base_financeira']), (-50, -10, 20))
        self.assertEqual(c['contado_original'], 1)
        self.assertEqual(c['conversao_texto'], '12 UN')
        self.assertIsNone(linha['comparacao_operacional']['diferenca'])
        self.assertFalse(linha['estoque_atual_equivalencia'])

    def test_http_distingue_intencoes_e_reconfirmacao_sem_escrita(self):
        self.conferir('1', '1')
        self.movimentar('.3', atribuir=True)
        p = self.pagina()
        self.assertContains(p, 'Corrigir contagem anterior')
        self.assertContains(p, 'Nova contagem física feita agora')
        url = reverse('inventario:editar_contagem_admin', args=[self.inv.pk, self.r.pk])
        dados = dict(autorizacao=self.token(), embalagens='1', avulsos='0', confirmacao='sim', tipo='corrigir')
        antes = list(ConfirmacaoContagem.objects.values())
        self.assertEqual(self.client.post(url, dados, HTTP_ACCEPT='application/json').status_code, 200)
        self.assertEqual(antes, list(ConfirmacaoContagem.objects.values()))
        dados.update(tipo='nova', embalagens='.7')
        self.assertEqual(self.client.post(url, dados, HTTP_ACCEPT='application/json').status_code, 200)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_conversao_de_pacotes_e_avulsos_preserva_total_fisico_inteiro(self):
        self.conferir('2', '3', unidade='PCT', fracionado=True, fator=6, avulsos='2')
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN', quantidade=20)
        linha, _ = self.consultar()
        c = linha['comparacao_atual']
        self.assertEqual(self.r.contado_base, D('3.333'))
        self.assertEqual((c['contado'], c['estoque'], c['diferenca']), (20, 20, 0))
        self.assertEqual(c['estimativa'], 0)
        self.assertEqual(c['conversao_texto'], '20 UN')
        self.assertTrue(c['contado_texto'].startswith('≈ '))

    def test_nova_contagem_com_cadastro_incompativel_reverte_abertura_da_rodada(self):
        self.conferir('1', '1')
        token = self.token()
        antes = list(RodadaContagem.objects.values())
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN')
        with self.assertRaises(ValueError):
            self.admin('1', tipo='nova', token=token)
        self.assertEqual(antes, list(RodadaContagem.objects.values()))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)

    def test_intencao_invalida_e_token_obsoleto_nao_alteram_referencia(self):
        self.conferir('1', '1')
        token = self.token()
        with self.assertRaises(ValueError):
            self.admin('.9', tipo='outro', token=token)
        self.admin('.9', token=token)
        with self.assertRaises(ValueError):
            self.admin('.8', token=token)
        with self.assertRaises(ValueError):
            self.admin('.9', tipo='nova', token=token)
        self.r.refresh_from_db()
        self.assertEqual(self.r.contado_base, D('.9'))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections
from django.test import TransactionTestCase


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ContagemTemporalConcorrenciaTests(TransactionTestCase):
    setUp = MatematicaTabelaTests.setUp
    conferir = MatematicaTabelaTests.conferir

    def concorrentes(self, tipo):
        self.conferir('1', '1')
        self.inv.refresh_from_db()
        token = contagem_admin.autorizacao(self.user, self.inv, self.r)
        barreira = Barrier(2)
        def executar(_):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                return contagem_admin.confirmar(self.user, self.inv.pk, self.r.pk, token, '.9', '0', tipo=tipo).pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(executar, range(2)))
        self.assertEqual(a, b)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 2)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_nova_contagem_concorrente_nao_cria_duas_rodadas(self):
        self.concorrentes('nova')
        self.assertEqual(RodadaContagem.objects.count(), 2)

    def test_correcao_concorrente_nao_renova_referencia(self):
        self.concorrentes('corrigir')
        self.assertEqual(RodadaContagem.objects.count(), 1)
        r = RodadaContagem.objects.get()
        self.assertEqual((r.ok_em, r.esperado, r.sequencia_ok), (self.r.ok_em, self.r.esperado, self.r.sequencia_ok))


from tempfile import TemporaryDirectory
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome
from .tests_browser import CHROME


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ContagemTemporalBrowserTests(StaticLiveServerTestCase):
    setUp = MatematicaTabelaTests.setUp
    conferir = MatematicaTabelaTests.conferir
    movimentar = MatematicaTabelaTests.movimentar

    def abrir_editor(self, tab):
        tab.evaluate("window.formTemporal=document.querySelector('form[data-edicao-admin]');document.querySelector('[data-abrir-edicao-admin=\"'+formTemporal.closest('dialog').id+'\"]').click();true")
        self.assertTrue(tab.evaluate("formTemporal.closest('dialog').matches(':modal')"))
        self.assertTrue(tab.evaluate('document.activeElement===formTemporal.elements.embalagens'))

    def preencher(self, tab, valor):
        tab.evaluate("formTemporal.elements.embalagens.value='" + valor + "';formTemporal.dispatchEvent(new Event('input',{bubbles:true}));true")

    def enviar(self, tab):
        tab.evaluate("window.envioTemporalAnterior=true;formTemporal.querySelector('button[type=submit]').click();true")
        try:
            tab.wait("window.envioTemporalAnterior!==true && window.inventarioContagemAdminPronta===true && window.inventarioAjustesModalPronto===true")
        except AssertionError:
            self.fail(str(tab.evaluate("({url:location.href,erro:document.querySelector('[data-mensagem-admin]')?.textContent,modal:document.querySelector('dialog.modal-contagem-admin')?.open})")))

    def estado_gravado(self):
        return (list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk').values()),
            list(ConfirmacaoContagem.objects.filter(rodada__item__inventario=self.inv).order_by('pk').values()),
            list(EventoInventario.objects.filter(inventario=self.inv).order_by('pk').values()),
            list(Produto.objects.filter(pk=self.produto.pk).values()),
            list(MovimentacaoEstoqueManual.objects.filter(produto=self.produto).values()))

    def test_seletor_correcao_nova_contagem_cancelamento_e_reconfirmacao_no_navegador(self):
        with TemporaryDirectory(prefix='contagem-temporal-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid',
                    value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                for width in (1366, 390):
                    with self.subTest(width=width):
                        self.conferir('1', '1')
                        original = ConfirmacaoContagem.objects.values().get(rodada=self.r)
                        referencia = (self.r.ok_em, self.r.esperado, self.r.sequencia_ok, self.r.versao)
                        self.movimentar('.3', atribuir=True)
                        tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900,
                            deviceScaleFactor=1, mobile=width < 600))
                        tab.call('Page.navigate', dict(url=self.live_server_url + reverse('inventario:detalhe', args=[self.inv.pk])))
                        tab.wait('window.inventarioContagemAdminPronta===true && window.inventarioAjustesModalPronto===true')
                        self.assertIn('0,300 MIL', tab.evaluate("document.querySelector('.diferenca').innerText"))
                        self.assertIn('Comparação com contagem anterior', tab.evaluate("document.querySelector('.diferenca').innerText"))
                        self.assertNotIn('Sobra', tab.evaluate("document.querySelector('[data-label=Situação]').innerText"))
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.assertEqual(tab.evaluate('document.querySelectorAll(".tabela-resumo thead th").length'), 7)

                        # Cancel changes in the correction path; no POST or database write.
                        antes = self.estado_gravado()
                        self.abrir_editor(tab)
                        self.assertEqual(tab.evaluate('formTemporal.elements.tipo.value'), 'corrigir')
                        self.preencher(tab, '.8')
                        tab.evaluate("formTemporal.querySelector('[data-fechar-edicao-admin]').click();true")
                        self.assertFalse(tab.evaluate("formTemporal.closest('dialog').open"))
                        self.assertEqual(antes, self.estado_gravado())

                        # Reconfirming the old count leaves its original timestamp/reference/version.
                        self.abrir_editor(tab)
                        self.preencher(tab, '1')
                        self.enviar(tab)
                        self.r.refresh_from_db()
                        self.assertEqual((self.r.ok_em, self.r.esperado, self.r.sequencia_ok, self.r.versao), referencia)
                        self.assertEqual(antes, self.estado_gravado())

                        # A correction appends evidence but preserves the temporal reference.
                        self.abrir_editor(tab)
                        self.preencher(tab, '.9')
                        self.enviar(tab)
                        self.r.refresh_from_db()
                        self.assertEqual((self.r.ok_em, self.r.esperado, self.r.sequencia_ok), referencia[:3])
                        self.assertEqual(self.r.contado_base, D('.9'))
                        self.assertEqual(original, ConfirmacaoContagem.objects.values().get(pk=original['id']))
                        self.assertEqual(self.r.confirmacoes.count(), 2)
                        self.assertIn('0,200 MIL', tab.evaluate("document.querySelector('.diferenca').innerText"))
                        self.assertIn('Estimativa da comparação', tab.evaluate("document.querySelector('.diferenca').innerText"))

                        # The existing signed application preview uses -.1, not the displayed +.2.
                        antes = self.estado_gravado()
                        tab.evaluate("document.querySelector('[data-revisar-estoque]').click();true")
                        tab.wait("!!document.querySelector('[data-confirmar-ajustes-modal]')")
                        previa = tab.evaluate("document.querySelector('[data-conteudo-previa]').innerText")
                        self.assertRegex(previa, r'Saldo anterior:\s*0[.,]700 MIL → 0[.,]600 MIL')
                        self.assertIn('Falta 0,100 MIL', previa)
                        tab.evaluate("document.querySelector('[data-cancelar-ajustes]').click();true")
                        self.assertEqual(antes, self.estado_gravado())

                        # Selecting a new count clears the inputs and explains the new temporal cut.
                        self.abrir_editor(tab)
                        tab.evaluate("formTemporal.querySelector('[name=tipo][value=nova]').click();true")
                        self.assertEqual(tab.evaluate('formTemporal.elements.embalagens.value'), '')
                        self.assertIn('outra rodada', tab.evaluate("formTemporal.querySelector('[data-referencia-contagem]').innerText"))
                        self.preencher(tab, '.7')
                        tab.evaluate("formTemporal.querySelector('[data-fechar-edicao-admin]').click();true")
                        self.assertEqual(antes, self.estado_gravado())
                        self.abrir_editor(tab)
                        self.enviar(tab)
                        nova = RodadaContagem.objects.get(item__inventario=self.inv, encerrada_em__isnull=True)
                        self.assertNotEqual(nova.pk, self.r.pk)
                        self.assertGreater(nova.ok_em, self.r.ok_em)
                        self.assertGreater(nova.sequencia_ok, self.r.sequencia_ok)
                        self.assertEqual((nova.contado_base, nova.esperado), (D('.7'), D('.7')))
                        self.assertEqual(ConfirmacaoContagem.objects.filter(rodada__item__inventario=self.inv).count(), 3)

                        # Reconfirming the new round is also a no-op; neither path writes stock.
                        antes = self.estado_gravado()
                        self.abrir_editor(tab)
                        self.enviar(tab)
                        self.assertEqual(antes, self.estado_gravado())
                        self.produto.refresh_from_db()
                        self.assertEqual(self.produto.quantidade, D('.7'))
                        self.assertFalse(MovimentacaoEstoqueManual.objects.filter(produto=self.produto).exists())
            finally:
                chrome.stop()
