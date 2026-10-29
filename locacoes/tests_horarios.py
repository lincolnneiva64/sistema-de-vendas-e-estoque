"""Horários de apresentação com fixtures sintéticas em banco isolado."""
import base64
import os
from datetime import date, datetime, time, timezone as dt_timezone
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.template import Context, Template
from django.template.loader import get_template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from offline.browser_support import Chrome
from .horarios import agenda_locacao, instante_locacao
from .models import (Locacao, PagamentoLocacao, EventoLocacao, MovimentoEstoqueLocacao,
                     TarefaOperacionalLocacao, ConferenciaEntregaLocacao, ConferenciaRecolhimentoLocacao)
from . import tests as existentes
from .views import (_mensagem_recibo_whatsapp, _texto_checklist_entrega,
                    _texto_checklist_recolhimento, _mensagem_tarefa_operacional_whatsapp)

ARTIFACTS = Path(__file__).resolve().parents[1] / 'artifacts/horarios-locacoes-20261028'
OUT = Path(os.environ['LOCACOES_HORARIOS_ARTIFACTS']) if os.environ.get('LOCACOES_HORARIOS_ARTIFACTS') else Path(
    mkdtemp(prefix='evidencias-', dir=ARTIFACTS))
INSTANTE = datetime(2026, 10, 25, 1, 2, 3, tzinfo=dt_timezone.utc)
INFORMADO = datetime(2026, 10, 24, 18, 4, 5, tzinfo=dt_timezone.utc)


class Fixture:
    dados_base = existentes.LocacoesPagamentosTermoTests.dados_base
    criar_locacao = existentes.LocacoesPagamentosTermoTests.criar_locacao

    def setUp(self):
        existentes.LocacoesPagamentosTermoTests.setUp(self)
        self.user = get_user_model().objects.create_superuser('horarios', 'teste@example.test', 'teste')
        self.client.force_login(self.user)
        self.locacao = self.criar_locacao(horario_entrega=time(9, 10, 11), horario_evento=time(18, 12, 13))
        self.pagamento = self.locacao.registrar_pagamento(5, PagamentoLocacao.FORMA_PIX, responsavel='Teste')
        PagamentoLocacao.objects.filter(pk=self.pagamento.pk).update(data_hora=INFORMADO, criado_em=INSTANTE)
        Locacao.objects.filter(pk=self.locacao.pk).update(criado_em=INSTANTE, atualizado_em=INSTANTE)
        self.pagamento.refresh_from_db()
        self.locacao.refresh_from_db()
        self.entrega = TarefaOperacionalLocacao.objects.create(locacao=self.locacao, tipo='entrega',
            data_agendada=date(2026, 10, 1), horario_agendado=time(9, 10, 11))
        self.recolhimento = TarefaOperacionalLocacao.objects.create(locacao=self.locacao, tipo='recolhimento',
            data_agendada=date(2026, 10, 2), confirmado_em=INSTANTE, tentativa_em=INFORMADO)
        self.ce = ConferenciaEntregaLocacao.objects.create(locacao=self.locacao, tarefa=self.entrega,
            previsto_mesas=2, previsto_cadeiras=8, situacao='parcial', responsavel='Teste',
            entregue_mesas=1, entregue_cadeiras=4, acumulado_mesas=1, acumulado_cadeiras=4,
            pendente_mesas=1, pendente_cadeiras=4, recebedor_nome='Cliente sintético',
            recebedor_relacao=ConferenciaEntregaLocacao.RELACAO_CLIENTE,
            estado_material=ConferenciaEntregaLocacao.ESTADO_BOM,
            justificativa_parcial='Restante agendado', previsao_conclusao=INSTANTE)
        self.cr = ConferenciaRecolhimentoLocacao.objects.create(locacao=self.locacao, tarefa=self.recolhimento,
            previsto_mesas=2, previsto_cadeiras=8, pendente_mesas=1, pendente_cadeiras=4,
            boa_mesas=1, boa_cadeiras=4, acumulado_boa_mesas=1, acumulado_boa_cadeiras=4,
            pessoa_local_nome='Cliente sintético', pessoa_local_relacao=ConferenciaEntregaLocacao.RELACAO_CLIENTE,
            justificativa_parcial='Restante agendado', situacao='parcial', responsavel='Teste', previsao_conclusao=INSTANTE)
        for obj in (self.ce, self.cr):
            type(obj).objects.filter(pk=obj.pk).update(criado_em=INSTANTE)
        EventoLocacao.objects.create(locacao=self.locacao, tipo='devolucao', descricao='Devolução parcial sintética')
        self.movimento = MovimentoEstoqueLocacao.registrar(item='mesa', tipo='entrada', quantidade=1,
            responsavel='Teste', locacao=self.locacao)
        MovimentoEstoqueLocacao.objects.filter(pk=self.movimento.pk).update(data_hora=INFORMADO, criado_em=INSTANTE)
        Locacao.objects.filter(pk=self.locacao.pk).update(status=Locacao.STATUS_ENTREGUE)

    def urls(self):
        urls = [(name, reverse('locacoes:' + name, args=args)) for name, args in [
            ('detalhe', [self.locacao.pk]), ('termo', [self.locacao.pk]),
            ('recibo_pagamento', [self.pagamento.pk]), ('recibos_pendentes', []),
            ('checklist_operacional', []), ('conferencia_entrega', [self.entrega.pk]),
            ('conferencia_recolhimento', [self.recolhimento.pk]),
            ('checklist_entrega_cliente', [self.ce.pk]), ('checklist_recolhimento_cliente', [self.cr.pk]),
            ('configuracoes', [])]]
        return [(name, url + (f'?conferencia={self.ce.pk}' if name == 'conferencia_entrega'
                             else '?data=2026-10-02' if name == 'checklist_operacional' else ''))
                for name, url in urls]


