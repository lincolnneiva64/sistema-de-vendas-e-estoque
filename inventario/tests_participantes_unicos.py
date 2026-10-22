from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario, Fornecedor, Produto, ProdutoFornecedor
from offline.browser_support import Chrome
from . import services
from .models import AcessoInventario, AtribuicaoContagem, EventoInventario, RodadaContagem, ConfirmacaoContagem
from .selecao import itens_incluidos
from .tests import produto
from .tests_browser import CHROME


class ParticipantesFixture:
    quantidade = 57

    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('gestor-participantes', '', 'teste')
        fornecedor = Fornecedor.objects.create(nome='Fornecedor sintetico')
        self.produtos = [produto(nome=f'Produto {i:02d}', unidade_compra='UN', vende_fracionado=False)
                         for i in range(self.quantidade)]
        ProdutoFornecedor.objects.bulk_create([ProdutoFornecedor(produto=p, fornecedor=fornecedor) for p in self.produtos])
        self.inv = services.criar_inventario(self.usuario, criterio='fornecedor', selecionados=fornecedor.pk)
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('pk'))
        self.ana = Funcionario.objects.create(nome='Ana sintetica')
        self.bruno = Funcionario.objects.create(nome='Bruno sintetico')
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.client.force_login(self.usuario)

    def enviar(self, indices, overrides=None, **extra):
        self.inv.refresh_from_db()
        dados = {'acao': 'acesso', 'versao': self.inv.versao, 'funcionario': self.ana.pk,
                 'rodadas': [self.rodadas[i].pk for i in indices], 'horas': 24}
        dados.update({f'funcionario_{self.rodadas[i].pk}': f.pk for i, f in (overrides or {}).items()})
        dados.update(extra)
        return self.client.post(self.url, dados)

    def mobile(self, link):
        client = Client()
        self.assertEqual(client.post(urlsplit(link).path).status_code, 302)
        return client, client.get(reverse('inventario:mobile'), HTTP_ACCEPT='application/json').json()


