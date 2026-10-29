"""Phase C: synthetic unit evidence, without operational databases."""
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from estoque.models import EventoVenda, ItemVenda, Produto
from offline.browser_support import Chrome
from . import tests_browser as browser
from . import tests_extrato_cronologico as fixtures
from . import tests_extrato_edicoes as edicoes
from .extrato_unidades import snapshot_base
from .models import MovimentoObservado


class SnapshotBaseTests(SimpleTestCase):
    def test_quantidade_base_independente_da_documental_sem_inferir_fator(self):
        item = dict(quantidade=D('3'), unidade='CB', estoque_antes=D('1'),
            estoque_movimentado=D('.250'), estoque_depois=D('.750'), estoque_unidade_snapshot='CX')
        base, erro = snapshot_base(item)
        self.assertFalse(erro)
        self.assertEqual(base, dict(quantidade=D('.250'), unidade='CX'))
        self.assertNotIn('fator', base)

    def test_nao_finitos_incompletos_inconsistentes_ou_negativos(self):
        bom = dict(estoque_antes=D('1'), estoque_movimentado=D('.250'),
            estoque_depois=D('.750'), estoque_unidade_snapshot='CX')
        for campo, valor in [('estoque_antes', None), ('estoque_depois', D('.5')),
                ('estoque_movimentado', D('NaN')), ('estoque_antes', D('Infinity')),
                ('estoque_movimentado', 0), ('estoque_depois', -1), ('estoque_unidade_snapshot', '')]:
            with self.subTest(campo=campo, valor=valor):
                base, erro = snapshot_base(dict(bom, **{campo: valor}))
                self.assertIsNone(base)
                self.assertTrue(erro)


