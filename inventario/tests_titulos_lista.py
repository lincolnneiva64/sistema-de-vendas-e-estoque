"""Automatic names and local-time inventory history; isolated fixtures only."""
import base64
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Fornecedor, ProdutoFornecedor
from offline.browser_support import Chrome
from . import services
from .models import Inventario, ItemInventario, EventoInventario
from .tests import produto
from .tests_browser import CHROME


class TitulosFixture:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('gestor-titulos', '', 'teste')
        self.product = produto(nome='Produto demonstrativo', categoria='Descartáveis')
        self.supplier = Fornecedor.objects.create(nome='Frango Americano')
        ProdutoFornecedor.objects.create(fornecedor=self.supplier, produto=self.product)
        self.url = reverse('inventario:lista')
        self.client.force_login(self.user)

    def create(self, criterion='manual', selected=None, **kwargs):
        if selected is None:
            selected = [self.product.pk]
        return services.criar_inventario(self.user, criterio=criterion, selecionados=selected, **kwargs)


class TitulosListaTests(TitulosFixture, TestCase):
    def test_three_titles_use_real_ids_and_selected_names(self):
        for criterion, selected, label in (
            ('manual', [self.product.pk], 'Produtos selecionados'),
            ('categoria', self.product.categoria, 'Descartáveis'),
            ('fornecedor', str(self.supplier.pk), 'Frango Americano')):
            with self.subTest(criterion=criterion):
                record = self.create(criterion, selected, titulo='Titulo externo ignorado')
                record.refresh_from_db()
                self.assertEqual(record.titulo, f'Conferência nº {record.pk} — {label}')
                self.assertEqual(list(record.itens.values_list('produto_id', flat=True)), [self.product.pk])

    def test_post_requires_no_title_and_ignores_other_selection_fields(self):
        response = self.client.post(self.url, {'criterio':'manual', 'produtos':[self.product.pk],
            'titulo':'Falso', 'categoria':'Inexistente', 'fornecedor':'999999'})
        record = Inventario.objects.get()
        self.assertRedirects(response, reverse('inventario:detalhe', args=[record.pk]), fetch_redirect_response=False)
        self.assertEqual(record.titulo, f'Conferência nº {record.pk} — Produtos selecionados')
        self.assertEqual(record.filtro, {'produto_ids':[self.product.pk]})
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, 10)

    def test_invalid_selections_create_nothing(self):
        for payload in ({'criterio':'manual'}, {'criterio':'fornecedor', 'fornecedor':'errado'},
                        {'criterio':'fornecedor', 'fornecedor':'999999'},
                        {'criterio':'categoria', 'categoria':'Inexistente'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.url, payload).status_code, 200)
                self.assertFalse(Inventario.objects.exists())

    def test_title_limit_rolls_back_conference_items_and_events(self):
        field = Inventario._meta.get_field('titulo')
        with patch.object(field, 'max_length', 10):
            with self.assertRaisesMessage(ValueError, 'limite do título'):
                self.create()
        for model in (Inventario, ItemInventario, EventoInventario):
            self.assertFalse(model.objects.exists())

    @override_settings(TIME_ZONE='Asia/Tokyo', LANGUAGE_CODE='en-us')
    def test_sections_order_real_dates_belem_and_read_only_legacy(self):
        first = self.create()
        second = self.create('categoria', self.product.categoria)
        last = self.create('fornecedor', self.supplier.pk)
        created = datetime(2026, 10, 19, 2, 15, 30, tzinfo=dt_timezone.utc)
        ended = datetime(2026, 10, 19, 4, 20, 45, tzinfo=dt_timezone.utc)
        Inventario.objects.filter(pk=second.pk).update(criado_em=created, finalizada_em=ended)
        Inventario.objects.filter(pk=first.pk).update(titulo='Título histórico preservado', criado_em=created)
        Inventario.objects.filter(pk=last.pk).update(criado_em=ended)
        before = list(Inventario.objects.order_by('pk').values())
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual([i.pk for i in response.context['em_andamento']], [last.pk, first.pk])
        self.assertEqual([i.pk for i in response.context['finalizados']], [second.pk])
        self.assertContains(response, '18/10/2026 23:15:30')
        self.assertContains(response, '19/10/2026 01:20:45')
        self.assertContains(response, 'Título histórico preservado')
        self.assertNotContains(response, 'name="titulo"')
        self.assertEqual(before, list(Inventario.objects.order_by('pk').values()))


@skipUnless(Path(CHROME).exists(), 'Chrome local')
class TitulosListaBrowserTests(TitulosFixture, StaticLiveServerTestCase):
    def test_desktop_mobile_creation_without_title_and_history_preview(self):
        finished = self.create('fornecedor', self.supplier.pk)
        Inventario.objects.filter(pk=finished.pk).update(
            criado_em=datetime(2026,10,18,13,30,0,tzinfo=dt_timezone.utc),
            finalizada_em=datetime(2026,10,19,13,30,0,tzinfo=dt_timezone.utc))
        self.create('categoria', self.product.categoria)
        with TemporaryDirectory(prefix='inventory-titles-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                tab.wait('!!document.querySelector("#conferencias-em-andamento")')
                self.assertFalse(tab.evaluate('!!document.querySelector("input[name=titulo]")'))
                tab.evaluate('const form=document.querySelector("form");form.closest("details").open=true;form.querySelector("select[name=criterio]").value="manual";form.querySelector("input[name=produtos]").checked=true;form.querySelector("button").click();true')
                tab.wait("!!document.querySelector('a[href$=\"/ajustes/\"]')")
                created = Inventario.objects.order_by('-pk').first()
                self.assertEqual(created.titulo, f'Conferência nº {created.pk} — Produtos selecionados')
                destination = Path('artifacts/titulos-conferencias') / f'validacao-{uuid4().hex[:8]}'
                destination.mkdir(parents=True, exist_ok=True)
                for width, height in ((1366,1000),(390,844)):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=height,deviceScaleFactor=1,mobile=width<768))
                    tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                    tab.wait('document.querySelectorAll(".conferencia-lista").length===3')
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertEqual(tab.evaluate('document.getElementById("titulo-em-andamento").textContent'), 'Conferências em andamento')
                    self.assertEqual(tab.evaluate('document.getElementById("titulo-historico").textContent'), 'Histórico de conferências finalizadas')
                    self.assertTrue(tab.evaluate('''[...document.querySelectorAll('.conferencia-lista')].every(card => {
                        const title = card.querySelector('h3 a');
                        return title && card.querySelectorAll('a').length === 1
                            && card.textContent.split('Conferência nº').length === 2
                            && card.firstElementChild.tagName === 'H3'
                            && !!card.querySelector('.situacao') && !!card.querySelector('time');
                    })'''))
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#historico-conferencias time").length'), 2)
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#conferencias-em-andamento article").length'), 2)
                    self.assertEqual(tab.evaluate('document.querySelectorAll("#historico-conferencias article").length'), 1)
                    (destination/f'lista-{width}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})['data']))
            finally:
                chrome.stop()