@override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class ParticipantesUnicosTests(ParticipantesFixture, TestCase):
    def test_cabecalhos_30_e_12_confirmados_recontagem_sem_duplicacao(self):
        resposta = self.enviar(range(30))
        client, _ = self.mobile(resposta.context['link'])
        self.assertContains(resposta, '30 produtos nesta conferência')
        self.assertContains(client.get(reverse('inventario:mobile')), '30 produtos para conferir')
        acesso = self.inv.acessos.get()
        for rodada in self.rodadas[:12]:
            services.salvar_contagem(acesso.pk, rodada.pk, 0, '2', None, True)
        self.assertContains(self.client.get(self.url), '12 de 30 conferidos — faltam 18')
        self.assertContains(client.get(reverse('inventario:mobile')), '12 de 30 conferidos — faltam 18')
        services.recontar(self.usuario, self.rodadas[0].pk, 'Recontagem sintetica')
        pagina = self.client.get(self.url)
        self.assertContains(pagina, '30 produtos nesta conferência')
        self.assertContains(pagina, '11 de 30 conferidos — faltam 19')
        self.assertEqual(len({l['item'].produto_id for l in pagina.context['linhas']}), 30)

    def test_57_menos_20_iguais_mobile_desktop_csv_revisao_e_finalizacao(self):
        saldos = list(Produto.objects.order_by('pk').values_list('pk', 'quantidade'))
        resposta = self.enviar(range(37))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, '37 produtos nesta conferência')
        self.assertNotContains(resposta, 'Manter produtos não selecionados')
        self.assertNotContains(resposta, 'name="manter_nao_selecionados"')
        client, payload = self.mobile(resposta.context['link'])
        ids_mobile = {p['id'] for p in payload['produtos']}
        ids_desktop = {l['rodada'].pk for l in resposta.context['linhas']}
        ids_revisao = {l['rodada'].pk for l in resposta.context['atualizacao']['linhas']}
        ids_finalizacao = {r.pk for r in services.estado_conclusao(self.inv)[0]}
        esperados = {r.pk for r in self.rodadas[:37]}
        self.assertEqual(ids_mobile, esperados)
        self.assertEqual(ids_desktop, esperados)
        self.assertEqual(ids_revisao, esperados)
        self.assertEqual(ids_finalizacao, esperados)
        produtos_mobile = set(RodadaContagem.objects.filter(pk__in=ids_mobile).values_list('item__produto_id', flat=True))
        produtos_desktop = {l['item'].produto_id for l in resposta.context['linhas']}
        self.assertEqual(produtos_mobile, {p.pk for p in self.produtos[:37]})
        self.assertEqual(produtos_mobile, produtos_desktop)
        self.assertEqual(payload['progresso'], {'total': 37, 'conferidos': 0, 'faltam': 37})
        self.assertContains(client.get(reverse('inventario:mobile')), '37 produtos para conferir')
        csv = self.client.get(reverse('inventario:relatorio', args=[self.inv.pk])).content.decode('utf-8')
        for p in self.produtos[37:]:
            self.assertNotIn(p.nome, csv)
        with self.assertRaises(ValueError):
            services.gerar_acesso(self.usuario, self.inv.pk, self.bruno.pk, [self.rodadas[-1].pk])
        acesso = self.inv.acessos.get()
        for rodada in self.rodadas[:37]:
            services.salvar_contagem(acesso.pk, rodada.pk, 0, '0', None, True)
        self.inv.refresh_from_db()
        self.assertEqual(client.post(reverse('inventario:finalizar'), {'versao': self.inv.versao}).status_code, 200)
        pagina = self.client.get(self.url)
        self.assertContains(pagina, '37 de 37 conferidos — faltam 0')
        self.assertEqual(saldos, list(Produto.objects.order_by('pk').values_list('pk', 'quantidade')))
        futura = services.criar_inventario(self.usuario, criterio='manual', selecionados=[p.pk for p in self.produtos])
        self.assertEqual(itens_incluidos(futura).count(), 57)

    def test_duas_pessoas_uniao_dos_ids_e_progresso_sem_duplicacao(self):
        resposta = self.enviar(range(37), {i: self.bruno for i in range(20, 37)})
        self.assertEqual(len(resposta.context['links_adicionais']), 1)
        ana_client, ana = self.mobile(resposta.context['link'])
        _, bruno = self.mobile(resposta.context['links_adicionais'][0]['link'])
        ana_ids = {p['id'] for p in ana['produtos']}
        bruno_ids = {p['id'] for p in bruno['produtos']}
        self.assertEqual(len(ana_ids), 20)
        self.assertEqual(len(bruno_ids), 17)
        self.assertFalse(ana_ids & bruno_ids)
        self.assertEqual(ana_ids | bruno_ids, {l['rodada'].pk for l in resposta.context['linhas']})
        acesso = self.inv.acessos.get(funcionario=self.ana)
        for rodada in self.rodadas[:12]:
            services.salvar_contagem(acesso.pk, rodada.pk, 0, '2', None, True)
        pagina = self.client.get(self.url)
        self.assertContains(pagina, '12 de 37 conferidos — faltam 25')
        self.assertContains(ana_client.get(reverse('inventario:mobile')), '12 de 20 conferidos — faltam 8')
        self.assertEqual(self.inv.acessos.get(funcionario=self.bruno).atribuicoes.count(), 17)

    def test_selecao_salva_antes_do_primeiro_link_e_rollback_integral(self):
        original = services.gerar_acesso
        chamadas = []
        def verificar(*args, **kwargs):
            self.assertEqual(itens_incluidos(self.inv).count(), 37)
            chamadas.append(args[2])
            if len(chamadas) == 2:
                raise ValueError('Falha sintetica no segundo funcionario')
            return original(*args, **kwargs)
        with patch.object(services, 'gerar_acesso', side_effect=verificar):
            resposta = self.enviar(range(37), {36: self.bruno})
        self.assertContains(resposta, 'Falha sintetica')
        self.assertEqual(itens_incluidos(self.inv).count(), 57)
        self.assertFalse(AcessoInventario.objects.exists())
        self.assertFalse(AtribuicaoContagem.objects.exists())
        self.assertFalse(EventoInventario.objects.filter(acao='produto_retirado').exists())

    def test_atribuicao_ok_rascunho_e_historico_protegidos(self):
        acesso, token = services.gerar_acesso(self.usuario, self.inv.pk, self.ana.pk, [self.rodadas[0].pk])
        for estado in ('atribuido', 'ok', 'rascunho'):
            if estado == 'ok':
                services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 0, '2', None, True)
            if estado == 'rascunho':
                services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 1, '3', None, False)
            antes = list(RodadaContagem.objects.values())
            resposta = self.enviar(range(1, 37))
            self.assertTrue(resposta.context['erro'])
            self.assertEqual(antes, list(RodadaContagem.objects.values()))
            self.assertEqual(itens_incluidos(self.inv).count(), 57)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        resposta = self.enviar(range(37))
        self.assertEqual(itens_incluidos(self.inv).count(), 37)
        self.assertEqual(self.inv.acessos.filter(pk=acesso.pk).get().token_hash, acesso.token_hash)
        _, payload = self.mobile(reverse('inventario:convite', args=[token]))
        self.assertEqual([p['id'] for p in payload['produtos']], [self.rodadas[0].pk])

    def test_formulario_antigo_reserva_e_versao_antiga_nao_gravam(self):
        resposta = self.enviar(range(37), manter_nao_selecionados='sim')
        self.assertTrue(resposta.context['erro'])
        self.assertEqual(itens_incluidos(self.inv).count(), 57)
        self.assertFalse(AcessoInventario.objects.exists())
        resposta = self.enviar(range(37), versao=-1)
        self.assertTrue(resposta.context['erro'])
        self.assertFalse(AcessoInventario.objects.exists())

    def test_get_57_preserva_otimizacao_e_nao_grava(self):
        with CaptureQueriesContext(connection) as consultas:
            resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 200)
        self.assertLessEqual(len(consultas), 25)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
