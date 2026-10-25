"""Single-save corrections on disposable PostgreSQL and real Chrome."""
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, Client
from django.db import connections
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from . import tests_correcao_automatica as automatic
from . import tests_compra_interface_finalizada as interface
from .models import ContaPagar, ContaFinanceira, PagamentoContaPagar, MovimentoFinanceiro, EdicaoCompra, Produto
from offline.browser_support import Chrome

OUT = Path(__file__).resolve().parent.parent / 'evidencias-correcao-simples'

class SimpleFixture(automatic.Fixture):
    def simple(self, **extra):
        data = self.data()
        data['acao_compra'] = 'salvar_correcao_simples'
        data['tipo_pagamento'] = self.compra.tipo_pagamento
        if self.compra.data_vencimento:
            data['data_vencimento'] = self.compra.data_vencimento.isoformat()
        data.update(extra)
        return data

    def save(self, data=None):
        return self.client.post(self.url, data or self.simple(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def paid(self, amount='400'):
        self.movement.delete()
        self.compra.tipo_pagamento = 'aprazo'
        self.compra.data_vencimento = date(2026,11,24)
        self.compra.save()
        conta = ContaPagar.objects.create(compra=self.compra, fornecedor=self.fornecedor,
            data_emissao=self.compra.data_compra, valor_original=Decimal('442.20'),
            valor_em_aberto=Decimal('442.20')-Decimal(amount), status='parcial')
        payment = PagamentoContaPagar.objects.create(conta=conta, data_pagamento=self.compra.data_compra, valor=amount)
        MovimentoFinanceiro.objects.create(pagamento_conta_pagar=payment, conta=self.bank,
            tipo='saida', valor=amount, data=self.compra.data_compra, origem='pagamento_conta_pagar')
        return conta, payment

class Backend(SimpleFixture, TestCase):
    def test_single_save_retifies_automatic_posting_without_credit_or_refund(self):
        data = self.simple()
        self.assertEqual(self.save(data).status_code, 200)
        self.compra.refresh_from_db(); self.item.refresh_from_db(); self.movement.refresh_from_db()
        self.assertEqual(self.compra.total, Decimal('438'))
        self.assertEqual(self.movement.valor, Decimal('438'))
        self.assertEqual(MovimentoFinanceiro.objects.count(), 1)
        self.assertEqual(self.item.produto_id, self.correct.pk)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, Decimal('17'))
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade, Decimal('23'))
        self.assertEqual(self.compra.conciliacao.credito_fornecedor, 0)
        self.assertEqual(self.compra.conciliacao.valor_devolvido, 0)
        audit = EdicaoCompra.objects.get()
        self.assertEqual(audit.motivo, data['motivo_edicao'])
        self.assertEqual(audit.antes['compra']['movimentos'][0]['valor'], '442.20')
        self.assertFalse(audit.depois['ajustes']['pagamento_real_confirmado'])
        self.assertEqual(self.save(data).status_code, 200)
        self.assertEqual(EdicaoCompra.objects.count(), 1)

    def test_positive_price_delta_and_second_correction(self):
        self.assertEqual(self.save(self.simple(**{'preco_unitario[]':['40','47,40']})).status_code,200)
        self.movement.refresh_from_db(); self.assertEqual(self.movement.valor,Decimal('451.80'))
        self.assertEqual(self.save().status_code,200)
        self.movement.refresh_from_db(); self.assertEqual(self.movement.valor,Decimal('438'))
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,Decimal('23'))

    def test_quantity_delta_and_historical_adjustment_are_preserved(self):
        self.compra.ajuste_total=Decimal('10');self.compra.total=Decimal('452.20');self.compra.save()
        self.movement.valor=Decimal('452.20');self.movement.save()
        response=self.save(self.simple(**{'quantidade[]':['4','7']}))
        self.assertEqual(response.status_code,200,response.content)
        self.compra.refresh_from_db();self.movement.refresh_from_db()
        self.assertEqual(self.compra.total_produtos,Decimal('473.40'))
        self.assertEqual(self.compra.total,Decimal('483.40'))
        self.assertEqual(self.compra.ajuste_total,10)
        self.assertEqual(self.movement.valor,Decimal('483.40'))
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,24)

    def test_negative_stock_blocks_without_partial_save(self):
        self.produto.quantidade=1;self.produto.save();data=self.simple();before=self.snapshot()
        self.assertEqual(self.save(data).status_code,422);self.assertEqual(self.snapshot(),before)

    def test_financial_permission_required_for_automatic_rectification(self):
        operator=get_user_model().objects.create_user('operator',password='test')
        operator.user_permissions.add(Permission.objects.get(codename='change_compra'))
        self.client.force_login(operator);data=self.simple();before=self.snapshot()
        self.assertEqual(self.save(data).status_code,422);self.assertEqual(self.snapshot(),before)

    def test_equal_total_substitution_and_later_price_edit_remain_correctable(self):
        self.assertEqual(self.save(self.simple(**{'preco_unitario[]':['36,80','47,40']})).status_code,200)
        self.assertEqual(self.save().status_code,200)
        self.movement.refresh_from_db();self.assertEqual(self.movement.valor,438)

    def test_proved_payment_preserved_and_open_balance_adjusted_both_directions(self):
        conta, payment = self.paid()
        for price, expected in [('35,40','38'),('40','51.80')]:
            data=self.simple(**{'preco_unitario[]':[price,'47,40']});data['tipo_pagamento']='aprazo'
            response=self.save(data);self.assertEqual(response.status_code,200,response.content)
            conta.refresh_from_db();payment.refresh_from_db()
            self.assertEqual(conta.valor_em_aberto,Decimal(expected));self.assertEqual(payment.valor,400)
            self.assertEqual(MovimentoFinanceiro.objects.get().valor,400)

    def test_proved_overpayment_blocks_without_inventing_credit(self):
        self.paid('442.20');data=self.simple();data['tipo_pagamento']='aprazo';before=self.snapshot()
        response=self.save(data);self.assertEqual(response.status_code,422,response.content)
        self.assertIn('pagamento comprovado',response.json()['erro']);self.assertEqual(self.snapshot(),before)

    def test_ambiguous_split_postings_block_without_partial_save(self):
        self.movement.valor=200;self.movement.save()
        other_bank=ContaFinanceira.objects.create(nome='Outra origem',tipo='banco')
        MovimentoFinanceiro.objects.create(compra=self.compra,conta=other_bank,tipo='saida',
            valor=Decimal('242.20'),data=self.compra.data_compra,origem='compra_a_vista')
        data=self.simple();before=self.snapshot();self.assertEqual(self.save(data).status_code,422)
        self.assertEqual(self.snapshot(),before)

    def test_stale_token_and_missing_justification_block(self):
        data=self.simple();data['motivo_edicao']='';before=self.snapshot()
        self.assertEqual(self.save(data).status_code,422);self.assertEqual(self.snapshot(),before)
        data=self.simple();self.movement.valor=441;self.movement.save();before=self.snapshot()
        self.assertEqual(self.save(data).status_code,422);self.assertEqual(self.snapshot(),before)

    def test_all_stages_rollback(self):
        for stage in ['estoque','itens','financeiro','auditoria']:
            data=self.simple();before=self.snapshot()
            def fail(name):
                if name==stage: raise RuntimeError('synthetic rollback')
            with patch('estoque.services.correcao_compra.etapa_correcao',side_effect=fail):
                self.assertEqual(self.save(data).status_code,500)
            self.assertEqual(self.snapshot(),before)

