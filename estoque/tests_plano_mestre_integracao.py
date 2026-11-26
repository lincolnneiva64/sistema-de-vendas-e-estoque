"""Contratos operacionais atuais: somente PostgreSQL descartável, dados fictícios."""
import json
import struct
from decimal import Decimal as D
from unittest.mock import Mock
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from offline.browser_support import DevTools
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from estoque.models import Cliente, Produto, Venda, ItemVenda, ContaReceber, RecebimentoContaReceber, OperacaoRecebimentoCliente, MovimentoFinanceiro


class TransporteVisualTests(SimpleTestCase):
    def test_frames_pequenos_medios_e_maiores_que_64kb(self):
        for tamanho in (10,130,70000):
            with self.subTest(tamanho=tamanho):
                tab=object.__new__(DevTools)
                tab.sequence=0;tab.socket=Mock()
                resposta=json.dumps({'id':1,'result':{'ok':True}}).encode()
                tab.exact=Mock(side_effect=[bytes([0x81,len(resposta)]),resposta])
                self.assertEqual(tab.call('Runtime.evaluate',{'expression':'x'*tamanho}),{'ok':True})
                frame=tab.socket.sendall.call_args.args[0]
                tipo=frame[1]&127
                if tipo<126: comprimento,offset=tipo,2
                elif tipo==126: comprimento,offset=struct.unpack('!H',frame[2:4])[0],4
                else: comprimento,offset=struct.unpack('!Q',frame[2:10])[0],10
                mascara=frame[offset:offset+4];corpo=frame[offset+4:]
                self.assertEqual(len(corpo),comprimento)
                dados=json.loads(bytes(b^mascara[i%4] for i,b in enumerate(corpo)))
                self.assertEqual(dados['params']['expression'],'x'*tamanho)


class ContratosOperacionaisTests(PostgreSQLIsoladoTests,TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_superuser('integracao-ficticia','','teste')
        self.client.force_login(self.admin)
        self.cliente=Cliente.objects.create(nome='Cliente fictício de integração')
        self.produto=Produto.objects.create(nome='Produto fictício de integração',quantidade=10,
            preco_compra=5,preco_vista=10,preco_prazo=10,unidade_compra='UN',unidade_venda_1='UN')

    def venda(self):
        dados=dict(cliente_id=self.cliente.pk,data_venda=timezone.localdate().isoformat(),
            data_vencimento=timezone.localdate().isoformat(),tipo_pagamento='A prazo',operador='Fictício',total='99999',
            itens=[dict(produto_id=self.produto.pk,produto_nome=self.produto.nome,quantidade='2',unidade='UN',preco_unitario='10',valor_total='99999')])
        resposta=self.client.post(reverse('estoque:gravar_venda'),json.dumps(dados),content_type='application/json')
        self.assertEqual(resposta.status_code,200,resposta.content)
        return Venda.objects.get(pk=resposta.json()['venda_id']),dados

    def test_venda_edicao_movimenta_somente_delta_e_nao_duplica_conta(self):
        venda,dados=self.venda()
        self.assertEqual(venda.total,D('20'))
        self.produto.refresh_from_db();self.assertEqual(self.produto.quantidade,D('8'))
        dados['venda_id']=venda.pk;dados['itens'][0].update(item_id=ItemVenda.objects.get(venda=venda).pk,quantidade='3')
        resposta=self.client.post(reverse('estoque:gravar_venda'),json.dumps(dados),content_type='application/json')
        self.assertEqual(resposta.status_code,200,resposta.content)
        self.produto.refresh_from_db();venda.refresh_from_db()
        self.assertEqual((self.produto.quantidade,venda.total),(D('7'),D('30')))
        self.assertEqual((Venda.objects.count(),ItemVenda.objects.count(),ContaReceber.objects.count()),(1,1,1))
        self.assertEqual(ContaReceber.objects.get().valor_original,D('30'))

    def test_recebimento_parcial_preserva_venda_e_estoque(self):
        venda,_=self.venda()
        conta=ContaReceber.objects.get(venda=venda)
        produto_antes=Produto.objects.filter(pk=self.produto.pk).values().get()
        nota_antes=Venda.objects.filter(pk=venda.pk).values().get()
        r=self.client.post(reverse('estoque:receber_cliente',kwargs={'cliente_id':self.cliente.pk}),
            dict(data_recebimento=timezone.localdate().isoformat(),valor='12,00',forma_pagamento='PIX',destino_diferenca='troco'))
        self.assertEqual(r.status_code,302,r.content)
        conta.refresh_from_db()
        self.assertEqual(conta.valor_em_aberto,D('8'))
        recebimento=RecebimentoContaReceber.objects.get(conta=conta)
        self.assertEqual(recebimento.valor,D('12'))
        self.assertEqual(OperacaoRecebimentoCliente.objects.get(pk=recebimento.operacao_id).valor_aplicado,D('12'))
        self.assertEqual(Produto.objects.filter(pk=self.produto.pk).values().get(),produto_antes)
        self.assertEqual(Venda.objects.filter(pk=venda.pk).values().get(),nota_antes)
        self.assertEqual(MovimentoFinanceiro.objects.filter(valor=D('12')).count(),1)

    def test_relatorios_nao_alteram_dados_operacionais(self):
        self.venda()
        modelos=(Produto,Venda,ItemVenda,ContaReceber,RecebimentoContaReceber,OperacaoRecebimentoCliente,MovimentoFinanceiro)
        def estado():return {m.__name__:list(m.objects.order_by('pk').values()) for m in modelos}
        antes=estado()
        params=dict(periodo='personalizado',data_inicial=timezone.localdate().isoformat(),data_final=timezone.localdate().isoformat())
        for url,extras in ((reverse('estoque:relatorios_vendas'),{}),(reverse('estoque:relatorios_produtos'),{}),
                           (reverse('estoque:relatorios_produtos'),dict(investigar='classes',ordenar_por='atual'))):
            self.assertEqual(self.client.get(url,{**params,**extras}).status_code,200)
            self.assertEqual(estado(),antes)
