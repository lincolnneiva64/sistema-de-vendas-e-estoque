from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection, transaction
from django.test import TestCase, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Venda, ItemVenda, EventoVenda, Produto
from estoque.views import _baixar_estoque_produto
from offline.browser_support import Chrome
from . import ajustes, services
from .models import MovimentoObservado, AplicacaoAjuste
from .saldos_apresentacao import equivalencia, cadeia_registrada
from .tests import produto
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME, capturar


class ConversoesTests(SimpleTestCase):
    def test_embalagens_6_12_24_sem_unidades_fracionarias(self):
        for fator, valor, inteiro in ((6,'9.833',59),(12,'2.083',25),(24,'1.042',25)):
            cadastro=dict(base='PCT',secundaria='UN',fracionado=True,fator=str(fator))
            e=equivalencia(D(valor),'PCT',cadastro)
            self.assertEqual(e['unidades'],inteiro)
            self.assertEqual(e['unidades'],e['unidades'].to_integral_value())
            self.assertTrue(e['arredondada'])
        self.assertEqual(equivalencia(D('1.331'),'PCT',dict(base='PCT',secundaria='UN',fracionado=True,fator='6'))['unidades'],8)

    def test_sem_conversao_comprovada_nao_inventa(self):
        for cadastro in ({},dict(base='PCT',fator='6'),dict(base='KG',continua=True),
                dict(base='PCT',fracionado=True,secundaria='KG',fator='6'),
                dict(base='PCT',fracionado=True,secundaria='UN',fator='NaN')):
            self.assertIsNone(equivalencia(D(1),cadastro.get('base'),cadastro))

    def test_sinal_e_composicao(self):
        c=dict(base='CX',secundaria='UN',fracionado=True,fator='6')
        e=equivalencia(D('-9.502'),'CX',c)
        self.assertEqual(e['unidades'],-57)
        self.assertTrue(e['negativa'])
        e=equivalencia(D('9.833'),'CX',c)
        self.assertEqual((e['embalagens'],e['avulsos']),(9,5))


