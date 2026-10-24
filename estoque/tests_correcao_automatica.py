"""Disposable-PostgreSQL regressions for explicit, atomic purchase corrections."""
import copy,json,threading,base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase,TransactionTestCase,Client
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connections
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from . import tests_compra_interface_finalizada as fixtures
from .models import (Compra,Produto,ItemCompra,ContaPagar,PagamentoContaPagar,MovimentoFinanceiro,EdicaoCompra,ConciliacaoCompra,ContaFinanceira)
from .services.edicao_compra import estado_compra
from .services import correcao_compra as service
from .tests_compra_mobile_console import AuditedChrome
from offline.browser_support import Chrome
OUT=Path(__file__).resolve().parent.parent/'evidencias-correcao-automatica'
OUT.mkdir(exist_ok=True)
CHROME=r'C:\Program Files\Google\Chrome\Application\chrome.exe'

class Fixture(fixtures.Fixture):
    def automatic(self,tipo='erro_lancamento',**extra):
        data=self.data();data.update(pagamento_real_confirmado='1',credito_real_confirmado='1',tipo_correcao=tipo,valor_pago_real='442,20',valor_devolvido_real='0',acao_compra='prever_correcao')
        data['tipo_pagamento']=self.compra.tipo_pagamento
        if self.compra.data_vencimento:data['data_vencimento']=self.compra.data_vencimento.isoformat()
        data.update(extra);return data
    def preview(self,data,client=None):
        response=(client or self.client).post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content);return response.json()
    def apply(self,data,preview=None,client=None):
        preview=preview or self.preview(data,client)
        data=copy.deepcopy(data);data['previa_token']=preview['previa_token'];data['acao_compra']='salvar_correcao_automatica'
        return (client or self.client).post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
    def snapshot(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in (Compra,Produto,ItemCompra,ContaPagar,PagamentoContaPagar,MovimentoFinanceiro,EdicaoCompra,ConciliacaoCompra)}
    def returned(self,amount='0'):
        return self.automatic('devolucao',**{'produto_id[]':[str(self.produto.pk),str(self.other.pk)],'quantidade[]':['1','7'],
            'preco_unitario[]':['36,80','47,40'],'unidade[]':['PCT','PCT'],
            'valor_devolvido_real':amount,'conta_devolucao':str(self.bank.pk),'data_devolucao':timezone.localdate().isoformat()})

