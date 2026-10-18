from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services, tests_browser as navegador_existente
from .models import ConfirmacaoContagem
from .tests import produto


class PrepararContagem:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('gestor-mobile', '', 'teste')
        self.produto = produto(nome='Produto fictício', unidade_compra='UN', vende_fracionado=False)
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.inv = services.criar_inventario(self.usuario, 'Mobile fictício', 'manual', [self.produto.pk])
        self.rodada = self.inv.itens.get().rodadas.get()
        self.acesso, self.token = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodada.pk])


class MobileProtecaoTests(PrepararContagem, TestCase):
    def setUp(self):
        super().setUp()
        self.client.post(reverse('inventario:convite', args=[self.token]))

    def contar(self, acao, quantidade, versao):
        return self.client.post(reverse('inventario:contar', args=[self.rodada.pk]),
                                {'acao': acao, 'embalagens': quantidade, 'versao': versao},
                                HTTP_ACCEPT='application/json')

    def test_guardar_e_recarregar_protege_sem_confirmar(self):
        response = self.client.get(reverse('inventario:mobile'))
        self.assertContains(response, 'Guardar contagem')
        self.assertNotContains(response, 'Salvar progresso')
        self.assertFalse(response.context['produtos'][0]['guardado'])
        response = self.contar('salvar', '6', 0)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['produto']['guardado'])
        self.assertFalse(response.json()['produto']['conferido'])
        page = self.client.get(reverse('inventario:mobile'))
        self.assertContains(page, 'value="6" readonly')
        self.assertContains(page, 'Continuar contagem')
        self.assertFalse(ConfirmacaoContagem.objects.exists())

    def test_corrigir_seis_para_oito_preserva_historico_e_estoque(self):
        self.assertEqual(self.contar('ok', '6', 0).status_code, 200)
        self.assertEqual(self.contar('salvar', '8', 1).status_code, 200)
        self.assertEqual(self.contar('ok', '8', 2).status_code, 200)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.contado_base, 8)
        self.assertEqual(list(self.rodada.confirmacoes.order_by('pk').values_list('contado_base', flat=True)), [6, 8])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        page = self.client.get(reverse('inventario:mobile'))
        self.assertContains(page, 'Recontar produto')
        self.assertContains(page, 'value="8" readonly')

    def test_envio_final_impede_novas_correcoes_no_servidor(self):
        self.contar('ok', '6', 0)
        self.inv.refresh_from_db()
        response = self.client.post(reverse('inventario:finalizar'), {'versao': self.inv.versao}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contar('ok', '8', 1).status_code, 409)
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.contado_base, 6)


@skipUnless(Path(navegador_existente.CHROME).is_file(), 'Chrome não disponível')
class MobileProtecaoBrowserTests(PrepararContagem, StaticLiveServerTestCase):
    @contextmanager
    def navegador(self):
        with TemporaryDirectory(prefix='inventario-mobile-protecao-') as profile:
            chrome = Chrome(navegador_existente.CHROME, profile).start()
            try:
                tab = chrome.tab()
                navegador_existente.InventarioBrowserTests.abrir(self, tab)
                yield tab
            finally:
                chrome.stop()

    def preencher(self, tab, quantidade):
        tab.evaluate(f"document.querySelector('[name=embalagens]').value='{quantidade}'; document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{{bubbles:true}})); true")

    def test_guardar_proteger_recarregar_continuar(self):
        with self.navegador() as tab:
            self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.preencher(tab, 6)
            tab.evaluate("document.querySelector('[value=salvar]').click(); true")
            tab.wait("document.querySelector('.mensagem').textContent.includes('Contagem guardada')")
            self.assertTrue(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            self.assertTrue(tab.evaluate("document.querySelector('[value=salvar]').disabled"))
            tab.call('Page.reload')
            tab.wait("window.inventarioContagemPronta && document.querySelector('[name=embalagens]')?.readOnly")
            self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '6')
            tab.evaluate("document.querySelector('[data-editar-contagem]').click(); true")
            self.assertFalse(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            self.assertFalse(tab.evaluate("document.querySelector('[value=salvar]').disabled"))
            self.preencher(tab, 8)
            tab.evaluate("document.querySelector('[value=salvar]').click(); true")
            tab.wait("document.querySelector('form.contagem [name=versao]').value === '2'")
            self.assertTrue(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.contado_base, 8)
        self.assertIsNone(self.rodada.ok_em)

    def test_ok_recontar_seis_para_oito_historico_e_envio_final(self):
        with self.navegador() as tab:
            self.preencher(tab, 6)
            tab.evaluate("document.querySelector('[value=ok]').click(); true")
            tab.wait("document.querySelector('.mensagem').textContent.includes('Produto conferido.')")
            self.assertTrue(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            self.assertEqual(tab.evaluate("document.querySelector('[data-editar-contagem]').textContent"), 'Recontar produto')
            tab.evaluate("document.querySelector('[data-editar-contagem]').click(); true")
            self.assertFalse(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            tab.evaluate("document.querySelector('#finalizar-conferencia button').click(); true")
            tab.wait("document.querySelector('.mensagem-finalizacao').textContent.includes('Confirme o OK')")
            self.preencher(tab, 8)
            tab.evaluate("document.querySelector('[value=ok]').click(); true")
            tab.wait("document.querySelector('form.contagem [name=versao]').value === '2'")
            self.assertTrue(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            self.assertIn('8 UN', tab.evaluate("document.querySelector('.quantidade-confirmada').textContent"))
            tab.call('Page.reload')
            tab.wait("window.inventarioContagemPronta && document.querySelector('[name=embalagens]')?.readOnly")
            tab.evaluate("document.querySelector('#finalizar-conferencia button').click(); true")
            tab.wait("document.querySelector('form.contagem fieldset').disabled")
            tab.wait("document.querySelector('.mensagem-finalizacao').textContent.includes('finalizada')")
            self.assertTrue(tab.evaluate("document.querySelector('[data-editar-contagem]').hidden"))
            self.assertTrue(tab.evaluate("document.querySelector('[value=salvar]').disabled"))
        self.assertEqual(list(self.rodada.confirmacoes.order_by('pk').values_list('contado_base', flat=True)), [6, 8])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)

    def test_falha_de_rede_nao_bloqueia_digitacao_nem_confirma_guardado(self):
        with self.navegador() as tab:
            self.preencher(tab, 6)
            tab.evaluate("window.realFetch=fetch;window.fetch=async()=>{throw new TypeError('offline')}; document.querySelector('[value=salvar]').click(); true")
            tab.wait("document.querySelector('.mensagem').textContent.includes('Sem confirmação')")
            self.assertFalse(tab.evaluate("document.querySelector('[name=embalagens]').readOnly"))
            self.assertTrue(tab.evaluate("document.querySelector('[value=salvar]').disabled"))
            tab.evaluate("window.fetch=window.realFetch;document.querySelector('#verificar-acesso').click();true")
            tab.wait("document.querySelector('#mensagem-acesso').textContent.includes('consultados no servidor')")
            self.assertFalse(tab.evaluate("document.querySelector('[value=salvar]').disabled"))
            self.assertEqual(tab.evaluate("document.querySelector('[name=embalagens]').value"), '6')
        self.rodada.refresh_from_db()
        self.assertEqual(self.rodada.versao, 0)