class HorariosTests(Fixture, TestCase):
    def test_fuso_explicito_e_ausencias(self):
        with timezone.override('Asia/Tokyo'):
            self.assertEqual(instante_locacao(INSTANTE), '24/10/2026 às 22:02:03')
        for valor in [None, date(2026, 10, 1), datetime(2026, 10, 1)]:
            self.assertEqual(instante_locacao(valor), 'Horário não registrado')
        self.assertEqual(agenda_locacao(date(2026, 10, 1)), '01/10/2026 · horário não informado')

    def test_recibo_whatsapp_distingue_informado_servidor_e_agenda(self):
        texto = _mensagem_recibo_whatsapp(self.pagamento)
        self.assertIn('Pagamento — horário informado: *24/10/2026 às 15:04:05*', texto)
        self.assertIn('Registro do pagamento no servidor: *24/10/2026 às 22:02:03*', texto)
        self.assertIn('01/10/2026 às 09:10:11', texto)
        self.assertIn('Entrega agendada', texto)
        self.assertIn('horário não informado', texto)
        self.pagamento.recibo_enviado_em = INSTANTE
        self.pagamento.recibo_dispensado_em = INFORMADO
        texto = _mensagem_recibo_whatsapp(self.pagamento)
        self.assertIn('Confirmação de envio do recibo: *24/10/2026 às 22:02:03*', texto)
        self.assertIn('Dispensa do recibo registrada: *24/10/2026 às 15:04:05*', texto)

    def test_templates_compilam_e_telas_abrem(self):
        for path in Path('locacoes/templates/locacoes').rglob('*.html'):
            get_template('locacoes/' + str(path.relative_to('locacoes/templates/locacoes')).replace('\\', '/'))
        for nome, url in self.urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, nome)
            self.assertContains(response, 'America/Belem')

    def test_consultas_nao_gravam_e_nao_alteram_saldos(self):
        antes = list(Locacao.objects.values())
        pagamentos = list(PagamentoLocacao.objects.values())
        movimentos = list(MovimentoEstoqueLocacao.objects.values())
        with CaptureQueriesContext(connection) as queries:
            for nome, url in self.urls():
                # Configurações inicializa faixas por regra existente; demais telas são leitura.
                if nome != 'configuracoes':
                    self.client.get(url)
        gravacoes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE '))]
        self.assertEqual(gravacoes, [])
        self.assertEqual(antes, list(Locacao.objects.values()))
        self.assertEqual(pagamentos, list(PagamentoLocacao.objects.values()))
        self.assertEqual(movimentos, list(MovimentoEstoqueLocacao.objects.values()))

    def test_legado_resolucao_administrativa_sem_horario_fisico_inferido(self):
        self.recolhimento.resolvida_em = INSTANTE
        html = Template('{% include "locacoes/includes/horarios_tarefa.html" %}').render(Context({'tarefa': self.recolhimento}))
        self.assertIn('Resolução administrativa', html)
        self.assertIn('Não comprova execução física', html)
        self.assertIn('24/10/2026 às 22:02:03', html)
        html = Template('{% include "locacoes/includes/horarios_pagamento.html" %}').render(Context({'pagamento': {}}))
        self.assertIn('Horário não registrado', html)
        self.assertNotIn('00:00:00', html)

    def test_textos_checklist_e_movimentos_separam_origens(self):
        self.ce.refresh_from_db()
        self.cr.refresh_from_db()
        for texto in [_texto_checklist_entrega(self.ce), _texto_checklist_recolhimento(self.cr, [])]:
            self.assertIn('Registro da conferência no servidor: *24/10/2026 às 22:02:03*',
                          texto.replace('servidor\n', 'servidor: '))
        texto = _mensagem_tarefa_operacional_whatsapp(self.recolhimento, 'https://example.test/')
        self.assertIn('Agendado: *02/10/2026 · horário não informado*', texto)
        response = self.client.get(reverse('locacoes:configuracoes'))
        self.assertContains(response, 'Horário informado da movimentação:')
        self.assertContains(response, '24/10/2026 às 15:04:05')
        self.assertContains(response, '24/10/2026 às 22:02:03')


