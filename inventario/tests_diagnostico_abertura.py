import json
from collections import Counter
from decimal import Decimal
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Fornecedor, ProdutoFornecedor
from . import services, views
from .models import Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem, EventoInventario
from .tests import produto


@override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class AberturaDiagnosticoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('diagnostico', '', 'teste')
        self.client.force_login(self.usuario)
        self.fornecedor = Fornecedor.objects.create(nome='Fornecedor ficticio 57')
        for i in range(57):
            p = produto(nome=f'Ficticio {i:02d}')
            ProdutoFornecedor.objects.create(fornecedor=self.fornecedor, produto=p)

    def medir(self, nome, acao):
        inicio = perf_counter()
        with CaptureQueriesContext(connection) as consultas:
            try:
                return acao()
            finally:
                grupos = Counter(q['sql'].split(' FROM ')[-1].split(' WHERE ')[0][:120] for q in consultas)
                print(json.dumps(dict(etapa=nome, consultas=len(consultas), segundos=round(perf_counter()-inicio, 3),
                    sql_segundos=round(sum(float(q['time']) for q in consultas), 3), grupos=grupos.most_common(12)), ensure_ascii=True), flush=True)

    def test_criacao_e_get_57_sem_ok(self):
        inv = self.medir('criar_servico_57', lambda: services.criar_inventario(
            self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk))
        self.assertEqual(inv.itens.count(), 57)
        response = self.medir('get_57_sem_ok', lambda: self.client.get(reverse('inventario:detalhe', args=[inv.pk])))
        self.assertEqual(response.status_code, 200)

    def test_get_pendente_consultas_limitadas_e_sem_escritas(self):
        inv = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk)
        antes = {m.__name__: list(m.objects.order_by('pk').values()) for m in
                 (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem, EventoInventario)}
        with CaptureQueriesContext(connection) as consultas:
            response = self.client.get(reverse('inventario:detalhe', args=[inv.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(consultas), 25)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))
        self.assertEqual(response.context['resumo_painel']['pendentes'], 57)
        for modelo in (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem, EventoInventario):
            self.assertEqual(antes[modelo.__name__], list(modelo.objects.order_by('pk').values()))

    def test_falha_no_get_apos_criacao_nao_desfaz_persistencia(self):
        # Controlled failure demonstrates the redirect boundary; it does not
        # claim that this exception occurred in production.
        with patch.object(views, 'linhas_revisao', side_effect=RuntimeError('Falha GET simulada')):
            with self.assertRaisesMessage(RuntimeError, 'Falha GET simulada'):
                self.client.post(reverse('inventario:lista'),
                    {'criterio': 'fornecedor', 'fornecedor': self.fornecedor.pk}, follow=True)
        inv = Inventario.objects.get()
        self.assertEqual(inv.itens.count(), 57)
        self.assertEqual(RodadaContagem.objects.count(), 57)
        self.assertEqual(self.client.get(reverse('inventario:detalhe', args=[inv.pk])).status_code, 200)

    def test_abertura_com_latencia_controlada(self):
        inv = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk)
        def latencia(execute, sql, params, many, context):
            sleep(0.06)  # Artificial 60ms per round trip, not measured Neon latency.
            return execute(sql, params, many, context)
        with connection.execute_wrapper(latencia):
            response = self.medir('get_57_latencia_artificial_60ms', lambda:
                self.client.get(reverse('inventario:detalhe', args=[inv.pk])))
        self.assertEqual(response.status_code, 200)

    def test_post_criacao_segue_redirect(self):
        response = self.medir('post_e_redirect_57', lambda: self.client.post(reverse('inventario:lista'),
            {'criterio': 'fornecedor', 'fornecedor': self.fornecedor.pk}, follow=True))
        self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=False)
    def test_variacoes_validas_de_cadastro_e_saldo_sem_ok(self):
        variantes = [
            dict(quantidade=None, preco_compra=None),
            dict(quantidade=Decimal('-2.125'), preco_compra=Decimal('-1')),
            dict(quantidade=0, preco_compra=0),
            dict(quantidade=Decimal('999999999.999'), preco_compra=Decimal('99999999.99')),
            dict(unidade_compra='UN', vende_fracionado=False),
            dict(unidade_compra='KG', vende_fracionado=False, quantidade=Decimal('1.375')),
            dict(unidade_compra='MIL', vende_fracionado=False),
            dict(unidade_compra='PCT', unidade_venda_2='UN'),
        ]
        selecionados = [produto(nome=f'Variação {i} óleo, açúcar & café', permitir_prejuizo=True,
                        motivo_prejuizo='Cadastro sintetico de diagnostico', **variantes[i % len(variantes)]).pk
                        for i in range(57)]
        inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=selecionados)
        response = self.medir('get_57_variacoes_debug_false', lambda:
            self.client.get(reverse('inventario:detalhe', args=[inv.pk])))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['resumo_painel']['pendentes'], 57)

    def test_renderizacao_com_manifest_e_debug_false(self):
        inv = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=self.fornecedor.pk)
        with TemporaryDirectory(prefix='inventario-static-disposable-') as diretorio:
            with override_settings(DEBUG=False, STATIC_ROOT=diretorio, STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
            }):
                call_command('collectstatic', interactive=False, verbosity=0)
                response = self.medir('get_57_manifest_debug_false', lambda:
                    self.client.get(reverse('inventario:detalhe', args=[inv.pk])))
                self.assertEqual(response.status_code, 200)
