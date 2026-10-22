import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from offline.browser_support import Chrome
from inventario.tests_browser import CHROME
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse
from estoque.models import Cliente, Produto, ItemVenda, Venda, ContaReceber, EventoVenda, MovimentoFinanceiro
from estoque.services import quantidade_venda as q
from estoque.services.vendas import criar_ou_atualizar_venda, ErroGravarVenda
from inventario.models import MovimentoObservado


class Fixture:
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('admin-sintetico', '', 'teste')
        self.client.force_login(self.user)
        self.cliente = Cliente.objects.create(nome='Cliente sintetico')
        self.produto = Produto.objects.create(nome='Cantina sintetica', quantidade=2, preco_compra=12,
            preco_vista=120, preco_prazo=120, unidade_compra='CX', unidade_venda_1='CX',
            unidade_venda_2='UN', vende_fracionado=True, fator_conversao=12)
        self.dados = dict(cliente_id=self.cliente.pk, data_venda='2026-10-22', data_vencimento='2026-11-22',
            tipo_pagamento='A prazo', operador='Operador original', itens=[dict(produto_id=self.produto.pk,
            produto_nome=self.produto.nome, quantidade='1', unidade='CX', preco_unitario='120')])
        self.venda = criar_ou_atualizar_venda(usuario=self.user, dados=self.dados).venda
        self.item = self.venda.itens.get()
        self.url = reverse('estoque:venda_editar_quantidade_item', args=[self.venda.pk, self.item.pk])

    def token(self, qtd, unidade):
        self.venda.refresh_from_db(); self.item.refresh_from_db()
        return q.previa(self.user, self.venda, self.item, qtd, unidade)[-1]

    def editar(self, qtd, unidade='UN', token=None):
        return q.confirmar(self.user, self.venda.pk, self.item.pk, token or self.token(qtd, unidade), qtd, unidade)

    def saldo(self):
        self.produto.refresh_from_db()
        return self.produto.quantidade

    def estado(self):
        return [list(model.objects.order_by('pk').values()) for model in
            (Produto, Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, EventoVenda, MovimentoObservado)]


