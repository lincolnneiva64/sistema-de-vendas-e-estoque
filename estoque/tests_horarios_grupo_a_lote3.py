"""Grupo A lote 3: somente apresentação, fixtures descartáveis."""
import base64
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone as utc
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from estoque import tests_rastreabilidade_apresentacao as base
from estoque.horarios_recebimentos import horario_legivel
from estoque.models import Produto, PixRecebido, Funcionario, MovimentoFinanceiro
from locacoes.models import Locacao, FaixaPrecoLocacao, TarefaOperacionalLocacao, AutorizacaoRecolhimento, EventoLocacao
from locacoes import pickup_links
from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parents[1]
INSTANTE = datetime(2018,11,5,1,2,3,tzinfo=utc.utc)
TEXTO = '04/11/2018 às 22:02:03'
CADASTRO = datetime(2026,7,27,14,2,3,tzinfo=utc.utc)


class Fixture(base.Fixture):
    def setUp(self):
        super().setUp()
        Produto.objects.filter(pk=self.produto.pk).update(codigo_legado='1.00.0001',
            criado_em=CADASTRO, preco_conferido=True, preco_conferido_em=INSTANTE, preco_conferido_por=self.user)
        self.produto.refresh_from_db()
        self.excluido = Produto.objects.create(nome='Produto excluído antigo', excluido=True, excluido_em=INSTANTE,
            preco_compra=10, preco_vista=15, preco_prazo=16)
        self.original = PixRecebido.objects.create(cliente=self.cliente, valor='25.50',
            data_pagamento=CADASTRO, nome_pagador='Pagador antigo', visualizado_em=INSTANTE)
        self.pix = PixRecebido.objects.create(cliente=self.cliente, valor='25.50',
            data_pagamento=CADASTRO, nome_pagador='Pagador antigo', pix_original=self.original,
            status='possivel_duplicado', visualizado_em=INSTANTE)
        PixRecebido.objects.filter(pk__in=[self.pix.pk,self.original.pk]).update(criado_em=INSTANTE)
        faixa = FaixaPrecoLocacao.objects.create(codigo='centro_perto', nome='Teste', preco_jogo_diaria=8)
        self.locacao = Locacao.objects.create(cliente=self.cliente, faixa_preco=faixa,
            faixa_preco_nome_snapshot='Teste', endereco_entrega='Endereço de teste',
            data_entrega=date(2018,11,1), horario_entrega=time(9), data_evento=date(2018,11,2),
            horario_evento=time(10), data_prevista_devolucao=date(2018,11,3))
        self.tarefa = TarefaOperacionalLocacao.objects.create(locacao=self.locacao,
            tipo='recolhimento', data_agendada=date(2018,11,3))
        self.funcionario = Funcionario.objects.create(nome='Funcionário teste', ativo=True,
            pode_receber_checklist=True, telefone_whatsapp='91999999999')
        self.grant = AutorizacaoRecolhimento.objects.create(tarefa=self.tarefa, funcionario=self.funcionario,
            emitida_por=self.user, expira_em=INSTANTE, token_hash='a'*64)
        self.grant.token_hash = pickup_links.token_hash(pickup_links._send_token(self.grant,settings.SECRET_KEY))
        self.grant.save(update_fields=['token_hash'])

    def paginas(self):
        paginas = [(n,reverse('estoque:'+n,args=args)+query,text) for n,args,query,text in [
            ('conferencia_precos_antigo',[],'',TEXTO), ('lixeira',[],'',TEXTO),
            ('revisao_produtos',[],'?filtro=todos','27/07/2026 às 11:02:03'),
            ('central_pix',[],'',TEXTO), ('central_pix_detalhe',[self.pix.pk],'',TEXTO),
            ('central_pix_envio_sucesso',[self.pix.pk],'',TEXTO)]]
        paginas.append(('recolhimento_autorizar', reverse('locacoes:autorizar_recolhimento',args=[self.tarefa.pk]), TEXTO))
        return paginas

    def retrato(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            [Produto,PixRecebido,MovimentoFinanceiro,Locacao,TarefaOperacionalLocacao,AutorizacaoRecolhimento,EventoLocacao]}


