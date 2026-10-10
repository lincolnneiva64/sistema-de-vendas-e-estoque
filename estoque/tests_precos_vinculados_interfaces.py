"""Operational commands through the real views; isolated fixtures only."""
from decimal import Decimal
from unittest.mock import patch
from unittest import skipUnless
from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from offline.browser_support import Chrome

from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from django.urls import reverse, include, path
from django.http import HttpResponse
from django.template.loader import get_template

from .forms import ProdutoForm
from .admin import ProdutoAdminForm
from .models import Produto, Categoria, Unidade, GrupoProdutoVinculado, AlteracaoPrecoVinculado
from . import tests_precos_vinculados_servico as servico_tests
from .services.precos_vinculados import alterar_precos_grupo
from .services.precos_vinculados import diagnosticar_grupo, regularizar_grupo
from .grupos_produtos import criar_grupo
from . import tests_compra_pre_revisao as compra_tests
from .tests_precos_vinculados import autorizar_operador


def pagina_novo_grupo(request):
    selecao = ''.join('<label hidden><input class="grupo-produto-checkbox" type="checkbox" value="'+str(p.pk)+'"></label>'
        for p in Produto.objects.filter(vinculo_grupo__isnull=True))
    return HttpResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'+
        selecao+get_template('estoque/grupos_produtos.html').render({}, request=request))


urlpatterns = [path('novo-grupo-teste/', pagina_novo_grupo), path('', include('sistema.urls'))]

from .tests_precos_vinculados import (alterar_todos_precos_fixture as alterar_precos_grupo,
    aplicar_todos_precos_compra_fixture as aplicar_precos_compra, confirmacao_todos_fixture, payload_todos_fixture)


