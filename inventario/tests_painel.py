import base64
from decimal import Decimal
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Objeto
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario, Produto
from offline.browser_support import Chrome
from . import services, views
from .models import (Inventario, RodadaContagem, ConfirmacaoContagem, EventoInventario,
                     AcessoInventario, AtribuicaoContagem, MovimentoObservado)
from .painel import resumo_painel
from .tests import produto
from .tests_browser import CHROME


def linha(esperado='10', contado='8', custo='10', atual='12', *, encerrada=False):
    esperado, contado, custo, atual = [Decimal(v) if v is not None else None
                                      for v in (esperado, contado, custo, atual)]
    return dict(item=Objeto(custo_base=custo, cadastro={'base': 'UN', 'continua': False, 'fracionado': False},
                           produto=Objeto(quantidade=atual, unidade_compra='UN', unidade_venda_1='')),
                rodada=Objeto(esperado=esperado, ok_em=True if contado is not None else None,
                              encerrada_em=True if encerrada else None),
                diferenca=contado-esperado if contado is not None and esperado is not None else None,
                situacao='Aguardando OK' if contado is None else 'Falta')


class CalculosPainelTests(SimpleTestCase):
    def test_totais_nao_compensam_falta_e_sobra_e_excluem_pendencias_da_base(self):
        linhas = [linha(), linha(contado='12'), linha(contado='10', custo='5'),
                  linha(esperado='1000', contado=None, custo='999')]
        resumo = resumo_painel(linhas)
        self.assertEqual([resumo[k] for k in ('total', 'conferidos', 'faltas', 'sobras', 'corretos', 'pendentes')],
                         [4, 3, 1, 1, 1, 1])
        self.assertEqual((resumo['impacto'], resumo['saldo'], resumo['base_financeira'], resumo['percentual_financeiro']),
                         (Decimal('40'), Decimal('0'), Decimal('250'), Decimal('16')))
        self.assertEqual(linhas[0]['percentual_quantidade'], -20)
        self.assertEqual(linhas[1]['percentual_quantidade'], 20)

    def test_base_zero_nao_produz_percentual_infinito_ou_inventado(self):
        linhas = [linha(esperado='0', contado='1')]
        resumo = resumo_painel(linhas)
        self.assertIsNone(linhas[0]['percentual_quantidade'])
        self.assertIn('zero', linhas[0]['percentual_motivo'])
        self.assertEqual(resumo['impacto'], 10)
        self.assertIsNone(resumo['percentual_financeiro'])

    def test_zero_contra_zero_e_correto_mas_percentual_indefinido(self):
        linhas = [linha(esperado='0', contado='0')]
        resumo = resumo_painel(linhas)
        self.assertEqual(resumo['corretos'], 1)
        self.assertEqual(resumo['impacto'], 0)
        self.assertIsNone(linhas[0]['percentual_quantidade'])

    def test_custos_ausentes_e_negativos_excluidos_dos_dois_lados(self):
        linhas = [linha(), linha(custo=None), linha(custo='-5')]
        resumo = resumo_painel(linhas)
        self.assertEqual((resumo['com_custo'], resumo['sem_custo']), (1, 2))
        self.assertTrue(resumo['parcial'])
        self.assertEqual((resumo['impacto'], resumo['base_financeira'], resumo['percentual_financeiro']), (20, 100, 20))
        self.assertIsNone(linhas[1]['estimativa'])

    def test_sem_custos_ou_sem_produtos_nao_finge_estimativa_zero(self):
        for linhas in ([], [linha(custo=None)], [linha(contado=None)]):
            resumo = resumo_painel(linhas)
            self.assertIsNone(resumo['impacto'])
            self.assertIsNone(resumo['saldo'])
            self.assertIsNone(resumo['percentual_financeiro'])

    def test_custo_zero_e_valido_mas_nao_gera_base_financeira(self):
        resumo = resumo_painel([linha(custo='0')])
        self.assertEqual(resumo['com_custo'], 1)
        self.assertEqual(resumo['impacto'], 0)
        self.assertIsNone(resumo['percentual_financeiro'])

    def test_saldo_esperado_negativo_usa_denominador_absoluto(self):
        linhas = [linha(esperado='-2', contado='0')]
        resumo = resumo_painel(linhas)
        self.assertEqual(linhas[0]['percentual_quantidade'], 100)
        self.assertEqual(resumo['base_financeira'], 20)

    def test_base_percentual_convertida_para_mesma_unidade_da_diferenca(self):
        linhas = [linha(esperado='0.2', contado='0')]
        linhas[0]['item'].cadastro.update(base='FD', secundaria='KG', fator='10', fracionado=True)
        resumo_painel(linhas)
        self.assertEqual(linhas[0]['diferenca_texto'], '-2 KG')
        self.assertEqual(linhas[0]['base_percentual'], '2 KG')
        self.assertEqual(linhas[0]['percentual_quantidade'], -100)

    def test_rodada_encerrada_nao_duplicada_e_saldo_atual_sem_referencia_inventada(self):
        linhas = [linha(encerrada=True), linha(contado=None, atual='7')]
        resumo = resumo_painel(linhas)
        self.assertEqual((resumo['conferidos'], resumo['pendentes']), (0, 2))
        self.assertEqual(linhas[1]['estoque_atual_texto'], '7 UN')
        self.assertIsNone(linhas[1]['percentual_quantidade'])

    def test_unidade_atual_e_saldo_desconhecido_nao_usam_cadastro_antigo(self):
        linhas = [linha(atual=None), linha(atual='2.5')]
        linhas[1]['item'].produto.unidade_venda_1 = 'KG'
        resumo_painel(linhas)
        self.assertEqual(linhas[0]['estoque_atual_texto'], '—')
        self.assertEqual(linhas[1]['estoque_atual_texto'], '2,500 KG')


