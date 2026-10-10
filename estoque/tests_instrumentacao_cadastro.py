"""Signed diagnostic evidence must never change concurrency decisions."""
import time
from unittest.mock import patch

from django.core import signing
from django.db import models
from django.test import Client, TestCase
from django.urls import reverse

from .forms import ProdutoForm
from .models import Produto, AlteracaoPrecoVinculado
from .services import diagnostico_cadastro as diagnostico
from .tests_precos_fracionados_diagnostico import FracionadosFixture
from .tests_precos_vinculados import criar_operador_precos


class InstrumentacaoCadastroTests(FracionadosFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.compatibilizar_fixture()
        self.url = reverse('estoque:produto_editar', args=[self.produtos[1].pk])

    def formulario(self, client=None):
        form = (client or self.client).get(self.url).context['form']
        dados = {k: v for k, v in form.initial.items() if v is not None}
        dados.pop('fornecedores', None)
        dados['categoria'] = 'Mercearia'
        self.assertTrue(dados['evidencia_diagnostico_cadastro'])
        return dados

    def alterar(self, **campos):
        # Only isolated fictional fixtures bypass the application command.
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), **campos)

    def recusar(self, dados, esperado, disponivel=True, client=None):
        antes = list(Produto.objects.order_by('pk').values())
        auditorias = list(AlteracaoPrecoVinculado.objects.values())
        with self.assertLogs(diagnostico.logger, level='WARNING') if disponivel else self.assertNoLogs(diagnostico.logger, level='WARNING') as captura:
            resposta = (client or self.client).post(self.url, dados)
        self.assertEqual(resposta.status_code, 200)
        erros = str(resposta.context['form'].errors)
        self.assertIn('nada foi gravado', erros)
        self.assertIn(esperado, erros)
        self.assertEqual(list(Produto.objects.order_by('pk').values()), antes)
        self.assertEqual(list(AlteracaoPrecoVinculado.objects.values()), auditorias)
        return captura

    def test_valid_form_saves_with_evidence(self):
        dados = self.formulario()
        dados['nome'] = 'Legitimate edit'
        with self.assertNoLogs(diagnostico.logger, level='WARNING'):
            self.assertEqual(self.client.post(self.url, dados).status_code, 302)

    def test_valid_legacy_form_saves_without_evidence(self):
        dados = self.formulario()
        dados.pop('evidencia_diagnostico_cadastro')
        self.assertEqual(self.client.post(self.url, dados).status_code, 302)

    def test_categories_cost_stock_fractionation_commercial_metadata(self):
        casos = [('preco_compra', 25, 'Custo', 'custo'), ('quantidade', 9, 'Estoque', 'estoque'),
                 ('fator_conversao', 12, 'Fracionamento', 'fracionamento'),
                 ('fornecedor', 'sensitive-fixture-value', 'Demais dados comerciais', 'comercial'),
                 ('autoria_precos', {'preco_vista': 'private-author-token'}, 'Metadados', 'metadados')]
        for campo, valor, label, categoria in casos:
            with self.subTest(campo=campo):
                dados = self.formulario()
                self.alterar(**{campo: valor})
                captura = self.recusar(dados, label)
                self.assertEqual(captura.records[0].args[1], categoria)

    def test_timestamp_only_change(self):
        dados = self.formulario()
        Produto.objects.get(pk=self.produtos[1].pk).save()
        captura = self.recusar(dados, 'Metadados')
        self.assertEqual(captura.records[0].args[1], 'metadados')

    def test_other_operator_and_same_operator_other_session(self):
        for outro in (False, True):
            with self.subTest(outro_operador=outro):
                antigo = self.formulario()
                segundo = Client()
                segundo.force_login(criar_operador_precos('other-diagnostic-operator') if outro else self.operador)
                novo = self.formulario(segundo)
                novo['nome'] = 'New name ' + str(outro)
                self.assertEqual(segundo.post(self.url, novo).status_code, 302)
                self.recusar(antigo, 'Demais dados comerciais')

    def test_absent_evidence_keeps_refusal(self):
        dados = self.formulario()
        dados.pop('evidencia_diagnostico_cadastro')
        self.alterar(quantidade=9)
        self.recusar(dados, 'Diagnóstico indisponível', disponivel=False)

    def test_tampered_evidence_keeps_refusal(self):
        dados = self.formulario()
        dados['evidencia_diagnostico_cadastro'] += 'tampered-sensitive-data'
        self.alterar(quantidade=9)
        self.recusar(dados, 'Diagnóstico indisponível', disponivel=False)

    def test_expired_evidence_keeps_refusal(self):
        with patch('django.core.signing.time.time', return_value=time.time() - diagnostico.MAX_AGE - 10):
            dados = self.formulario()
        self.alterar(quantidade=9)
        self.recusar(dados, 'Diagnóstico indisponível', disponivel=False)

    def test_evidence_bound_to_operator_product_and_original_token(self):
        for campo, valor in [('operador', -1), ('produto', -1), ('token', 'wrong-original-token')]:
            with self.subTest(campo=campo):
                dados = self.formulario()
                evidencia = signing.loads(dados['evidencia_diagnostico_cadastro'], salt=diagnostico.SALT)
                evidencia[campo] = valor
                dados['evidencia_diagnostico_cadastro'] = signing.dumps(evidencia, salt=diagnostico.SALT)
                self.alterar(quantidade=Produto.objects.get(pk=self.produtos[1].pk).quantidade - 1)
                self.recusar(dados, 'Diagnóstico indisponível', disponivel=False)

    def test_group_version_refusal_is_also_read_only(self):
        dados = self.formulario()
        self.grupo.versao_precos += 1
        self.grupo.save(update_fields=['versao_precos'])
        captura = self.recusar(dados, 'Nenhuma categoria do cadastro')
        self.assertEqual(captura.records[0].args[1], 'nenhuma')

    def test_logs_contain_only_random_identifier_and_category_names(self):
        dados = self.formulario()
        self.alterar(fornecedor='sensitive-fixture-value')
        captura = self.recusar(dados, 'Demais dados comerciais')
        record = captura.records[0]
        self.assertRegex(record.args[0], r'^[0-9a-f]{32}$')
        self.assertEqual(record.args[1], 'comercial')
        for proibido in ('sensitive-fixture-value', self.operador.username, self.produtos[1].nome,
                         dados['evidencia_diagnostico_cadastro'], dados['versao_cadastro_precos']):
            self.assertNotIn(proibido, record.getMessage())
        self.assertIsNone(record.exc_info)

    def test_expired_evidence_does_not_reject_current_form(self):
        with patch('django.core.signing.time.time', return_value=time.time() - diagnostico.MAX_AGE - 10):
            dados = self.formulario()
        self.assertEqual(self.client.post(self.url, dados).status_code, 302)
