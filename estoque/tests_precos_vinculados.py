"""Preparation only: previews cannot mutate prices or individual records."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.db import models
from .grupos_produtos import criar_grupo
from .models import Produto,GrupoProdutoVinculado,MembroGrupoProduto
from .services.precos_vinculados import diagnosticar_grupo

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.http import HttpResponse
from django.template.loader import get_template
from django.test import override_settings
from django.urls import include,path
from offline.browser_support import Chrome


def preview_test_page(request):
    grupo=GrupoProdutoVinculado.objects.first()
    url=reverse('estoque:grupos_produtos_detalhe',args=[grupo.pk])
    html=get_template('estoque/grupos_produtos.html').render({}, request=request)
    return HttpResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<button class="grupo-produto-vinculo" data-grupo-url="'+url+'">Grupo</button>'+html)


urlpatterns=[path('diagnostico-teste/',preview_test_page),path('',include('sistema.urls'))]


def atualizar_fixture(query, **campos):
    # Simulate legacy/inconsistent data in the isolated test database. Runtime
    # direct price writers are deliberately forbidden by the new service guard.
    return models.QuerySet.update(query, **campos)


def autorizar_operador(user):
    from django.contrib.auth.models import Permission
    user.user_permissions.add(Permission.objects.get(codename='change_produto', content_type__app_label='estoque'))
    return user


def criar_operador_precos(username):
    from django.contrib.auth import get_user_model
    return autorizar_operador(get_user_model().objects.create_user(username=username))


class PreviaPrecosVinculadosTests(TestCase):
    def setUp(self):
        self.produtos=[Produto.objects.create(nome='Sabor '+str(i),preco_compra=Decimal('10')+i,
            preco_vista=Decimal('36'),preco_prazo=Decimal('39'),preco_vista_fracionado=Decimal('7'),
            preco_prazo_fracionado=Decimal('7.50'),unidade_compra='PCT',unidade_venda_2='UN',
            vende_fracionado=True,fator_conversao=6,quantidade=10+i,codigo='codigo-'+str(i)) for i in range(3)]
        self.grupo=criar_grupo([p.pk for p in self.produtos],'Sabores teste')
        self.url=reverse('estoque:grupos_produtos_detalhe',args=[self.grupo.pk])

    def snapshot(self):
        return [list(m.objects.order_by('pk').values()) for m in [Produto,GrupoProdutoVinculado,MembroGrupoProduto]]

    def test_equal_compatible_no_implicit_reference_no_writes(self):
        before=self.snapshot();d=self.client.get(self.url).json()['precos_vinculados']
        self.assertEqual(d['status'],'precos_iguais');self.assertTrue(d['propagacao_disponivel'])
        self.assertIsNone(d['referencia_id']);self.assertEqual(d['previa'],[])
        self.assertEqual(self.snapshot(),before)

    def test_each_field_divergence_and_any_explicit_reference_preserve_data(self):
        for field in ['preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado']:
            original=getattr(self.produtos[0],field)
            atualizar_fixture(Produto.objects.filter(pk=self.produtos[0].pk),**{field:original+Decimal('1')})
            before=self.snapshot();d=self.client.get(self.url).json()['precos_vinculados']
            self.assertIn(field,d['divergentes']);self.assertEqual(d['status'],'precos_individuais')
            for p in self.produtos:
                response=self.client.get(self.url,{'referencia':p.pk});self.assertEqual(response.status_code,200)
                data=response.json()['precos_vinculados'];p.refresh_from_db()
                self.assertTrue(all(row['novos'][field]==str(getattr(p,field)) for row in data['previa']))
                self.assertEqual(self.snapshot(),before)
            atualizar_fixture(Produto.objects.filter(pk=self.produtos[0].pk),**{field:original,'preco_venda':Decimal('36')})

    def test_inactive_member_included(self):
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),ativo=False)
        d=self.client.get(self.url,{'referencia':self.produtos[0].pk}).json()['precos_vinculados']
        self.assertEqual(len(d['previa']),3);self.assertEqual(d['bloqueios'],[])

    def test_unit_factor_and_fractionation_incompatible(self):
        for updates in [{'unidade_venda_1':'CX'},{'unidade_venda_2':'DZ'}, {'fator_conversao':12},{'vende_fracionado':False}]:
            with self.subTest(updates=updates):
                atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),**updates)
                before=self.snapshot();d=self.client.get(self.url).json()['precos_vinculados']
                self.assertEqual(d['status'],'bloqueado');self.assertEqual(self.snapshot(),before)
                atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),unidade_venda_1='PCT',unidade_venda_2='UN',fator_conversao=6,vende_fracionado=True)

    def test_existing_loss_protection_identifies_member(self):
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),preco_compra=40)
        before=self.snapshot();d=self.client.get(self.url,{'referencia':self.produtos[0].pk}).json()['precos_vinculados']
        self.assertEqual(d['status'],'bloqueado')
        self.assertIn(self.produtos[1].pk,[b['produto_id'] for b in d['bloqueios']])
        self.assertEqual(self.snapshot(),before)

    def test_nonfractional_prices_do_not_include_fractional_fields(self):
        atualizar_fixture(Produto.objects.all(),vende_fracionado=False)
        d=self.client.get(self.url).json()['precos_vinculados'];self.assertEqual(d['campos'],['preco_vista','preco_prazo'])

    def test_invalid_reference_refused_and_version_changes_with_data(self):
        self.assertEqual(self.client.get(self.url,{'referencia':99999}).status_code,400)
        self.assertEqual(self.client.get(self.url,{'referencia':'invalid'}).status_code,400)
        before=self.client.get(self.url).json()['precos_vinculados']['versao_observada']
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[0].pk),preco_prazo=40)
        after=self.client.get(self.url).json()['precos_vinculados']['versao_observada']
        self.assertNotEqual(before,after)

    def test_excluded_and_incomplete_conversion_blocked(self):
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),excluido=True)
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[2].pk),fator_conversao=0)
        d=self.client.get(self.url).json()['precos_vinculados'];self.assertEqual(d['status'],'bloqueado')
        self.assertEqual({b['produto_id'] for b in d['bloqueios'] if b['produto_id']},{self.produtos[1].pk,self.produtos[2].pk})


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
@override_settings(ROOT_URLCONF=__name__)
class PreviaPrecosBrowserTests(StaticLiveServerTestCase):
    setUp=PreviaPrecosVinculadosTests.setUp
    snapshot=PreviaPrecosVinculadosTests.snapshot

    def preview(self,width):
        atualizar_fixture(Produto.objects.filter(pk=self.produtos[1].pk),preco_vista=37,preco_venda=37)
        before=self.snapshot()
        with TemporaryDirectory(prefix='linked-prices-preview-') as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab();tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Page.navigate',{'url':self.live_server_url+'/diagnostico-teste/'})
                tab.wait('!!document.getElementById("grupoProdutoPrecosReferencia")')
                tab.evaluate('document.querySelector(".grupo-produto-vinculo").click();true')
                tab.wait('document.getElementById("grupoProdutoModal").open && !document.getElementById("grupoProdutoPrecos").hidden')
                self.assertEqual(tab.evaluate('document.getElementById("grupoProdutoPrecosReferencia").value'),'')
                self.assertIn('precos_individuais',tab.evaluate('document.getElementById("grupoProdutoPrecosEstado").textContent'))
                tab.evaluate('document.getElementById("grupoProdutoPrecosReferencia").value="'+str(self.produtos[0].pk)+'";document.getElementById("grupoProdutoPrecosReferencia").dispatchEvent(new Event("change"));true')
                tab.wait('document.getElementById("grupoProdutoPrecosTabela").textContent.includes("37.00")')
                self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(self.snapshot(),before)
            finally:chrome.stop()

    def test_desktop_preview_no_price_writes(self):self.preview(1280)
    def test_mobile_preview_no_price_writes(self):self.preview(390)


def confirmacao_todos_fixture(produto_id):
    """Explicit all-peer consent for tests that exercise all-peer transactions."""
    from .models import MembroGrupoProduto
    grupo=MembroGrupoProduto.objects.select_related('grupo').get(produto_id=produto_id).grupo
    produtos=list(grupo.produtos.order_by('pk'))
    return dict(selecionados=[p.pk for p in produtos],confirmar=True,
        assinatura_esperada=diagnosticar_grupo(produtos,validar_precos=False)['assinatura_precos'])


def alterar_todos_precos_fixture(produto_id, atualizacoes, **kwargs):
    from .services.precos_vinculados import alterar_precos_grupo
    return alterar_precos_grupo(produto_id,atualizacoes,**{**confirmacao_todos_fixture(produto_id),**kwargs})


def aplicar_todos_precos_compra_fixture(item, atualizacoes, **kwargs):
    from .services.precos_compra import aplicar_precos_compra
    return aplicar_precos_compra(item,atualizacoes,**{**confirmacao_todos_fixture(item.produto_id),**kwargs})


def payload_todos_fixture(produto_id):
    comando=confirmacao_todos_fixture(produto_id);pk=str(produto_id)
    return {'confirmar_precos_produto_'+pk:'1','destinatarios_precos_produto_'+pk:comando['selecionados'],
            'assinatura_precos_produto_'+pk:comando['assinatura_esperada']}
