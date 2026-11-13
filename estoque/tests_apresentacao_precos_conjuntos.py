"""Joint presentation/prices regression, synthetic Friscos only."""
from decimal import Decimal
from unittest.mock import patch
from tempfile import TemporaryDirectory

from django.db import models
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from estoque import tests_apresentacao_vinculada as anterior
from estoque.models import Produto, AlteracaoPrecoVinculado, MembroGrupoProduto
from estoque.services.precos_vinculados import PRECOS_VINCULADOS
from offline.browser_support import Chrome
from estoque.tests_frisco_vinculo import CHROME


class ConjuntoFixture(anterior.ApresentacaoFixture):
    def setUp(self):
        super().setUp()
        for i, p in enumerate(self.produtos):
            models.QuerySet.update(Produto.objects.filter(pk=p.pk),
                preco_compra=Decimal('11.70')+i, preco_vista_fracionado=0,
                preco_prazo_fracionado=0)
            p.refresh_from_db()

    def dados(self, **novos):
        return super().dados(incluir_precos='1', preco_vista='15.80', preco_prazo='16.00',
            preco_vista_fracionado='1.00', preco_prazo_fracionado='1.10', **novos)

    def conferir(self, antes):
        permitidos = set(PRECOS_VINCULADOS) | {'preco_venda', 'precos_canonicos_adotados',
            'autoria_precos', 'atualizado_em'}
        permitidos |= {'unidade_compra','unidade_venda_1','unidade_venda_2','fator_conversao','vende_fracionado'}
        for a, d in zip(antes, self.estado()):
            self.assertEqual({k:v for k,v in a.items() if k not in permitidos},
                {k:v for k,v in d.items() if k not in permitidos})
            self.assertEqual(tuple(d[c] for c in PRECOS_VINCULADOS),
                (Decimal('15.80'),Decimal('16'),Decimal('1'),Decimal('1.10')))
            self.assertEqual((d['vende_fracionado'],d['unidade_venda_2'],d['fator_conversao']), (True,'UN',18))
        self.assertEqual(MembroGrupoProduto.objects.filter(grupo=self.grupo).count(),3)