class QuantidadeVendaTests(Fixture, TestCase):
    def test_cantina_sequencia_relata_2un_sem_reparar_historico(self):
        # Second recorded box: stock becomes zero. Two physically sold units were never recorded.
        segunda = criar_ou_atualizar_venda(usuario=self.user, dados=copy.deepcopy(self.dados)).venda
        self.venda = segunda; self.item = segunda.itens.get()
        self.assertEqual(self.saldo(), 0)
        self.editar('10')
        self.assertEqual(self.saldo(), Decimal('.167'))
        self.assertEqual(self.venda.itens.get().unidade, 'UN')
        self.assertEqual(ContaReceber.objects.get(venda=self.venda).valor_original, 100)
        self.assertEqual(ItemVenda.objects.count(), 2)

    def test_reducao_aumento_mesma_unidade_financeiro_e_snapshots(self):
        self.editar('10')
        self.assertEqual(self.saldo(), Decimal('1.167'))
        self.editar('8')
        self.assertEqual(self.saldo(), Decimal('1.334'))
        self.editar('11')
        self.assertEqual(self.saldo(), Decimal('1.084'))
        self.item.refresh_from_db(); self.venda.refresh_from_db()
        self.assertEqual(self.item.preco_unitario, 10)
        self.assertEqual(self.item.valor_total, 110)
        self.assertEqual(self.venda.total, 110)
        conta = ContaReceber.objects.get(venda=self.venda)
        self.assertEqual((conta.valor_original, conta.valor_em_aberto), (110, 110))
        self.assertEqual(self.item.estoque_depois, self.saldo())
        self.assertEqual(self.item.custo_total_snapshot, 11)
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_unidades_cadastradas_cx_pct_dz_respeitam_fator(self):
        for base, fator in [('CX',12), ('PCT',24), ('DZ',12)]:
            with self.subTest(base=base):
                Produto.objects.filter(pk=self.produto.pk).update(unidade_compra=base, unidade_venda_1=base, fator_conversao=fator, quantidade=2)
                ItemVenda.objects.filter(pk=self.item.pk).update(unidade=base, quantidade=1, preco_unitario=120)
                self.editar(str(fator//2))
                self.assertEqual(self.saldo(), Decimal('2.500'))
                self.item.refresh_from_db()
                self.assertEqual(self.item.preco_unitario, Decimal(120)/fator)
                self.editar('1', base)
                self.assertEqual(self.saldo(), Decimal('2.000'))

    def test_duplicado_sem_movimentos_eventos_ou_financeiro_repetido(self):
        token = self.token('10','UN')
        self.editar('10',token=token)
        estado = self.estado()
        self.assertFalse(self.editar('10',token=token)[2])
        self.assertEqual(self.estado(), estado)
        # Retry after a newer correction must not overwrite it.
        self.editar('8')
        estado = self.estado()
        with self.assertRaises(ValueError): self.editar('10',token=token)
        self.assertEqual(estado, self.estado())

    def test_quantidades_invalidas_e_unidade_invalida_sem_efeitos(self):
        estado = self.estado()
        for qtd, un in [('0','UN'),('-1','UN'),('NaN','UN'),('Infinity','UN'),('1.1','UN'),('1.0001','UN'),('abc','UN'),('1','KG')]:
            with self.subTest(qtd=qtd, un=un):
                with self.assertRaises(ValueError): self.editar(qtd,un)
                self.assertEqual(estado,self.estado())

    def test_sem_estoque_aumento_rejeitado_atomicamente(self):
        estado = self.estado()
        with self.assertRaises((ValueError, ErroGravarVenda)): self.editar('30')
        self.assertEqual(estado,self.estado())

    def test_rollback_se_financeiro_ou_separacao_falhar(self):
        for helper in ['_sincronizar_conta_receber','_recalcular_status_separacao_da_venda']:
            estado = self.estado()
            with patch('estoque.views.'+helper, side_effect=RuntimeError('Falha sintetica')):
                with self.assertRaises(RuntimeError): self.editar('10')
            self.assertEqual(estado,self.estado())

    def test_cancelada_quitada_recebimentos_bloqueiam(self):
        for campo, valor in [('cancelada',True),('tipo_pagamento','A vista')]:
            token = self.token('10','UN')
            Venda.objects.filter(pk=self.venda.pk).update(**{campo:valor})
            estado = self.estado()
            with self.assertRaises(ValueError): self.editar('10',token=token)
            self.assertEqual(estado,self.estado())
            Venda.objects.filter(pk=self.venda.pk).update(cancelada=False,tipo_pagamento='A prazo')
        token = self.token('10','UN')
        ContaReceber.objects.filter(venda=self.venda).update(status=ContaReceber.STATUS_PAGA,valor_em_aberto=0)
        estado = self.estado()
        with self.assertRaises(ValueError): self.editar('10',token=token)
        self.assertEqual(estado,self.estado())

    def test_token_vinculado_a_usuario_item_e_valores(self):
        token = self.token('10','UN'); estado = self.estado()
        for qtd, un, tk in [('9','UN',token),('10','CX',token),('10','UN',token+'x')]:
            with self.assertRaises(ValueError): self.editar(qtd,un,tk)
        outro = get_user_model().objects.create_superuser('outro', '', 'teste')
        with self.assertRaises(ValueError): q.confirmar(outro,self.venda.pk,self.item.pk,token,'10','UN')
        self.assertEqual(estado,self.estado())

    def test_edicao_completa_preservada_e_token_antigo_recusado(self):
        token = self.token('10','UN')
        dados = copy.deepcopy(self.dados)
        dados.update(venda_id=self.venda.pk)
        dados['itens'][0].update(item_id=self.item.pk,quantidade='6',unidade='UN',preco_unitario='10')
        criar_ou_atualizar_venda(usuario=self.user,dados=dados)
        self.assertEqual(self.saldo(), Decimal('1.500'))
        estado = self.estado()
        with self.assertRaises(ValueError): self.editar('10',token=token)
        self.assertEqual(estado,self.estado())

    def test_http_previa_csrf_e_confirmacao(self):
        estado = self.estado()
        previa = self.client.get(self.url,{'nova_quantidade':'10','nova_unidade':'UN'})
        self.assertEqual(previa.status_code,200)
        self.assertEqual(previa.context['preco_preview'],10)
        self.assertEqual(estado,self.estado())
        payload = dict(nova_quantidade='10',nova_unidade='UN',autorizacao_quantidade=previa.context['autorizacao_quantidade'])
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url,payload).status_code,403)
        self.assertEqual(self.client.post(self.url,payload).status_code,302)
        self.assertEqual(self.saldo(),Decimal('1.167'))
        estado = self.estado()
        self.client.post(self.url,payload)
        self.assertEqual(estado,self.estado())

    def test_previa_expirada_cadastro_alterado_e_post_sem_token(self):
        token = self.token('10','UN')
        estado = self.estado()
        with patch('django.core.signing.time.time', return_value=9999999999):
            with self.assertRaises(ValueError): self.editar('10',token=token)
        self.client.post(self.url,{'nova_quantidade':'10','nova_unidade':'UN'})
        self.assertEqual(estado,self.estado())
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=24)
        estado = self.estado()
        with self.assertRaises(ValueError): self.editar('10',token=token)
        self.assertEqual(estado,self.estado())

    def test_outro_item_e_financeiro_nao_sao_recriados(self):
        outro = Produto.objects.create(nome='Outro produto sintetico',quantidade=10,unidade_compra='UN',unidade_venda_1='UN',preco_compra=1,preco_vista=3,preco_prazo=3)
        ItemVenda.objects.create(venda=self.venda,produto=outro,quantidade=2,unidade='UN',preco_unitario=3,valor_total=6)
        Venda.objects.filter(pk=self.venda.pk).update(total=126)
        original = list(ItemVenda.objects.exclude(pk=self.item.pk).values())
        self.editar('10')
        self.assertEqual(original,list(ItemVenda.objects.exclude(pk=self.item.pk).values()))
        self.assertEqual(Produto.objects.get(pk=outro.pk).quantidade,10)
        self.assertEqual(ContaReceber.objects.get(venda=self.venda).valor_original,106)
        evento = EventoVenda.objects.get(venda=self.venda,tipo_evento='quantidade_item_confirmada')
        self.assertEqual(evento.usuario,self.user.username)
        self.assertFalse(MovimentoObservado.objects.filter(produto=self.produto,referencia='').exists())

    def test_kg_quantidade_continua_permitida(self):
        Produto.objects.filter(pk=self.produto.pk).update(unidade_compra='KG',unidade_venda_1='KG',vende_fracionado=False)
        ItemVenda.objects.filter(pk=self.item.pk).update(unidade='KG')
        self.editar('1.125','KG')
        self.assertEqual(self.saldo(),Decimal('.875'))