class Concurrency(SimpleFixture, TransactionTestCase):
    def parallel(self, same):
        first=self.simple();second=first if same else self.simple()
        barrier=threading.Barrier(2)
        def run(data):
            connections.close_all()
            client=Client();client.force_login(self.user);barrier.wait(timeout=15)
            response=client.post(self.url,data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            connections.close_all()
            return response.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,[first,second]))
        self.assertEqual(sorted(results),[200,200] if same else [200,422])
        self.assertEqual(EdicaoCompra.objects.count(),1)
        self.assertEqual(Produto.objects.get(pk=self.correct.pk).quantidade,23)

    def test_duplicate_requests(self): self.parallel(True)
    def test_competing_edits(self): self.parallel(False)

class Browser(SimpleFixture, StaticLiveServerTestCase):
    open = interface.Browser.open
    select = interface.Browser.select
    fresh_purchase = automatic.Browser.fresh_purchase

    def shot(self, tab, name):
        data=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
        (OUT/(name+'.png')).write_bytes(base64.b64decode(data))

    def test_edit_and_save_desktop_mobile(self):
        OUT.mkdir(exist_ok=True)
        evidence=[]
        with TemporaryDirectory(prefix='simple-purchase-chrome-') as profile:
            browser=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                for n,width in enumerate([1280,1440,320,360,390]):
                    if n: self.fresh_purchase()
                    tab=self.open(browser,width)
                    self.assertFalse(tab.evaluate('!!document.querySelector("[name=valor_pago_real], [name=tipo_correcao]")'))
                    # Exercise the existing row editor then save with a previously filled justification.
                    tab.evaluate('document.getElementById("motivoEdicaoCompra").value="Produto errado na nota";true')
                    if width<820: tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    if width<820: tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    self.assertEqual(tab.evaluate('document.getElementById("motivoEdicaoCompra").value'),'Produto errado na nota')
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    self.shot(tab,f'corrigir-{width}')
                    if n==0:
                        before=self.snapshot()
                        with patch('estoque.services.correcao_compra.etapa_correcao',side_effect=RuntimeError('browser rollback')):
                            tab.evaluate('document.getElementById("btnSalvarCompra").click();true')
                            tab.wait('!document.getElementById("erroSalvarPreRevisaoCompra").hidden && !document.getElementById("btnSalvarCompra").disabled')
                        self.assertEqual(self.snapshot(),before)
                        self.assertEqual(tab.evaluate('document.getElementById("motivoEdicaoCompra").value'),'Produto errado na nota')
                        self.shot(tab,'erro-preserva-justificativa')
                    tab.evaluate('document.getElementById("btnSalvarCompra").click();document.getElementById("btnSalvarCompra").click();true')
                    tab.wait('location.pathname.includes("/compras/") && !document.getElementById("formCompra") && document.readyState==="complete"')
                    self.compra.refresh_from_db();self.movement.refresh_from_db()
                    self.assertEqual(self.compra.total,Decimal('438'))
                    self.assertEqual(self.movement.valor,Decimal('438'))
                    self.assertEqual(self.compra.edicoes.count(),1)
                    self.assertEqual(self.compra.conciliacao.credito_fornecedor,0)
                    self.assertIn('Alterações salvas',tab.evaluate('document.body.innerText'))
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    self.shot(tab,f'salvo-{width}')
                    tab=self.open(browser,width)
                    self.assertEqual(tab.evaluate('document.getElementById("motivoEdicaoCompra").value'),'Produto errado na nota')
                    evidence.append({'width':width,'total_before':'442.20','total_after':'438.00','delta':'-4.20','credit':'0','refund':'0','one_save':True})
            finally:
                browser.stop()
        (OUT/'browser.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