class ConjuntoTests(ConjuntoFixture, TestCase):
    def test_final_substitui_zeros_persiste_preserva_e_reenvio_recusado(self):
        from inventario.models import MovimentoObservado
        antes = self.estado()
        movimentos = list(MovimentoObservado.objects.values())
        dados = self.dados(); previa = self.client.post(self.grupo_url,dados).json()
        self.assertFalse(previa['bloqueios']); self.assertEqual(self.estado(),antes)
        self.assertEqual(self.confirmar(dados,previa).status_code,200)
        self.conferir(antes)
        self.assertEqual(list(MovimentoObservado.objects.values()),movimentos)
        evento = AlteracaoPrecoVinculado.objects.get(origem='apresentacao_precos_grupo')
        self.assertEqual(len(evento.evidencia['apresentacao']),3)
        self.assertEqual(len(evento.evidencia['alteracoes']),3)
        for p in self.produtos:
            self.assertEqual(self.client.get(self.url(p)).context['form'].precos_observados['preco_vista_fracionado'],Decimal('1'))
        self.assertEqual(self.confirmar(dados,previa).status_code,400)

    def test_sem_confirmacao_adulteracao_e_custo_individual_recusados(self):
        antes = self.estado(); dados = self.dados()
        previa = self.client.post(self.grupo_url,dados).json()
        self.assertEqual(self.client.post(self.grupo_url,{**dados,'acao':'aplicar_apresentacao','assinatura':previa['assinatura']}).status_code,400)
        self.assertEqual(self.confirmar({**dados,'preco_vista_fracionado':'2'},previa).status_code,400)
        self.assertEqual(self.estado(),antes)
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk),preco_compra=Decimal('20'))
        antes = self.estado(); dados = self.dados()
        previa = self.client.post(self.grupo_url,dados).json()
        self.assertIn('Maracujá',str(previa['bloqueios']))
        self.assertIn('abaixo do custo',str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados,previa).status_code,400)
        self.assertEqual(self.estado(),antes)
        self.assertFalse(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_precos_grupo').exists())

    def test_custo_fracionado_persistido_e_previa_obsoleta(self):
        dados = self.dados(); previa = self.client.post(self.grupo_url,dados).json()
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[2].pk),
            preco_compra_fracionado=Decimal('1.20'))
        antes = self.estado()
        self.assertEqual(self.confirmar(dados,previa).status_code,400)
        previa = self.client.post(self.grupo_url,dados).json()
        self.assertIn('Morango',str(previa['bloqueios']))
        self.assertIn('abaixo do custo',str(previa['bloqueios']))
        self.assertEqual(self.confirmar(dados,previa).status_code,400)
        self.assertEqual(self.estado(),antes)

    def test_falha_auditoria_reverte_produtos_e_grupo(self):
        dados = self.dados(); previa = self.client.post(self.grupo_url,dados).json()
        antes = self.estado(); self.grupo.refresh_from_db()
        versao = self.grupo.versao_precos; original = models.QuerySet.create
        def falhar(queryset, **valores):
            if queryset.model is AlteracaoPrecoVinculado:
                raise RuntimeError('falha sint?tica auditoria')
            return original(queryset,**valores)
        with patch.object(models.QuerySet,'create',falhar):
            with self.assertRaises(RuntimeError): self.confirmar(dados,previa)
        self.assertEqual(self.estado(),antes)
        self.grupo.refresh_from_db(); self.assertEqual(self.grupo.versao_precos,versao)
        self.assertFalse(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_precos_grupo').exists())

    def test_rollback_segunda_escrita_e_auditoria(self):
        dados = self.dados(); previa = self.client.post(self.grupo_url,dados).json()
        antes = self.estado(); original = models.QuerySet.update; escritas = []
        self.grupo.refresh_from_db(); versao = self.grupo.versao_precos
        def falhar(queryset, **valores):
            if queryset.model is Produto and 'vende_fracionado' in valores:
                escritas.append(valores)
                if len(escritas)==2: raise RuntimeError('falha sintética')
            return original(queryset,**valores)
        with patch.object(models.QuerySet,'update',falhar):
            with self.assertRaises(RuntimeError): self.confirmar(dados,previa)
        self.assertEqual(len(escritas),2)
        self.assertIn('preco_vista_fracionado',escritas[0])
        self.assertEqual(self.estado(),antes)
        self.grupo.refresh_from_db(); self.assertEqual(self.grupo.versao_precos,versao)
        self.assertFalse(AlteracaoPrecoVinculado.objects.filter(origem='apresentacao_precos_grupo').exists())


class ConjuntoBrowserTests(ConjuntoFixture, StaticLiveServerTestCase):
    tocar = anterior.ApresentacaoBrowserTests.tocar
    abrir = anterior.ApresentacaoBrowserTests.abrir
    foto = anterior.ApresentacaoBrowserTests.foto

    def fluxo(self, largura):
        antes = self.estado()
        with TemporaryDirectory(prefix='conjunto-browser-',ignore_cleanup_errors=True) as perfil:
            browser = Chrome(CHROME,perfil).start()
            try:
                t = browser.tab()
                t.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
                if largura<820: t.call('Emulation.setTouchEmulationEnabled',dict(enabled=True,maxTouchPoints=1))
                t.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                self.abrir(t,self.produtos[0])
                t.evaluate("document.querySelector('#vende_fracionado_visual').value='True';document.querySelector('#vende_fracionado_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#unidade_fracionada_visual').value='UN';document.querySelector('#unidade_fracionada_visual').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#fator_visual').value='18';document.querySelector('#fator_visual').dispatchEvent(new Event('input',{bubbles:true}));true")
                t.evaluate("for(const [c,v] of Object.entries({preco_vista:'15.80',preco_prazo:'16.00',preco_vista_fracionado:'1.00',preco_prazo_fracionado:'1.10'})){const e=document.querySelector('[name='+c+']');e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));}true")
                self.tocar(t,'.btn-salvar'); t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                self.assertIn('Confirmar apresentação e preços',t.evaluate("document.querySelector('#confirmarApresentacao').textContent"))
                self.assertFalse(t.evaluate("document.querySelector('[data-confirmar-apresentacao]').disabled"))
                self.assertEqual(t.evaluate("document.querySelectorAll('#confirmarApresentacao .ps-confirm-price').length"),12)
                self.assertFalse(t.evaluate("document.querySelector('#confirmarApresentacao').getBoundingClientRect().width>innerWidth"))
                self.foto(t,str(largura)+'-conjunto-previa')
                self.assertEqual(self.estado(),antes)
                self.tocar(t,'[data-cancelar-apresentacao]'); self.assertEqual(self.estado(),antes)
                self.tocar(t,'.btn-salvar'); t.wait("!!document.querySelector('#confirmarApresentacao[open]')")
                t.evaluate('window.apresentacaoAnterior=true;true'); self.tocar(t,'[data-confirmar-apresentacao]')
                t.wait("!window.apresentacaoAnterior && document.readyState==='complete' && !!document.querySelector('#fator_visual')")
                self.assertIn('preços de venda salvos juntos',t.evaluate('document.body.innerText'))
                self.conferir(antes)
                for p in self.produtos:
                    self.abrir(t,p)
                    self.assertEqual(Decimal(t.evaluate("document.querySelector('[name=preco_vista_fracionado]').value").replace(',','.')),1)
                    self.assertEqual(Decimal(t.evaluate("document.querySelector('#fator_visual').value").replace(',','.')),18)
            finally: browser.stop()

    def test_desktop(self): self.fluxo(1280)
    def test_mobile(self): self.fluxo(390)