@override_settings(INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class ParticipantesBrowserTests(ParticipantesFixture, StaticLiveServerTestCase):
    quantidade = 30

    def test_dois_funcionarios_links_e_copia(self):
        with TemporaryDirectory(prefix='participantes-multiplos-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('window.inventarioRevisaoPronta === true')
                tab.evaluate(f"document.querySelector('[name=funcionario]').value='{self.ana.pk}';true")
                for rodada in self.rodadas[:5]:
                    tab.evaluate(f"document.querySelector('[name=funcionario_{rodada.pk}]').value='{self.bruno.pk}';true")
                tab.evaluate("document.querySelector('#confirmar-atribuicao').click();true")
                tab.wait("!!document.querySelector('[data-link-adicional]')")
                primeiro = tab.evaluate("document.querySelector('#link-funcionario').value")
                segundo = tab.evaluate("document.querySelector('[data-link-adicional]').value")
                _, ana = self.mobile(primeiro)
                _, bruno = self.mobile(segundo)
                self.assertEqual(len(ana['produtos']), 25)
                self.assertEqual(len(bruno['produtos']), 5)
                tab.evaluate("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiado=text}},configurable:true});document.querySelector('[data-copiar-adicional]').click();true")
                tab.wait("document.querySelector('[data-feedback-copia]').textContent === 'Link copiado.'")
                self.assertEqual(tab.evaluate('window.copiado'), segundo)
                self.assertIn('30 produtos', tab.evaluate("document.querySelector('#total-participantes').textContent"))
            finally:
                chrome.stop()

    def test_lista_unica_totais_e_progresso_320_390(self):
        with TemporaryDirectory(prefix='participantes-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url + self.url})
                tab.wait('window.inventarioRevisaoPronta === true')
                self.assertIn('30 produtos selecionados', tab.evaluate("document.querySelector('#distribuicao summary').textContent"))
                for width in (320, 390, 1280):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 600})
                    medidas = tab.evaluate("""(() => {
                        const nome = document.querySelector('.participante-nome');
                        const checkbox = nome.querySelector('input');
                        const texto = nome.querySelector('span');
                        const funcionario = document.querySelector('.participante-funcionario');
                        return {fonte:parseFloat(getComputedStyle(nome).fontSize), peso:parseInt(getComputedStyle(nome).fontWeight),
                            fonteFuncionario:parseFloat(getComputedStyle(funcionario).fontSize),
                            gap:texto.getBoundingClientRect().left-checkbox.getBoundingClientRect().right,
                            alinhamento:Math.abs(checkbox.getBoundingClientRect().top-texto.getBoundingClientRect().top),
                            semOverflow:document.documentElement.scrollWidth<=innerWidth};
                    })()""")
                    self.assertGreaterEqual(medidas['fonte'], 16.8)
                    self.assertGreaterEqual(medidas['peso'], 700)
                    self.assertGreater(medidas['fonte'], medidas['fonteFuncionario'])
                    self.assertGreaterEqual(medidas['gap'], 8)
                    self.assertLessEqual(medidas['alinhamento'], 5)
                    self.assertTrue(medidas['semOverflow'])
                    import base64
                    destino = Path('artifacts') / f'produtos-selecao-{width}.png'
                    destino.parent.mkdir(exist_ok=True)
                    destino.write_bytes(base64.b64decode(tab.call('Page.captureScreenshot', {'captureBeyondViewport': True})['data']))
                tab.call('Emulation.clearDeviceMetricsOverride')
                self.assertIn('30 produtos', tab.evaluate("document.querySelector('#total-participantes').textContent"))
                self.assertEqual(tab.evaluate("document.querySelectorAll('[name=manter_nao_selecionados]').length"), 0)
                tab.evaluate(f"document.querySelector('[name=funcionario]').value='{self.ana.pk}';document.querySelectorAll('input[type=checkbox][name=rodadas]')[29].click();true")
                self.assertIn('29 participantes', tab.evaluate("document.querySelector('#total-selecao').textContent"))
                self.assertIn('30 produtos', tab.evaluate("document.querySelector('#total-participantes').textContent"))
                tab.evaluate("document.querySelector('#confirmar-atribuicao').click();true")
                tab.wait("!!document.querySelector('#link-funcionario')")
                self.assertIn('29 produtos', tab.evaluate("document.querySelector('#total-participantes').textContent"))
                link = tab.evaluate("document.querySelector('#link-funcionario').value")
                tab.call('Page.navigate', {'url': link})
                tab.wait("!!document.querySelector('form button')")
                tab.evaluate("document.querySelector('form button').click();true")
                tab.wait('window.inventarioContagemPronta === true')
                for width in (320, 390):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertIn('29 produtos', tab.evaluate("document.querySelector('#total-produtos-contagem').textContent"))
                tab.evaluate("document.querySelector('[name=embalagens]').value='2';document.querySelector('[name=embalagens]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[value=ok]').click();true")
                tab.wait("document.querySelector('#progresso-contagem').textContent.includes('1 de 29')")
                tab.call('Page.reload')
                tab.wait("window.inventarioContagemPronta === true && document.querySelector('#progresso-contagem').textContent.includes('1 de 29')")
            finally:
                chrome.stop()
