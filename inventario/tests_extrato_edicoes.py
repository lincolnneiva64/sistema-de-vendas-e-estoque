"""Phase B regressions; synthetic reproductions, never operational records."""
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from estoque.models import EventoVenda, ItemVenda, ItemVendaRemovido, Venda
from offline.browser_support import Chrome
from . import tests_extrato_cronologico as fixtures
from . import tests_browser as browser
from . import tests as produtos
from .models import MovimentoObservado


class EdicoesTests(fixtures.CronologiaFixture, TestCase):
    def preparar(self, nome='Itaipava 12/269Ml', original='4', atual='3', base='PCT', antes='9'):
        self.produto.nome = nome
        self.produto.save(update_fields=['nome'])
        self.original, self.atual = D(original), D(atual)
        self.antes = D(antes)
        self.hora = self.posterior.venda.criado_em
        # Fetch the intentionally backdated timestamp from the database.
        self.posterior.venda.refresh_from_db()
        self.hora = self.posterior.venda.criado_em
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=self.atual,
            unidade=base, estoque_unidade_snapshot=base, estoque_antes=self.antes,
            estoque_movimentado=self.atual, estoque_depois=self.antes-self.atual)
        self.saida = self.movimento(1, self.antes, self.antes-self.original, self.hora-timedelta(seconds=3))
        self.edicao = self.movimento(2, self.antes-self.original, self.antes-self.atual,
            self.hora+timedelta(minutes=31), 'devolucao_venda' if self.atual < self.original else 'venda')
        self.evento = EventoVenda.objects.create(venda=self.posterior.venda,
            tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {nome}. De {original} {base} para {atual} {base}. Total sera recalculado ao final da edicao.')
        EventoVenda.objects.filter(pk=self.evento.pk).update(criado_em=self.edicao.registrado_em+timedelta(seconds=1))

    def movimento(self, seq, antes, depois, instante, origem='venda', referencia=None):
        return MovimentoObservado.objects.create(produto=self.produto, sequencia=seq,
            antes=antes, depois=depois, registrado_em=instante, origem=origem,
            referencia=referencia if referencia is not None else f'Venda #{self.posterior.venda_id}')

    def resposta(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return r

    def verificar(self):
        r = self.resposta()
        linhas = list(r.context['extrato'])
        doc = next(l for l in linhas if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        movimentos = [l for l in linhas if l['fonte']=='digital']
        self.assertEqual(len(movimentos), 2)
        self.assertIsNone(doc['saida'])
        self.assertIsNone(doc['saldo'])
        self.assertTrue(doc['documento_atual'])
        self.assertEqual(movimentos[0]['saida'], self.original)
        self.assertEqual(movimentos[0]['instante'], self.saida.registrado_em)
        self.assertEqual(movimentos[1]['instante'], self.edicao.registrado_em)
        delta = sum((l['saida'] or 0)-(l['entrada'] or 0) for l in movimentos)
        self.assertEqual(delta, self.atual)
        self.assertEqual(movimentos[1]['saldo'], self.antes-self.atual)
        self.assertContains(r, 'Quantidade atual da nota:')
        self.assertContains(r, 'não é outra baixa')
        self.assertEqual(r.context['esperado'], 15)
        self.assertEqual(r.context['fisica'], 10)
        return r, movimentos

    def test_itaipava_1031(self):
        self.preparar()
        r, linhas = self.verificar()
        self.assertEqual(linhas[1]['entrada'], 1)
        self.assertContains(r, 'Devolução por edição')

    def test_alternativa_visual_a_tambem_separa_nota_atual(self):
        self.preparar()
        self.rodada.ok_em = self.hora+timedelta(hours=1)
        self.rodada.save(update_fields=['ok_em'])
        r = self.resposta(visual='a')
        doc = next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        self.assertIsNone(doc['saida'])
        self.assertTrue(doc['documento_atual'])
        self.assertContains(r, 'Saída original')
        self.assertContains(r, 'Devolução por edição')

    def test_arroz_975(self):
        self.preparar('Arroz Fazenda', '2', '1', 'KG', antes='53')
        self.verificar()

    def test_skol_1031(self):
        self.preparar('Skol 12/350Ml', '2', '1', 'DZ', antes='5.583')
        self.verificar()

    def test_imperio_1010(self):
        self.preparar('Império Ultra', '1', '1.5', 'DZ', antes='1.5')
        r, linhas = self.verificar()
        self.assertEqual(linhas[1]['saida'], D('.5'))
        self.assertContains(r, 'Acréscimo por edição')

    def test_sacola_971_sem_inventar_abertura_ou_conversao(self):
        self.preparar('Sacola 2Kg', '1', '2', 'CT')
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_antes=D('.6'),
            estoque_movimentado=D('.2'), estoque_depois=D('.4'), estoque_unidade_snapshot='MIL')
        MovimentoObservado.objects.filter(pk=self.saida.pk).update(antes=D('.1'), depois=0)
        MovimentoObservado.objects.filter(pk=self.edicao.pk).update(antes=D('.5'), depois=D('.4'))
        self.movimento(3, 0, D('.5'), self.hora+timedelta(minutes=1), 'nao_identificada', '')
        r = self.resposta()
        self.assertIsNone(r.context['extrato'].saldo_inicial)
        digitais = [l for l in r.context['extrato'] if l['fonte']=='digital']
        self.assertEqual(len(digitais), 3)
        self.assertTrue(all(l['saldo'] is None for l in digitais))
        self.assertTrue(all(l['saida'] is None for l in digitais))

    def test_sal_966_edicao_documental_sem_nova_baixa(self):
        self.preparar('Sal 30/1Kg', '0.990', '1', 'KG')
        self.edicao.delete()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_unidade_snapshot='FD',
            estoque_antes=D('8.033'), estoque_movimentado=D('.033'), estoque_depois=8)
        MovimentoObservado.objects.filter(pk=self.saida.pk).update(antes=D('.033'), depois=0)
        r = self.resposta()
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='digital']), 1)
        self.assertNotContains(r, 'Acréscimo por edição')
        self.assertContains(r, 'Nota alterada em')
        doc = next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        self.assertIsNone(doc['saida'])
        self.assertIsNone(doc['saldo'])

    def test_cancelamento_nao_deduz_quantidade_devolvida(self):
        self.preparar()
        Venda.objects.filter(pk=self.posterior.venda_id).update(cancelada=True,
            estoque_devolvido_cancelamento=True, estoque_devolvido_cancelamento_em=self.edicao.registrado_em)
        r = self.resposta()
        digitais = [l for l in r.context['extrato'] if l['fonte']=='digital']
        self.assertEqual(len(digitais), 2)
        self.assertTrue(all(l['saldo'] is None for l in digitais))
        cancelamento = next(l for l in r.context['extrato'] if l['fonte']=='cancelamento')
        self.assertIsNone(cancelamento['entrada'])

    def test_remocao_nao_recria_item_original(self):
        self.preparar()
        removido = ItemVendaRemovido.objects.create(venda_id=self.posterior.venda_id,
            produto=self.produto, produto_nome_snapshot=self.produto.nome,
            quantidade_snapshot=3, unidade_snapshot='PCT', preco_unitario_snapshot=0,
            valor_total_snapshot=0, estoque_devolvido=True,
            estoque_devolvido_em=self.edicao.registrado_em)
        ItemVendaRemovido.objects.filter(pk=removido.pk).update(criado_em=self.edicao.registrado_em)
        self.posterior.delete()
        r = self.resposta()
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='digital']), 2)
        self.assertNotContains(r, 'Saída original')
        remocao = next(l for l in r.context['extrato'] if l['fonte']=='remocao')
        self.assertIsNone(remocao['entrada'])
        self.assertIsNone(remocao['saida'])

    def test_multiplos_itens_mesmo_produto_sem_prova(self):
        self.preparar()
        ItemVenda.objects.create(venda_id=self.posterior.venda_id, produto=self.produto,
            quantidade=1, unidade='PCT', preco_unitario=0, valor_total=0)
        r = self.resposta()
        docs = [l for l in r.context['extrato'] if l['fonte']=='venda' and l['documento']==self.posterior.venda_id]
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(l['saida'] is None for l in docs))
        self.assertNotContains(r, 'Saída original')

    def test_sem_referencia_nao_associa_por_horario(self):
        self.preparar()
        MovimentoObservado.objects.filter(pk=self.edicao.pk).update(referencia='')
        r = self.resposta()
        linha = next(l for l in r.context['extrato'] if l['fonte']=='digital' and l['pk']==self.edicao.pk)
        self.assertFalse(linha['url'])
        self.assertIsNone(linha['saldo'])
        self.assertNotContains(r, 'Saída original')

    def test_horarios_coincidentes_preservam_registros(self):
        self.preparar()
        MovimentoObservado.objects.filter(pk=self.edicao.pk).update(registrado_em=self.saida.registrado_em)
        anterior = self.movimento(0, 10, 9, self.saida.registrado_em, 'nao_identificada', '')
        r = self.resposta()
        self.assertEqual([l['pk'] for l in r.context['extrato'] if l['fonte']=='digital'],
            [anterior.pk, self.saida.pk, self.edicao.pk])

    def test_evento_desconhecido_nao_certifica(self):
        self.preparar()
        EventoVenda.objects.create(venda=self.posterior.venda, tipo_evento='desconhecido')
        r = self.resposta()
        self.assertNotContains(r, 'Registro digital corroborado')

    def test_snapshot_inconsistente_nao_certifica(self):
        self.preparar()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(estoque_depois=99)
        self.assertNotContains(self.resposta(), 'Registro digital corroborado')

    def test_paginacao_50_e_prova_independente_da_pagina(self):
        self.preparar()
        for n in range(50):
            self.movimento(3+n, 10, 9, self.hora+timedelta(seconds=n), 'nao_identificada', '')
        r = self.resposta()
        pagina = r.context['extrato']
        self.assertEqual(len(pagina), 50)
        self.assertEqual(pagina.paginator.per_page, 50)
        primeira = [l['pk'] for l in pagina if l['fonte']=='digital']
        r2 = self.resposta(extrato_pagina=2)
        segunda = [l for l in r2.context['extrato'] if l['fonte']=='digital']
        self.assertFalse(set(primeira) & {l['pk'] for l in segunda})
        retorno = next(l for l in segunda if l['pk']==self.edicao.pk)
        self.assertEqual(retorno['entrada'], 1)
        self.assertEqual(retorno['saldo'], 6)
        self.assertEqual(pagina.paginator.count, len(list(pagina))+len(list(r2.context['extrato'])))

    def test_get_somente_leitura_e_sem_consultas_por_linha(self):
        self.preparar()
        with CaptureQueriesContext(connection) as consultas:
            self.resposta()
        total = len(consultas)
        for n in range(30):
            self.movimento(3+n, 10, 9, self.hora+timedelta(seconds=n), 'nao_identificada', '')
        with CaptureQueriesContext(connection) as consultas:
            self.resposta()
        self.assertLessEqual(len(consultas), total+1)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT', '(SELECT')) for q in consultas))

    def test_atual_mais_devolucao_nao_prova_original(self):
        self.preparar()
        self.evento.delete()
        r = self.resposta()
        self.assertNotContains(r, 'Saída original')
        self.assertNotContains(r, 'Registro digital corroborado')

    def test_nome_ambiguo_nao_certifica(self):
        self.preparar()
        produtos.produto(nome=self.produto.nome)
        self.assertNotContains(self.resposta(), 'Registro digital corroborado')

    def test_edicoes_de_outros_produtos_na_mesma_nota_exigem_identidade(self):
        self.preparar()
        outro = produtos.produto(nome='Skol 12/350Ml')
        ItemVenda.objects.create(venda_id=self.posterior.venda_id, produto=outro,
            quantidade=1, unidade='DZ', preco_unitario=0, valor_total=0)
        evento = EventoVenda.objects.create(venda_id=self.posterior.venda_id,
            tipo_evento='quantidade_item_alterada',
            descricao='Quantidade alterada na nota: Skol 12/350Ml. De 2 DZ para 1 DZ.')
        self.verificar()
        EventoVenda.objects.filter(pk=evento.pk).update(
            descricao='Quantidade alterada na nota: Produto sem identificação. De 2 DZ para 1 DZ.')
        self.assertNotContains(self.resposta(), 'Registro digital corroborado')

    def test_evidencia_truncada_nao_certifica_nem_oculta_movimentos(self):
        self.preparar()
        with patch('inventario.extrato_edicoes.LIMITE', 1):
            r = self.resposta()
        self.assertNotContains(r, 'Registro digital corroborado')
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='digital']), 2)
        self.assertContains(r, 'Evidência limitada')

    def test_cancelamento_de_uma_saida_exata_nao_a_oculta(self):
        self.preparar()
        self.edicao.delete()
        self.evento.delete()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=4,
            estoque_movimentado=4, estoque_depois=5)
        Venda.objects.filter(pk=self.posterior.venda_id).update(cancelada=True)
        r = self.resposta()
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='digital']), 1)
        self.assertIsNone(next(l for l in r.context['extrato'] if l['fonte']=='venda'
            and l['pk']==self.posterior.pk)['saida'])

    def test_saldos_digitais_nao_fazem_ponte_sobre_movimento_sem_origem(self):
        self.preparar()
        self.movimento(3, 5, 5, self.hora+timedelta(seconds=1), 'nao_identificada', '')
        r = self.resposta()
        linhas = [l for l in r.context['extrato'] if l.get('unidade_comprovada')]
        self.assertEqual(len(linhas), 2)
        self.assertTrue(all(l['saldo_pontual'] for l in linhas))
        self.assertTrue(all('continuidade não certificada' in l['saldo_continuidade'] for l in linhas))


