"""Selective commands against isolated databases and real HTTP consumers."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.http import HttpResponse
from django.template.loader import get_template
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse, path, include
from django.utils import timezone

from offline.browser_support import Chrome
from .models import Produto, Compra, ItemCompra, Venda, ItemVenda, Pedido, ItemPedido, AlteracaoPrecoVinculado, Categoria, Unidade
from .tests_precos_vinculados import PreviaPrecosVinculadosTests, criar_operador_precos
from .tests_precos_vinculados_interfaces import InterfacesPrecosVinculadosTests
from .services.precos_vinculados import (
    diagnosticar_grupo, regularizar_grupo, alterar_precos_grupo,
    restaurar_evento_grupo, adicionar_produtos_regularizados,
)
from .services.precos_compra import aplicar_precos_compra, restaurar_precos_da_compra
from .grupos_produtos import remover_membro


def pagina_revisao(request):
    return HttpResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'+
        get_template('estoque/includes/revisao_precos_posterior.html').render(
            {'revisao_precos_pendentes_qtd':2,'revisao_precos_compras_qtd':1},request=request))


urlpatterns=[path('seletivos-revisao-teste/',pagina_revisao),path('',include('sistema.urls'))]


class SeletivosFixture:
    def setUp(self):
        PreviaPrecosVinculadosTests.setUp(self)
        self.operador = criar_operador_precos('selective-test')
        self.client.force_login(self.operador)
        for index, produto in enumerate(self.produtos):
            models.QuerySet.update(Produto.objects.filter(pk=produto.pk),
                preco_venda_1=Decimal(70+index), preco_venda_2=Decimal(10+index),
                preco_prazo=Decimal(39+index))

    def estado(self):
        return list(Produto.objects.order_by('pk').values())

    def comando(self, ids=None, fonte=0):
        self.grupo.refresh_from_db()
        return dict(versao_esperada=self.grupo.versao_precos, operador=self.operador,
            selecionados=ids if ids is not None else [self.produtos[fonte].pk], confirmar=True,
            assinatura_esperada=diagnosticar_grupo(list(self.grupo.produtos.all()), validar_precos=False)['assinatura_precos'])

    def alterar(self, precos=None, ids=None, fonte=0):
        return alterar_precos_grupo(self.produtos[fonte].pk, precos or {'preco_vista':Decimal(40)}, **self.comando(ids, fonte))

    def compra(self, indices=(0,)):
        compra=Compra.objects.create(data_compra=timezone.localdate(),status=Compra.STATUS_FINALIZADA,revisao_precos_pendente=True)
        itens=[ItemCompra.objects.create(compra=compra,produto=self.produtos[i],quantidade=1,
            unidade='PCT',preco_unitario=10+i,valor_total=10+i,preco_compra_anterior=9+i) for i in indices]
        return compra,itens

    def ativar(self):
        return regularizar_grupo(self.grupo.pk,diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],confirmar=True,operador=self.operador)

    def payload(self, compra, item, ids=None):
        comando=self.comando(ids)
        pk=str(item.produto_id)
        return {'compra_id':compra.pk,'item_id':item.pk,'produto_id':pk,
            'versao_grupo_produto_'+pk:comando['versao_esperada'],
            'assinatura_precos_produto_'+pk:comando['assinatura_esperada'],
            'destinatarios_precos_produto_'+pk:comando['selecionados'],
            'confirmar_precos_produto_'+pk:'1',
            'atualizar_preco_venda_produto_ids[]':[pk],
            'atualizar_preco_venda_nomes[]':['preco_vista'],
            'atualizar_preco_venda_valores[]':['40.00']}


class PrecosSeletivosTests(SeletivosFixture, TestCase):
    def test_activation_preserves_every_product_even_with_divergence(self):
        antes=self.estado();self.ativar()
        self.assertEqual(self.estado(),antes)
        self.grupo.refresh_from_db();self.assertTrue(self.grupo.precos_regularizados)

    def test_first_command_needs_no_reference_and_preserves_absent_entire_row(self):
        antes=self.estado();evento=self.alterar()
        depois=self.estado();self.assertEqual(depois[1:],antes[1:])
        self.assertTrue(depois[0]['precos_canonicos_adotados'])
        self.assertEqual(depois[0]['preco_venda_1'],antes[0]['preco_venda_1'])
        self.assertEqual(evento.evidencia['selecionados'],[self.produtos[0].pk])
        self.assertEqual(evento.operador,self.operador)

    def test_four_fields_any_initiator_raise_and_reduce_only_selected_fields(self):
        for campo in ('preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'):
            for fonte in range(3):
                for delta in (Decimal(1),Decimal(-1)):
                    antes=self.estado();novo=antes[fonte][campo]+delta
                    self.alterar({campo:novo},fonte=fonte)
                    depois=self.estado()
                    for i in range(3):
                        if i!=fonte:self.assertEqual(depois[i],antes[i])
                    for outro in {'preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'}-{campo}:
                        self.assertEqual(depois[fonte][outro],antes[fonte][outro])

    def test_purchase_all_members_one_snapshot(self):
        compra,itens=self.compra((0,1,2));antes=self.estado()
        aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando([p.pk for p in self.produtos]))
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(40)})
        compra.refresh_from_db();self.assertEqual(len(compra.alteracoes_precos_vinculados),1)
        restaurar_precos_da_compra(compra,operador=self.operador)
        for novo,velho in zip(self.estado(),antes):
            for campo in ('preco_vista','preco_venda','precos_canonicos_adotados','autoria_precos','preco_compra','quantidade'):
                self.assertEqual(novo[campo],velho[campo])

    def test_purchase_some_members_absent_not_selected_unchanged(self):
        _,itens=self.compra((0,1));antes=self.estado()
        aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando([p.pk for p in self.produtos[:2]]))
        self.assertEqual(self.estado()[2],antes[2])
        self.assertEqual(list(Produto.objects.order_by('pk').values_list('preco_vista',flat=True)),[40,40,36])

    def test_single_purchase_with_optional_absent_selected(self):
        _,itens=self.compra();antes=self.estado()
        aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando([self.produtos[0].pk,self.produtos[2].pk]))
        self.assertEqual(self.estado()[1],antes[1])
        self.assertEqual(list(Produto.objects.order_by('pk').values_list('preco_vista',flat=True)),[40,36,40])

    def test_missing_copurchased_member_rejected_atomically(self):
        _,itens=self.compra((0,1));antes=self.estado()
        with self.assertRaises(ValueError):aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando())
        self.assertEqual(self.estado(),antes)
        self.assertFalse(AlteracaoPrecoVinculado.objects.exists())

    def test_costs_stock_and_margins_independent(self):
        Produto.objects.filter(pk=self.produtos[0].pk).update(preco_compra=20,preco_compra_fracionado=2)
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_compra=25,preco_compra_fracionado=3)
        antes=self.estado();self.alterar({'preco_vista':30,'preco_vista_fracionado':9},ids=[p.pk for p in self.produtos[:2]])
        depois=self.estado()
        self.assertEqual([p['preco_compra'] for p in depois],[20,25,12])
        self.assertEqual([p['quantidade'] for p in depois],[p['quantidade'] for p in antes])
        self.assertEqual([p['percentual_vista_fracionado'] for p in depois[:2]],[350,200])

    def test_loss_and_fractional_loss_block_entire_command(self):
        for valores in ({'preco_vista':1},{'preco_vista_fracionado':Decimal('.01')}):
            antes=self.estado()
            with self.assertRaises(ValidationError):self.alterar(valores,ids=[p.pk for p in self.produtos])
            self.assertEqual(self.estado(),antes)
            self.assertFalse(AlteracaoPrecoVinculado.objects.exists())

    def test_failure_after_first_write_rolls_back_prices_adoption_and_audit(self):
        original=models.QuerySet.update;chamadas=0;antes=self.estado()
        def falhar(qs,**valores):
            nonlocal chamadas
            if qs.model is Produto and 'preco_vista' in valores:
                chamadas+=1
                if chamadas==2:raise RuntimeError('isolated failure')
            return original(qs,**valores)
        with patch.object(models.QuerySet,'update',falhar),self.assertRaises(RuntimeError):
            self.alterar(ids=[p.pk for p in self.produtos])
        self.assertEqual(self.estado(),antes);self.assertFalse(AlteracaoPrecoVinculado.objects.exists())

    def test_stale_version_and_legacy_preview_are_rejected(self):
        stale=self.comando();self.alterar()
        antes=self.estado()
        with self.assertRaises(ValidationError):alterar_precos_grupo(self.produtos[0].pk,{'preco_vista':41},**stale)
        self.assertEqual(self.estado(),antes)

    def test_pending_legacy_change_invalidates_preview(self):
        stale=self.comando();Produto.objects.filter(pk=self.produtos[1].pk).update(preco_venda_1=85)
        antes=self.estado()
        with self.assertRaises(ValidationError):alterar_precos_grupo(self.produtos[0].pk,{'preco_vista':40},**stale)
        self.assertEqual(self.estado(),antes)

    def test_later_disjoint_update_blocks_purchase_restore_conservatively(self):
        compra,itens=self.compra();aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando())
        self.alterar({'preco_vista':41},fonte=1);compra.refresh_from_db();antes=self.estado()
        with self.assertRaises(ValueError):restaurar_precos_da_compra(compra,operador=self.operador)
        self.assertEqual(self.estado(),antes)

    def test_restore_reverts_only_selected_adoption_with_divergent_peers(self):
        evento=self.alterar();ausentes=self.estado()[1:]
        restaurar_evento_grupo(evento.operation_id,operador=self.operador)
        self.assertEqual(self.estado()[1:],ausentes)
        p=Produto.objects.get(pk=self.produtos[0].pk)
        self.assertFalse(p.precos_canonicos_adotados);self.assertEqual(p.preco_venda_1,70)
        self.assertIsNone(restaurar_evento_grupo(evento.operation_id,operador=self.operador))

    def test_inactive_selected_only_by_explicit_confirmation(self):
        Produto.objects.filter(pk=self.produtos[2].pk).update(ativo=False);antes=self.estado()[2]
        self.alterar();self.assertEqual(self.estado()[2],antes)
        self.alterar({'preco_vista':41},ids=[p.pk for p in self.produtos])
        p=Produto.objects.get(pk=self.produtos[2].pk);self.assertFalse(p.ativo);self.assertTrue(p.precos_canonicos_adotados)

    def test_new_member_retains_prices_and_adoption_and_exit_retains_adoption(self):
        self.ativar()
        novo=Produto.objects.create(nome='Novo seletivo',unidade_compra='PCT',unidade_venda_2='UN',vende_fracionado=True,
            fator_conversao=6,preco_compra=10,preco_vista=50,preco_prazo=55,preco_vista_fracionado=9,preco_prazo_fracionado=10,preco_venda_1=80)
        antes=Produto.objects.values().get(pk=novo.pk)
        token=diagnosticar_grupo([*self.grupo.produtos.all(),novo])['versao_observada']
        adicionar_produtos_regularizados(self.grupo.pk,[novo.pk],token,confirmar=True,operador=self.operador)
        self.assertEqual(Produto.objects.values().get(pk=novo.pk),antes)
        self.alterar();remover_membro(self.grupo.pk,self.produtos[0].pk)
        self.assertTrue(Produto.objects.get(pk=self.produtos[0].pk).precos_canonicos_adotados)

    def test_no_consent_or_invalid_target_rejected(self):
        for kwargs in ({'confirmar':False},{'selecionados':[]},{'selecionados':[999999]}):
            comando={**self.comando(),**kwargs};antes=self.estado()
            with self.assertRaises(ValidationError):alterar_precos_grupo(self.produtos[0].pk,{'preco_vista':40},**comando)
            self.assertEqual(self.estado(),antes)

    def test_adoption_and_legacy_cannot_be_changed_through_alternate_writers(self):
        self.alterar();antes=self.estado()
        for valores in ({'precos_canonicos_adotados':False},{'preco_venda_1':90},{'preco_vista':41}):
            with self.assertRaises(ValidationError),transaction.atomic():Produto.objects.filter(pk=self.produtos[0].pk).update(**valores)
            self.assertEqual(self.estado(),antes)

    def test_historical_sales_and_orders_untouched(self):
        venda=Venda.objects.create(data_venda=timezone.localdate(),total=36)
        ItemVenda.objects.create(venda=venda,produto=self.produtos[0],quantidade=1,unidade='PCT',preco_unitario=36,valor_total=36)
        pedido=Pedido.objects.create(data_pedido=timezone.localdate(),total=70)
        ItemPedido.objects.create(pedido=pedido,produto=self.produtos[0],quantidade=1,unidade='PCT',preco_unitario=70,valor_total=70)
        antes=(list(Venda.objects.values()),list(ItemVenda.objects.values()),list(Pedido.objects.values()),list(ItemPedido.objects.values()))
        self.alterar()
        self.assertEqual((list(Venda.objects.values()),list(ItemVenda.objects.values()),list(Pedido.objects.values()),list(ItemPedido.objects.values())),antes)

    def test_posterior_http_deduplicates_copurchased_and_finishes_all(self):
        compra,itens=self.compra((0,1));antes=self.estado()[2]
        itens_json=self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()['itens']
        self.assertEqual(len(itens_json),1)
        self.assertEqual(set(itens_json[0]['grupo_precos']['comprados']),{p.pk for p in self.produtos[:2]})
        dados=self.payload(compra,itens[0],[p.pk for p in self.produtos[:2]])
        resposta=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),dados)
        self.assertEqual(resposta.status_code,200,resposta.content)
        self.assertFalse(compra.itens.filter(revisao_preco_concluida=False).exists())
        self.assertEqual(self.estado()[2],antes)

    def test_posterior_auth_and_missing_consent_preserve_data(self):
        compra,itens=self.compra();dados=self.payload(compra,itens[0]);antes=self.estado()
        dados.pop('confirmar_precos_produto_'+str(itens[0].produto_id))
        self.assertEqual(self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),dados).status_code,400)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),dados).status_code,403)
        self.assertEqual(self.estado(),antes)

    def test_prior_audit_format_remains_restorable_without_rewriting_history(self):
        models.QuerySet.update(Produto.objects.all(),precos_canonicos_adotados=True)
        evento=self.alterar(ids=[p.pk for p in self.produtos])
        evidencia=dict(evento.evidencia);evidencia.pop('adocao');evidencia.pop('formato')
        AlteracaoPrecoVinculado.objects.filter(pk=evento.pk).update(evidencia=evidencia)
        historico=AlteracaoPrecoVinculado.objects.values().get(pk=evento.pk)
        restaurar_evento_grupo(evento.operation_id,operador=self.operador)
        self.assertEqual(AlteracaoPrecoVinculado.objects.values().get(pk=evento.pk),historico)
        self.assertEqual(Produto.objects.filter(precos_canonicos_adotados=True).count(),3)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(36)})

    def test_purchase_exclusion_restores_adoption_and_absent_effective_price(self):
        compra,itens=self.compra();antes=self.estado()
        aplicar_precos_compra(itens[0],{'preco_vista':40},**self.comando())
        resposta=self.client.post(reverse('estoque:compra_excluir',args=[compra.pk]))
        self.assertEqual(resposta.status_code,302)
        self.assertFalse(Compra.objects.filter(pk=compra.pk).exists())
        for atual,anterior in zip(self.estado(),antes):
            for campo in ('preco_vista','preco_venda','preco_venda_1','preco_venda_2','precos_canonicos_adotados','preco_compra','quantidade'):
                self.assertEqual(atual[campo],anterior[campo])

    def test_editor_pending_group_only_explicit_optional_selection(self):
        Categoria.objects.get_or_create(nome='Bebidas')
        for sigla in ('PCT','UN'):Unidade.objects.get_or_create(sigla=sigla,defaults={'nome':sigla})
        antes=self.estado()
        dados=InterfacesPrecosVinculadosTests.editor(self)
        dados.update(preco_vista=40,destinatarios_precos=[])
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[0].pk]),dados)
        self.assertEqual(resposta.status_code,302)
        self.assertEqual(self.estado()[1:],antes[1:])
        self.assertTrue(Produto.objects.get(pk=self.produtos[0].pk).precos_canonicos_adotados)

    def test_editor_refusal_preserves_persisted_prices_for_next_confirmation(self):
        Categoria.objects.get_or_create(nome='Bebidas')
        for sigla in ('PCT','UN'):Unidade.objects.get_or_create(sigla=sigla,defaults={'nome':sigla})
        dados=InterfacesPrecosVinculadosTests.editor(self)
        dados.update(preco_vista=40,confirmar_precos_seletivos=False)
        antes=self.estado()
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[0].pk]),dados)
        self.assertEqual(resposta.status_code,200)
        self.assertEqual(resposta.context['form'].precos_observados['preco_vista'],Decimal(36))
        self.assertEqual(resposta.context['form'].instance.preco_vista,Decimal(40))
        self.assertEqual(self.estado(),antes)

    def test_purchase_batch_uses_actual_initiator_not_an_unchanged_member(self):
        from . import views
        compra,_=self.compra((0,1))
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk),preco_vista=37,preco_venda=37)
        source=self.produtos[1].pk;comando=self.comando([p.pk for p in self.produtos[:2]],fonte=1)
        valores=views._PrecosCompraRevisao({(self.produtos[0].pk,'preco_vista'):Decimal(36),(source,'preco_vista'):Decimal(36)})
        valores.operador=self.operador;valores.versoes_grupos={p.pk:comando['versao_esperada'] for p in self.produtos}
        valores.selecionados={source:comando['selecionados']};valores.confirmados={source};valores.assinaturas={source:comando['assinatura_esperada']}
        ausente=self.estado()[2];views._atualizar_precos_venda_produtos_compra(valores,compra)
        self.assertEqual(AlteracaoPrecoVinculado.objects.get().evidencia['produto_id'],source)
        self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[:2]],precos_canonicos_adotados=True).count(),2)
        self.assertEqual(self.estado()[2],ausente)

    def test_ambiguous_multiple_initiators_rejected_instead_of_partial_propagation(self):
        from . import views
        compra,_=self.compra((0,1))
        valores=views._PrecosCompraRevisao({(self.produtos[0].pk,'preco_vista'):Decimal(40),(self.produtos[1].pk,'preco_prazo'):Decimal(39)})
        valores.operador=self.operador;valores.versoes_grupos={p.pk:0 for p in self.produtos}
        antes=self.estado()
        with self.assertRaises(ValueError):views._atualizar_precos_venda_produtos_compra(valores,compra)
        self.assertEqual(self.estado(),antes);self.assertFalse(AlteracaoPrecoVinculado.objects.exists())


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL')
class PrecosSeletivosConcorrenciaTests(SeletivosFixture, TransactionTestCase):
    def test_disjoint_selections_same_version_one_winner(self):
        comandos=[self.comando(fonte=i) for i in (0,1)];barreira=Barrier(2)
        def executar(i):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                try:alterar_precos_grupo(self.produtos[i].pk,{'preco_vista':40+i},**comandos[i]);return True
                except ValidationError:return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:resultados=list(pool.map(executar,(0,1)))
        self.assertEqual(sum(resultados),1)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
        self.assertEqual(Produto.objects.filter(precos_canonicos_adotados=True).count(),1)

    def test_purchase_and_editor_same_version_one_winner(self):
        _,itens=self.compra((0,1));comando=self.comando([p.pk for p in self.produtos[:2]])
        outro=self.comando(fonte=2);barreira=Barrier(2)
        def executar(i):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                try:
                    if i==0:aplicar_precos_compra(ItemCompra.objects.get(pk=itens[0].pk),{'preco_vista':40},**comando)
                    else:alterar_precos_grupo(self.produtos[2].pk,{'preco_vista':41},**outro)
                    return True
                except (ValidationError,ValueError):return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:resultados=list(pool.map(executar,(0,1)))
        self.assertEqual(sum(resultados),1);self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
        self.assertEqual(Produto.objects.filter(precos_canonicos_adotados=True).count(),2 if resultados[0] else 1)

    def test_restore_and_editor_cannot_overwrite_each_other(self):
        evento=self.alterar();comando=self.comando(fonte=1);barreira=Barrier(2)
        def executar(i):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                try:
                    if i==0:restaurar_evento_grupo(evento.operation_id,operador=self.operador)
                    else:alterar_precos_grupo(self.produtos[1].pk,{'preco_vista':41},**comando)
                    return True
                except ValidationError:return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:resultados=list(pool.map(executar,(0,1)))
        self.assertEqual(sum(resultados),1)
        self.assertEqual(Produto.objects.filter(precos_canonicos_adotados=True).count(),0 if resultados[0] else 2)


class AdocaoMigrationTests(TransactionTestCase):
    def test_migration_preserves_effective_source_and_all_prices(self):
        from django.conf import settings
        self.assertEqual(settings.SETTINGS_MODULE,'offline.test_settings')
        anterior=[('estoque','0131_precos_vinculados_auditoria')]
        atual=[('estoque','0132_adocao_precos_por_produto')]
        executor=MigrationExecutor(connection)
        executor.migrate(anterior)
        try:
            apps=executor.loader.project_state(anterior).apps
            P=apps.get_model('estoque','Produto');G=apps.get_model('estoque','GrupoProdutoVinculado');M=apps.get_model('estoque','MembroGrupoProduto')
            for i,ativo in enumerate((True,False)):
                grupo=G.objects.create(nome='Migration fixture '+str(i),precos_regularizados=ativo)
                produto=P.objects.create(nome='Migration product '+str(i),preco_compra=10,preco_vista=36,
                    preco_prazo=39,preco_venda=36,preco_venda_1=80+i,preco_venda_2=9+i)
                M.objects.create(grupo=grupo,produto=produto)
            campos=('id','preco_compra','preco_vista','preco_prazo','preco_venda','preco_venda_1','preco_venda_2','quantidade')
            antes=list(P.objects.order_by('pk').values(*campos))
            executor=MigrationExecutor(connection);executor.migrate(atual)
            depois=list(Produto.objects.order_by('pk').values(*campos))
            self.assertEqual(depois,antes)
            self.assertEqual(list(Produto.objects.order_by('pk').values_list('precos_canonicos_adotados',flat=True)),[True,False])
        finally:MigrationExecutor(connection).migrate(atual)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
class PrecosSeletivosBrowserTests(SeletivosFixture, StaticLiveServerTestCase):
    def pedido(self,width):
        with TemporaryDirectory(prefix='selective-browser-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                def abrir():
                    tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:pedido_criar')})
                    tab.wait('document.readyState === "complete" && typeof produtos !== "undefined"')
                    return tab.evaluate('produtos.filter(p=>'+str([str(p.pk) for p in self.produtos]).replace("'",'"')+'.includes(p.id)).map(p=>({id:p.id,precos:p.unidades.map(u=>u.preco)}))')
                antes=abrir();self.ativar();self.assertEqual(abrir(),antes)
                ausente=Produto.objects.values().get(pk=self.produtos[2].pk)
                self.alterar(ids=[p.pk for p in self.produtos[:2]])
                depois=abrir();precos={p['id']:p['precos'] for p in depois}
                self.assertEqual(precos[str(self.produtos[2].pk)],[72,12])
                self.assertEqual(precos[str(self.produtos[0].pk)],[40,7])
                self.assertEqual(Produto.objects.values().get(pk=self.produtos[2].pk),ausente)
            finally:chrome.stop()

    def test_pedido_desktop_preserves_unselected_effective_prices(self):self.pedido(1280)
    def test_pedido_mobile_preserves_unselected_effective_prices(self):self.pedido(390)


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
@override_settings(ROOT_URLCONF=__name__)
class RevisaoSeletivaPosteriorBrowserTests(SeletivosFixture, StaticLiveServerTestCase):
    def revisao(self,width,selecionar):
        compra,itens=self.compra((0,1));antes=Produto.objects.values().get(pk=self.produtos[2].pk)
        with TemporaryDirectory(prefix='selective-posterior-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/seletivos-revisao-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("rpAgora")')
                tab.evaluate('document.getElementById("rpAgora").click();true')
                tab.wait('!!document.querySelector("#rpLista .campoNovoPrecoVendaCompra")')
                tab.evaluate('''document.querySelectorAll('#rpLista .marcarAtualizarVendaCompra').forEach(b=>b.checked=false);
                    const sim=document.querySelector('#rpLista [data-campo-preco="preco_vista"]');
                    const campo=sim.querySelector('.campoNovoPrecoVendaCompra');campo.value='40,00';campo.dispatchEvent(new Event('input',{bubbles:true}));
                    sim.querySelector('.marcarAtualizarVendaCompra').checked=true;document.getElementById('rpSalvar').click();true''')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertEqual(tab.evaluate('document.querySelectorAll("dialog[open] [data-destinatario]:checked:disabled").length'),2)
                self.assertFalse(tab.evaluate('document.querySelector("dialog[open] [data-destinatario]:not(:disabled)").checked'))
                tab.evaluate('document.querySelector("dialog[open] [data-destinatario]:not(:disabled)").checked='+str(selecionar).lower()+';Array.from(document.querySelectorAll("dialog[open] button")).find(b=>b.textContent.includes("Confirmar selecionados")).click();true')
                tab.wait('!document.getElementById("revisaoPrecosPosterior").classList.contains("visivel")')
                compra.refresh_from_db();self.assertFalse(compra.revisao_precos_pendente)
                self.assertEqual(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[:2]],preco_vista=40).count(),2)
                if selecionar:self.assertTrue(Produto.objects.get(pk=self.produtos[2].pk).precos_canonicos_adotados)
                else:self.assertEqual(Produto.objects.values().get(pk=self.produtos[2].pk),antes)
                self.assertEqual(AlteracaoPrecoVinculado.objects.count(),1)
            finally:chrome.stop()

    def test_desktop_copurchased_and_absent_not_selected(self):self.revisao(1280,False)
    def test_mobile_copurchased_and_optional_absent_selected(self):self.revisao(390,True)