class HorariosBrowserTests(Fixture, StaticLiveServerTestCase):
    def test_desktop_mobile_impressao(self):
        OUT.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix='locacoes-horarios-chrome-') as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setTimezoneOverride', {'timezoneId': 'Asia/Tokyo'})
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 768})
                    for nome, url in self.urls():
                        tab.call('Page.navigate', {'url': self.live_server_url + url})
                        tab.wait('document.readyState === "complete"')
                        self.assertEqual(tab.evaluate('location.pathname'), url.split('?')[0], nome)
                        self.assertIn('America/Belem', tab.evaluate('document.body.innerText'), nome)
                        styles = tab.evaluate('''[...document.querySelectorAll('.horario-registro')].map(e=>({weight:parseInt(getComputedStyle(e).fontWeight), color:getComputedStyle(e).color}))''')
                        for style in styles:
                            self.assertGreaterEqual(style['weight'], 700)
                            self.assertEqual(style['color'], 'rgb(30, 41, 59)')
                        self.assertEqual(tab.evaluate('document.compatMode'), 'CSS1Compat', nome)
                        screenshot = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})
                        (OUT / f'{nome}-{width}.png').write_bytes(base64.b64decode(screenshot['data']))
                        if nome == 'recibo_pagamento':
                            tab.evaluate('''Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text}}});document.querySelector('#copiar-mensagem-recibo').click();true''')
                            self.assertIn('Registro do pagamento no servidor: *24/10/2026 às 22:02:03*',
                                          tab.evaluate('window.copied'))
                        if width == 1366:
                            tab.call('Emulation.setEmulatedMedia', {'media': 'print'})
                            printed = tab.evaluate('''[...document.querySelectorAll('.horario-registro')].filter(e=>e.getClientRects().length).map(e=>e.textContent)''')
                            if styles:
                                self.assertTrue(printed, nome)
                                self.assertTrue(any('24/10/2026 às 22:02:03' in text for text in printed), nome)
                            if nome == 'recibo_pagamento':
                                self.assertEqual(tab.evaluate("getComputedStyle(document.querySelector('.painel')).display"), 'none')
                            print_image = tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})
                            (OUT / f'{nome}-impressao.png').write_bytes(base64.b64decode(print_image['data']))
                            pdf = tab.call('Page.printToPDF', {'printBackground': True, 'preferCSSPageSize': True})
                            self.assertTrue(base64.b64decode(pdf['data']).startswith(b'%PDF'))
                            (OUT / f'{nome}-impressao.pdf').write_bytes(base64.b64decode(pdf['data']))
                            tab.call('Emulation.setEmulatedMedia', {'media': 'screen'})
            finally:
                chrome.stop()