class Backend(Fixture,TestCase):
    def test_real_payment_must_be_confirmed_not_inferred_from_record(self):
        before=self.snapshot()
        for extra in ({'pagamento_real_confirmado':''}, {'valor_pago_real':''}, {'credito_real_confirmado':''}):
            response=self.client.post(self.url,self.automatic(**extra),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(response.status_code,422,response.content)
            self.assertEqual(self.snapshot(),before)

    def test_wrong_record_is_reconciled_without_credit_confirmation(self):
        data=self.automatic(valor_pago_real='438,00',credito_real_confirmado='')
        preview=self.preview(data)
        self.assertEqual(preview['previa']['pago_registrado_antes'],'442.20')
        self.assertEqual(preview['previa']['pago_preservado'],'438.00')
        self.assertEqual(preview['previa']['credito_fornecedor'],'0.00')
        self.assertEqual(self.apply(data,preview).status_code,200)
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.valor,Decimal('438.00'))
        self.assertEqual(MovimentoFinanceiro.objects.count(),1)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('0'))

    def test_imperio_product_substitution_preserves_money_list_item_ids_and_history(self):
        before=estado_compra(self.compra);data=self.automatic();preview=self.preview(data)
        self.assertEqual(preview['previa']['total_corrigido'],'438.00');self.assertEqual(preview['previa']['credito_fornecedor'],'4.20')
        self.assertEqual(self.apply(data,preview).status_code,200)
        self.item.refresh_from_db();self.compra.refresh_from_db()
        self.assertEqual(self.item.produto_id,self.correct.pk);self.assertEqual(self.item.unidade,'DZ')
        self.assertEqual(self.compra.lista_fornecedor_id,self.lista.pk);self.assertEqual(self.lista.itens.count(),3)
        self.assertEqual(self.compra.estoque_entrada_realizada_em,before['compra']['estoque_entrada_realizada_em'])
        self.assertEqual(self.compra.status,Compra.STATUS_FINALIZADA);self.assertEqual(self.compra.total,Decimal('438'))
        self.assertEqual(estado_compra(self.compra)['movimentos'],before['movimentos'])
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,Decimal('17'))
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,Decimal('23'))
        self.assertEqual(self.compra.conciliacao.credito_fornecedor,Decimal('4.20'))
        audit=EdicaoCompra.objects.get();self.assertEqual(audit.antes['compra']['itens'][0]['produto_id'],self.produto.pk)
        self.assertEqual(audit.depois['ajustes']['itens'][0]['produto_anterior_id'],self.produto.pk)
        self.assertEqual(audit.depois['ajustes']['itens'][0]['produto_corrigido_id'],self.correct.pk)
        detail=self.client.get(reverse('estoque:compras_detalhe',args=[self.compra.pk]))
        self.assertContains(detail,'Crédito do fornecedor');self.assertContains(detail,'4,20');self.assertContains(detail,'438,00')
        self.assertContains(detail,f'data-conta-conciliada="{self.bank.pk}"')
    def test_unit_conversion_and_second_correction_use_actual_base_snapshot(self):
        self.produto.unidade_venda_1='PCT';self.produto.unidade_venda_2='UN';self.produto.vende_fracionado=True
        self.produto.fator_conversao=15;self.produto.preco_vista_fracionado=4;self.produto.preco_prazo_fracionado=5;self.produto.save()
        data=self.automatic('diferenca',**{'produto_id[]':[str(self.produto.pk),str(self.other.pk)],'quantidade[]':['30','7'],'preco_unitario[]':['2,50','47,40'],'unidade[]':['UN','PCT']})
        self.assertEqual(self.apply(data).status_code,200)
        self.produto.refresh_from_db();self.item.refresh_from_db();self.assertEqual(self.produto.quantidade,Decimal('19'))
        self.assertEqual(self.item.estoque_base_registrado,Decimal('2'))
        data=self.automatic('diferenca',**{'produto_id[]':[str(self.produto.pk),str(self.other.pk)],'quantidade[]':['45','7'],'preco_unitario[]':['2,50','47,40'],'unidade[]':['UN','PCT']})
        self.assertEqual(self.apply(data).status_code,200)
        self.produto.refresh_from_db();self.assertEqual(self.produto.quantidade,Decimal('20'))
        self.assertEqual(self.compra.contas_pagar.get().valor_em_aberto,Decimal('2.10'))
    def test_actual_return_without_refund_becomes_supplier_credit(self):
        self.assertEqual(self.apply(self.returned()).status_code,200)
        self.assertEqual(MovimentoFinanceiro.objects.count(),1)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('73.60'))
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,Decimal('18'))
    def test_actual_return_with_explicit_partial_refund_keeps_remaining_credit(self):
        original=list(MovimentoFinanceiro.objects.values())
        self.assertEqual(self.apply(self.returned('50')).status_code,200)
        self.assertEqual(MovimentoFinanceiro.objects.count(),2)
        refund=MovimentoFinanceiro.objects.get(origem='compra_devolucao_correcao')
        self.assertEqual(refund.valor,Decimal('50'));self.assertEqual(refund.conta_id,self.bank.pk)
        self.assertEqual(refund.tipo,'entrada');self.assertEqual(refund.compra_id,self.compra.pk)
        self.assertEqual(list(MovimentoFinanceiro.objects.filter(pk=self.movement.pk).values()),original)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('23.60'))
    def test_full_return_zero_remaining_items(self):
        data=self.returned();data['produto_id[]']=[]
        self.assertEqual(self.apply(data).status_code,200);self.assertEqual(self.compra.itens.count(),0)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('442.20'))
    def test_higher_corrected_amount_creates_only_open_obligation_no_cash_outflow(self):
        data=self.automatic('diferenca',**{'preco_unitario[]':['100','47,40']})
        self.assertEqual(self.apply(data).status_code,200)
        self.assertEqual(MovimentoFinanceiro.objects.count(),1)
        self.assertEqual(self.compra.contas_pagar.get().valor_em_aberto,Decimal('189.60'))
        self.assertEqual(ConciliacaoCompra.objects.get().saldo_a_pagar,Decimal('189.60'))
        from estoque.views import _situacao_financeira_compra_lista
        self.compra.refresh_from_db()
        self.assertNotEqual(_situacao_financeira_compra_lista(self.compra)[1],'paga')
        detail=self.client.get(reverse('estoque:compras_detalhe',args=[self.compra.pk]))
        self.assertContains(detail,'Em aberto')
    def test_registered_payments_preserved_and_open_balance_reconciled(self):
        self.movement.delete();self.compra.tipo_pagamento='aprazo';self.compra.data_vencimento=date(2026,11,24);self.compra.save()
        conta=ContaPagar.objects.create(compra=self.compra,fornecedor=self.fornecedor,data_emissao=self.compra.data_compra,data_vencimento=self.compra.data_vencimento,
            valor_original=Decimal('442.20'),valor_em_aberto=Decimal('42.20'),status='parcial')
        payment=PagamentoContaPagar.objects.create(conta=conta,data_pagamento=self.compra.data_compra,valor=400)
        MovimentoFinanceiro.objects.create(pagamento_conta_pagar=payment,conta=self.bank,tipo='saida',valor=400,data=self.compra.data_compra,origem='pagamento_conta_pagar')
        original=list(PagamentoContaPagar.objects.values());mov=list(MovimentoFinanceiro.objects.values())
        data=self.automatic('diferenca',**{'valor_pago_real':'400','preco_unitario[]':['10','47,40']})
        self.assertEqual(self.apply(data).status_code,200);conta.refresh_from_db()
        self.assertEqual(conta.status,'paga');self.assertEqual(conta.valor_original,Decimal('400'));self.assertEqual(conta.valor_em_aberto,0)
        self.assertEqual(list(PagamentoContaPagar.objects.values()),original);self.assertEqual(list(MovimentoFinanceiro.objects.values()),mov)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('38.20'))
    def test_duplicate_request_does_not_duplicate_stock_refund_or_history(self):
        data=self.returned('50');preview=self.preview(data)
        for _ in range(2):self.assertEqual(self.apply(data,preview).status_code,200)
        self.assertEqual(MovimentoFinanceiro.objects.count(),2);self.assertEqual(EdicaoCompra.objects.count(),1)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade,Decimal('18'))
    def test_post_preview_changes_to_payload_stock_or_payment_are_rejected(self):
        for change in ['payload','stock','payment']:
            with self.subTest(change=change):
                data=self.automatic();preview=self.preview(data)
                if change=='payload':data['quantidade[]']=['4','7']
                elif change=='stock':Produto.objects.filter(pk=self.correct.pk).update(quantidade=25)
                else:MovimentoFinanceiro.objects.filter(pk=self.movement.pk).update(valor=Decimal('440'))
                before=self.snapshot();response=self.apply(data,preview)
                self.assertEqual(response.status_code,422);self.assertEqual(self.snapshot(),before)
    def test_invalid_reasons_units_amounts_and_refunds_do_not_write(self):
        cases=[{'tipo_correcao':'xxx'},{'motivo_edicao':''},{'tipo_correcao':'diferenca','valor_pago_real':'438'},
            {'valor_pago_real':'NaN'},{'valor_devolvido_real':'1'},
            {'unidade[]':['CX','PCT']},{'quantidade[]':['0','7']},
            {'item_edicao_id[]':['999999',str(self.second.pk)]},
            {'tipo_correcao':'diferenca','valor_devolvido_real':'500'}]
        before=self.snapshot()
        for extra in cases:
            response=self.client.post(self.url,self.automatic(**extra),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(response.status_code,422,response.content);self.assertEqual(self.snapshot(),before)
    def test_legacy_ambiguous_links_and_catalog_history_stay_protected(self):
        self.movement.compra=None;self.movement.descricao=f'Compra #{self.compra.pk}';self.movement.save()
        response=self.client.post(self.url,self.automatic(),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertIn('legado',response.json()['erro'])
        self.movement.compra=self.compra;self.movement.save()
        self.item.alteracoes_precos={'historico':'existente'};self.item.save()
        response=self.client.post(self.url,self.automatic(),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200)
        data=self.automatic();self.assertEqual(self.apply(data).status_code,200)
        self.item.refresh_from_db();audit=EdicaoCompra.objects.get()
        self.assertEqual(audit.antes['compra']['itens'][0]['alteracoes_precos'],{'historico':'existente'})
        self.assertEqual(self.item.alteracoes_precos,{})
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).preco_compra,Decimal('36.80'))
    def test_every_intermediate_failure_rolls_back_all_effects(self):
        data=self.returned('50');preview=self.preview(data);before=self.snapshot()
        for stage in ['estoque','itens','financeiro','auditoria']:
            def fail(name):
                if name==stage:raise RuntimeError('Synthetic rollback probe')
            with patch.object(service,'etapa_correcao',side_effect=fail):
                response=self.apply(data,preview)
            self.assertEqual(response.status_code,500);self.assertEqual(self.snapshot(),before)
    def test_financial_permission_and_migration_consistency(self):
        from django.contrib.auth.models import Permission
        user=get_user_model().objects.create_user('limited-correction',password='test-only')
        user.user_permissions.add(Permission.objects.get(codename='change_compra'))
        self.client.force_login(user)
        response=self.client.post(self.url,self.returned('50'),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertIn('permissão',response.json()['erro'])
        call_command('makemigrations',check=True,dry_run=True,verbosity=0)

    def test_legacy_edit_and_delete_cannot_destroy_reconciliation(self):
        self.assertEqual(self.apply(self.automatic()).status_code,200)
        before=self.snapshot()
        for name in ['compra_corrigir_itens','compra_corrigir_financeiro','compra_corrigir_origem_pagamento','compra_excluir']:
            response=self.client.post(reverse('estoque:'+name,args=[self.compra.pk]),{})
            self.assertEqual(response.status_code,302)
            self.assertEqual(self.snapshot(),before)
        data=self.data()
        response=self.client.post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertEqual(self.snapshot(),before)
    def test_subsequent_return_preserves_previous_refund_and_credit(self):
        self.assertEqual(self.apply(self.returned('50')).status_code,200)
        data=self.returned('0');data['unidade[]']=['SACO','PCT']
        # Never infer a conversion for an unregistered physical-return unit.
        response=self.client.post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422)
        data=self.returned('23,60');data['produto_id[]']=[str(self.other.pk)]
        data['item_edicao_id[]']=[str(self.second.pk)];data['quantidade[]']=['7'];data['preco_unitario[]']=['47,40'];data['unidade[]']=['PCT']
        self.assertEqual(self.apply(data).status_code,200)
        self.assertEqual(ConciliacaoCompra.objects.get().valor_devolvido,Decimal('73.60'))
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('36.80'))
        self.assertEqual(MovimentoFinanceiro.objects.count(),3)

    def test_wrong_automatic_financial_record_can_match_declared_real_payment_without_new_cash_entry(self):
        data=self.automatic(valor_pago_real='438')
        preview=self.preview(data)
        self.assertEqual(preview['previa']['correcao_registro_financeiro']['valor_anterior'],'442.20')
        self.assertEqual(preview['previa']['credito_fornecedor'],'0.00')
        self.assertEqual(self.apply(data,preview).status_code,200)
        self.movement.refresh_from_db();self.assertEqual(self.movement.valor,Decimal('438'))
        self.assertEqual(MovimentoFinanceiro.objects.count(),1)
        audit=EdicaoCompra.objects.get()
        self.assertEqual(audit.antes['compra']['movimentos'][0]['valor'],'442.20')
        self.assertEqual(audit.depois['ajustes']['correcao_registro_financeiro']['valor_real_informado'],'438.00')
    def test_multiple_origins_cannot_be_reallocated_by_guessing(self):
        self.movement.valor=400;self.movement.save()
        cash=ContaFinanceira.objects.create(nome='Caixa adicional',tipo='caixa')
        MovimentoFinanceiro.objects.create(compra=self.compra,conta=cash,tipo='saida',valor=Decimal('42.20'),data=self.compra.data_compra,origem='compra_a_vista')
        response=self.client.post(self.url,self.automatic(valor_pago_real='438'),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertIn('múltiplas',response.json()['erro'])

    def test_financial_record_rectification_rolls_back_if_audit_fails(self):
        data=self.automatic(valor_pago_real='438');preview=self.preview(data);before=self.snapshot()
        def fail(name):
            if name=='auditoria':raise RuntimeError('Synthetic original-ledger rollback')
        with patch.object(service,'etapa_correcao',side_effect=fail):response=self.apply(data,preview)
        self.assertEqual(response.status_code,500);self.assertEqual(self.snapshot(),before)
    def test_multiple_proved_origins_are_preserved_when_real_total_matches(self):
        self.movement.valor=400;self.movement.save()
        cash=ContaFinanceira.objects.create(nome='Segunda origem comprovada',tipo='caixa')
        MovimentoFinanceiro.objects.create(compra=self.compra,conta=cash,tipo='saida',valor=Decimal('42.20'),data=self.compra.data_compra,origem='compra_a_vista')
        original=list(MovimentoFinanceiro.objects.order_by('pk').values())
        self.assertEqual(self.apply(self.automatic()).status_code,200)
        self.assertEqual(list(MovimentoFinanceiro.objects.order_by('pk').values()),original)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('4.20'))
    def test_unpaid_installments_reconcile_without_changing_due_dates_or_creating_payment(self):
        self.movement.delete();self.compra.tipo_pagamento='aprazo';self.compra.data_vencimento=date(2026,11,24);self.compra.save()
        contas=[]
        for n in [1,2]:
            contas.append(ContaPagar.objects.create(compra=self.compra,fornecedor=self.fornecedor,data_emissao=self.compra.data_compra,
                data_vencimento=date(2026,10+n,24),valor_original=Decimal('221.10'),valor_em_aberto=Decimal('221.10'),numero_parcela=n,total_parcelas=2))
        data=self.automatic(valor_pago_real='0');self.assertEqual(self.apply(data).status_code,200)
        for c in contas:c.refresh_from_db()
        self.assertEqual([c.valor_em_aberto for c in contas],[Decimal('221.10'),Decimal('216.90')])
        self.assertEqual([c.data_vencimento for c in contas],[date(2026,11,24),date(2026,12,24)])
        self.assertFalse(PagamentoContaPagar.objects.exists());self.assertFalse(MovimentoFinanceiro.objects.exists())
    def test_followup_real_refund_settles_credit_without_touching_stock(self):
        self.assertEqual(self.apply(self.automatic()).status_code,200)
        stocks=list(Produto.objects.order_by('pk').values())
        data=self.automatic('diferenca',valor_devolvido_real='4,20',conta_devolucao=str(self.bank.pk),data_devolucao=timezone.localdate().isoformat())
        self.assertEqual(self.apply(data).status_code,200)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('0'))
        self.assertEqual(list(Produto.objects.order_by('pk').values()),stocks)
        self.assertEqual(EdicaoCompra.objects.count(),2);self.assertEqual(MovimentoFinanceiro.objects.count(),2)
    def test_new_item_gets_audited_id_and_changed_stock_base_is_not_reinterpreted(self):
        data=self.automatic('diferenca',**{'produto_id[]':[str(self.produto.pk),str(self.other.pk),str(self.correct.pk)],
            'item_edicao_id[]':[str(self.item.pk),str(self.second.pk),''],'quantidade[]':['3','7','3'],
            'preco_unitario[]':['36,80','47,40','35,40'],'unidade[]':['PCT','PCT','DZ'],'observacao_item[]':['','','']})
        self.assertEqual(self.apply(data).status_code,200)
        added=self.compra.itens.get(produto=self.correct)
        self.assertEqual(EdicaoCompra.objects.get().depois['ajustes']['itens'][-1]['item_corrigido_id'],added.pk)
        self.correct.unidade_compra='UN';self.correct.unidade_venda_1='UN';self.correct.save()
        data=self.automatic('diferenca')
        data['item_edicao_id[]']=[str(added.pk),str(self.second.pk)]
        data['unidade[]']=['UN','PCT']
        response=self.client.post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertIn('unidade base',response.json()['erro'])
    def test_actual_registered_payment_is_never_rewritten_to_declared_different_value(self):
        self.movement.delete();self.compra.tipo_pagamento='aprazo';self.compra.data_vencimento=date(2026,11,24);self.compra.save()
        conta=ContaPagar.objects.create(compra=self.compra,fornecedor=self.fornecedor,data_emissao=self.compra.data_compra,
            data_vencimento=self.compra.data_vencimento,valor_original=Decimal('442.20'),valor_em_aberto=Decimal('42.20'),status='parcial')
        payment=PagamentoContaPagar.objects.create(conta=conta,data_pagamento=self.compra.data_compra,valor=400)
        MovimentoFinanceiro.objects.create(pagamento_conta_pagar=payment,conta=self.bank,tipo='saida',valor=400,data=self.compra.data_compra)
        before=self.snapshot()
        response=self.client.post(self.url,self.automatic(valor_pago_real='390'),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,422);self.assertEqual(self.snapshot(),before)

    def test_observation_only_edit_remains_available_after_reconciliation(self):
        self.assertEqual(self.apply(self.automatic()).status_code,200)
        items=list(ItemCompra.objects.order_by('pk').values());stock=list(Produto.objects.order_by('pk').values())
        movements=list(MovimentoFinanceiro.objects.values());credit=ConciliacaoCompra.objects.get().credito_fornecedor
        data=self.data();data['valor_cobrado']='438,00';data['observacao']='Referencia documental complementar'
        response=self.client.post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(list(ItemCompra.objects.order_by('pk').values()),items)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),stock)
        self.assertEqual(list(MovimentoFinanceiro.objects.values()),movements)
        self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,credit)
        self.compra.refresh_from_db();self.assertIn('Referencia documental complementar',self.compra.observacao)