class SaldoFixture(AjustesFixture):
    def setUp(self):
        super().setUp()
        self.produto=produto(nome='Cola fictícia 6/2L',unidade_compra='PCT',unidade_venda_1='PCT',
            unidade_venda_2='UN',fator_conversao=6,quantidade=D('1.331'))
        self.inv=services.criar_inventario(self.usuario,'Saldos fictícios','manual',[self.produto.pk])
        rodada=self.inv.itens.get().rodadas.get()
        acesso,_=services.gerar_acesso(self.usuario,self.inv.pk,self.funcionario.pk,[rodada.pk])
        services.salvar_contagem(acesso.pk,rodada.pk,0,'10','5',True)
        self.rodada=self.fresca(rodada.pk)
        venda=Venda.objects.create(data_venda=timezone.localdate())
        with transaction.atomic():
            snap=_baixar_estoque_produto(self.produto.pk,D(1),unidade='PCT')
            self.venda=ItemVenda.objects.create(venda=venda,produto=self.produto,quantidade=1,unidade='PCT',
                preco_unitario=0,valor_total=0,**snap)
            mov=MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            MovimentoObservado.objects.filter(pk=mov.pk).update(referencia=f'Venda #{venda.pk}')
        EventoVenda.objects.create(venda=venda,tipo_evento='venda_gravada')
        proposta=self.aprovar()
        self.aplicada=ajustes.aplicar(self.usuario,self.inv.pk,
            ajustes.previa_aplicacao(self.inv.pk,[proposta]),confirmacao=True)[0]
        self.url=reverse('inventario:historico_produto',args=[self.inv.pk,self.rodada.pk])


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class SaldosTests(SaldoFixture, TestCase):
    def resposta(self, **params):
        r=self.client.get(self.url,params)
        self.assertEqual(r.status_code,200)
        return r

    def test_ok_venda_e_regularizacao_sem_confundir_saldo_antigo(self):
        r=self.resposta()
        linha=next(l for l in r.context['extrato'] if l['fonte']=='venda')
        self.assertEqual(linha['saldo'],D('.331'))
        self.assertEqual(linha['saldo_contagem']['antes'],D('10.833'))
        self.assertEqual(linha['saldo_contagem']['depois'],D('9.833'))
        self.assertEqual(linha['antes_equivalencia']['unidades'],65)
        self.assertEqual(linha['saldo_equivalencia']['unidades'],59)
        self.assertEqual(linha['saida_equivalencia']['unidades'],6)
        eq=r.context['extrato'].ok_equivalencias
        self.assertEqual([eq[k]['unidades'] for k in ('esperado','contado','diferenca')],[8,65,57])
        self.assertContains(r,'Estoque registrado antes da conferência')
        self.assertContains(r,'Diferença encontrada — Sobra')
        self.assertContains(r,'Saldo registrado pelo sistema antes da regularização: 0,331 PCT')
        self.assertContains(r,'Saldo registrado após regularização')
        self.assertContains(r,'9 PCT + 5 UN')

    def test_cadeia_incompleta_ou_alterada_nao_projeta(self):
        self.aplicada.proposta=self.aplicada.proposta.__class__.objects.select_related('confirmacao').get(pk=self.aplicada.proposta_id)
        for alterar in ('documentos','sequencia','contado'):
            copia=deepcopy(self.aplicada)
            prep=copia.evidencia['preparacao']
            prep[alterar]=[] if alterar=='documentos' else 999
            self.assertEqual(cadeia_registrada(copia,self.rodada),{})

    def test_edicao_posterior_da_venda_nao_reconstroi(self):
        EventoVenda.objects.create(venda_id=self.venda.venda_id,tipo_evento='evento_desconhecido',descricao='Dúvida fictícia')
        linha=next(l for l in self.resposta().context['extrato'] if l['fonte']=='venda')
        self.assertNotIn('saldo_contagem',linha)

    def test_cadastro_atual_alterado_preserva_conversao_historica(self):
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=24)
        linha=next(l for l in self.resposta().context['extrato'] if l['fonte']=='venda')
        self.assertEqual(linha['saldo_equivalencia']['unidades'],59)

    def test_copia_historica_sem_fator_nao_inventa_unidades(self):
        item=self.rodada.item
        item.cadastro=dict(item.cadastro,fator='',secundaria='')
        item.save(update_fields=['cadastro'])
        r=self.resposta()
        self.assertIsNone(r.context['extrato'].ok_equivalencias['contado'])
        linha=next(l for l in r.context['extrato'] if l['fonte']=='venda')
        self.assertNotIn('saldo_contagem',linha)

    def test_multiplas_saidas_exigem_cadeia_completa(self):
        from datetime import timedelta
        from django.utils.dateparse import parse_datetime
        self.aplicada.proposta=self.aplicada.proposta.__class__.objects.select_related('confirmacao').get(pk=self.aplicada.proposta_id)
        copia=deepcopy(self.aplicada)
        docs=copia.evidencia['preparacao']['documentos']
        outro=deepcopy(docs[0])
        outro.update(item=999999,venda=999999,observador=999999,referencia='Venda #999999',
            sequencia=docs[0]['sequencia']+1,antes='.331',depois='.164',quantidade='.167',movimentado='.167',
            criado_em=(parse_datetime(docs[0]['criado_em'])+timedelta(microseconds=1)).isoformat())
        docs.append(outro)
        copia.evidencia['preparacao']['sequencia']+=1
        copia.antes=D('.164');copia.depois=D('9.666')
        cadeia=cadeia_registrada(copia,self.rodada)
        self.assertEqual(cadeia[999999]['depois'],D('9.666'))
        docs.pop(0)
        self.assertEqual(cadeia_registrada(copia,self.rodada),{})

    def test_sem_aplicacao_sem_conversao_nao_inventa(self):
        from unittest.mock import patch
        with patch('inventario.saldos_apresentacao.cadeia_registrada',return_value={}):
            linha=next(l for l in self.resposta().context['extrato'] if l['fonte']=='venda')
        self.assertNotIn('saldo_contagem',linha)

    def test_a_preservada_e_get_somente_leitura(self):
        antes=list(Produto.objects.values_list('pk','quantidade'))
        apps=list(AplicacaoAjuste.objects.values())
        with CaptureQueriesContext(connection) as consultas:
            r=self.resposta()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','(SELECT')) for q in consultas))
        self.assertEqual(list(Produto.objects.values_list('pk','quantidade')),antes)
        self.assertEqual(list(AplicacaoAjuste.objects.values()),apps)
        a=self.resposta(visual='a')
        self.assertNotContains(a,'Antes da venda, conforme contagem física')
        self.assertNotContains(a,'equivalencia-un')


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
@skipUnless(Path(CHROME).is_file(),'Chrome indisponível')
class SaldosBrowserTests(SaldoFixture,StaticLiveServerTestCase):
    def test_desktop_mobile_saldos_e_evidencias(self):
        with TemporaryDirectory(prefix='saldos-chrome-') as perfil:
            chrome=Chrome(CHROME,perfil).start()
            try:
                tab=chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie',dict(name='sessionid',value=self.client.cookies['sessionid'].value,url=self.live_server_url))
                for largura in (1440,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<768))
                    tab.call('Page.navigate',dict(url=self.live_server_url+self.url))
                    tab.wait("!!document.querySelector('.saldo-contagem-antes')")
                    tab.wait("getComputedStyle(document.querySelector('.evento-conferencia')).display === 'block'")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'),
                        tab.evaluate("({largura:innerWidth,documento:document.documentElement.scrollWidth,fora:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>e.tagName+'.'+e.className).slice(0,12)})"))
                    texto=tab.evaluate("document.querySelector('[data-registro^=registro-venda-]').innerText")
                    self.assertIn('65 UN',texto)
                    self.assertIn('59 UN',texto)
                    self.assertNotIn('0,331',texto)
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.comparacao .equivalencia-un')].every(e=>getComputedStyle(e).display==='block')"))
                    capturar(tab,f'saldos-{largura}.png')
                    tab.evaluate("document.querySelector('[data-registro^=registro-venda-] details').open=true;true")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    capturar(tab,f'saldos-evidencias-{largura}.png')
            finally:
                chrome.stop()
