"""Final blockers: legacy consumers and authorized human price commands."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import Chrome
from . import tests_precos_vinculados_servico as core
from .admin import ProdutoAdminForm
from .models import Produto, Compra, ItemCompra, GrupoProdutoVinculado, AlteracaoPrecoVinculado, Pedido, ItemPedido
from .services.precos_vinculados import alterar_precos_grupo


class BloqueiosPrecosTests(TestCase):
    setUp = core.ServicoPrecosVinculadosTests.setUp
    ativar = core.ServicoPrecosVinculadosTests.ativar
    compra = core.ServicoPrecosVinculadosTests.compra

    def estado(self):
        return [list(model.objects.order_by('pk').values()) for model in
            (Produto, Compra, ItemCompra, GrupoProdutoVinculado, AlteracaoPrecoVinculado, Pedido, ItemPedido)]

    def revisao(self):
        self.ativar(); compra, item = self.compra()
        compra.revisao_precos_pendente = True; compra.save(update_fields=['revisao_precos_pendente'])
        self.grupo.refresh_from_db()
        dados = {'compra_id': compra.pk, 'item_id': item.pk, 'produto_id': item.produto_id,
            'versao_grupo_produto_'+str(item.produto_id): self.grupo.versao_precos,
            'atualizar_preco_venda_produto_ids[]': [item.produto_id],
            'atualizar_preco_venda_nomes[]': ['preco_vista'], 'atualizar_preco_venda_valores[]': ['40']}
        return compra, dados

    def test_legacy_save_update_bulk_and_admin_refused_without_partial_write(self):
        self.ativar(); before = self.estado()
        for field in ('preco_venda_1', 'preco_venda_2'):
            with self.assertRaises(ValidationError): Produto.objects.filter(pk=self.produtos[0].pk).update(**{field: Decimal(99)})
            p = Produto.objects.get(pk=self.produtos[0].pk); setattr(p, field, Decimal(99))
            with self.assertRaises(ValidationError): p.save(update_fields=[field])
            with self.assertRaises(ValidationError), transaction.atomic(): Produto.objects.bulk_update([p], [field])
            self.assertEqual(self.estado(), before)
        p = Produto.objects.get(pk=self.produtos[0].pk)
        form = ProdutoAdminForm(instance=p); form.cleaned_data = {'preco_venda_1': Decimal(99)}
        with self.assertRaises(ValidationError): form.clean()
        self.assertEqual(self.estado(), before)

    def test_unlinked_legacy_prices_remain_editable(self):
        p = Produto.objects.create(nome='Unlinked legacy', preco_compra=10, preco_vista=36, preco_prazo=39,
            unidade_compra='PCT', unidade_venda_2='UN', vende_fracionado=True, fator_conversao=6)
        p.preco_venda_1 = 88; p.save(update_fields=['preco_venda_1'])
        Produto.objects.filter(pk=p.pk).update(preco_venda_2=9)
        p.refresh_from_db(); self.assertEqual(p.preco_venda_1, 88); self.assertEqual(p.preco_venda_2, 9)

    def test_anonymous_and_unpermitted_revision_preserve_all_data(self):
        _, data = self.revisao(); before = self.estado()
        self.client.logout()
        self.assertEqual(self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), data).status_code, 403)
        self.assertEqual(self.estado(), before)
        viewer = get_user_model().objects.create_user(username='no-price-permission')
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), data).status_code, 403)
        self.assertEqual(self.estado(), before)

    def test_authorized_revision_records_human_actor(self):
        _, data = self.revisao()
        self.assertEqual(self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), data).status_code, 200)
        event = AlteracaoPrecoVinculado.objects.get(origem='compra')
        self.assertEqual(event.operador_id, self.operador.pk)
        self.assertEqual(event.evidencia['operador_snapshot']['id'], self.operador.pk)
        self.assertEqual(set(Produto.objects.values_list('preco_vista', flat=True)), {Decimal(40)})

    def test_equivalent_purchase_commands_rejected_before_any_writes(self):
        compra, data = self.revisao(); before = self.estado(); self.client.logout()
        urls = [reverse('estoque:compras_nova'), reverse('estoque:compra_editar', args=[compra.pk]),
            reverse('estoque:compra_finalizar', args=[compra.pk]), reverse('estoque:compra_salvar_pre_revisao', args=[compra.pk])]
        for url in urls:
            self.assertEqual(self.client.post(url, data).status_code, 403)
            self.assertEqual(self.estado(), before)

    def test_group_mutations_reject_unpermitted_and_anonymous(self):
        before = self.estado()
        user = get_user_model().objects.create_user(username='read-only-groups')
        self.client.force_login(user)
        for data in ({'acao': 'regularizar', 'referencia': self.produtos[0].pk, 'confirmar': '1'},
            {'acao': 'remover', 'produto_id': self.produtos[0].pk}, {'acao': 'excluir', 'confirmar': '1'},
            {'acao': 'renomear', 'nome': 'Not allowed'}):
            self.assertEqual(self.client.post(self.url, data).status_code, 403)
            self.assertEqual(self.estado(), before)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'acao': 'excluir', 'confirmar': '1'}).status_code, 403)
        self.assertEqual(self.estado(), before)

    def test_service_cannot_emit_actorless_price_audit(self):
        self.ativar(); self.grupo.refresh_from_db(); before = self.estado()
        with self.assertRaises(PermissionDenied):
            alterar_precos_grupo(self.produtos[0].pk, {'preco_vista': 40}, versao_esperada=self.grupo.versao_precos)
        self.assertEqual(self.estado(), before)

    def test_purchase_restore_requires_price_permission(self):
        compra, data = self.revisao()
        self.client.post(reverse('estoque:revisao_precos_posterior_salvar'), data)
        before = self.estado(); self.client.logout()
        self.assertEqual(self.client.post(reverse('estoque:compra_excluir', args=[compra.pk])).status_code, 403)
        self.assertEqual(self.estado(), before)

    def test_historical_pedido_is_not_repriced(self):
        self.ativar()
        pedido = Pedido.objects.create(data_pedido=timezone.localdate(), total=99)
        item = ItemPedido.objects.create(pedido=pedido, produto=self.produtos[0], quantidade=1, unidade='PCT', preco_unitario=99, valor_total=99)
        before = (Pedido.objects.values().get(pk=pedido.pk), ItemPedido.objects.values().get(pk=item.pk))
        self.grupo.refresh_from_db()
        alterar_precos_grupo(self.produtos[1].pk, {'preco_vista': 40}, versao_esperada=self.grupo.versao_precos, operador=self.operador)
        self.assertEqual((Pedido.objects.values().get(pk=pedido.pk), ItemPedido.objects.values().get(pk=item.pk)), before)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(), 'Chrome unavailable')
class PedidosVinculadosBrowserTests(StaticLiveServerTestCase):
    setUp = core.ServicoPrecosVinculadosTests.setUp
    ativar = core.ServicoPrecosVinculadosTests.ativar

    def pedido(self, width):
        # Legacy data is established before activation and must not be erased.
        Produto.objects.filter(pk=self.produtos[0].pk).update(preco_venda_1=99, preco_venda_2=9)
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_venda_1=88, preco_venda_2=8)
        free = Produto.objects.create(nome='Free legacy', unidade_compra='PCT', unidade_venda_2='UN',
            vende_fracionado=True, fator_conversao=6, preco_compra=10, preco_vista=36, preco_prazo=39,
            preco_venda_1=77, preco_venda_2=11)
        self.ativar(); self.grupo.refresh_from_db()
        alterar_precos_grupo(self.produtos[1].pk, {'preco_vista': 40, 'preco_vista_fracionado': 8},
            versao_esperada=self.grupo.versao_precos, operador=self.operador)
        with TemporaryDirectory(prefix='linked-pedido-', ignore_cleanup_errors=True) as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 1, 'mobile': width < 860})
                tab.call('Network.setCookie', {'name': 'sessionid', 'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Page.navigate', {'url': self.live_server_url+reverse('estoque:pedido_criar')})
                tab.wait('document.readyState === "complete" && !!document.getElementById("produto-busca")')
                for p in self.produtos:
                    self.assertEqual(tab.evaluate('produtos.find(p=>p.id==="'+str(p.pk)+'").unidades.map(u=>u.preco)'), [40, 8])
                self.assertEqual(tab.evaluate('produtos.find(p=>p.id==="'+str(free.pk)+'").unidades.map(u=>u.preco)'), [77, 11])
                tab.evaluate('preencherProduto(produtos.find(p=>p.id==="'+str(self.produtos[0].pk)+'"));true')
                self.assertEqual(tab.evaluate('document.getElementById("item-preco").value').replace(',', '.'), '40.00')
                self.assertEqual(Produto.objects.get(pk=self.produtos[0].pk).preco_venda_1, 99)
            finally: chrome.stop()

    def test_desktop_pedido_prices_are_canonical(self): self.pedido(1280)
    def test_mobile_pedido_prices_are_canonical(self): self.pedido(390)
