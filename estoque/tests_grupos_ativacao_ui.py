"""Persisted activation indicators; isolated fixtures, no commercial changes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from offline.browser_support import Chrome
from .models import GrupoProdutoVinculado, Produto, AlteracaoPrecoVinculado
from .tests_precos_vinculados_servico import ServicoPrecosVinculadosTests


class GruposAtivacaoFixture:
    setUp = ServicoPrecosVinculadosTests.setUp

    def listar(self, client=None):
        return (client or self.client).get(reverse('estoque:grupos_produtos_lista'))


class GruposAtivacaoEstadoTests(GruposAtivacaoFixture, TestCase):
    def test_mixed_states_and_counts_come_from_persisted_groups(self):
        outro=GrupoProdutoVinculado.objects.create(nome='Outro ativado',precos_regularizados=True)
        antes=list(Produto.objects.values())
        resposta=self.listar();dados=resposta.json()
        self.assertEqual((dados['ativados'],dados['total']),(1,2))
        estados={g['id']:g['ativado'] for g in dados['grupos']}
        self.assertEqual(estados,{self.grupo.pk:False,outro.pk:True})
        self.assertIn('no-store',resposta['Cache-Control'])
        self.assertEqual(list(Produto.objects.values()),antes)
        self.assertFalse(AlteracaoPrecoVinculado.objects.exists())

    def test_activation_survives_new_session_and_does_not_reprice(self):
        antes=list(Produto.objects.values())
        previa=self.client.get(self.url).json()['precos_vinculados']
        resposta=self.client.post(self.url,{'acao':'regularizar','confirmar':'1','versao_observada':previa['versao_observada']})
        self.assertEqual(resposta.status_code,200)
        novo=Client();novo.force_login(self.operador)
        for _ in range(2):
            dados=self.listar(novo).json()
            self.assertEqual((dados['ativados'],dados['total']),(1,1))
            self.assertTrue(dados['grupos'][0]['ativado'])
            self.assertTrue(novo.get(self.url).json()['precos_vinculados']['regularizado'])
        self.assertEqual(list(Produto.objects.values()),antes)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)

    def test_new_read_reflects_state_changes_not_a_cached_counter(self):
        self.assertEqual(self.listar().json()['ativados'],0)
        GrupoProdutoVinculado.objects.filter(pk=self.grupo.pk).update(precos_regularizados=True)
        self.assertEqual(self.listar().json()['ativados'],1)
        GrupoProdutoVinculado.objects.filter(pk=self.grupo.pk).update(precos_regularizados=False)
        self.assertEqual(self.listar().json()['ativados'],0)

    def test_empty_list_has_zero_counter(self):
        from .grupos_produtos import excluir_grupo
        excluir_grupo(self.grupo.pk)
        self.assertEqual(self.listar().json(),{'grupos':[],'total':0,'ativados':0})


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
@override_settings(ROOT_URLCONF='estoque.tests_precos_vinculados')
class GruposAtivacaoBrowserTests(GruposAtivacaoFixture, StaticLiveServerTestCase):
    def fluxo(self,width):
        antes=list(Produto.objects.order_by('pk').values())
        with TemporaryDirectory(prefix='group-activation-state-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/diagnostico-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("grupoProdutoGerenciar")')
                origem=tab.evaluate('performance.timeOrigin')
                def abrir_lista(estado,contagem):
                    tab.evaluate('document.getElementById("grupoProdutoGerenciar").click();true')
                    tab.wait('document.getElementById("grupoProdutoAtivadosContador").textContent === "'+contagem+'"')
                    self.assertIn(estado,tab.evaluate('document.getElementById("grupoProdutoGrupos").textContent'))
                abrir_lista('PENDENTE','0 de 1 grupos ativados')
                tab.evaluate('document.querySelector("#grupoProdutoGrupos button").click();true')
                tab.wait('document.getElementById("grupoProdutoModal").open && !document.getElementById("grupoProdutoPrecos").hidden')
                self.assertFalse(tab.evaluate('document.getElementById("grupoProdutoRegularizar").hidden'))
                self.assertFalse(tab.evaluate('document.getElementById("grupoProdutoAtivacaoConfirmacao").hidden'))
                self.assertIn('PENDENTE DE ATIVAÇÃO',tab.evaluate('document.getElementById("grupoProdutoAtivacaoEstado").textContent'))
                self.assertIn('pendente',tab.evaluate('document.getElementById("grupoProdutoAtivacaoPainel").className'))
                tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").click();document.getElementById("grupoProdutoRegularizar").click();true')
                tab.wait('document.getElementById("grupoProdutoAtivadosContador").textContent === "1 de 1 grupos ativados"')
                self.assertEqual(tab.evaluate('document.getElementById("grupoProdutoAtivacaoEstado").textContent'),'✓ GRUPO ATIVADO')
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoRegularizar").hidden'))
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoAtivacaoConfirmacao").hidden'))
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").disabled'))
                self.assertEqual(tab.evaluate('document.getElementById("grupoProdutoAtivacaoAjuda").textContent'),'Pronto para reajustes seletivos. Preços atuais preservados.')
                self.assertIn('ATIVADO',tab.evaluate('document.getElementById("grupoProdutoGrupos").textContent'))
                tab.evaluate('document.getElementById("grupoProdutoFechar").click();true')
                tab.wait('document.getElementById("grupoProdutoGerenciarModal").open && document.getElementById("grupoProdutoAtivadosContador").textContent === "1 de 1 grupos ativados"')
                self.assertEqual(tab.evaluate('performance.timeOrigin'),origem)
                tab.evaluate('document.querySelector("#grupoProdutoGrupos button").click();true')
                tab.wait('document.getElementById("grupoProdutoAtivacaoEstado").textContent === "✓ GRUPO ATIVADO"')
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoRegularizar").hidden'))
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoAtivacaoConfirmacao").hidden'))
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").disabled'))
                self.assertEqual(tab.evaluate('document.getElementById("grupoProdutoAtivacaoAjuda").textContent'),'Pronto para reajustes seletivos. Preços atuais preservados.')
                self.assertIn(self.produtos[0].nome,tab.evaluate('document.getElementById("grupoProdutoMembros").textContent'))
                tab.evaluate('document.getElementById("grupoProdutoFechar").click();true')
                tab.wait('document.getElementById("grupoProdutoGerenciarModal").open')
                tab.evaluate('document.getElementById("grupoProdutoGerenciarFechar").click();true')
                abrir_lista('ATIVADO','1 de 1 grupos ativados')
                tab.call('Page.reload')
                tab.wait('performance.timeOrigin !== '+str(origem)+' && document.readyState === "complete" && !!document.getElementById("grupoProdutoGerenciar")')
                abrir_lista('ATIVADO','1 de 1 grupos ativados')
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
            finally:chrome.stop()

    def test_desktop_activation_list_counter_and_reload(self):self.fluxo(1280)
    def test_mobile_activation_list_counter_and_reload(self):self.fluxo(390)

    def administrar_ativo(self,width):
        ServicoPrecosVinculadosTests.ativar(self)
        novo=Produto.objects.create(nome='Novo integrante visual',preco_compra=10,preco_vista=50,preco_prazo=55,
            preco_vista_fracionado=9,preco_prazo_fracionado=10,unidade_compra='PCT',unidade_venda_2='UN',vende_fracionado=True,fator_conversao=6)
        antes=list(Produto.objects.order_by('pk').values())
        with TemporaryDirectory(prefix='group-active-administration-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/diagnostico-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("grupoProdutoModal")')
                tab.evaluate('document.querySelector(".grupo-produto-vinculo").click();true')
                tab.wait('document.getElementById("grupoProdutoAtivacaoEstado").textContent === "✓ GRUPO ATIVADO"')
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoAtivacaoConfirmacao").hidden'))
                # Dispatch cannot start a second activation even if a script checks the hidden input.
                tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").checked=true;document.getElementById("grupoProdutoRegularizar").dispatchEvent(new Event("click"));true')
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
                tab.evaluate('document.getElementById("grupoProdutoAdicionar").click();true')
                tab.wait('!!document.querySelector("#grupoProdutoElegiveis input")')
                tab.evaluate('document.querySelector("#grupoProdutoElegiveis input").click();document.getElementById("grupoProdutoConfirmarAdicao").click();true')
                tab.wait('!document.getElementById("grupoProdutoAplicarAdicao").hidden')
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoAtivacaoConfirmacao").hidden'))
                self.assertEqual(tab.evaluate('document.getElementById("grupoProdutoAtivacaoEstado").textContent'),'✓ GRUPO ATIVADO')
                self.assertFalse(tab.evaluate('document.getElementById("grupoProdutoAdicaoConfirmacao").hidden'))
                self.assertFalse(tab.evaluate('document.getElementById("grupoProdutoAdicaoCompatibilidade").checked'))
                tab.evaluate('document.getElementById("grupoProdutoAplicarAdicao").click();true')
                tab.wait('document.getElementById("grupoProdutoErro").textContent.includes("Confirme")')
                self.assertFalse(self.grupo.produtos.filter(pk=novo.pk).exists())
                tab.evaluate('document.getElementById("grupoProdutoAdicaoCompatibilidade").click();document.getElementById("grupoProdutoAplicarAdicao").click();true')
                tab.wait('document.getElementById("grupoProdutoMembros").textContent.toLowerCase().includes("novo integrante visual") && document.getElementById("grupoProdutoAdicionarPainel").hidden')
                self.assertTrue(self.grupo.produtos.filter(pk=novo.pk).exists())
                tab.evaluate('Array.from(document.querySelectorAll("#grupoProdutoMembros button")).find(b=>b.getAttribute("aria-label").toLowerCase().includes("novo integrante visual")).click();true')
                tab.wait('!document.getElementById("grupoProdutoMembros").textContent.toLowerCase().includes("novo integrante visual")')
                self.assertFalse(self.grupo.produtos.filter(pk=novo.pk).exists())
                tab.evaluate('document.getElementById("grupoProdutoNome").value="Grupo visual renomeado";document.getElementById("grupoProdutoSalvar").click();true')
                tab.wait('document.getElementById("grupoProdutoSucesso").textContent.includes("Grupo atualizado")')
                self.grupo.refresh_from_db();self.assertEqual(self.grupo.nome,'Grupo visual renomeado')
                self.assertTrue(self.grupo.precos_regularizados)
                self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
            finally:chrome.stop()

    def test_desktop_active_group_preserves_administration_and_entry_confirmation(self):self.administrar_ativo(1280)
    def test_mobile_active_group_preserves_administration_and_entry_confirmation(self):self.administrar_ativo(390)
