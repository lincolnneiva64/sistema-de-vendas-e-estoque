"""Apresentação de créditos, cobranças e reserva em banco descartável."""
import base64
import os
import re
from datetime import date, datetime, timezone as utc
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from estoque import views, tests_rastreabilidade_apresentacao as base
from estoque.models import (CreditoCliente, RegistroCobrancaCliente, ContaReceber,
    MovimentoFinanceiro, ContaFinanceira, RecebimentoContaReceber, Produto, Venda, ItemVenda)

INSTANTE = datetime(2018, 11, 5, 1, 2, 3, tzinfo=utc.utc)
TEXTO = '04/11/2018 às 22:02:03'
ROOT = Path(__file__).resolve().parents[1]


class Fixture(base.Fixture):
    def setUp(self):
        super().setUp()
        views._garantir_contas_financeiras_padrao()
        self.conta = ContaReceber.objects.create(cliente=self.cliente, venda=self.venda,
            data_emissao=date(2018, 10, 1), data_vencimento=date(2018, 10, 2),
            valor_original=50, valor_em_aberto=50)
        for valor, instante in [(Decimal('30.50'), INSTANTE.replace(day=4)), (Decimal('-5.25'), INSTANTE)]:
            c = CreditoCliente.objects.create(cliente=self.cliente, valor=valor,
                origem_conta_receber=self.conta, observacao='Histórico antigo preservado')
            CreditoCliente.objects.filter(pk=c.pk).update(criado_em=instante)
        registro = RegistroCobrancaCliente.objects.create(cliente=self.cliente, conta=self.conta,
            tipo='telefone', status='contatado', observacao='Contato antigo preservado')
        RegistroCobrancaCliente.objects.filter(pk=registro.pk).update(criado_em=INSTANTE)
        movimento = MovimentoFinanceiro.objects.create(conta=views._conta_financeira_padrao('reserva'),
            tipo='entrada', valor=Decimal('123.45'), data=date(2018, 10, 3), descricao='Reserva antiga preservada')
        MovimentoFinanceiro.objects.filter(pk=movimento.pk).update(criado_em=INSTANTE)

    def paginas(self):
        return [(name, reverse('estoque:'+name, args=args)) for name,args in [
            ('contas_receber', []), ('cliente_credito_detalhe', [self.cliente.pk]),
            ('central_cobrancas', []), ('caixa_banco', [])]]

    def retrato(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            [CreditoCliente, RegistroCobrancaCliente, ContaReceber, RecebimentoContaReceber,
             MovimentoFinanceiro, ContaFinanceira, Produto, Venda, ItemVenda]}


class HorariosLote2Tests(Fixture, TestCase):
    def test_paginas_antigas_fuso_e_integridade(self):
        antes = self.retrato()
        with timezone.override('Asia/Tokyo'):
            for nome,url in self.paginas():
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, nome)
                self.assertContains(response, TEXTO)
                if nome == 'contas_receber':
                    credito = response.context['clientes_com_credito'][0]
                    self.assertEqual(credito['total_credito'], Decimal('25.25'))
                    self.assertEqual(credito['credito_mais_recente'], INSTANTE)
                if nome == 'cliente_credito_detalhe':
                    self.assertEqual(response.context['credito_atual'], Decimal('25.25'))
                    self.assertEqual(response.context['ultimo_movimento']['credito_antes'], Decimal('30.50'))
                    self.assertEqual(response.context['ultimo_movimento']['credito_depois'], Decimal('25.25'))
                if nome == 'caixa_banco':
                    self.assertEqual(response.context['movimentos_reserva'][0].data, date(2018,10,3))
        self.assertEqual(antes, self.retrato())

    def test_ausencias_nos_sete_usos_reais(self):
        total = 0
        for nome,_ in self.paginas():
            source = (ROOT/f'estoque/templates/estoque/{nome}.html').read_text(encoding='utf-8')
            paths = re.findall(r'{% instante_registro ([\w.]+) %}', source)
            for path in paths:
                # Outros blocos já aprovados da página não fazem parte deste lote.
                if path not in ['credito.credito_mais_recente','ultimo_movimento.criado_em',
                    'credito.criado_em','cliente.ultimo_contato.criado_em','registro.criado_em','movimento.criado_em']:
                    continue
                total += 1
                for value in [None, date(2018,11,4), datetime(2018,11,4)]:
                    obj = value
                    for key in reversed(path.split('.')[1:]):
                        obj = SimpleNamespace(**{key:obj})
                    html = Template('{% load horarios_documentos %}{% instante_registro '+path+' %}').render(Context({path.split('.')[0]:obj}))
                    self.assertIn('Horário não registrado', html)
                    self.assertNotIn('00:00:00', html)
        self.assertEqual(total, 7)


class HorariosLote2BrowserTests(Fixture, StaticLiveServerTestCase):
    def test_desktop_mobile_sem_gravacao(self):
        out = Path(os.environ['HORARIOS_A2_ARTIFACTS'])
        antes = self.retrato()
        with TemporaryDirectory(prefix='horarios-a2-chrome-') as profile:
            chrome = Chrome(base.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride', {'timezoneId':'Asia/Tokyo'})
                for width in (1366,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    for nome,url in self.paginas():
                        tab.call('Page.navigate', {'url':self.live_server_url+url})
                        tab.wait('document.readyState === "complete"')
                        self.assertEqual(tab.evaluate('location.pathname'), url)
                        if nome == 'central_cobrancas':
                            tab.evaluate("[...document.querySelectorAll('[data-cobranca-toggle]')].filter(e=>e.dataset.cobrancaToggle.startsWith('historico-')).forEach(e=>e.click());true")
                        found = tab.evaluate("""[...document.querySelectorAll('.horario-registro')].filter(e=>e.textContent.includes('04/11/2018 às 22:02:03')&&e.getClientRects().length).map(e=>({weight:getComputedStyle(e).fontWeight,color:getComputedStyle(e).color}))""")
                        self.assertTrue(found, (nome,width))
                        self.assertTrue(all(x['weight']=='700' and x['color']=='rgb(30, 41, 59)' for x in found))
                        tab.evaluate("[...document.querySelectorAll('.horario-registro')].find(e=>e.textContent.includes('04/11/2018 às 22:02:03')&&e.getClientRects().length).scrollIntoView({block:'center'});true")
                        image = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})
                        (out/f'{nome}-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(antes,self.retrato())
            finally:
                chrome.stop()
