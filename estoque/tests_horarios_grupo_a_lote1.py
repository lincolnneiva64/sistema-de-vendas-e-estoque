"""Grupo A, lote 1: apresentação e integridade com fixtures descartáveis."""
import base64
import json
import os
import re
from datetime import date, datetime, timezone as utc
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from types import SimpleNamespace
from uuid import uuid4

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from offline.browser_support import Chrome
from estoque import views
from estoque import tests_rastreabilidade_apresentacao as documentos
from estoque.horarios_recebimentos import horario_legivel
from estoque.models import (Compra, ConciliacaoCompra, ContaReceber, EdicaoCompra,
    EnvioInternoListaCompraFornecedor, EnvioListaCompraFornecedor, EventoVenda,
    ItemCompra, ItemListaCompraFornecedor, ItemVenda, ListaCompraFornecedor,
    MovimentoFinanceiro, Produto, SeparacaoVenda, SeparacaoVendaItem, Venda)

ARTIFACTS = Path(__file__).resolve().parents[1] / 'artifacts/horarios-grupo-a-lote1-20261028'
OUT = Path(os.environ['HORARIOS_A1_ARTIFACTS']) if os.environ.get('HORARIOS_A1_ARTIFACTS') else Path(
    mkdtemp(prefix='execucao-', dir=ARTIFACTS))
INSTANTE = datetime(2018, 11, 5, 1, 2, 3, tzinfo=utc.utc)
TEXTO = '04/11/2018 às 22:02:03'


class Fixture(documentos.Fixture):
    def setUp(self):
        super().setUp()
        self.fornecedor.telefone_whatsapp = '91999999999'
        self.fornecedor.save(update_fields=['telefone_whatsapp'])
        self.venda.tipo_pagamento = 'A prazo'
        self.venda.save(update_fields=['tipo_pagamento'])
        ContaReceber.objects.create(venda=self.venda, cliente=self.cliente, data_emissao=date(2026, 10, 22),
            data_vencimento=date(2026, 11, 22), valor_original=50, valor_em_aberto=50)
        self.edicao = EdicaoCompra.objects.create(compra=self.compra, operacao=uuid4(), usuario=self.user,
            motivo='Correção sintética antiga', payload_hash='a' * 64, antes={},
            depois={'ajustes': {'total_anterior':'50.00', 'total_corrigido':'50.00'}})
        EdicaoCompra.objects.filter(pk=self.edicao.pk).update(criado_em=INSTANTE)
        ConciliacaoCompra.objects.create(compra=self.compra, edicao=self.edicao, tipo='credito', valor_pago=50)
        self.interno = EnvioInternoListaCompraFornecedor.objects.create(lista=self.lista,
            fornecedor=self.fornecedor, versao='analitica', nome_destinatario='Funcionário fictício',
            telefone_destinatario='5591999999999', origem_destinatario='avulso',
            registrado_em=INSTANTE, registrado_por=self.user)
        self.envio = EnvioListaCompraFornecedor.objects.create(lista=self.lista, fornecedor=self.fornecedor,
            nome_destinatario='Vendedor fictício', telefone_destinatario='5591999999999',
            confirmado_em=INSTANTE, confirmado_por=self.user, origem_destinatario='personalizado',
            chave_idempotencia=str(uuid4()))
        ListaCompraFornecedor.objects.filter(pk=self.lista.pk).update(atualizado_em=INSTANTE)
        self.lista.refresh_from_db()
        self.separacao = SeparacaoVenda.objects.create(venda=self.venda, enviado_em=INSTANTE,
            data_sequencia=date(2018, 11, 4))
        SeparacaoVendaItem.objects.create(separacao=self.separacao, item_venda=self.venda.itens.get(),
            produto_nome_snapshot=self.produto.nome, unidade_snapshot='PCT', quantidade_solicitada=1)
        self.evento = EventoVenda.objects.create(venda=self.venda, tipo_evento='nota_impressa',
            descricao='Evento sintético antigo', canal='sistema')
        EventoVenda.objects.filter(pk=self.evento.pk).update(criado_em=INSTANTE)
        self.dinamicas = []
        for _ in range(3):
            lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor, data_lista=date(2026, 10, 22),
                data_inicio_periodo=date(2026, 10, 1), data_fim_periodo=date(2026, 10, 22), total_lista=50)
            ItemListaCompraFornecedor.objects.create(lista=lista, produto=self.produto, quantidade_final=1,
                unidade='PCT', preco_unitario=50, total=50)
            self.dinamicas.append(lista)

    def paginas(self):
        return [(name, reverse('estoque:' + name, args=args) + query) for name, args, query in [
            ('compras_detalhe', [self.compra.pk], ''),
            ('compras_lista_fornecedor_interno', [self.lista.pk], ''),
            ('compras_lista_fornecedor_ver', [self.lista.pk], ''),
            ('compras_lista_fornecedor_whatsapp', [self.lista.pk], ''),
            ('separacao_vendas_fila', [], '?data=2018-11-04'),
            ('venda_editar_revisao', [self.venda.pk], '')]]

    def retrato(self, incluir_envios=True):
        models = [Compra, EdicaoCompra, ConciliacaoCompra, Venda, ItemVenda, ItemCompra,
            Produto, ContaReceber, MovimentoFinanceiro, EventoVenda, SeparacaoVenda, SeparacaoVendaItem]
        if incluir_envios:
            models += [ListaCompraFornecedor, ItemListaCompraFornecedor,
                       EnvioListaCompraFornecedor, EnvioInternoListaCompraFornecedor]
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in models}


