"""Historical original-selection evidence; all records below are synthetic."""
import base64
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.management.color import no_style
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Fornecedor, ProdutoFornecedor
from offline.browser_support import Chrome
from . import services
from .conversao import snapshot
from .models import Inventario, ItemInventario
from .tests import produto
from .tests_browser import CHROME


class HistoricoFixture:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('gestor-historico-titulos', '', 'teste')
        # Reproduce only the inspected original selection, never operational stock.
        self.supplier = Fornecedor.objects.create(pk=10, nome='Garcia Distribudora')
        self.other = Fornecedor.objects.create(nome='Outro fornecedor ficticio')
        self.product = produto(nome='Produto sintetico para titulo')
        ProdutoFornecedor.objects.create(produto=self.product, fornecedor=self.supplier)
        ProdutoFornecedor.objects.create(produto=self.product, fornecedor=self.other)
        self.record = Inventario.objects.create(pk=1, titulo='Fornecedor', criterio='fornecedor',
            filtro={'fornecedor_id':'10'}, criado_por=self.user,
            finalizada_em=datetime(2026,10,19,13,0,tzinfo=timezone.utc))
        Inventario.objects.filter(pk=1).update(criado_em=datetime(2026,10,18,6,52,14,tzinfo=timezone.utc))
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Inventario]):
                cursor.execute(sql)
        self.product.refresh_from_db()
        ItemInventario.objects.create(inventario=self.record, produto=self.product, cadastro=snapshot(self.product))
        self.url = reverse('inventario:lista')
        self.client.force_login(self.user)

    def displayed(self):
        return next(i for i in self.client.get(self.url).context['finalizados'] if i.pk == self.record.pk)


class TitulosHistoricosTests(HistoricoFixture, TestCase):
    def test_original_supplier_filter_resolves_number_one_using_only_selects(self):
        before = Inventario.objects.values().get(pk=1)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertContains(response, 'Conferência nº 1 — Garcia Distribudora')
        self.assertEqual(Inventario.objects.values().get(pk=1), before)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, 10)

    def test_current_product_supplier_links_never_determine_original_selection(self):
        ProdutoFornecedor.objects.filter(fornecedor=self.supplier).delete()
        self.assertEqual(self.displayed().titulo_exibicao, 'Conferência nº 1 — Garcia Distribudora')

    def test_missing_multiple_or_invalid_original_identifiers_do_not_guess(self):
        for selection in ({}, [], {'fornecedor_ids':[10,11]},
                          {'fornecedor_id':'10','fornecedor_ids':[10,11]},
                          {'fornecedor_id':'999999'}, {'fornecedor_id':True},
                          {'fornecedor_id':'10,11'}, {'fornecedor_id':'9'*1000}):
            with self.subTest(selection=selection):
                Inventario.objects.filter(pk=1).update(filtro=selection)
                displayed = self.displayed()
                self.assertEqual(displayed.titulo_exibicao, 'Conferência nº 1 — Fornecedor não identificado')
                self.assertTrue(displayed.aviso_titulo)

    def test_custom_and_new_automatic_titles_remain_unchanged(self):
        for title in ('Auditoria original preservada', 'Conferência nº 1 — Nome congelado'):
            Inventario.objects.filter(pk=1).update(titulo=title)
            self.assertEqual(self.displayed().titulo_exibicao, title)
        new = services.criar_inventario(self.user, criterio='fornecedor', selecionados=self.supplier.pk)
        response = self.client.get(self.url)
        shown = next(i for i in response.context['em_andamento'] if i.pk == new.pk)
        self.assertEqual(shown.titulo_exibicao, new.titulo)

    def test_generic_category_and_manual_titles_use_only_saved_criterion(self):
        Inventario.objects.filter(pk=1).update(titulo='Categoria', criterio='categoria', filtro={'categoria':'Categoria histórica'})
        self.assertEqual(self.displayed().titulo_exibicao, 'Conferência nº 1 — Categoria histórica')
        Inventario.objects.filter(pk=1).update(titulo='Produtos selecionados', criterio='manual', filtro={'produto_ids':[self.product.pk]})
        self.assertEqual(self.displayed().titulo_exibicao, 'Conferência nº 1 — Produtos selecionados')


@skipUnless(Path(CHROME).exists(), 'Chrome local')
class TitulosHistoricosBrowserTests(HistoricoFixture, StaticLiveServerTestCase):
    def test_real_expected_name_and_unresolved_history_desktop_mobile(self):
        unknown = Inventario.objects.create(titulo='Fornecedor', criterio='fornecedor', filtro={}, criado_por=self.user)
        with TemporaryDirectory(prefix='historical-title-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                destination = Path('artifacts/titulos-historicos') / uuid4().hex[:8]
                destination.mkdir(parents=True, exist_ok=True)
                for width,height in ((1366,1000),(390,844)):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=height,deviceScaleFactor=1,mobile=width<768))
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait('document.querySelectorAll(".conferencia-lista").length===2')
                    self.assertEqual(tab.evaluate('document.querySelector(\'[data-conferencia="1"] h3 a\').textContent'), 'Conferência nº 1 — Garcia Distribudora')
                    self.assertEqual(tab.evaluate('document.querySelector(\'[data-conferencia="'+str(unknown.pk)+'"] h3 a\').textContent'), f'Conferência nº {unknown.pk} — Fornecedor não identificado')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    (destination/f'historico-{width}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']))
                self.record.refresh_from_db()
                self.assertEqual(self.record.titulo, 'Fornecedor')
            finally:
                chrome.stop()
