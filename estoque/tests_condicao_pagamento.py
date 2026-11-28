"""Regressões de condição de venda; banco permitido somente PostgreSQL descartável."""
import json
from pathlib import Path
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from estoque.models import Cliente, ContaReceber, DespesaDiaria, ItemVenda, MovimentoFinanceiro, Venda
from estoque.services.vendas import criar_ou_atualizar_venda
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from inventario.tests import produto
from offline import tests_suspension_js as browser_tests


class CondicaoPagamentoJavaScriptTests(SimpleTestCase):
    def test_padrao_so_na_escolha_efetiva_preserva_manual_e_edicao(self):
        template = (Path(__file__).resolve().parent / 'templates/estoque/vendas_layout_teste.html').read_text(encoding='utf-8')

        def function(name):
            start = template.index('function ' + name + '(')
            opening = template.index('{', start)
            depth = 1
            end = opening + 1
            while depth:
                depth += (template[end] == '{') - (template[end] == '}')
                end += 1
            return template[start:end]

        functions = '\n'.join(function(name) for name in (
            'tipoPagamentoConsumoProprioVenda', 'vendaEdicaoConsumoProprio',
            'atualizarOpcoesConsumoProprio', 'garantirTipoVendaEdicaoConsumoProprio',
            'atualizarPrazoEVencimentoCliente', 'preencherResumoCliente', 'limparTipoPagamentoVenda'))
        setup = r"""
const tipoVenda=document.createElement('select');
for(const v of ['', '\u00c0 vista', 'A prazo']) tipoVenda.add(new Option(v,v));
const clienteId={value:''},clienteBusca={value:''},vencimentoVenda={value:''},clienteResumoPrazo={textContent:''};
const clientesConsumoProprioVenda=new Set(['6','10','1078','1086']);
let clienteSelecionado=null,vendaEdicaoVenda=null,clienteBuscaTimer=null,clienteBuscaAbortController=null;
let clienteResumoSuspenso=false,clienteIndexAtivo=-1;
const clienteSugestoesBox={style:{}};
const normalizarTextoCliente=s=>s.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
const usarVencimentoAutomaticoMobile=()=>false,calcularVencimentoCliente=()=> '2026-12-27';
function atualizarVencimentoAutomaticoMobile(){}
function guardarClienteSelecionado(){}
function atualizarCabecalhoCliente(){}
function atualizarHistoricoProdutoCliente(){}
function atualizarClienteSelecionadoDoServidor(){}
"""
        scenarios = r"""
const choose=id=>preencherResumoCliente({id,nome:'Nome pode mudar',prazo:0});
const expect=v=>{if(tipoVenda.value!==v)throw Error('Expected '+v+' got '+tipoVenda.value)};
expect('');atualizarOpcoesConsumoProprio();expect('');
choose(1126);expect('\u00c0 vista');choose(31);expect('A prazo');
tipoVenda.value='\u00c0 vista';tipoVenda.dispatchEvent(new Event('change'));
choose(31);atualizarPrazoEVencimentoCliente();atualizarOpcoesConsumoProprio();expect('\u00c0 vista');
choose(1126);tipoVenda.value='A prazo';choose(1126);expect('A prazo');
for(const id of [6,10,1078,1086]){
 choose(id);expect('consumo_proprio');
 choose(1124);expect('A prazo');
 if([...tipoVenda.options].some(o=>o.value==='consumo_proprio'))throw Error('Roselene permitted');
}
choose(31);tipoVenda.value='\u00c0 vista';
preencherResumoCliente({id:6,nome:'Teste'},false,true);expect('consumo_proprio');
clienteId.value='';atualizarOpcoesConsumoProprio();expect('');choose(6);expect('consumo_proprio');
clienteSelecionado=null;clienteId.value='';limparTipoPagamentoVenda();expect('');
vendaEdicaoVenda={id:123,tipo_pagamento:'A prazo'};tipoVenda.value='A prazo';
choose(6);limparTipoPagamentoVenda();expect('A prazo');
if([...tipoVenda.options].some(o=>o.value==='consumo_proprio'))throw Error('Commercial edit conversion');
vendaEdicaoVenda={id:124,tipo_pagamento:'consumo_proprio'};
choose(1124);expect('consumo_proprio');
if(!tipoVenda.disabled)throw Error('Historical consumption unlocked');
return true;
"""
        self.assertTrue(browser_tests.SuspensionJavaScriptTests().evaluate(setup + functions + scenarios))


class CondicaoPagamentoBackendTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('condicao-fixture', '', 'fixture-only')
        for pk, nome in ((6, 'Lincoln Neiva'), (10, 'Roseli da Costa Gama'),
                         (1078, 'Lorena Gama Neiva'), (1086, 'Camila Gama Neiva'),
                         (1124, 'Roselene da Costa Gama'), (1126, 'Venda A Vista Sta Bárbara'),
                         (31, 'Cliente comum fictício')):
            Cliente.objects.create(pk=pk, nome=nome)
        cls.product = produto(unidade_compra='UN', unidade_venda_1='UN',
                              vende_fracionado=False, quantidade=Decimal('50'))

    def payload(self, cliente, tipo):
        return dict(cliente_id=cliente, data_venda='2026-11-27', data_vencimento='2026-12-27',
                    tipo_pagamento=tipo, operador='Fixture', itens=[dict(
                        produto_id=self.product.pk, produto_nome=self.product.nome,
                        quantidade='1', unidade='UN', preco_unitario='10')])

    def state(self):
        self.product.refresh_from_db()
        return (self.product.quantidade, *[m.objects.count() for m in (
            Venda, ItemVenda, ContaReceber, MovimentoFinanceiro, DespesaDiaria)])

    def test_roselene_balcao_comum_e_sem_cliente_rejeitados_sem_efeitos(self):
        self.client.force_login(self.user)
        for cliente in (1124, 1126, 31, None):
            for spelling in ('consumo_proprio', 'Consumo próprio', 'CONSUMO PROPRIO'):
                with self.subTest(cliente=cliente, tipo=spelling):
                    before = self.state()
                    response = self.client.post('/vendas/gravar/',
                        json.dumps(self.payload(cliente, spelling)), content_type='application/json')
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertIn('quatro clientes', response.json()['mensagem'])
                    self.assertEqual(self.state(), before)

    def test_quatro_ids_criam_consumo_sem_recebimento_ou_divida(self):
        for cliente in (6, 10, 1078, 1086):
            with self.subTest(cliente=cliente):
                result = criar_ou_atualizar_venda(usuario=self.user,
                    dados=self.payload(cliente, 'consumo_proprio'))
                self.assertEqual(result.venda.cliente_id, cliente)
                self.assertEqual(result.venda.tipo_pagamento, 'consumo_proprio')
                self.assertFalse(ContaReceber.objects.filter(venda=result.venda).exists())
        self.assertEqual(Venda.objects.count(), 4)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_edicao_comercial_preserva_condicao_conta_e_saldo(self):
        result = criar_ou_atualizar_venda(usuario=self.user, dados=self.payload(31, 'A prazo'))
        sale = result.venda
        account = ContaReceber.objects.get(venda=sale)
        before = (account.pk, account.valor_original, account.valor_em_aberto, account.status)
        data = dict(self.payload(31, 'A prazo'), venda_id=str(sale.pk))
        edited = criar_ou_atualizar_venda(usuario=self.user, dados=data)
        account.refresh_from_db()
        self.assertEqual(edited.venda.pk, sale.pk)
        self.assertEqual(edited.venda.tipo_pagamento, 'A prazo')
        self.assertEqual((account.pk, account.valor_original, account.valor_em_aberto, account.status), before)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_edicao_a_vista_sem_mudancas_preserva_recebimentos_e_estoque(self):
        data = self.payload(1126, 'À vista')
        data['origem_recebimento'] = {'caixa': '10', 'banco': '0'}
        sale = criar_ou_atualizar_venda(usuario=self.user, dados=data).venda
        before = list(MovimentoFinanceiro.objects.order_by('pk').values())
        self.assertTrue(before)
        self.product.refresh_from_db()
        stock = self.product.quantidade
        data['venda_id'] = str(sale.pk)
        data['itens'][0]['item_id'] = str(sale.itens.get().pk)
        data.pop('origem_recebimento')  # A edição não reenvia um recebimento já confirmado.
        edited = criar_ou_atualizar_venda(usuario=self.user, dados=data)
        self.assertEqual(edited.venda.tipo_pagamento, 'À vista')
        self.assertEqual(list(MovimentoFinanceiro.objects.order_by('pk').values()), before)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantidade, stock)