class HorariosLote3Tests(Fixture,TestCase):
    def test_paginas_antigas_e_integridade(self):
        antes=self.retrato()
        for nome,url,text in self.paginas():
            response=self.client.get(url)
            self.assertEqual(response.status_code,200,nome)
            self.assertContains(response,text)
        self.assertEqual(antes,self.retrato())

    def test_campos_ausentes_e_preservacao_data_pagamento(self):
        total=0
        for nome,url,text in self.paginas():
            prefix='locacoes/templates/locacoes' if nome=='recolhimento_autorizar' else 'estoque/templates/estoque'
            source=(ROOT/prefix/(nome+'.html')).read_text(encoding='utf8')
            for path in re.findall(r'{% instante_registro ([\w.]+) %}',source):
                total+=1
                for value in [None,date(2018,11,4),datetime(2018,11,4)]:
                    obj=value
                    for key in reversed(path.split('.')[1:]): obj=SimpleNamespace(**{key:obj})
                    html=Template('{% load horarios_documentos %}{% instante_registro '+path+' %}').render(Context({path.split('.')[0]:obj}))
                    self.assertIn('Horário não registrado',html)
                    self.assertNotIn('00:00:00',html)
            if nome.startswith('central_pix'):
                old=(ROOT/'artifacts/horarios-grupo-a-lote3-20261028/antes'/prefix/(nome+'.html')).read_text(encoding='utf8')
                pattern=r'{{[^}]*data_pagamento[^}]*}}'
                self.assertEqual(re.findall(pattern,old),re.findall(pattern,source))
        self.assertEqual(total,10)

    def test_backend_conferencia_mesmo_padrao_sem_mudar_precos(self):
        precos=(self.produto.preco_compra,self.produto.preco_vista,self.produto.preco_prazo,self.produto.quantidade)
        with timezone.override('Asia/Tokyo'):
            response=self.client.post(reverse('estoque:conferencia_precos_antigo_salvar'),{
                'produto_id':self.produto.pk,'preco_compra':str(precos[0]),'preco_vista':str(precos[1]),'preco_prazo':str(precos[2])})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['preco_conferido_em'],TEXTO)
            response=self.client.post(reverse('estoque:conferencia_precos_antigo_marcar_conferido'),{
                'produto_id':self.produto.pk,'confirmacao':'CONFERIDO'})
            self.assertEqual(response.status_code,200)
            self.produto.refresh_from_db()
            self.assertEqual(response.json()['preco_conferido_em'],horario_legivel(self.produto.preco_conferido_em))
        self.assertEqual(precos,(self.produto.preco_compra,self.produto.preco_vista,self.produto.preco_prazo,self.produto.quantidade))

    @override_settings(CHECKLIST_BASE_URL='https://checklist.example.test')
    def test_validade_mensagem_reuso_expiracao_e_duracao(self):
        antes=self.retrato()
        with timezone.override('Asia/Tokyo'),patch('locacoes.pickup_links.timezone.now',return_value=INSTANTE-timedelta(hours=1)):
            response=self.client.post(reverse('locacoes:enviar_recolhimento',args=[self.tarefa.pk]),{'funcionario':self.funcionario.pk})
        self.assertEqual(response.status_code,200,response.content)
        self.assertTrue(response.json()['reutilizado'])
        message=parse_qs(urlsplit(response.json()['whatsapp_url']).query)['text'][0]
        self.assertIn('*'+TEXTO+'*',message)
        self.assertEqual(response.json()['expira_em'],INSTANTE.isoformat())
        self.assertEqual(antes,self.retrato())
        # The existing duration and expiry boundary are not reformatted into new instants.
        grant,token=pickup_links._new_grant(self.tarefa,self.funcionario,self.user,INSTANTE)
        self.assertEqual(grant.expira_em,INSTANTE+timedelta(hours=24))
        with patch('locacoes.pickup_links.timezone.now',return_value=grant.expira_em):
            response=self.client.get(reverse('locacoes:recolhimento_individual',args=[token]))
        self.assertEqual(response.status_code,403)


class HorariosLote3BrowserTests(Fixture,StaticLiveServerTestCase):
    def test_desktop_mobile_e_atualizacao_dinamica(self):
        out=Path(os.environ['HORARIOS_A3_ARTIFACTS'])
        antes=self.retrato()
        with TemporaryDirectory(prefix='horarios-a3-chrome-') as profile:
            chrome=Chrome(base.CHROME,profile).start()
            try:
                tab=chrome.tab(); tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride',{'timezoneId':'Asia/Tokyo'})
                for width in (1366,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    for nome,url,text in self.paginas():
                        tab.call('Page.navigate',{'url':self.live_server_url+url}); tab.wait('document.readyState === "complete"')
                        self.assertEqual(tab.evaluate('location.pathname'),url.split('?')[0],nome)
                        found=tab.evaluate("[...document.querySelectorAll('.horario-registro')].filter(e=>e.textContent.includes("+json.dumps(text)+")&&e.getClientRects().length).map(e=>({weight:getComputedStyle(e).fontWeight,color:getComputedStyle(e).color}))")
                        self.assertTrue(found,(nome,width))
                        self.assertTrue(all(x['weight']=='700' and x['color']=='rgb(30, 41, 59)' for x in found))
                        if nome == 'central_pix':
                            self.assertTrue(tab.evaluate("[...document.querySelectorAll('td.pix-col-cadastro .pix-meta')].every(e=>e.scrollWidth<=e.clientWidth+1&&getComputedStyle(e).whiteSpace==='normal')"))
                        tab.evaluate("[...document.querySelectorAll('.horario-registro')].find(e=>e.textContent.includes("+json.dumps(text)+")&&e.getClientRects().length).scrollIntoView({block:'center',inline:'center'});true")
                        image=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                        (out/f'{nome}-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(antes,self.retrato())
                for width in (1366,320):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:conferencia_precos_antigo')});tab.wait('document.readyState === "complete"')
                    tab.evaluate("document.querySelector('[data-mark-reviewed]').click();true")
                    tab.wait("document.querySelector('[data-row-message]').textContent.includes('Produto conferido')")
                    self.produto.refresh_from_db()
                    self.assertIn(horario_legivel(self.produto.preco_conferido_em),tab.evaluate("document.querySelector('[data-conference-status]').textContent"))
                    image=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                    (out/f'conferencia-dinamica-{width}.png').write_bytes(base64.b64decode(image['data']))
            finally: chrome.stop()
