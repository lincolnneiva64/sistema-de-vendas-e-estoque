import copy
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.core.exceptions import ValidationError
from django.db import connection, close_old_connections, models
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from .models import (Produto, GrupoProdutoVinculado, AlteracaoPrecoVinculado,
                     Compra, ItemCompra, Venda, ItemVenda, ControlePrecosVinculados)
from . import tests_precos_vinculados as preparatoria
from .services.precos_vinculados import (regularizar_grupo, alterar_precos_grupo,
    diagnosticar_grupo, restaurar_evento_grupo, adicionar_produtos_regularizados)
from .services.precos_compra import aplicar_precos_compra, restaurar_precos_compra
from .grupos_produtos import remover_membro


class ServicoPrecosVinculadosTests(TestCase):
    def setUp(self):
        preparatoria.PreviaPrecosVinculadosTests.setUp(self)
        self.operador=preparatoria.criar_operador_precos('central-prices-test')
        self.client.force_login(self.operador)

    def ativar(self,referencia=None):
        produtos=list(self.grupo.produtos.all())
        return regularizar_grupo(self.grupo.pk,diagnosticar_grupo(produtos)['versao_observada'],
            referencia_id=referencia,confirmar=True,operador=self.operador)

    def alterar(self,index=0,**precos):
        self.grupo.refresh_from_db()
        return alterar_precos_grupo(self.produtos[index].pk,precos,versao_esperada=self.grupo.versao_precos,operador=self.operador)

    def compra(self):
        compra=Compra.objects.create(data_compra=timezone.localdate(),status=Compra.STATUS_FINALIZADA)
        item=ItemCompra.objects.create(compra=compra,produto=self.produtos[0],quantidade=1,unidade='PCT',preco_unitario=10,valor_total=10,preco_compra_anterior=9)
        return compra,item

    def snapshot(self):return list(Produto.objects.order_by('pk').values())

    def test_all_four_fields_from_any_member_and_independent_data(self):
        self.ativar();individual=[(p.pk,p.quantidade,p.preco_compra,p.codigo) for p in Produto.objects.order_by('pk')]
        for campo in ['preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado']:
            for index in range(3):
                before=self.snapshot();value=getattr(Produto.objects.get(pk=self.produtos[index].pk),campo)+1
                event=self.alterar(index,**{campo:value})
                self.assertIsNotNone(event)
                for p,old in zip(Produto.objects.order_by('pk'),before):
                    self.assertEqual(getattr(p,campo),value)
                    for untouched in set(['preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado'])-{campo}:
                        self.assertEqual(getattr(p,untouched),old[untouched])
                    self.assertEqual(p.preco_venda,p.preco_vista)
        self.assertEqual([(p.pk,p.quantidade,p.preco_compra,p.codigo) for p in Produto.objects.order_by('pk')],individual)

    def test_pending_and_direct_writers_blocked_without_partial_effect(self):
        before=self.snapshot()
        with self.assertRaises(ValidationError):self.alterar(preco_vista=37)
        with self.assertRaises(ValidationError),transaction_for_test():Produto.objects.filter(pk=self.produtos[0].pk).update(preco_vista=37)
        p=Produto.objects.get(pk=self.produtos[0].pk);p.preco_vista=37
        with self.assertRaises(ValidationError),transaction_for_test():p.save()
        self.assertEqual(self.snapshot(),before)

    def test_divergent_requires_explicit_reference_and_no_migration_normalization(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk),preco_vista=38,preco_venda=38)
        before=self.snapshot();self.assertFalse(self.grupo.precos_regularizados)
        with self.assertRaises(ValidationError):self.ativar()
        self.assertEqual(self.snapshot(),before)
        self.ativar(self.produtos[1].pk)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(38)})

    def test_inactive_member_and_loss_block_whole_group(self):
        self.ativar();Produto.objects.filter(pk=self.produtos[1].pk).update(ativo=False)
        self.alterar(preco_vista=37)
        self.assertEqual(Produto.objects.get(pk=self.produtos[1].pk).preco_vista,37)
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_compra=40)
        before=self.snapshot()
        with self.assertRaises(ValidationError):self.alterar(preco_vista=38)
        self.assertEqual(self.snapshot(),before)

    def test_stale_version_and_idempotent_unchanged_values(self):
        self.ativar();self.grupo.refresh_from_db();version=self.grupo.versao_precos
        self.alterar(preco_prazo=40);before=self.snapshot()
        with self.assertRaises(ValidationError):alterar_precos_grupo(self.produtos[1].pk,{'preco_vista':37},versao_esperada=version,operador=self.operador)
        self.assertEqual(self.snapshot(),before)
        count=AlteracaoPrecoVinculado.objects.count()
        self.assertIsNone(self.alterar(preco_prazo='40.00'))
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),count)

    def test_purchase_all_members_evidence_restore_and_idempotence(self):
        self.ativar();compra,item=self.compra();before=self.snapshot();self.grupo.refresh_from_db()
        aplicar_precos_compra(item,{'preco_vista':37,'preco_venda':37,'preco_prazo':40,'preco_vista_fracionado':8,'preco_prazo_fracionado':9},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        item.refresh_from_db();self.assertEqual(len(item.alteracoes_precos_vinculados),1)
        event=AlteracaoPrecoVinculado.objects.get(operation_id=item.alteracoes_precos_vinculados[0])
        self.assertEqual(len(event.evidencia['alteracoes']),3)
        restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        for p,old in zip(Produto.objects.order_by('pk'),before):
            for field in ['preco_vista','preco_prazo','preco_vista_fracionado','preco_prazo_fracionado','preco_venda','quantidade','preco_compra','codigo']:
                self.assertEqual(getattr(p,field),old[field])
        after=self.snapshot();restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        self.assertEqual(self.snapshot(),after)

    def test_later_update_or_member_change_blocks_purchase_undo(self):
        self.ativar();_,item=self.compra();self.grupo.refresh_from_db()
        aplicar_precos_compra(item,{'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        item.refresh_from_db();self.alterar(1,preco_vista=38);before=self.snapshot()
        with self.assertRaises(ValueError):restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        self.assertEqual(self.snapshot(),before)

    def test_cost_only_does_not_propagate_and_legacy_snapshot_refused(self):
        self.ativar();_,item=self.compra();prices=list(Produto.objects.values_list('preco_vista',flat=True))
        aplicar_precos_compra(item,{'preco_compra':13},operador=self.operador)
        self.assertEqual(list(Produto.objects.values_list('preco_vista',flat=True)),prices)
        self.assertEqual(Produto.objects.get(pk=self.produtos[1].pk).preco_compra,11)
        item.refresh_from_db();item.alteracoes_precos['preco_vista']={'anterior':'36','novo':'37','alteracao_id':'legacy'};item.save()
        before=self.snapshot()
        with self.assertRaises(ValueError):restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        self.assertEqual(self.snapshot(),before)

    def test_failure_mid_write_rolls_back_all_and_audit(self):
        self.ativar();before=self.snapshot();count=AlteracaoPrecoVinculado.objects.count()
        original=models.QuerySet.update;writes=0
        def fail(qs,**values):
            nonlocal writes
            if qs.model is Produto and 'preco_vista' in values:
                writes+=1
                if writes==2:raise RuntimeError('isolated injected failure')
            return original(qs,**values)
        with patch.object(models.QuerySet,'update',fail),self.assertRaises(RuntimeError):self.alterar(preco_vista=37)
        self.assertEqual(self.snapshot(),before);self.assertEqual(AlteracaoPrecoVinculado.objects.count(),count)

    def test_entry_exit_and_membership_undo_guard(self):
        self.ativar();new=Produto.objects.create(nome='Novo teste',preco_compra=10,preco_vista=50,preco_prazo=51,unidade_compra='PCT',unidade_venda_2='UN',vende_fracionado=True,fator_conversao=6,preco_vista_fracionado=8,preco_prazo_fracionado=9)
        token=diagnosticar_grupo([*self.grupo.produtos.all(),new])['versao_observada']
        adicionar_produtos_regularizados(self.grupo.pk,[new.pk],token,confirmar=True,operador=self.operador)
        new.refresh_from_db();self.assertEqual(new.preco_vista,36)
        remover_membro(self.grupo.pk,new.pk);self.alterar(preco_vista=37)
        new.refresh_from_db();self.assertEqual(new.preco_vista,36)

    def test_real_revision_endpoint_and_purchase_exclusion(self):
        self.ativar();compra,item=self.compra();compra.revisao_precos_pendente=True;compra.save()
        self.grupo.refresh_from_db()
        payload={'compra_id':compra.pk,'item_id':item.pk,'produto_id':item.produto_id,
            'atualizar_preco_venda_produto_ids[]':[str(item.produto_id)],'atualizar_preco_venda_nomes[]':['preco_vista'],
            'atualizar_preco_venda_valores[]':['37.00'],'versao_grupo_produto_'+str(item.produto_id):str(self.grupo.versao_precos)}
        response=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),payload)
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(37)})
        response=self.client.post(reverse('estoque:compra_excluir',args=[compra.pk]))
        self.assertEqual(response.status_code,302)
        self.assertFalse(Compra.objects.filter(pk=compra.pk).exists())
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(36)})

    def test_historical_sales_unchanged(self):
        sale=Venda.objects.create(data_venda=timezone.localdate(),total=36)
        ItemVenda.objects.create(venda=sale,produto=self.produtos[0],quantidade=1,unidade='PCT',preco_unitario=36,valor_total=36)
        before=(list(Venda.objects.values()),list(ItemVenda.objects.values()))
        self.ativar();self.alterar(preco_vista=37)
        self.assertEqual((list(Venda.objects.values()),list(ItemVenda.objects.values())),before)

    def test_member_change_and_invalid_evidence_block_restore(self):
        self.ativar();_,item=self.compra();self.grupo.refresh_from_db()
        aplicar_precos_compra(item,{'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        item.refresh_from_db();remover_membro(self.grupo.pk,self.produtos[2].pk);before=self.snapshot()
        with self.assertRaises(ValueError):restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        self.assertEqual(self.snapshot(),before)
        item.alteracoes_precos_vinculados=['00000000-0000-0000-0000-000000000000'];item.save()
        with self.assertRaises(ValueError):restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)

    def test_nonfractional_and_active_conversion_change_blocked(self):
        Produto.objects.all().update(vende_fracionado=False)
        self.ativar();before=self.snapshot()
        with self.assertRaises(ValidationError):self.alterar(preco_vista_fracionado=8)
        with self.assertRaises(ValidationError),transaction_for_test():Produto.objects.filter(pk=self.produtos[0].pk).update(unidade_compra='CX')
        self.assertEqual(self.snapshot(),before)

    def test_multiple_revisions_same_purchase_restore_proven_chain(self):
        self.ativar();_,item=self.compra()
        for value in [37,38]:
            self.grupo.refresh_from_db()
            aplicar_precos_compra(item,{'preco_vista':value},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        item.refresh_from_db();self.assertEqual(len(item.alteracoes_precos_vinculados),2)
        restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(36)})
        self.assertEqual(AlteracaoPrecoVinculado.objects.filter(reverte__isnull=False).count(),2)

    def test_exclusion_after_later_price_update_rolls_back_stock_and_sale(self):
        self.ativar();compra,item=self.compra()
        compra.estoque_entrada_realizada=True;compra.save()
        Produto.objects.filter(pk=item.produto_id).update(quantidade=11)
        self.grupo.refresh_from_db()
        aplicar_precos_compra(item,{'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        self.alterar(1,preco_vista=38);before=self.snapshot()
        response=self.client.post(reverse('estoque:compra_excluir',args=[compra.pk]))
        self.assertEqual(response.status_code,302)
        self.assertTrue(Compra.objects.filter(pk=compra.pk).exists())
        self.assertTrue(ItemCompra.objects.filter(pk=item.pk).exists())
        self.assertEqual(self.snapshot(),before)

    def test_invalid_entry_or_missing_confirmation_no_partial_membership(self):
        self.ativar()
        new=Produto.objects.create(nome='Incompativel',preco_compra=10,preco_vista=50,preco_prazo=51,unidade_compra='CX')
        token=diagnosticar_grupo([*self.grupo.produtos.all(),new])['versao_observada'];before=self.snapshot()
        for confirm in [False,True]:
            with self.assertRaises(ValidationError):adicionar_produtos_regularizados(self.grupo.pk,[new.pk],token,confirmar=confirm,operador=self.operador)
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(hasattr(Produto.objects.get(pk=new.pk),'vinculo_grupo'))

    def test_fractional_cost_protection_and_per_product_percentages(self):
        Produto.objects.filter(pk=self.produtos[0].pk).update(preco_compra_fracionado=2)
        Produto.objects.filter(pk=self.produtos[1].pk).update(preco_compra_fracionado=3)
        self.ativar();before=self.snapshot()
        with self.assertRaises(ValidationError):self.alterar(preco_vista_fracionado=1)
        self.assertEqual(self.snapshot(),before)
        self.alterar(preco_vista_fracionado=9)
        a,b=[Produto.objects.get(pk=p.pk) for p in self.produtos[:2]]
        self.assertEqual(a.percentual_vista_fracionado,350)
        self.assertEqual(b.percentual_vista_fracionado,200)

    def test_mixed_cost_and_sale_prices_validate_final_cost_and_rollback(self):
        self.ativar();_,item=self.compra();self.grupo.refresh_from_db();before=self.snapshot()
        with self.assertRaises(ValueError):aplicar_precos_compra(item,{'preco_compra':40,'preco_vista':38},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        self.assertEqual(self.snapshot(),before)
        item.refresh_from_db();self.assertEqual(item.alteracoes_precos,{})
        aplicar_precos_compra(item,{'preco_compra':13,'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        item.refresh_from_db();restaurar_precos_compra(item,Produto.objects.get(pk=item.produto_id),operador=self.operador)
        for p,old in zip(Produto.objects.order_by('pk'),before):
            self.assertEqual(p.preco_compra,old['preco_compra']);self.assertEqual(p.preco_vista,old['preco_vista'])

    def test_purchase_batch_two_members_one_event_and_conflicting_requests_refused(self):
        from . import views
        self.ativar();compra,item=self.compra();self.grupo.refresh_from_db()
        ItemCompra.objects.create(compra=compra,produto=self.produtos[1],quantidade=1,preco_unitario=11,valor_total=11)
        values=views._PrecosCompraRevisao({(self.produtos[0].pk,'preco_vista'):Decimal(37),(self.produtos[1].pk,'preco_prazo'):Decimal(40)})
        values.versoes_grupos={p.pk:self.grupo.versao_precos for p in self.produtos};values.operador=self.operador
        views._atualizar_precos_venda_produtos_compra(values,compra)
        compra.refresh_from_db();self.assertEqual(len(compra.alteracoes_precos_vinculados),1)
        self.assertEqual(set(Produto.objects.values_list('preco_vista','preco_prazo')),{(Decimal(37),Decimal(40))})
        self.grupo.refresh_from_db();bad=views._PrecosCompraRevisao({(self.produtos[0].pk,'preco_vista'):Decimal(38),(self.produtos[1].pk,'preco_vista'):Decimal(39)})
        bad.versoes_grupos={p.pk:self.grupo.versao_precos for p in self.produtos};bad.operador=self.operador;before=self.snapshot()
        with self.assertRaises(ValueError):views._atualizar_precos_venda_produtos_compra(bad,compra)
        self.assertEqual(self.snapshot(),before)

    def test_purchase_ledger_survives_removed_item(self):
        from .services.precos_compra import restaurar_precos_da_compra
        self.ativar();compra,item=self.compra();self.grupo.refresh_from_db()
        aplicar_precos_compra(item,{'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        compra.refresh_from_db();self.assertEqual(len(compra.alteracoes_precos_vinculados),1)
        item.delete();restaurar_precos_da_compra(compra,operador=self.operador)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(36)})


def transaction_for_test():
    from django.db import transaction
    return transaction.atomic()


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL')
class ConcorrenciaPrecosVinculadosTests(TransactionTestCase):
    def setUp(self):
        preparatoria.PreviaPrecosVinculadosTests.setUp(self)
        self.operador=preparatoria.criar_operador_precos('central-prices-test')
        self.client.force_login(self.operador)

    def test_two_members_same_version_one_winner(self):
        regularizar_grupo(self.grupo.pk,diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],confirmar=True,operador=self.operador)
        self.grupo.refresh_from_db();version=self.grupo.versao_precos;gate=Barrier(2)
        def run(index):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                try:
                    alterar_precos_grupo(self.produtos[index].pk,{'preco_vista':Decimal(37+index)},versao_esperada=version,operador=self.operador)
                    return True
                except ValidationError:return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,[0,1]))
        self.assertEqual(sum(result),1)
        self.assertEqual(len(set(Produto.objects.values_list('preco_vista',flat=True))),1)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),2)

    def race(self,actions):
        gate=Barrier(2)
        def run(action):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                try:action();return True
                except (ValidationError,ValueError):return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,actions))

    def prepare_purchase(self):
        regularizar_grupo(self.grupo.pk,diagnosticar_grupo(list(self.grupo.produtos.all()))['versao_observada'],confirmar=True,operador=self.operador)
        self.grupo.refresh_from_db()
        compra=Compra.objects.create(data_compra=timezone.localdate(),status=Compra.STATUS_FINALIZADA)
        return ItemCompra.objects.create(compra=compra,produto=self.produtos[0],quantidade=1,preco_unitario=10,valor_total=10)

    def test_purchase_and_operator_share_one_serialized_version(self):
        item=self.prepare_purchase();version=self.grupo.versao_precos
        result=self.race([
            lambda:aplicar_precos_compra(ItemCompra.objects.get(pk=item.pk),{'preco_vista':37},versao_esperada=version,operador=self.operador),
            lambda:alterar_precos_grupo(self.produtos[1].pk,{'preco_vista':38},versao_esperada=version,operador=self.operador)])
        self.assertEqual(sum(result),1)
        self.assertEqual(len(set(Produto.objects.values_list('preco_vista',flat=True))),1)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),2)

    def test_purchase_undo_and_operator_never_overwrite_each_other(self):
        item=self.prepare_purchase();aplicar_precos_compra(item,{'preco_vista':37},versao_esperada=self.grupo.versao_precos,operador=self.operador)
        self.grupo.refresh_from_db();version=self.grupo.versao_precos
        result=self.race([
            lambda:restaurar_precos_compra(ItemCompra.objects.get(pk=item.pk),Produto.objects.get(pk=item.produto_id),operador=self.operador),
            lambda:alterar_precos_grupo(self.produtos[1].pk,{'preco_vista':38},versao_esperada=version,operador=self.operador)])
        self.assertEqual(sum(result),1)
        self.assertEqual(set(Produto.objects.values_list('preco_vista',flat=True)),{Decimal(36)} if result[0] else {Decimal(38)})
