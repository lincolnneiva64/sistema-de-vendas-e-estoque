"""Presentation regressions on fictitious documents; never operational IDs."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import ItemVenda, MovimentacaoEstoqueManual, Venda
from offline.browser_support import Chrome
from . import ajustes
from .models import AplicacaoAjuste, DecisaoAjuste, MovimentoObservado
from .models import ItemInventario, RodadaContagem
from .conversao import snapshot
from .tests import produto
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME, capturar
from .tests_extrato_cronologico import CronologiaFixture


class VendasDuplicadasTests(CronologiaFixture, TestCase):
    def setUp(self):
        super().setUp()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_antes=10,
            estoque_movimentado=1, estoque_depois=9, estoque_unidade_snapshot='PCT')
        self.tecnico = MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=10, depois=9, origem='venda', referencia=f'Venda #{self.posterior.venda_id}',
            registrado_em=self.ok.replace(hour=16, minute=19)+timedelta(seconds=1))

    def resposta(self, **params):
        resposta = self.client.get(self.url, params)
        self.assertEqual(resposta.status_code, 200)
        return resposta

    def digitais(self, resposta):
        return [l for l in resposta.context['extrato'] if l['fonte']=='digital']

    def test_uma_saida_documental_com_evidencia_tecnica_recolhida(self):
        r = self.resposta()
        self.assertEqual(self.digitais(r), [])
        venda = next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        self.assertEqual(venda['saida'], 1)
        self.assertEqual(venda['unidade'], 'PCT')
        self.assertEqual([m.pk for m in venda['registros_tecnicos']], [self.tecnico.pk])
        self.assertContains(r, f'data-registro="registro-venda-{self.posterior.pk}"', count=1)
        self.assertNotContains(r, f'data-registro="registro-digital-{self.tecnico.pk}"')
        self.assertContains(r, f'Registro técnico #{self.tecnico.pk}')
        self.assertContains(r, 'não é uma saída adicional')
        self.assertContains(r, 'Conferência física — OK', count=1)
        self.assertEqual([l['fonte'] for l in r.context['extrato']],
            ['compra','venda','conferencia','venda','venda'])

    def test_sem_numero_na_referencia_nao_inventa_associacao(self):
        MovimentoObservado.objects.filter(pk=self.tecnico.pk).update(referencia='Gravação de venda')
        self.assertEqual(len(self.digitais(self.resposta())), 1)

    def test_saldos_diferentes_preservam_evento_distinto(self):
        MovimentoObservado.objects.filter(pk=self.tecnico.pk).update(antes=11, depois=10)
        self.assertEqual(len(self.digitais(self.resposta())), 1)

    def test_duas_baixas_da_mesma_venda_nao_sao_ocultadas(self):
        MovimentoObservado.objects.create(produto=self.produto, sequencia=2,
            antes=9, depois=8, origem='venda', referencia=self.tecnico.referencia,
            registrado_em=self.tecnico.registrado_em+timedelta(seconds=1))
        self.assertEqual(len(self.digitais(self.resposta())), 2)

    def test_dois_itens_do_mesmo_produto_sao_ambiguos(self):
        ItemVenda.objects.create(venda_id=self.posterior.venda_id, produto=self.produto, quantidade=1,
            unidade='PCT', preco_unitario=0, valor_total=0)
        self.assertEqual(len(self.digitais(self.resposta())), 1)

    def test_referencia_de_outra_venda_nao_e_mesclada_por_horario(self):
        MovimentoObservado.objects.filter(pk=self.tecnico.pk).update(referencia=f'Venda #{self.outra.venda_id}')
        self.assertEqual(len(self.digitais(self.resposta())), 1)

    def test_documento_fora_do_intervalo_nao_oculta_registro_dentro(self):
        MovimentoObservado.objects.filter(pk=self.tecnico.pk).update(registrado_em=self.inicio)
        dia = timezone.localtime(self.inicio).date().isoformat()
        r = self.resposta(periodo='personalizado', data_inicial=dia, data_final=dia)
        self.assertEqual(len(self.digitais(r)), 1)

    def test_opcao_a_preserva_os_dois_registros(self):
        # A still ends at the OK; include the observer in that original interval.
        self.rodada.ok_em = self.ok.replace(hour=19)
        self.rodada.save()
        r = self.resposta(visual='a')
        self.assertEqual(len(self.digitais(r)), 1)
        self.assertFalse(any(l.get('registros_tecnicos') for l in r.context['extrato']))

    def test_mesma_venda_dois_produtos_uma_saida_por_extrato(self):
        outro = produto(nome='Cola lata — produto fictício')
        item = ItemInventario.objects.create(inventario=self.inv, produto=outro, cadastro=snapshot(outro))
        rodada = RodadaContagem.objects.create(item=item, numero=1,
            ok_em=self.ok.replace(minute=14), esperado=10, contado_base=10)
        ItemVenda.objects.create(venda_id=self.posterior.venda_id, produto=outro, quantidade=1,
            unidade='PCT', preco_unitario=0, valor_total=0, estoque_antes=20,
            estoque_depois=19, estoque_movimentado=1, estoque_unidade_snapshot='PCT')
        MovimentoObservado.objects.create(produto=outro, sequencia=1, antes=20, depois=19,
            origem='venda', referencia=self.tecnico.referencia, registrado_em=self.tecnico.registrado_em)
        for url in (self.url, reverse('inventario:historico_produto',args=[self.inv.pk,rodada.pk])):
            r = self.client.get(url)
            self.assertEqual(r.status_code,200)
            self.assertEqual(self.digitais(r), [])
            vendas = [l for l in r.context['extrato'] if l['fonte']=='venda' and l['documento']==self.posterior.venda_id]
            self.assertEqual(len(vendas),1)
            self.assertEqual((vendas[0]['saida'],vendas[0]['unidade']), (1,'PCT'))

    def test_deduplicacao_antes_da_paginacao_sem_perdas(self):
        for i in range(55):
            self.registrar_venda(self.ok+timedelta(minutes=i+1))
        paginas = [self.resposta(extrato_pagina=n).context['extrato'] for n in (1,2)]
        ids = [l['detalhe_id'] for pagina in paginas for l in pagina]
        self.assertEqual(len(ids),60)
        self.assertEqual(len(set(ids)),60)
        self.assertEqual(sum(l['fonte']=='conferencia' for p in paginas for l in p),1)
        self.assertNotIn(f'registro-digital-{self.tecnico.pk}',ids)

    def test_sem_escritas_e_sem_consultas_por_cartao(self):
        antes = list(MovimentoObservado.objects.values())
        with CaptureQueriesContext(connection) as poucas:
            self.resposta()
        for i in range(20):
            item = self.registrar_venda(self.ok+timedelta(minutes=i+1))
            ItemVenda.objects.filter(pk=item.pk).update(estoque_antes=10, estoque_depois=9,
                estoque_movimentado=1, estoque_unidade_snapshot='PCT')
            MovimentoObservado.objects.create(produto=self.produto, sequencia=i+2,
                antes=10, depois=9, origem='venda', referencia=f'Venda #{item.venda_id}',
                registrado_em=self.ok+timedelta(minutes=i+1))
        with CaptureQueriesContext(connection) as muitas:
            r = self.resposta()
        self.assertLessEqual(len(muitas), len(poucas)+1)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','(SELECT')) for q in muitas))
        self.assertEqual(list(MovimentoObservado.objects.filter(pk=self.tecnico.pk).values()), antes)
        self.assertEqual(self.digitais(r), [])


class AplicadaFixture(AjustesFixture):
    def setUp(self):
        super().setUp()
        proposta = self.aprovar()
        self.aplicada = ajustes.aplicar(self.usuario, self.inv.pk,
            ajustes.previa_aplicacao(self.inv.pk, [proposta]), confirmacao=True)[0]
        self.historico = reverse('inventario:historico_produto', args=[self.inv.pk,self.rodada.pk])


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class RegularizacaoTests(AplicadaFixture, TestCase):
    def resposta(self, **params):
        r = self.client.get(self.historico, params)
        self.assertEqual(r.status_code, 200)
        return r

    def test_aplicacao_comprovada_vira_regularizacao_sem_saida_comercial(self):
        r = self.resposta()
        linhas = list(r.context['extrato'])
        self.assertEqual([l['fonte'] for l in linhas], ['conferencia','regularizacao'])
        linha = linhas[-1]
        self.assertEqual(linha['instante'], self.aplicada.aplicado_em)
        self.assertIsNone(linha['entrada'])
        self.assertIsNone(linha['saida'])
        self.assertEqual(linha['saldo'], self.aplicada.depois)
        self.assertContains(r, f'Regularização — Conferência nº {self.inv.pk}')
        self.assertContains(r, 'Efetivamente aplicada em')
        self.assertContains(r, str(self.aplicada.pk))
        self.assertContains(r, str(self.aplicada.lote))
        self.assertContains(r, 'Regularização aplicada')
        self.assertNotContains(r, '<span class="saida">Saída</span>')
        self.assertEqual(self.client.get(linha['url']).status_code,200)

    def test_a_continua_com_ajuste_manual_e_dados_originais(self):
        # A ends at the OK, so select a fixture OK after the application to inspect
        # its unchanged presentation, without altering the immutable application.
        self.rodada.ok_em = timezone.now()+timedelta(seconds=1)
        self.rodada.save()
        r = self.resposta(visual='a')
        self.assertTrue(any(l['fonte']=='manual' for l in r.context['extrato']))
        self.assertNotContains(r, 'Regularização aplicada')

    def test_decisao_antiga_sem_aplicacao_nao_finge_regularizacao(self):
        outra = self.segunda()
        manual = MovimentacaoEstoqueManual.objects.create(produto=outra.item.produto,
            tipo='ajuste', estoque_antes=20, estoque_depois=18, diferenca=-2,
            motivo='Registro legado fictício')
        DecisaoAjuste.objects.create(item=outra.item, rodada=outra, movimentacao=manual,
            diferenca=-2, justificativa='Legado fictício', aprovado_por=self.usuario)
        url = reverse('inventario:historico_produto', args=[self.inv.pk,outra.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(l['fonte']=='manual' for l in r.context['extrato']))
        self.assertNotContains(r, 'Regularização aplicada')

    def test_texto_de_conferencia_sem_vinculo_nao_prova_aplicacao(self):
        MovimentacaoEstoqueManual.objects.create(produto=self.produto, tipo='ajuste',
            estoque_antes=8, estoque_depois=7, diferenca=-1,
            motivo=f'Conferência {self.inv.pk}; ajuste fictício sem aplicação')
        r = self.resposta()
        self.assertEqual([l['fonte'] for l in r.context['extrato']],
            ['conferencia','regularizacao','manual'])

    def test_consulta_preserva_banco_e_auditoria(self):
        antes = list(AplicacaoAjuste.objects.values())
        movimentos = list(MovimentacaoEstoqueManual.objects.values())
        with CaptureQueriesContext(connection) as consultas:
            self.resposta()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','(SELECT')) for q in consultas))
        self.assertEqual(list(AplicacaoAjuste.objects.values()), antes)
        self.assertEqual(list(MovimentacaoEstoqueManual.objects.values()), movimentos)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class RegularizacaoBrowserTests(AplicadaFixture, StaticLiveServerTestCase):
    def test_desktop_mobile_evidencias_recolhidas_e_sem_rolagem(self):
        with TemporaryDirectory(prefix='extrato-duplicacoes-chrome-') as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid', value=self.client.cookies['sessionid'].value,
                    url=self.live_server_url))
                for largura in (1440,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=900,
                        deviceScaleFactor=1,mobile=largura<768))
                    tab.call('Page.navigate', dict(url=self.live_server_url+self.historico))
                    tab.wait("!!document.querySelector('[data-registro^=registro-regularizacao-]')")
                    self.assertEqual(tab.evaluate("document.querySelectorAll('[data-registro^=registro-regularizacao-]').length"),1)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('details')].every(e=>!e.open)"))
                    self.assertIn('Regularização aplicada',tab.evaluate("document.querySelector('[data-registro^=registro-regularizacao-]').innerText"))
                    capturar(tab,f'regularizacao-{largura}.png')
                    tab.evaluate("document.querySelector('[data-registro^=registro-regularizacao-] details').open=true; true")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            finally:
                chrome.stop()
