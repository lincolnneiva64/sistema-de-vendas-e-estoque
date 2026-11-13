"""Coordinated presentation, synthetic records only."""
import base64
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.db import models
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque.tests_frisco_divergencia import LegadoFixture
from estoque.tests_frisco_vinculo import CHROME
from estoque import tests_decisoes_precos_browser as interacao
from estoque.models import Produto, MembroGrupoProduto, AlteracaoPrecoVinculado, Unidade
from estoque.models import Compra, ItemCompra, MovimentacaoEstoqueManual
from estoque.services.precos_vinculados import versao_cadastro_produto
from offline.browser_support import Chrome


class ApresentacaoFixture(LegadoFixture):
    def setUp(self):
        super().setUp()
        # Deliberately different prices, costs and minimums; do not homogenize.
        for i, p in enumerate(self.produtos):
            models.QuerySet.update(Produto.objects.filter(pk=p.pk),
                preco_vista=Decimal('15.80')+i, preco_prazo=Decimal('16.00')+i,
                preco_vista_fracionado=Decimal('1.00')+Decimal(i)/10,
                preco_prazo_fracionado=Decimal('1.10')+Decimal(i)/10,
                estoque_minimo=i+1)
            p.refresh_from_db()
        self.grupo_url = reverse('estoque:grupos_produtos_detalhe', args=[self.grupo.pk])

    def dados(self, **novos):
        self.produtos[0].refresh_from_db()
        return dict(acao='previa_apresentacao', fonte=self.produtos[0].pk,
            unidade_compra='CX', vende_fracionado='True', unidade_venda_2='UN',
            fator_conversao='18', versao_cadastro_precos=versao_cadastro_produto(self.produtos[0]), **novos)

    def confirmar(self, dados, previa):
        return self.client.post(self.grupo_url, {**dados, 'acao':'aplicar_apresentacao',
            'confirmar':'1', 'assinatura':previa['assinatura']})

    def conferir_preservacao(self, antes):
        depois = self.estado()
        estrutura = {'unidade_compra', 'unidade_venda_1', 'unidade_venda_2',
            'vende_fracionado', 'fator_conversao', 'atualizado_em'}
        for a, d in zip(antes, depois):
            self.assertEqual({k:v for k,v in a.items() if k not in estrutura},
                {k:v for k,v in d.items() if k not in estrutura})
        self.assertEqual(set(MembroGrupoProduto.objects.filter(grupo=self.grupo).values_list('produto_id',flat=True)),
            {p.pk for p in self.produtos})