class InterfacesPrecosVinculadosTests(TestCase):
    compra = servico_tests.ServicoPrecosVinculadosTests.compra
    ativar = servico_tests.ServicoPrecosVinculadosTests.ativar
    snapshot = servico_tests.ServicoPrecosVinculadosTests.snapshot

    def setUp(self):
        servico_tests.ServicoPrecosVinculadosTests.setUp(self)
        self.operador = autorizar_operador(get_user_model().objects.create_user(username='precos-test', password='test-only'))
        self.client.force_login(self.operador)
        Categoria.objects.get_or_create(nome='Bebidas')
        for sigla in ('PCT', 'UN'):
            Unidade.objects.get_or_create(sigla=sigla, defaults={'nome': sigla})
        self.criar_url = reverse('estoque:grupos_produtos_criar')

    def comando(self, referencia=None):
        referencia = referencia or self.produtos[0].pk
        d = self.client.get(self.url, {'referencia': referencia}).json()['precos_vinculados']
        return {'acao': 'regularizar', 'referencia': referencia, 'versao_observada': d['versao_observada'], 'confirmar': '1'}

    def editor(self, produto=None):
        produto = produto or Produto.objects.get(pk=self.produtos[0].pk)
        form = ProdutoForm(instance=produto, integrar_precos_vinculados=True)
        dados = {name: value for name, value in form.initial.items() if value is not None}
        dados.update(categoria='Bebidas', vende_fracionado='on', grupo_precos_id=self.grupo.pk)
        self.grupo.refresh_from_db()
        dados['versao_precos_grupo'] = self.grupo.versao_precos
        comando=confirmacao_todos_fixture(produto.pk)
        dados.update(destinatarios_precos=[p.pk for p in self.produtos if p.pk!=produto.pk],confirmar_precos_seletivos=True,assinatura_precos_seletivos=comando['assinatura_esperada'])
        dados.pop('fornecedores', None)
        return dados

    def test_activation_requires_confirmation_and_fresh_preview_without_reference(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), preco_prazo=42)
        before = self.snapshot()
        payload = self.comando(self.produtos[1].pk)
        for field in ('confirmar', 'versao_observada'):
            invalid = dict(payload); invalid.pop(field)
            self.assertEqual(self.client.post(self.url, invalid).status_code, 400)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.post(self.url, payload).status_code, 200)
        self.grupo.refresh_from_db()
        self.assertTrue(self.grupo.precos_regularizados)
        self.assertEqual(set(Produto.objects.values_list('preco_prazo', flat=True)), {Decimal(39),Decimal(42)})
        self.assertEqual(AlteracaoPrecoVinculado.objects.get().operador, self.operador)

    def test_stale_and_incompatible_activation_blocked_without_repricing(self):
        payload = self.comando()
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_compra=40)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, payload).status_code, 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.post(self.url, self.comando()).status_code, 200)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(), 1)
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), preco_compra=10, fator_conversao=12)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, self.comando()).status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_new_group_preview_and_confirmed_default_activation(self):
        self.client.post(self.url, {'acao': 'excluir', 'confirmar': '1'})
        ids = [p.pk for p in self.produtos]
        payload = {'acao': 'previa', 'produto_ids': ids, 'referencia': self.produtos[2].pk}
        d = self.client.post(self.criar_url, payload).json()['precos_vinculados']
        before = self.snapshot()
        payload.update(acao='criar', nome='Novo grupo', versao_observada=d['versao_observada'])
        self.assertEqual(self.client.post(self.criar_url, payload).status_code, 400)
        self.assertFalse(GrupoProdutoVinculado.objects.exists())
        self.assertEqual(self.snapshot(), before)
        payload['confirmar'] = '1'
        self.assertEqual(self.client.post(self.criar_url, payload).status_code, 201)
        self.assertTrue(GrupoProdutoVinculado.objects.get().precos_regularizados)

    def test_group_activation_does_not_apply_existing_invalid_prices(self):
        self.client.post(self.url, {'acao': 'excluir', 'confirmar': '1'})
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_compra=40)
        payload = {'acao': 'previa', 'produto_ids': [p.pk for p in self.produtos], 'referencia': self.produtos[0].pk}
        d = self.client.post(self.criar_url, payload).json()['precos_vinculados']
        payload.update(acao='criar', nome='Não deve existir', confirmar='1', versao_observada=d['versao_observada'])
        before = self.snapshot()
        self.assertEqual(self.client.post(self.criar_url, payload).status_code, 201)
        self.assertTrue(GrupoProdutoVinculado.objects.exists())
        self.assertEqual(self.snapshot(), before)

    def test_editor_any_member_all_four_prices_and_individual_data(self):
        self.ativar()
        before = {p.pk: (p.quantidade, p.preco_compra, p.codigo) for p in Produto.objects.all()}
        for index, field in enumerate(('preco_vista', 'preco_prazo', 'preco_vista_fracionado', 'preco_prazo_fracionado')):
            p = Produto.objects.get(pk=self.produtos[index % 3].pk)
            payload = self.editor(p); payload[field] = str(getattr(p, field) + 1)
            response = self.client.post(reverse('estoque:produto_editar', args=[p.pk]), payload)
            self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['form'].errors)
            self.assertEqual(set(Produto.objects.values_list(field, flat=True)), {getattr(p, field) + 1})
        self.assertEqual({p.pk: (p.quantidade, p.preco_compra, p.codigo) for p in Produto.objects.all()}, before)

    def test_stale_editor_missing_version_and_changed_membership_refused(self):
        self.ativar(); payload = self.editor(); stale = dict(payload)
        self.grupo.refresh_from_db()
        alterar_precos_grupo(self.produtos[1].pk, {'preco_prazo': 44}, versao_esperada=self.grupo.versao_precos, operador=self.operador)
        before = self.snapshot(); url = reverse('estoque:produto_editar', args=[self.produtos[0].pk])
        for values in (stale, {**payload, 'versao_precos_grupo': ''}, {**self.editor(), 'grupo_precos_id': ''}):
            response = self.client.post(url, values)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertEqual(self.snapshot(), before)

    def test_editor_cost_and_prices_validated_together_and_rollback(self):
        self.ativar(); payload = self.editor()
        payload.update(preco_compra=35, preco_vista=40, preco_prazo=45)
        url = reverse('estoque:produto_editar', args=[self.produtos[0].pk])
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(Produto.objects.get(pk=self.produtos[0].pk).preco_compra, 35)
        self.assertEqual(Produto.objects.get(pk=self.produtos[1].pk).preco_compra, 11)
        before = self.snapshot()
        payload = self.editor(); payload.update(preco_compra=12, preco_vista_fracionado='0.01')
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_purchase_pending_json_supplies_version_and_revision_uses_it(self):
        self.ativar(); compra, item = self.compra()
        compra.revisao_precos_pendente = True; compra.save(update_fields=['revisao_precos_pendente'])
        d = self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()['itens'][0]
        self.assertTrue(d['grupo_precos']['regularizado'])
        payload = {'compra_id': compra.pk, 'item_id': item.pk, 'produto_id': item.produto_id,
            'versao_grupo_produto_'+str(item.produto_id): d['grupo_precos']['versao'],
            'atualizar_preco_venda_produto_ids[]': [item.produto_id],
            'atualizar_preco_venda_nomes[]': ['preco_vista'], 'atualizar_preco_venda_valores[]': ['40']}
        payload.update(payload_todos_fixture(item.produto_id))
        response = self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(Produto.objects.values_list('preco_vista', flat=True)), {Decimal(40)})
        compra.refresh_from_db(); self.assertTrue(compra.alteracoes_precos_vinculados)

    def test_admin_and_old_price_conference_block_isolated_write(self):
        self.ativar(); before = self.snapshot()
        p = Produto.objects.get(pk=self.produtos[0].pk)
        form = ProdutoAdminForm(instance=p)
        form.cleaned_data = {'preco_vista': Decimal(99)}
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError): form.clean()
        response = self.client.post(reverse('estoque:conferencia_precos_antigo_salvar'),
            {'produto_id': p.pk, 'preco_compra': '10', 'preco_vista': '99', 'preco_prazo': '39'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('cadastro', response.json()['erro'])
        self.assertEqual(self.snapshot(), before)

    def test_entry_preview_confirmation_and_exit_keep_prices(self):
        self.ativar()
        novo = Produto.objects.create(nome='Novo sabor', unidade_compra='PCT', unidade_venda_2='UN',
            vende_fracionado=True, fator_conversao=6, preco_compra=10, preco_vista=40, preco_prazo=45,
            preco_vista_fracionado=8, preco_prazo_fracionado=9)
        payload = {'acao': 'previa_adicao', 'produto_ids': [novo.pk]}
        d = self.client.post(self.url, payload).json()['precos_vinculados']
        payload.update(acao='adicionar', versao_observada=d['versao_observada'])
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, payload).status_code, 400)
        self.assertEqual(self.snapshot(), before)
        payload['confirmar'] = '1'
        self.assertEqual(self.client.post(self.url, payload).status_code, 200)
        novo.refresh_from_db(); self.assertEqual(novo.preco_vista, 40)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'acao': 'remover', 'produto_id': novo.pk}).status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_editor_shows_members_and_hidden_version(self):
        self.ativar(); self.grupo.refresh_from_db()
        response = self.client.get(reverse('estoque:produto_editar', args=[self.produtos[0].pk]))
        self.assertContains(response, 'Atualizacao seletiva')
        self.assertContains(response, 'name="versao_precos_grupo"')
        self.assertContains(response, self.produtos[1].nome)

    def test_unidentified_operator_cannot_activate(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, self.comando()).status_code, 403)
        self.grupo.refresh_from_db(); self.assertFalse(self.grupo.precos_regularizados)

    def test_admin_http_and_home_reject_isolated_price_change(self):
        self.ativar(); before = self.snapshot()
        p = Produto.objects.get(pk=self.produtos[0].pk)
        self.operador.is_staff = True; self.operador.is_superuser = True; self.operador.save()
        data = {name: value if value is not None else '' for name, value in ProdutoAdminForm(instance=p).initial.items()}
        data.update(preco_vista='99', _save='Salvar')
        response = self.client.post(reverse('admin:estoque_produto_change', args=[p.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'alteração isolada de preço bloqueada')
        self.assertEqual(self.snapshot(), before)
        data = self.editor(); data.update(produto_id=p.pk, preco_vista='99')
        response = self.client.post(reverse('estoque:home'), data)
        self.assertRedirects(response, reverse('estoque:produto_editar', args=[p.pk]), fetch_redirect_response=False)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_purchase_version_is_clear_error_without_write(self):
        from .models import Compra
        compra, item = self.compra()
        compra.status = Compra.STATUS_RASCUNHO; compra.save(update_fields=['status'])
        before = self.snapshot()
        response = self.client.post(reverse('estoque:compra_editar', args=[compra.pk]),
            {'acao_compra': 'finalizar', 'versao_grupo_produto_'+str(item.produto_id): 'inválida'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_price_editor_cannot_overwrite_new_individual_cost_or_stock(self):
        self.ativar()
        for changes in ({'preco_compra': Decimal(11)}, {'quantidade': Decimal(25)}):
            payload = self.editor(); payload['preco_vista'] = '40'
            Produto.objects.filter(pk=self.produtos[0].pk).update(**changes)
            before = self.snapshot(); events = AlteracaoPrecoVinculado.objects.count()
            response = self.client.post(reverse('estoque:produto_editar', args=[self.produtos[0].pk]), payload)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(AlteracaoPrecoVinculado.objects.count(), events)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(), 'Chrome indisponível')
@override_settings(ROOT_URLCONF='estoque.tests_precos_vinculados')
class InterfacesPrecosBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        InterfacesPrecosVinculadosTests.setUp(self)

    def fluxo(self, width, selecionar_todos=True):
        Produto.objects.all().update(categoria='Bebidas')
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk), preco_prazo=42)
        individuais = [(p.pk, p.quantidade, p.preco_compra, p.codigo) for p in Produto.objects.order_by('pk')]
        pares_antes=list(Produto.objects.exclude(pk=self.produtos[2].pk).order_by('pk').values())
        with TemporaryDirectory(prefix='linked-prices-interface-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 860})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url+'/diagnostico-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("grupoProdutoModal")')
                tab.evaluate('document.querySelector(".grupo-produto-vinculo").click();true')
                tab.wait('document.getElementById("grupoProdutoModal").open && !document.getElementById("grupoProdutoPrecos").hidden')
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoRegularizar").disabled'))
                tab.wait('document.getElementById("grupoProdutoPrecosTabela").textContent.includes("42.00")')
                tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").click();document.getElementById("grupoProdutoRegularizar").click();true')
                tab.wait('document.getElementById("grupoProdutoSucesso").textContent.includes("ativado")')
                self.grupo.refresh_from_db(); self.assertTrue(self.grupo.precos_regularizados)
                self.assertEqual(set(Produto.objects.values_list('preco_prazo', flat=True)), {Decimal(39),Decimal(42)})
                tab.call('Page.navigate', {'url': self.live_server_url+reverse('estoque:produto_editar', args=[self.produtos[2].pk])})
                tab.wait('document.readyState === "complete" && !!document.getElementById("form-produto")')
                self.assertTrue(tab.evaluate('!!document.querySelector("[name=versao_precos_grupo]").value'))
                tab.evaluate('document.getElementById("id_preco_vista").value="40";document.getElementById("id_preco_vista").dispatchEvent(new Event("input",{bubbles:true}));true')
                # Native form submission exercises server validation and version.
                tab.evaluate('document.getElementById("form-produto").requestSubmit();true')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertFalse(tab.evaluate('Array.from(document.querySelectorAll("dialog[open] [data-destinatario]:not(:disabled)")).some(b=>b.checked)'))
                tab.evaluate('document.querySelectorAll("dialog[open] [data-destinatario]:not(:disabled)").forEach(b=>b.checked='+str(selecionar_todos).lower()+');Array.from(document.querySelectorAll("dialog[open] button")).find(b=>b.textContent.includes("Confirmar selecionados")).click();true')
                tab.wait('location.pathname === "/" && document.readyState === "complete"')
                self.assertEqual(set(Produto.objects.values_list('preco_vista', flat=True)), {Decimal(40)} if selecionar_todos else {Decimal(36),Decimal(40)})
                if not selecionar_todos:self.assertEqual(list(Produto.objects.exclude(pk=self.produtos[2].pk).order_by('pk').values()),pares_antes)
                self.assertEqual([(p.pk, p.quantidade, p.preco_compra, p.codigo) for p in Produto.objects.order_by('pk')], individuais)
            finally:
                chrome.stop()

    def test_desktop_activation_and_editor(self): self.fluxo(1280)
    def test_mobile_activation_and_editor(self): self.fluxo(390)
    def test_desktop_editor_unselected_members_preserved(self):self.fluxo(1280,False)
    def test_mobile_editor_unselected_members_preserved(self):self.fluxo(390,False)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(), 'Chrome indisponível')
@override_settings(ROOT_URLCONF=__name__)
class NovoGrupoPrecosBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        InterfacesPrecosVinculadosTests.setUp(self)
        self.client.post(self.url, {'acao': 'excluir', 'confirmar': '1'})

    def criar(self, width):
        individuais = [(p.pk, p.quantidade, p.preco_compra) for p in Produto.objects.order_by('pk')]
        with TemporaryDirectory(prefix='linked-new-group-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 860})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url+'/novo-grupo-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("grupoProdutoAtivar")')
                tab.evaluate('document.getElementById("grupoProdutoAtivar").click();document.querySelectorAll(".grupo-produto-checkbox").forEach(c=>c.click());document.getElementById("grupoProdutoCriar").click();true')
                tab.wait('document.getElementById("grupoProdutoModal").open && !document.getElementById("grupoProdutoPrecos").hidden')
                self.assertFalse(GrupoProdutoVinculado.objects.exists())
                tab.wait('document.getElementById("grupoProdutoPrecosTabela").textContent.includes("36.00")')
                tab.evaluate('document.getElementById("grupoProdutoPrecosConfirmacao").click();document.getElementById("grupoProdutoSalvar").click();true')
                tab.wait('document.getElementById("grupoProdutoMensagemPagina").textContent.includes("vinculados com sucesso")')
                self.assertTrue(GrupoProdutoVinculado.objects.get().precos_regularizados)
                self.assertEqual([(p.pk, p.quantidade, p.preco_compra) for p in Produto.objects.order_by('pk')], individuais)
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(), 1)
            finally:
                chrome.stop()

    def test_desktop_new_group_requires_preview(self): self.criar(1280)
    def test_mobile_new_group_requires_preview(self): self.criar(390)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(), 'Chrome indisponível')
