"""Horário da sinalização: apresentação sem alteração de inventário ou estoque."""
import base64
import os
import re
from datetime import date, datetime, timezone as utc
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from estoque.tests_rastreabilidade_apresentacao import CHROME
from inventario import services
from inventario.tests import produto
from inventario.models import SinalizacaoFisica
from offline.browser_support import Chrome

INSTANTE = datetime(2018,11,5,1,2,3,tzinfo=utc.utc)
TEXTO = '04/11/2018 às 22:02:03'
ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('horarios-inventario','','teste')
        self.client.force_login(self.user)
        self.produto = produto()
        self.inventario = services.criar_inventario(self.user,'Conferência teste','manual',[self.produto.pk])
        rodada = self.inventario.itens.get().rodadas.get()
        self.sinal = SinalizacaoFisica.objects.create(rodada=rodada,chave=uuid4(),
            descricao='Sinalização antiga preservada; instante físico não informado.',usuario=self.user)
        SinalizacaoFisica.objects.filter(pk=self.sinal.pk).update(criado_em=INSTANTE)
        self.url = reverse('inventario:detalhe',args=[self.inventario.pk])

    def retrato(self):
        modelos = list(apps.get_app_config('inventario').get_models())
        modelos += [apps.get_model('estoque',n) for n in ['Produto','MovimentacaoEstoqueManual','MovimentoFinanceiro']]
        return {m._meta.label:list(m.objects.order_by('pk').values()) for m in modelos}


class HorariosSinalizacaoTests(Fixture,TestCase):
    def test_registro_antigo_belem_e_leitura_sem_mutacao(self):
        antes = self.retrato()
        with timezone.override('Asia/Tokyo'):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Sinalização registrada em')
        self.assertContains(response,TEXTO)
        self.assertContains(response,self.sinal.descricao)
        self.assertEqual(antes,self.retrato())

    def test_ausencia_sem_inventar_horario(self):
        source = (ROOT/'inventario/templates/inventario/detalhe.html').read_text(encoding='utf8')
        trecho = re.search(r'<p[^>]*>Sinalização registrada em.*?</p>',source).group()
        for valor in [None,'',date(2018,11,4),datetime(2018,11,4)]:
            html = Template('{% load horarios_documentos %}'+trecho).render(Context({'sinal':SimpleNamespace(criado_em=valor)}))
            self.assertIn('Horário não registrado',html)
            self.assertNotIn('00:00:00',html)
        # O único incremento no template permanece estritamente no registro da sinalização.
        old = (ROOT/'artifacts/horarios-grupo-a-lote4-20261028/antes/inventario/templates/inventario/detalhe.html').read_text(encoding='utf8')
        self.assertEqual(source,old.replace('<p>{{ sinal.criado_em }}',
            '<p style="color:#334155">Sinalização registrada em {% instante_registro sinal.criado_em %}',1))


class HorariosSinalizacaoBrowserTests(Fixture,StaticLiveServerTestCase):
    def test_desktop_mobile_sem_alterar_operacoes(self):
        antes = self.retrato()
        out = Path(os.environ['HORARIOS_A4_ARTIFACTS'])
        with TemporaryDirectory(prefix='horarios-a4-chrome-') as profile:
            chrome = Chrome(CHROME,profile).start()
            try:
                tab = chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride',{'timezoneId':'Asia/Tokyo'})
                for width in (1366,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    tab.call('Page.navigate',{'url':self.live_server_url+self.url})
                    tab.wait('document.readyState === "complete"')
                    self.assertEqual(tab.evaluate('location.pathname'),self.url)
                    tab.evaluate("[...document.querySelectorAll('details')].filter(e=>e.querySelector('.ocorrencias-fisicas')||e.classList.contains('ocorrencias-fisicas')).forEach(e=>e.open=true);true")
                    found = tab.evaluate("""[...document.querySelectorAll('.ocorrencias-fisicas .horario-registro')].filter(e=>e.textContent.includes('04/11/2018 às 22:02:03')&&e.getClientRects().length).map(e=>({weight:getComputedStyle(e).fontWeight,color:getComputedStyle(e).color}))""")
                    self.assertTrue(found,width)
                    self.assertTrue(all(x['weight']=='700' and x['color']=='rgb(30, 41, 59)' for x in found))
                    tab.evaluate("document.querySelector('.ocorrencias-fisicas .horario-registro').scrollIntoView({block:'center'});true")
                    image = tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                    (out/f'sinalizacao-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(antes,self.retrato())
            finally:
                chrome.stop()