class HorariosGrupoA1Tests(Fixture, TestCase):
    def test_seis_paginas_com_horario_antigo_e_sem_alterar_operacoes(self):
        before = self.retrato()
        for nome, url in self.paginas():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, nome)
            if nome == 'compras_lista_fornecedor_whatsapp':
                self.assertEqual(response.context['whatsapp_payload']['ultimoEnvioVendedor']['confirmadoEmTexto'], TEXTO)
            else:
                self.assertContains(response, TEXTO)
        self.assertEqual(before, self.retrato())

    def test_backend_preserva_iso_e_usa_belem_independente_do_fuso_ativo(self):
        before = self.retrato()
        with timezone.override('Asia/Tokyo'):
            payload = views._payload_envio_lista_fornecedor(self.envio)
        self.assertEqual(payload['confirmadoEmTexto'], TEXTO)
        self.assertEqual(payload['confirmadoEm'], self.envio.confirmado_em.isoformat())
        self.assertEqual(before, self.retrato())

    def test_campos_ausentes_nao_recebem_hora_atual_ou_meia_noite(self):
        fields = [('compras_detalhe','edicao.criado_em'),
                  ('compras_lista_fornecedor_interno','envio.registrado_em'),
                  ('compras_lista_fornecedor_ver','ultimo_envio_interno.registrado_em'),
                  ('separacao_vendas_fila','separacao.enviado_em'),
                  ('venda_editar_revisao','evento.criado_em')]
        for filename, field in fields:
            source = (Path('estoque/templates/estoque') / (filename + '.html')).read_text(encoding='utf-8-sig')
            tag = re.search(r'{% instante_registro ' + re.escape(field) + r' %}', source).group()
            root, attr = field.split('.')
            for value in (None, date(2018, 11, 4), datetime(2018, 11, 4, 22, 2, 3)):
                html = Template('{% load horarios_documentos %}' + tag).render(Context({root:SimpleNamespace(**{attr:value})}))
                self.assertIn('Horário não registrado', html)
                self.assertNotIn('00:00:00', html)

    def test_confirmacao_continua_idempotente_e_sem_mudar_compras_vendas_estoque(self):
        before = self.retrato(incluir_envios=False)
        lista = self.dinamicas[0]
        url = reverse('estoque:compras_lista_fornecedor_confirmar_envio_vendedor', args=[lista.pk])
        data = {'telefone':'91999999999','nome':'Vendedor fictício','origem':'personalizado','chaveConfirmacao':str(uuid4())}
        first = self.client.post(url, json.dumps(data), content_type='application/json')
        second = self.client.post(url, json.dumps(data), content_type='application/json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['envio'], second.json()['envio'])
        self.assertTrue(first.json()['criado'])
        self.assertFalse(second.json()['criado'])
        envio = EnvioListaCompraFornecedor.objects.get(lista=lista)
        self.assertEqual(first.json()['envio']['confirmadoEmTexto'], horario_legivel(envio.confirmado_em))
        self.assertEqual(EnvioListaCompraFornecedor.objects.filter(lista=lista).count(), 1)
        self.assertEqual(before, self.retrato(incluir_envios=False))


class HorariosGrupoA1BrowserTests(Fixture, StaticLiveServerTestCase):
    def test_desktop_mobile_reabertura_confirmacao_dinamica_e_texto_escapado(self):
        OUT.mkdir(parents=True, exist_ok=True)
        before = self.retrato()
        with TemporaryDirectory(prefix='horarios-a1-chrome-') as profile:
            chrome = Chrome(documentos.CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride', {'timezoneId':'Asia/Tokyo'})
                tab.call('Page.addScriptToEvaluateOnNewDocument', {'source':'''window.externalOpened=[];window.open=(url)=>{externalOpened.push(String(url));return null};'''})
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    for nome, url in self.paginas():
                        tab.call('Page.navigate', {'url':self.live_server_url + url})
                        tab.wait('document.readyState === "complete"')
                        self.assertEqual(tab.evaluate('location.pathname'), url.split('?')[0], nome)
                        if nome == 'compras_detalhe':
                            tab.evaluate("[...document.querySelectorAll('details')].forEach(e=>e.open=true);true")
                        if nome == 'compras_lista_fornecedor_whatsapp':
                            tab.wait('document.querySelector("#confirmacaoEnvioMeta").textContent.includes("04/11/2018 às 22:02:03")')
                            self.assertIn('Não comprova entrega ou leitura', tab.evaluate('document.querySelector("#confirmacaoEnvioMeta").textContent'))
                        found = tab.evaluate('''[...document.querySelectorAll('.horario-registro')].filter(e=>e.textContent.includes('04/11/2018 às 22:02:03')).map(e=>({weight:getComputedStyle(e).fontWeight,color:getComputedStyle(e).color}))''')
                        self.assertTrue(found, (width,nome))
                        self.assertTrue(all(x['weight']=='700' and x['color']=='rgb(30, 41, 59)' for x in found))
                        image = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})
                        (OUT / f'{nome}-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(before, self.retrato())
                source = Path('estoque/templates/estoque/compras_sugestao_fornecedor_whatsapp.html').read_text(encoding='utf8')
                parts = [source.split('    function '+name,1)[1].split('\n    function ',1)[0]
                         for name in ['html(', 'texto(', 'textoDestinatarioConfirmado(']]
                code = ' '.join('function '+name+part for name,part in zip(['html(', 'texto(', 'textoDestinatarioConfirmado('],parts))
                tab.evaluate('window.testTimeText=(()=>{' + code + ';return textoDestinatarioConfirmado})()')
                escaped = tab.evaluate('testTimeText({nomeDestinatario:"<img src=x onerror=alert(1)>",confirmadoEmTexto:"",telefoneDestinatario:"123"})')
                self.assertIn('Horário não registrado', escaped)
                self.assertNotIn('<img', escaped)
                protected_before = self.retrato(incluir_envios=False)
                for width, lista in zip((1366,390,320), self.dinamicas):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    url = reverse('estoque:compras_lista_fornecedor_whatsapp', args=[lista.pk])
                    tab.call('Page.navigate', {'url':self.live_server_url + url})
                    tab.wait('document.readyState === "complete" && !document.querySelector("#btnConfirmarEnvioVendedor").disabled')
                    tab.evaluate('document.querySelector("#whatsappDestinatario").value="91999999999";document.querySelector("#whatsappDestinatarioNome").value="Vendedor fictício";document.querySelector("#btnConfirmarEnvioVendedor").click();true')
                    tab.wait('document.querySelector("#confirmacaoEnvioMeta").classList.contains("visivel")')
                    envio = EnvioListaCompraFornecedor.objects.get(lista=lista)
                    text = tab.evaluate('document.querySelector("#confirmacaoEnvioMeta").innerText')
                    self.assertIn(horario_legivel(envio.confirmado_em), text)
                    self.assertIn('Não comprova entrega ou leitura', text)
                    self.assertEqual(tab.evaluate('externalOpened'), [])
                    self.assertTrue(tab.evaluate('document.querySelector("#btnConfirmarEnvioVendedor").disabled'))
                    image = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})
                    (OUT / f'confirmacao-dinamica-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(protected_before, self.retrato(incluir_envios=False))
            finally:
                chrome.stop()