class ApresentacaoTests(ApresentacaoFixture, TestCase):
    def test_confirmar_reabrir_preserva_precos_saldos_minimos_historico_e_reenvio(self):
        from inventario.models import MovimentoObservado, SequenciaEstoque
        antes = self.estado()
        movimentos = list(MovimentoObservado.objects.order_by('pk').values())
        sequencias = list(SequenciaEstoque.objects.order_by('pk').values())
        eventos = list(AlteracaoPrecoVinculado.objects.values())
        dados = self.dados()
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertFalse(previa['bloqueios'])
        self.assertEqual(len(previa['produtos']), 3)
        self.assertEqual(self.estado(), antes)
        resposta = self.confirmar(dados, previa)
        self.assertEqual(resposta.status_code, 200, resposta.content)
        for p in self.produtos:
            form = self.client.get(self.url(p)).context['form']
            self.assertEqual((form.apresentacao_observada['vende_fracionado'],
                form.apresentacao_observada['unidade_venda_2'], Decimal(form.apresentacao_observada['fator_conversao'])), (True, 'UN', 18))
        self.conferir_preservacao(antes)
        self.assertEqual(list(MovimentoObservado.objects.order_by('pk').values()), movimentos)
        self.assertEqual(list(SequenciaEstoque.objects.order_by('pk').values()), sequencias)
        self.assertEqual(list(AlteracaoPrecoVinculado.objects.exclude(origem='apresentacao_grupo').values()), eventos)
        self.assertEqual(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_grupo').count(), 1)
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_grupo').count(), 1)

    def test_cancelar_ou_omitir_confirmacao_nao_escreve(self):
        antes = self.estado()
        dados = self.dados()
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertFalse(previa['bloqueios'])
        self.assertEqual(self.estado(), antes)
        resposta = self.client.post(self.grupo_url, {**dados, 'acao':'aplicar_apresentacao', 'assinatura':previa['assinatura']})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self.estado(), antes)
        self.assertFalse(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_grupo').exists())

    def test_produto_incompativel_identificado_sem_gravacao_parcial(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), preco_vista_fracionado=Decimal('.10'))
        antes = self.estado()
        dados = self.dados()
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertTrue(previa['bloqueios'])
        self.assertIn('Maracujá', str(previa['bloqueios']))
        self.assertIn('abaixo do custo', str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)

    def test_unidade_base_com_saldo_e_fator_ja_em_uso_bloqueados(self):
        Unidade.objects.get_or_create(sigla='CX2', defaults={'nome':'Caixa 2'})
        dados = self.dados(); dados['unidade_compra'] = 'CX2'
        antes = self.estado()
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertIn('saldo ou o mínimo', str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)
        dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
        self.assertEqual(self.confirmar(dados, previa).status_code, 200)
        dados = self.dados(); dados['fator_conversao'] = '24'
        antes = self.estado()
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertIn('saldo ou o mínimo', str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)

    def test_previa_obsoleta_assinatura_adulterada_e_permissao(self):
        dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), quantidade=7)
        antes = self.estado()
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)
        previa = self.client.post(self.grupo_url, dados).json()
        previa['assinatura'] += 'invalida'
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.user.is_superuser = False; self.user.is_staff = False; self.user.save()
        self.assertEqual(self.client.post(self.grupo_url, dados).status_code, 403)

    def test_rollback_de_falha_durante_escrita(self):
        dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
        antes = self.estado()
        original = models.QuerySet.update
        escritas = []
        def falhar(queryset, **kwargs):
            if queryset.model is Produto and 'vende_fracionado' in kwargs:
                escritas.append(1)
                if len(escritas) == 2:
                    raise RuntimeError('Falha de teste exclusivamente descartável')
            return original(queryset, **kwargs)
        with patch.object(models.QuerySet, 'update', falhar):
            with self.assertRaises(RuntimeError):
                self.confirmar(dados, previa)
        self.assertEqual(len(escritas), 2)
        self.assertEqual(self.estado(), antes)
        self.assertFalse(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_grupo').exists())

    def test_fator_vazio_ou_zero_nao_recebe_fallback(self):
        antes = self.estado()
        for fator in ('', '0', 'NaN', '18,00'):
            dados = self.dados(); dados['fator_conversao'] = fator
            self.assertEqual(self.client.post(self.grupo_url, dados).status_code, 400)
        self.assertEqual(self.estado(), antes)

    def test_historico_cx_preservado_mas_unidade_antiga_nao_primaria_bloqueia(self):
        compra = Compra.objects.create(data_compra='2026-11-13')
        item = ItemCompra.objects.create(compra=compra, produto=self.produtos[1],
            unidade='CX', quantidade=2, preco_unitario=Decimal('11.70'), valor_total=Decimal('23.40'))
        historico = list(ItemCompra.objects.values())
        dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
        self.assertFalse(previa['bloqueios'])
        self.assertEqual(self.confirmar(dados, previa).status_code, 200)
        self.assertEqual(list(ItemCompra.objects.values()), historico)

    def test_historico_desconhecido_ou_fracionado_impede_nova_conversao(self):
        compra = Compra.objects.create(data_compra='2026-11-13')
        item = ItemCompra.objects.create(compra=compra, produto=self.produtos[1],
            unidade='UN', quantidade=2, preco_unitario=1, valor_total=2)
        antes = self.estado()
        for unidade in ('UN', ''):
            ItemCompra.objects.filter(pk=item.pk).update(unidade=unidade)
            dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
            self.assertIn('Maracujá', str(previa['bloqueios']))
            self.assertIn('histórico', str(previa['bloqueios']))
            self.assertEqual(self.confirmar(dados, previa).status_code, 400)
            self.assertEqual(self.estado(), antes)

    def test_historico_base_impede_troca_de_unidade_mesmo_sem_saldo(self):
        models.QuerySet.update(Produto.objects.all(), quantidade=0, estoque_minimo=0)
        MovimentacaoEstoqueManual.objects.create(produto=self.produtos[1], tipo='ajuste',
            estoque_antes=4, estoque_depois=0, diferenca=-4, motivo='Fixture')
        Unidade.objects.create(sigla='CX2', nome='Caixa 2')
        antes = self.estado()
        dados = self.dados(); dados['unidade_compra'] = 'CX2'
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertIn('Maracujá', str(previa['bloqueios']))
        self.assertIn('histórico', str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)

    def test_codigo_legitimo_com_numero_e_precos_injetados_nao_sao_salvos(self):
        Unidade.objects.create(sigla='UN2', nome='Unidade 2')
        antes = self.estado()
        dados = self.dados(); dados.update(unidade_venda_2='UN2', preco_vista='999', quantidade='999')
        previa = self.client.post(self.grupo_url, dados).json()
        self.assertFalse(previa['bloqueios'])
        self.assertEqual(self.confirmar(dados, previa).status_code, 200)
        self.conferir_preservacao(antes)
        self.assertEqual(set(Produto.objects.values_list('unidade_venda_2', flat=True)), {'UN2'})

    def test_unidade_monetaria_nao_cadastrada_rejeitada_e_csrf_obrigatorio(self):
        from django.test import Client
        dados = self.dados(); dados['unidade_venda_2'] = '2,95'
        antes = self.estado()
        self.assertEqual(self.client.post(self.grupo_url, dados).status_code, 400)
        self.assertEqual(self.estado(), antes)
        cliente = Client(enforce_csrf_checks=True); cliente.force_login(self.user)
        self.assertEqual(cliente.post(self.grupo_url, self.dados()).status_code, 403)

    def test_unidade_principal_legada_divergente_nao_e_corrigida_com_saldo(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), unidade_venda_1='UN')
        antes = self.estado()
        dados = self.dados(); previa = self.client.post(self.grupo_url, dados).json()
        self.assertIn('Maracujá', str(previa['bloqueios']))
        self.assertIn('reinterpretaria', str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados, previa).status_code, 400)
        self.assertEqual(self.estado(), antes)


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class ApresentacaoBrowserTests(ApresentacaoFixture, StaticLiveServerTestCase):
    tocar = interacao.DecisoesBrowserTests.tocar

    def abrir(self, t, produto):
        t.evaluate('window.anteriorApresentacao=true;true')
        t.call('Page.navigate', {'url':self.live_server_url+self.url(produto)})
        t.wait("!window.anteriorApresentacao && document.readyState==='complete' && !!document.querySelector('#fator_visual')")

    def foto(self, t, nome):
        pasta = Path('amostras/apresentacao-vinculada'); pasta.mkdir(parents=True, exist_ok=True)
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(t.call('Page.captureScreenshot', {'format':'png'})['data']))

    def fluxo(self, largura, bloqueado=False):
        if bloqueado:
            models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), preco_vista_fracionado=Decimal('.10'))
        antes = self.estado()
        with TemporaryDirectory(prefix='apresentacao-browser-', ignore_cleanup_errors=True) as perfil:
            browser = Chrome(CHROME, perfil).start()
            try:
                t = browser.tab()
                t.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
                if largura < 820:
                    t.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
                t.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                self.abrir(t, self.produtos[0])
                self.assertEqual(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value"), '')
                t.evaluate("document.querySelector('#vende_fracionado_visual').value='True';document.querySelector('#vende_fracionado_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#unidade_fracionada_visual').value='UN';document.querySelector('#unidade_fracionada_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#fator_visual').value='18';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));true")
                self.assertEqual(Decimal(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value")), Decimal('.65'))
                self.tocar(t, '.btn-salvar')
                t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                self.assertIn('Deseja atualizar os demais produtos?', t.evaluate("document.querySelector('#confirmarApresentacao').textContent"))
                self.assertEqual(t.evaluate("document.querySelectorAll('#confirmarApresentacao .ps-confirm-card').length"), 3)
                self.assertEqual(self.estado(), antes)
                self.assertFalse(t.evaluate("document.querySelector('#confirmarApresentacao').getBoundingClientRect().width > innerWidth"))
                self.foto(t, str(largura)+('-bloqueio' if bloqueado else '-previa'))
                if bloqueado:
                    self.assertTrue(t.evaluate("document.querySelector('[data-confirmar-apresentacao]').disabled"))
                    self.assertIn('Maracujá', t.evaluate("document.querySelector('#confirmarApresentacao [role=alert]').textContent"))
                    self.assertIn('Nada foi salvo', t.evaluate("document.querySelector('#confirmarApresentacao [role=alert]').textContent"))
                self.tocar(t, '[data-cancelar-apresentacao]')
                self.assertEqual(self.estado(), antes)
                self.assertIn('não salva', t.evaluate("document.querySelector('#apresentacaoPendente').textContent"))
                if bloqueado:
                    self.abrir(t, self.produtos[0])
                    self.assertEqual(t.evaluate("document.querySelector('#vende_fracionado_visual').value"), 'False')
                    return
                self.tocar(t, '.btn-salvar')
                t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                t.evaluate('window.apresentacaoAnterior=true;true')
                self.tocar(t, '[data-confirmar-apresentacao]')
                t.wait("!window.apresentacaoAnterior && document.readyState==='complete' && !!document.querySelector('#fator_visual')")
                self.assertIn('Apresentação salva nos produtos confirmados', t.evaluate('document.body.innerText'))
                self.assertEqual(t.evaluate("document.querySelector('#vende_fracionado_visual').value"), 'True')
                for p in self.produtos:
                    self.abrir(t, p)
                    self.assertEqual(t.evaluate("document.querySelector('#vende_fracionado_visual').value"), 'True')
                    self.assertEqual(t.evaluate("document.querySelector('#unidade_fracionada_visual').value"), 'UN')
                    self.assertEqual(Decimal(t.evaluate("document.querySelector('#fator_visual').value").replace(',','.')), 18)
                    self.assertEqual(Decimal(t.evaluate("document.querySelector('#id_preco_compra_fracionado').value")), Decimal('.65'))
                self.conferir_preservacao(antes)
                t.evaluate("document.querySelector('#fator_visual').scrollIntoView({block:'center',behavior:'instant'});true")
                self.foto(t, str(largura)+'-persistiu')
            finally:
                browser.stop()

    def test_desktop_confirmar_cancelar_reabrir(self):
        self.fluxo(1280)

    def test_mobile_confirmar_cancelar_reabrir(self):
        self.fluxo(390)

    def test_mobile_incompativel_nao_parece_salvo(self):
        self.fluxo(390, bloqueado=True)