class UnidadesFixture(fixtures.CronologiaFixture):
    def preparar_unidades(self, documental='CB', base='CX', quantidade='3', baixa='.250'):
        self.quantidade, self.baixa = D(quantidade), D(baixa)
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=self.quantidade,
            unidade=documental, estoque_antes=10, estoque_movimentado=self.baixa,
            estoque_depois=10-self.baixa, estoque_unidade_snapshot=base)
        self.posterior.venda.refresh_from_db()
        self.observador = MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=10, depois=10-self.baixa, registrado_em=self.posterior.venda.criado_em,
            origem='venda', referencia=f'Venda #{self.posterior.venda_id}')

    def resposta_unidades(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return r

    def linha(self, resposta):
        return next(l for l in resposta.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)


class UnidadesTests(UnidadesFixture, TestCase):
    def verificar_snapshot(self):
        r = self.resposta_unidades()
        linha = self.linha(r)
        evidencia = linha['unidades_evidencia']
        self.assertEqual(evidencia['tipo'], 'snapshot_base')
        self.assertEqual(evidencia['baixa']['quantidade'], self.baixa)
        self.assertFalse(evidencia['conversao_comprovada'])
        self.assertIsNone(linha['saldo'])
        self.assertContains(r, 'Baixa registrada:')
        self.assertContains(r, 'Conversão histórica não comprovada')
        self.assertEqual(r.context['esperado'], 15)
        self.assertEqual(r.context['fisica'], 10)
        return r

    def test_un_pct(self):
        self.preparar_unidades('UN', 'PCT', '6', '.5')
        self.verificar_snapshot()

    def test_cb_cx(self):
        self.preparar_unidades()
        r = self.verificar_snapshot()
        self.assertContains(r, 'Nota: 3 CB')
        self.assertContains(r, 'Baixa registrada: 0,250 CX')

    def test_kg_sc(self):
        self.preparar_unidades('KG', 'SC', '5', '.1')
        self.verificar_snapshot()

    def test_bd_sc(self):
        self.preparar_unidades('BD', 'SC', '4', '.5')
        self.verificar_snapshot()

    def test_fator_atual_compativel_ou_incompativel_nunca_e_prova(self):
        self.preparar_unidades()
        for fator in (0, 12, 99):
            with self.subTest(fator=fator):
                Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=fator)
                self.verificar_snapshot()

    def test_fator_alterado_depois_da_venda_nao_altera_a_exibicao(self):
        self.preparar_unidades()
        antes = self.linha(self.resposta_unidades())['unidades_evidencia']
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=48,
            unidade_venda_1='FD', unidade_venda_2='UN')
        depois = self.linha(self.resposta_unidades())['unidades_evidencia']
        self.assertEqual(antes, depois)
        self.assertEqual(depois['baixa']['unidade'], 'CX')

    def test_fator_historico_ausente_nao_e_calculado_por_divisao(self):
        self.preparar_unidades()
        evidencia = self.linha(self.verificar_snapshot())['unidades_evidencia']
        self.assertNotIn('fator', evidencia)

    def test_conferencia_posterior_nao_certifica_conversao_da_venda(self):
        self.preparar_unidades()
        self.item.cadastro = dict(self.item.cadastro, base='CX', secundaria='CB', fator='12', fracionado=True)
        self.item.save(update_fields=['cadastro'])
        self.rodada.ok_em = self.posterior.venda.criado_em+timedelta(hours=2)
        self.rodada.save(update_fields=['ok_em'])
        self.verificar_snapshot()

    def test_fator_congelado_incompativel_na_conferencia_nao_resgata_conversao(self):
        self.preparar_unidades()
        self.item.cadastro = dict(self.item.cadastro, base='CX', secundaria='CB', fator='99', fracionado=True)
        self.item.save(update_fields=['cadastro'])
        self.verificar_snapshot()

    def test_conferencia_anterior_sem_regra_vinculada_a_venda_nao_e_prova(self):
        self.preparar_unidades()
        self.item.cadastro = dict(self.item.cadastro, base='CX', secundaria='CB', fator='12', fracionado=True)
        self.item.save(update_fields=['cadastro'])
        self.rodada.ok_em = self.posterior.venda.criado_em-timedelta(hours=2)
        self.rodada.save(update_fields=['ok_em'])
        self.verificar_snapshot()

    def test_precisao_documental_divergente_nao_e_corrigida_nem_certificada(self):
        self.preparar_unidades('KG', 'KG', '1.18', '1.175')
        r = self.resposta_unidades()
        linha = self.linha(r)
        self.assertEqual(linha['unidades_evidencia']['baixa']['quantidade'], D('1.175'))
        self.assertIsNone(linha['saldo'])
        self.assertContains(r, 'Nota: 1,180 KG')
        self.assertContains(r, 'Baixa registrada: 1,175 KG')
        self.posterior.refresh_from_db()
        self.assertEqual(self.posterior.quantidade, D('1.18'))

    def test_arredondamento_preserva_baixa_gravada_sem_reconverter(self):
        self.preparar_unidades('UN', 'PCT', '1', '.333')
        r = self.verificar_snapshot()
        self.assertContains(r, 'Baixa registrada: 0,333 PCT')
        self.assertNotContains(r, '≈')

    def test_snapshot_incompleto_nao_certifica(self):
        self.preparar_unidades()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_movimentado=None)
        r = self.resposta_unidades()
        self.assertIsNone(self.linha(r)['saldo'])
        self.assertNotContains(r, 'Baixa registrada:')

    def test_movimento_bruto_sem_referencia_mostra_variacao_sem_unidade(self):
        self.preparar_unidades()
        MovimentoObservado.objects.filter(pk=self.observador.pk).update(referencia='')
        r = self.resposta_unidades()
        digital = next(l for l in r.context['extrato'] if l['fonte']=='digital')
        self.assertEqual(digital['unidades_evidencia']['tipo'], 'bruto')
        self.assertEqual(digital['unidades_evidencia']['variacao'], D('-.250'))
        self.assertIsNone(digital['saldo'])
        self.assertContains(r, 'Variação registrada: -0,250')

    def test_evento_desconhecido_impede_atribuicao_do_snapshot(self):
        self.preparar_unidades()
        EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento='desconhecido')
        self.assertNotContains(self.resposta_unidades(), 'Baixa registrada:')

    def test_impressao_preserva_a_classificacao_sem_mudar_a_fase_a(self):
        self.preparar_unidades()
        EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento='nota_impressa')
        self.verificar_snapshot()

    def test_paginacao_50_sem_consultas_por_linha_ou_escritas(self):
        self.preparar_unidades()
        with CaptureQueriesContext(connection) as poucas:
            self.resposta_unidades()
        for n in range(55):
            MovimentoObservado.objects.create(produto=self.produto, sequencia=n+2,
                antes=2, depois=1, registrado_em=self.observador.registrado_em+timedelta(seconds=n+1),
                origem='nao_identificada', referencia='')
        with CaptureQueriesContext(connection) as muitas:
            r = self.resposta_unidades()
        self.assertEqual(len(r.context['extrato']), 50)
        self.assertLessEqual(len(muitas), len(poucas)+1)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT', '(SELECT')) for q in muitas))
        r2 = self.resposta_unidades(extrato_pagina=2)
        self.assertEqual(r.context['extrato'].paginator.count,
            len(r.context['extrato'])+len(r2.context['extrato']))

    def test_truncamento_nao_cria_certificacao(self):
        self.preparar_unidades()
        with patch('inventario.extrato_unidades.LIMITE', 1):
            r = self.resposta_unidades()
        self.assertIsNone(self.linha(r)['saldo'])
        self.assertNotContains(r, 'Baixa registrada:')
        self.assertContains(r, 'Evidência incompleta, limitada ou ambígua')

    def test_venda_editada_e_devolucao_nao_herdam_snapshot_atual(self):
        edicoes.EdicoesTests.preparar(self, base='PCT')
        ItemVenda.objects.filter(pk=self.posterior.pk).update(unidade='UN')
        r = self.resposta_unidades()
        doc = self.linha(r)
        self.assertTrue(doc['documento_atual'])
        self.assertIsNone(doc['saida'])
        digitais = [l for l in r.context['extrato'] if l['fonte']=='digital']
        self.assertEqual(len(digitais), 2)
        self.assertTrue(all(l['unidades_evidencia']['tipo']=='bruto' for l in digitais))
        self.assertNotContains(r, 'Baixa registrada:')
        self.assertContains(r, 'snapshot atual não aplicado aos movimentos anteriores')

    movimento = edicoes.EdicoesTests.movimento


@skipUnless(Path(browser.CHROME).is_file(), 'Chrome indisponível')
class UnidadesBrowserTests(UnidadesFixture, StaticLiveServerTestCase):
    def test_desktop_mobile_nota_baixa_e_aviso_sem_rolagem(self):
        self.preparar_unidades()
        with TemporaryDirectory(prefix='extrato-unidades-chrome-') as perfil:
            chrome = Chrome(browser.CHROME, perfil).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', dict(name='sessionid',
                    value=self.client.cookies['sessionid'].value, url=self.live_server_url))
                for largura in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=900,
                        deviceScaleFactor=1, mobile=largura < 768))
                    tab.call('Page.navigate', dict(url=self.live_server_url+self.url))
                    tab.wait("document.readyState === 'complete' && !!document.querySelector('[data-registro^=registro-venda-]')")
                    seletor = f'[data-registro="registro-venda-{self.posterior.pk}"]'
                    texto = tab.evaluate(f'document.querySelector({seletor!r}).innerText')
                    self.assertIn('Nota: 3 CB', texto)
                    self.assertIn('Baixa registrada: 0,250 CX', texto)
                    self.assertIn('Conversão histórica não comprovada', texto)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    tab.evaluate(f'document.querySelector({(seletor+" details")!r}).open=true; true')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    browser.capturar(tab, f'extrato-unidades-{largura}.png')
                # An edited converted note cannot lend its current base snapshot
                # to either historical movement, including on narrow phones.
                ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=2,
                    estoque_movimentado=D('.167'), estoque_depois=D('9.833'))
                MovimentoObservado.objects.create(produto=self.produto, sequencia=2,
                    antes=D('9.750'), depois=D('9.833'), origem='devolucao_venda',
                    referencia=self.observador.referencia,
                    registrado_em=self.observador.registrado_em+timedelta(minutes=1))
                evento = EventoVenda.objects.create(venda_id=self.posterior.venda_id,
                    tipo_evento='quantidade_item_alterada',
                    descricao=f'Quantidade alterada na nota: {self.produto.nome}. De 3 CB para 2 CB.')
                EventoVenda.objects.filter(pk=evento.pk).update(
                    criado_em=self.observador.registrado_em+timedelta(minutes=1))
                for largura in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=900,
                        deviceScaleFactor=1, mobile=largura < 768))
                    tab.call('Page.navigate', dict(url=self.live_server_url+self.url))
                    tab.wait("document.readyState === 'complete' && !!document.querySelector('[data-registro^=registro-digital-]')")
                    texto = tab.evaluate(f'document.querySelector({seletor!r}).innerText')
                    self.assertIn('Quantidade atual da nota: 2 CB', texto)
                    self.assertIn('Conversão histórica não comprovada', texto)
                    self.assertNotIn('Baixa registrada:', texto)
                    self.assertEqual(tab.evaluate("document.querySelectorAll('[data-registro^=registro-digital-]').length"), 2)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    browser.capturar(tab, f'extrato-unidades-editadas-{largura}.png')
            finally:
                chrome.stop()