class QuantidadeVendaConcorrenciaTests(Fixture, TransactionTestCase):
    def disputar(self, alvos):
        token = self.token('10','UN')
        token2 = token if alvos[1]=='10' else self.token(alvos[1],'UN')
        barreira = Barrier(2)
        def executar(args):
            qtd, tk = args
            try:
                usuario=get_user_model().objects.get(pk=self.user.pk)
                barreira.wait(timeout=15)
                try:
                    q.confirmar(usuario,self.venda.pk,self.item.pk,tk,qtd,'UN')
                    return 'ok'
                except ValueError: return 'conflito'
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(executar,[(alvos[0],token),(alvos[1],token2)]))

    def test_duplo_envio_simultaneo_aplica_uma_vez(self):
        self.assertEqual(self.disputar(['10','10']),['ok','ok'])
        self.assertEqual(self.saldo(), Decimal('1.167'))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto,origem='devolucao_venda').count(),1)

    def test_edicoes_diferentes_simultaneas_nao_sobrescrevem(self):
        self.assertCountEqual(self.disputar(['10','8']),['ok','conflito'])
        self.assertIn(self.saldo(),[Decimal('1.167'),Decimal('1.333')])


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponivel')
class QuantidadeVendaBrowserTests(Fixture, StaticLiveServerTestCase):
    def test_troca_unidade_previa_confirmacao_real_csrf(self):
        with TemporaryDirectory(prefix='quantidade-venda-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', {'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                tab.wait("document.querySelector('select[name=nova_unidade]')!==null")
                self.assertEqual(self.saldo(),1)
                tab.evaluate("document.querySelector('[name=nova_quantidade]').value='10';document.querySelector('[name=nova_unidade]').value='UN';document.querySelector('form[method=get]').submit();true")
                tab.wait("document.querySelector('[name=autorizacao_quantidade]')!==null")
                self.assertEqual(self.saldo(),1)
                self.assertEqual(tab.evaluate("document.querySelector('[name=autorizacao_quantidade]').form.querySelector('[name=nova_unidade]').value"),'UN')
                tab.evaluate("document.querySelector('[data-qty-action=confirmar]').click();true")
                tab.wait("location.pathname.endsWith('/editar/')")
                self.assertEqual(self.saldo(),Decimal('1.167'))
                self.item.refresh_from_db()
                self.assertEqual((self.item.quantidade,self.item.unidade,self.item.preco_unitario),(10,'UN',10))
            finally:
                chrome.stop()
