"""Read-only presentation of immutable application balances; fictional data only."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from offline.browser_support import Chrome
from . import ajustes, services
from .conversao import snapshot
from .models import AplicacaoAjuste, DecisaoAjuste, Inventario, ItemInventario, RodadaContagem
from .tests import produto
from .tests_ajustes import AjustesFixture
from .tests_browser import CHROME


class SaldoAplicadoFixture(AjustesFixture):
    def aplicar_exemplo(self, rodada=None):
        linha = self.avaliar(rodada)
        previa = ajustes.previa_atualizacao(self.inv.pk, [linha])
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            return ajustes.atualizar(self.usuario, self.inv.pk, previa, confirmacao=True)[0]

    def novo_item(self, nome, unidade, atual, contado):
        p = produto(nome=nome, unidade_compra=unidade, unidade_venda_1=unidade,
                    vende_fracionado=False, quantidade=Decimal(atual))
        p.refresh_from_db()
        item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p))
        rodada = RodadaContagem.objects.create(item=item, numero=1)
        self.confirmar(self.inv, contado)
        return self.fresca(rodada.pk)


class SaldoAplicadoTests(SaldoAplicadoFixture, TestCase):
    def test_applied_balance_is_historical_and_get_only_reads(self):
        application = self.aplicar_exemplo()
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7,
                                  motivo='Movimento posterior ficticio', usuario=self.usuario)
        before = AplicacaoAjuste.objects.values().get(pk=application.pk)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        linha = response.context['linhas'].object_list[0]
        self.assertEqual(linha['atual'], Decimal('7'))
        self.assertEqual(linha['saldo_aplicado'], Decimal('8'))
        self.assertIsNone(linha['resultante'])
        self.assertContains(response, 'Atualizado: 8 <small>CX</small>')
        self.assertEqual(AplicacaoAjuste.objects.values().get(pk=application.pk), before)

    def test_zero_and_historical_unit_are_not_replaced_by_current_balance(self):
        rodada = self.novo_item('Farinha ficticia', 'FD', '0.200', 0)
        application = self.aplicar_exemplo(rodada)
        conferir_ou_ajustar_estoque(rodada.item.produto_id, 'ajuste', novo_estoque=Decimal('0.500'),
                                  motivo='Entrada posterior ficticia', usuario=self.usuario)
        response = self.client.get(self.url)
        linha = next(l for l in response.context['linhas'] if l['rodada'].pk == rodada.pk)
        self.assertEqual(linha['saldo_aplicado'], Decimal('0'))
        self.assertEqual(linha['atual'], Decimal('0.500'))
        self.assertEqual(linha['unidade_aplicada'], application.unidade)
        self.assertContains(response, 'Atualizado: 0 <small>FD</small>')

    def test_legacy_decision_without_application_has_no_invented_balance(self):
        movement = conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=8,
            motivo='Decisao legada ficticia', usuario=self.usuario)['historico']
        DecisaoAjuste.objects.create(item=self.rodada.item, rodada=self.rodada,
            movimentacao=movement, diferenca=-2, justificativa='Registro legado ficticio', aprovado_por=self.usuario)
        response = self.client.get(self.url)
        linha = response.context['linhas'].object_list[0]
        self.assertEqual(linha['estado'], 'Aplicado')
        self.assertIsNone(linha['saldo_aplicado'])
        audit_url = reverse('inventario:historico_produto', args=[self.inv.pk, self.rodada.pk])
        self.assertContains(response, f'Ajuste aplicado <a href="{audit_url}">Ver auditoria</a>')
        self.assertNotContains(response, 'Atualizado:')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_pending_forecast_is_unchanged(self):
        response = self.client.get(self.url)
        linha = response.context['linhas'].object_list[0]
        self.assertEqual(linha['atual'], Decimal('10'))
        self.assertEqual(linha['resultante'], Decimal('8'))
        self.assertIsNone(linha['saldo_aplicado'])
        self.assertNotContains(response, 'Atualizado:')
        self.assertContains(response, 'Atualizar estoque')


@skipUnless(Path(CHROME).exists(), 'Chrome local')
class SaldoAplicadoBrowserTests(SaldoAplicadoFixture, StaticLiveServerTestCase):
    def test_preview_desktop_mobile_four_applied_and_later_movements(self):
        # Only synthetic fixtures: no operational conference or production stock.
        self.inv.titulo = 'Previa de ajustes aplicados - dados ficticios'
        self.inv.save(update_fields=['titulo'])
        rounds = [self.rodada,
                  self.novo_item('Produto A ficticio', 'UN', '20', 18),
                  self.novo_item('Produto B ficticio', 'UN', '6', 4),
                  self.novo_item('Farinha ficticia', 'FD', '0.200', 0)]
        for rodada in rounds:
            self.aplicar_exemplo(rodada)
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7,
                                  motivo='Saida posterior ficticia', usuario=self.usuario)
        self.novo_item('Produto pendente ficticio', 'UN', '5', 3)
        before = list(AplicacaoAjuste.objects.order_by('pk').values())
        with TemporaryDirectory(prefix='applied-balance-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid', 'value':self.client.cookies['sessionid'].value,
                                              'url':self.live_server_url})
                destination = Path('artifacts/saldos-aplicados')
                destination.mkdir(parents=True, exist_ok=True)
                for width, height in ((1366, 1000), (390, 844)):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=height,
                        deviceScaleFactor=1, mobile=width < 768))
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait("document.querySelectorAll('[data-estado=Aplicado]').length===4")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertEqual(tab.evaluate("[...document.querySelectorAll('[data-estado=Aplicado] .saldo-resultante')].filter(e=>e.textContent.includes('Atualizado:')).length"), 4)
                    self.assertFalse(tab.evaluate("[...document.querySelectorAll('[data-estado=Aplicado] .saldo-resultante')].some(e=>e.textContent.includes('Não disponível'))"))
                    (destination/f'saldos-{width}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',
                        {'format':'png', 'captureBeyondViewport':True})['data']))
                self.assertEqual(list(AplicacaoAjuste.objects.order_by('pk').values()), before)
            finally:
                chrome.stop()