class RevisaoCompraVinculadaBrowserTests(compra_tests.CompraPreRevisaoFixture, StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        self.par = Produto.objects.create(nome='Outro integrante', quantidade=50, preco_compra=6,
            preco_vista=15, preco_prazo=16, unidade_compra='UN')
        self.grupo = criar_grupo([self.produto.pk, self.par.pk], 'Grupo da compra')
        regularizar_grupo(self.grupo.pk, diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],
            confirmar=True, operador=self.user)

    def revisao(self, mobile, selecionar_ausente=True):
        with TemporaryDirectory(prefix='linked-purchase-review-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width': 390 if mobile else 1280, 'height': 844, 'deviceScaleFactor': 1, 'mobile': mobile})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url+self.editar_url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("compraQuantidadeBoletos")')
                tab.evaluate("""document.getElementById('tipoPagamentoCompra').value='aprazo';
                    document.getElementById('tipoPagamentoCompra').dispatchEvent(new Event('change'));
                    document.getElementById('compraFormaCobrancaNota').value='varios_boletos';
                    document.getElementById('compraQuantidadeBoletos').value='3';
                    document.getElementById('compraFormaCobrancaNota').dispatchEvent(new Event('change'));
                    for(let n=1;n<=3;n++) {
                      document.querySelector('[name="compra_parcela_valor_'+n+'"]').value='30,00';
                      document.querySelector('[name="compra_parcela_vencimento_'+n+'"]').value='2026-11-'+String(8+(n-1)*10).padStart(2,'0');
                    }
                    document.getElementById('btnSalvarCompra').click();true""")
                tab.wait('document.getElementById("modalConferenciaPrecosCompra").classList.contains("visivel")')
                if mobile:
                    tab.evaluate('document.getElementById("btnRevisarPrecosAgoraCompra").click();true')
                tab.wait('!!document.querySelector("#modalConferenciaPrecosCompra .campoNovoPrecoVendaCompra")')
                self.assertIn('seletivo', tab.evaluate('document.getElementById("modalConferenciaPrecosCompra").textContent'))
                tab.evaluate("""const simulador=document.querySelector('#modalConferenciaPrecosCompra .compras-preco-simulador[data-campo-preco="preco_vista"]');
                    const campo=simulador.querySelector('.campoNovoPrecoVendaCompra');campo.value='20,00';
                    campo.dispatchEvent(new Event('input',{bubbles:true}));
                    simulador.querySelector('.marcarAtualizarVendaCompra').checked=true;
                    document.getElementById('btnAtualizarPrecosContinuarCompra').click();true""")
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertFalse(tab.evaluate('document.querySelector("dialog[open] [data-destinatario]:not(:disabled)").checked'))
                tab.evaluate('document.querySelector("dialog[open] [data-destinatario]:not(:disabled)").checked='+str(selecionar_ausente).lower()+';Array.from(document.querySelectorAll("dialog[open] button")).find(b=>b.textContent.includes("Confirmar selecionados")).click();true')
                tab.wait('location.pathname === "/compras/" && document.readyState === "complete"')
                self.produto.refresh_from_db(); self.par.refresh_from_db(); self.compra.refresh_from_db()
                self.assertEqual(self.produto.preco_vista, 20)
                self.assertEqual(self.par.preco_vista, 20 if selecionar_ausente else 15)
                self.assertEqual(self.par.precos_canonicos_adotados,selecionar_ausente)
                self.assertEqual(self.produto.quantidade, 19)
                self.assertEqual(self.par.quantidade, 50)
                self.assertEqual(self.produto.preco_compra, 10)
                self.assertEqual(self.par.preco_compra, 6)
                self.assertTrue(self.compra.alteracoes_precos_vinculados)
                self.assertEqual(len(self.compra.alteracoes_precos_vinculados), 1)
            finally:
                chrome.stop()

    def test_desktop_review_propagates_with_purchase_evidence(self): self.revisao(False)
    def test_mobile_review_propagates_with_purchase_evidence(self): self.revisao(True)
    def test_desktop_absent_unchecked_remains_unchanged(self):self.revisao(False,False)
    def test_mobile_absent_unchecked_remains_unchanged(self):self.revisao(True,False)