@skipUnless(Path(browser.CHROME).is_file(), 'Chrome indisponível')
class EdicoesBrowserTests(fixtures.CronologiaFixture, StaticLiveServerTestCase):
    preparar = EdicoesTests.preparar
    movimento = EdicoesTests.movimento

    def test_desktop_mobile_sem_rolagem_e_nota_sem_outra_baixa(self):
        self.preparar()
        with TemporaryDirectory(prefix='extrato-edicoes-chrome-') as perfil:
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
                    tab.wait("!!document.querySelector('[data-registro^=registro-digital-]')")
                    tab.wait("document.readyState === 'complete'")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'),
                        tab.evaluate("({width:innerWidth, scroll:document.documentElement.scrollWidth, excess:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(-12).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width,e.innerText?.slice(0,80)])})"))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('details')].every(e=>!e.open)"))
                    seletor = f'[data-registro="registro-venda-{self.posterior.pk}"]'
                    texto = tab.evaluate(f'document.querySelector({seletor!r}).innerText')
                    self.assertIn('Quantidade atual da nota:', texto)
                    self.assertIn('não é outra baixa', texto)
                    self.assertNotIn('Saída −', texto)
                    self.assertEqual(tab.evaluate("document.querySelectorAll('[data-registro^=registro-digital-]').length"), 2)
                    tab.evaluate(f'document.querySelector({(seletor+" details")!r}).open=true; true')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertIn('Nota alterada em', tab.evaluate(f'document.querySelector({seletor!r}).innerText'))
                    browser.capturar(tab, f'extrato-edicoes-{largura}.png')
            finally:
                chrome.stop()