class PainelFixture:
    def setUp(self):
        self.gestor = get_user_model().objects.create_superuser('gestor-painel', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Responsável fictício')
        self.produtos = [produto(nome=nome, unidade_compra='UN', vende_fracionado=False)
                         for nome in ('Farinha · falta fictícia', 'Fermento · sobra fictícia',
                                      'Condicionador · correto fictício',
                                      'Produto pendente com nome extenso para validar a leitura em telas pequenas')]
        self.inv = services.criar_inventario(self.gestor, 'Piloto visual · dados fictícios', 'manual', [p.pk for p in self.produtos])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk'))
        self.acesso, self.token = services.gerar_acesso(self.gestor, self.inv.pk, self.funcionario.pk, [r.pk for r in self.rodadas])
        for rodada, quantidade in zip(self.rodadas, ('8', '12', '10')):
            services.salvar_contagem(self.acesso.pk, rodada.pk, 0, quantidade, '', True)
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])


class PainelHTTPTests(PainelFixture, TestCase):
    def test_get_painel_sem_escrita_com_estoque_atual_e_referencia_separados(self):
        Produto.objects.filter(pk=self.produtos[0].pk).update(quantidade=7)
        self.client.force_login(self.gestor)
        modelos = (Produto, Inventario, RodadaContagem, ConfirmacaoContagem,
                   EventoInventario, AcessoInventario, AtribuicaoContagem, MovimentoObservado)
        antes = [list(model.objects.order_by('pk').values()) for model in modelos]
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        escritas = [q['sql'] for q in consultas if re.match(r'\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b', q['sql'], re.I)]
        self.assertEqual(escritas, [])
        self.assertEqual(antes, [list(model.objects.order_by('pk').values()) for model in modelos])
        linha_falta = next(l for l in resposta.context['linhas'] if l['item'].produto_id == self.produtos[0].pk)
        self.assertEqual((linha_falta['estoque_atual_texto'], linha_falta['esperado_texto']), ('7 UN', '10 UN'))
        self.assertEqual(resposta.context['resumo_painel']['impacto'], 40)
        self.assertContains(resposta, 'base 10 UN')
        self.assertContains(resposta, 'R$ 40,00')
        self.assertContains(resposta, '13,33%')
        self.assertContains(resposta, 'name="acao" value="recontar"', count=4)
        self.assertNotContains(resposta, 'value="aprovar"')
        self.assertFalse(settings.INVENTARIO_AJUSTES_HABILITADOS)

    def test_estoque_aparece_antes_do_primeiro_ok(self):
        self.client.force_login(self.gestor)
        resposta = self.client.get(self.url)
        pendente = next(l for l in resposta.context['linhas'] if l['resultado'] == 'pendente')
        self.assertEqual(pendente['estoque_atual_texto'], '10 UN')
        self.assertEqual(pendente['esperado_texto'], '—')
        self.assertIsNone(pendente['percentual_quantidade'])

    def test_nova_rodada_tira_ok_antigo_do_resumo_preservando_historico(self):
        services.recontar(self.gestor, self.rodadas[0].pk, 'Recontagem fictícia')
        self.client.force_login(self.gestor)
        resumo = self.client.get(self.url).context['resumo_painel']
        self.assertEqual((resumo['conferidos'], resumo['pendentes'], resumo['faltas']), (2, 2, 0))
        self.assertEqual(ConfirmacaoContagem.objects.count(), 3)

    def test_contador_nao_recebe_resumo_saldos_custos_ou_percentuais(self):
        contador = Client()
        contador.post(reverse('inventario:convite', args=[self.token]))
        self.assertEqual(contador.get(self.url).status_code, 403)
        for accept in ('text/html', 'application/json'):
            resposta = contador.get(reverse('inventario:mobile'), HTTP_ACCEPT=accept)
            self.assertEqual(resposta.status_code, 200)
            for campo in ('estoque_atual', 'base_financeira', 'percentual_quantidade', 'custo_base', 'esperado'):
                self.assertNotIn(campo, resposta.content.decode())