class Concurrency(Fixture,TransactionTestCase):
    def parallel(self,same):
        data=self.automatic();first=self.preview(data)
        second=first if same else self.preview(self.automatic())
        barrier=threading.Barrier(2)
        def run(preview):
            connections.close_all()
            client=Client();client.force_login(self.user)
            barrier.wait(timeout=15)
            response=self.apply(data,preview,client)
            connections.close_all();return response.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[first,second]))
        self.assertEqual(sorted(results),[200,200] if same else [200,422])
        self.assertEqual(EdicaoCompra.objects.count(),1);self.assertEqual(ConciliacaoCompra.objects.count(),1)
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,Decimal('23'))
    def test_simultaneous_duplicate_executes_once(self):self.parallel(True)
    def test_competing_operations_reject_stale_preview(self):self.parallel(False)


class Browser(Fixture,StaticLiveServerTestCase):
    open=fixtures.Browser.open
    select=fixtures.Browser.select
    def shot(self,tab,name):
        data=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
        (OUT/(name+'.png')).write_bytes(base64.b64decode(data))
    def fresh_purchase(self):
        self.lista=type(self.lista).objects.create(fornecedor=self.fornecedor,data_lista=self.compra.data_compra,data_inicio_periodo=self.compra.data_compra,data_fim_periodo=self.compra.data_compra)
        self.compra=Compra.objects.create(fornecedor=self.fornecedor,lista_fornecedor=self.lista,data_compra=date(2026,10,24),
            tipo_pagamento='avista',total=Decimal('442.20'),total_produtos=Decimal('442.20'),status='finalizada',estoque_entrada_realizada=True)
        self.item=ItemCompra.objects.create(compra=self.compra,produto=self.produto,quantidade=3,preco_unitario=Decimal('36.80'),valor_total=Decimal('110.40'),unidade='PCT')
        self.second=ItemCompra.objects.create(compra=self.compra,produto=self.other,quantidade=7,preco_unitario=Decimal('47.40'),valor_total=Decimal('331.80'),unidade='PCT')
        self.movement=MovimentoFinanceiro.objects.create(compra=self.compra,conta=self.bank,tipo='saida',valor=Decimal('442.20'),data=self.compra.data_compra,origem='compra_a_vista')
        self.url=reverse('estoque:compra_editar',args=[self.compra.pk])
    def mode(self,tab,tipo='erro_lancamento'):
        tab.evaluate(f'document.getElementById("painelCorrecaoAutomatica").open=true;document.getElementById("tipoCorrecaoCompra").value={tipo!r};document.getElementById("tipoCorrecaoCompra").dispatchEvent(new Event("change",{{bubbles:true}}));document.getElementById("motivoEdicaoCompra").value="Conferencia da nota ficticia e mercadoria recebida";true')
        tab.evaluate('document.getElementById("valorPagoRealCorrecao").value="442,20";document.querySelector("[name=pagamento_real_confirmado]").checked=true;document.querySelector("[name=credito_real_confirmado]").checked=true;true')
    def test_imperio_preview_cancel_confirm_double_click_and_reopen_all_widths(self):
        report=[]
        with TemporaryDirectory(prefix='correcao-auto-browser-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for n,width in enumerate([1280,1440,320,360,390]):
                    if n:self.fresh_purchase()
                    tab=self.open(browser,width)
                    self.shot(tab,f'formulario-{width}')
                    if width<820:tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    if width<820:tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    self.mode(tab)
                    before=estado_compra(self.compra);original=list(MovimentoFinanceiro.objects.filter(compra=self.compra).values());stock=Produto.objects.get(pk=self.correct.pk).quantidade
                    tab.evaluate('document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('document.getElementById("previaCorrecaoCompra").open')
                    self.assertEqual(estado_compra(self.compra),before)
                    self.assertIn('438,00',tab.evaluate('document.getElementById("resumoPreviaCorrecaoCompra").textContent'))
                    self.assertIn('4,20',tab.evaluate('document.getElementById("resumoPreviaCorrecaoCompra").textContent'))
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    self.shot(tab,f'previa-imperio-{width}')
                    tab.evaluate('document.getElementById("cancelarPreviaCorrecaoCompra").click();true')
                    tab.wait('!document.getElementById("btnSalvarCompra").disabled')
                    self.assertEqual(estado_compra(self.compra),before)
                    self.assertIn('Ultra',tab.evaluate('busca.value'))
                    tab.evaluate('document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('document.getElementById("previaCorrecaoCompra").open')
                    tab.evaluate('document.getElementById("aplicarPreviaCorrecaoCompra").click();document.getElementById("aplicarPreviaCorrecaoCompra").click();true')
                    tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                    self.compra.refresh_from_db();self.item.refresh_from_db()
                    self.assertEqual(self.item.produto_id,self.correct.pk);self.assertEqual(self.compra.total,Decimal('438'))
                    self.assertEqual(list(MovimentoFinanceiro.objects.filter(compra=self.compra).values()),original)
                    self.assertEqual(self.compra.edicoes.count(),1)
                    self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,stock+3)
                    self.assertEqual(self.compra.conciliacao.credito_fornecedor,Decimal('4.20'))
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.shot(tab,f'salvo-imperio-{width}')
                    tab=self.open(browser,width)
                    self.assertEqual(tab.evaluate('document.querySelector("#tabelaItensCompra select").value'),str(self.correct.pk))
                    self.shot(tab,f'reaberto-imperio-{width}')
                    report.append({'width':width,'preview':'438.00','credit':'4.20','cash_preserved':True,'history_entries':1,'reopened':True})
            finally:browser.stop()
        (OUT/'chrome-imperio.json').write_text(json.dumps(report,indent=2))
    def test_recording_error_with_actual_payment_has_no_fictitious_credit(self):
        with TemporaryDirectory(prefix='correcao-registro-browser-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                for n,width in enumerate([1280,1440,320,360,390]):
                    if n:self.fresh_purchase()
                    tab=self.open(browser,width)
                    self.assertEqual(tab.evaluate('document.getElementById("valorPagoRealCorrecao").value'),'')
                    if width<820:tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    if width<820:tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    self.mode(tab)
                    tab.evaluate('document.getElementById("valorPagoRealCorrecao").value="438,00";document.querySelector("[name=credito_real_confirmado]").checked=false;document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('document.getElementById("previaCorrecaoCompra").open')
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    self.shot(tab,f'previa-registro-sem-credito-{width}')
                    tab.evaluate('document.getElementById("aplicarPreviaCorrecaoCompra").click();true')
                    tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                    self.movement.refresh_from_db()
                    self.assertEqual(self.movement.valor,Decimal('438.00'))
                    self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)
                    self.assertEqual(ConciliacaoCompra.objects.get(compra=self.compra).credito_fornecedor,Decimal('0'))
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.shot(tab,f'salvo-registro-sem-credito-{width}')
            finally:browser.stop()
    def test_physical_return_with_real_refund_in_mobile(self):
        with TemporaryDirectory(prefix='correcao-devolucao-browser-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                tab=self.open(browser,390)
                tab.evaluate(r'window.row=document.querySelector("#tabelaItensCompra tbody .linha-item");row.querySelector(".btn-editar-item-mobile").click();row.querySelector("[name=\"quantidade[]\"]").value="1";row.querySelector("[name=\"quantidade[]\"]").dispatchEvent(new Event("input",{bubbles:true}));row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                self.mode(tab,'devolucao')
                tab.evaluate(r'document.getElementById("valorDevolvidoRealCorrecao").value="50,00";document.getElementById("valorDevolvidoRealCorrecao").dispatchEvent(new Event("input",{bubbles:true}));document.querySelector("[name=conta_devolucao]").value="'+str(self.bank.pk)+r'";document.getElementById("btnSalvarCompra").click();true')
                tab.wait('document.getElementById("previaCorrecaoCompra").open')
                self.assertIn('23,60',tab.evaluate('document.getElementById("resumoPreviaCorrecaoCompra").textContent'))
                self.shot(tab,'previa-devolucao-390')
                tab.evaluate('document.getElementById("aplicarPreviaCorrecaoCompra").click();true')
                tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),2)
                self.assertEqual(ConciliacaoCompra.objects.get().credito_fornecedor,Decimal('23.60'))
                self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                self.shot(tab,'salvo-devolucao-390')
            finally:browser.stop()
    def test_preview_validation_and_apply_failure_preserve_form(self):
        with TemporaryDirectory(prefix='correcao-erro-browser-') as profile:
            browser=Chrome(CHROME,profile).start()
            try:
                tab=self.open(browser,1280)
                self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                self.mode(tab,'diferenca')
                tab.evaluate('document.getElementById("valorPagoRealCorrecao").value="438";document.getElementById("btnSalvarCompra").click();true')
                tab.wait('!document.getElementById("erroSalvarPreRevisaoCompra").hidden && !document.getElementById("btnSalvarCompra").disabled')
                self.assertIn('diverge',tab.evaluate('document.getElementById("erroSalvarPreRevisaoCompra").textContent'))
                self.assertIn('Ultra',tab.evaluate('busca.value'))
                tab.evaluate('document.getElementById("valorPagoRealCorrecao").value="442,20";document.getElementById("btnSalvarCompra").click();true')
                tab.wait('document.getElementById("previaCorrecaoCompra").open')
                before=self.snapshot()
                with patch.object(service,'etapa_correcao',side_effect=RuntimeError('Synthetic browser rollback')):
                    tab.evaluate('document.getElementById("aplicarPreviaCorrecaoCompra").click();true')
                    tab.wait('!document.getElementById("erroSalvarPreRevisaoCompra").hidden && !document.getElementById("btnSalvarCompra").disabled')
                self.assertEqual(self.snapshot(),before);self.assertIn('Ultra',tab.evaluate('busca.value'))
                self.assertIn('Nenhuma alteracao',tab.evaluate('document.getElementById("erroSalvarPreRevisaoCompra").textContent'))
                self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                self.shot(tab,'nao-salvo-rollback-1280')
            finally:browser.stop()