@skipUnless(Path(CHROME).is_file(), 'Chrome não disponível')
class PainelBrowserTests(PainelFixture, StaticLiveServerTestCase):
    def test_desktop_e_mobile_numeros_centralizados_secoes_recolhidas_e_cores(self):
        client = Client()
        client.force_login(self.gestor)
        with TemporaryDirectory(prefix='inventario-painel-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('window.inventarioRevisaoPronta === true')
                artifacts = Path('artifacts/painel')
                artifacts.mkdir(parents=True, exist_ok=True)
                for width, height in ((1366, 1000), (768, 1024), (390, 844), (320, 800)):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': width < 768})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(tab.evaluate("document.querySelector('.tabela-resumo').scrollWidth <= document.querySelector('.tabela-resumo').clientWidth"))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.numero-painel')].every(e=>getComputedStyle(e).textAlign==='center')"))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.detalhe-produto,.criterios-painel')].every(e=>!e.open)"))
                    self.assertFalse(tab.evaluate("document.querySelector('#distribuicao').open"))
                    self.assertEqual(tab.evaluate("document.querySelectorAll('.indicador').length"), 5)
                    (artifacts / f'painel-{width}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True})['data']))
                cores = tab.evaluate("[...document.querySelectorAll('.situacao')].map(e=>getComputedStyle(e).color)")
                self.assertEqual(len(set(cores)), 4)
                tab.evaluate("document.querySelector('a[data-detalhe]').click();true")
                self.assertTrue(tab.evaluate("document.querySelector('.detalhe-produto').open"))
                self.assertTrue(tab.evaluate("!!document.querySelector('input[value=revisar]') && !!document.querySelector('input[value=recontar]')"))
                self.assertFalse(tab.evaluate("!!document.querySelector('input[value=aprovar]')"))
            finally:
                chrome.stop()
