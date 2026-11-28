"""Validacao direcionada sem acesso a banco ou escrita comercial."""
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from estoque.consumo_proprio import CLIENTES_CONSUMO_PROPRIO_IDS
from estoque.models import Cliente, Venda, Produto
from estoque.services.vendas import criar_ou_atualizar_venda, ErroGravarVenda
from offline.tests_suspension_js import SuspensionJavaScriptTests

ROOT = Path(__file__).resolve().parents[1]

class ConsumoRestritoTests(SimpleTestCase):
    def test_rejeita_consumo_na_criacao_e_edicao_antes_de_processar_itens(self):
        for venda_id in (None, '1496'):
            for cliente_id in (None, 1124, 1126, 1145, 99999):
                with self.subTest(venda=venda_id, cliente=cliente_id), patch.object(Cliente.objects, 'filter') as clientes, patch.object(Venda.objects, 'filter') as vendas, patch.object(Produto.objects, 'filter') as produtos:
                    clientes.return_value.first.return_value = Cliente(pk=cliente_id)
                    antiga = Venda(pk=1496, cliente_id=1145, tipo_pagamento='consumo_proprio')
                    vendas.return_value.first.return_value = antiga
                    with self.assertRaisesMessage(ErroGravarVenda, 'quatro clientes'):
                        criar_ou_atualizar_venda(usuario=None, dados={
                            'cliente_id':cliente_id, 'venda_id':venda_id,
                            'data_venda':'2026-11-25', 'tipo_pagamento':'consumo_proprio',
                            'itens':[{'produto_nome':'Teste'}]})
                    produtos.assert_not_called()
                    self.assertEqual(antiga.cliente_id, 1145)
                    self.assertEqual(antiga.tipo_pagamento, 'consumo_proprio')

    def test_quatro_ids_permitidos_e_nomes_nao_concedem_acesso(self):
        self.assertEqual(CLIENTES_CONSUMO_PROPRIO_IDS, {6, 10, 1078, 1086})
        for pk in CLIENTES_CONSUMO_PROPRIO_IDS:
            with patch.object(Cliente.objects, 'filter') as clientes:
                clientes.return_value.first.return_value = Cliente(pk=pk, nome='Nome atualizado')
                with self.assertRaisesMessage(ErroGravarVenda, 'Revise quantidade e preco'):
                    criar_ou_atualizar_venda(usuario=None, dados={
                        'cliente_id':pk, 'data_venda':'2026-11-25',
                        'tipo_pagamento':'consumo_proprio', 'itens':[{'produto_nome':'Teste'}]})

    def test_troca_cliente_remove_opcao_e_preserva_consumo_historico_bloqueado(self):
        template = (ROOT / 'estoque/templates/estoque/vendas_layout_teste.html').read_text(encoding='utf-8')
        function = template.split('function atualizarOpcoesConsumoProprio() {',1)[1].split('function garantirTipoVendaEdicaoConsumoProprio()',1)[0]
        code = r"""
const tipoVenda = document.createElement('select');
for (const value of ['','\u00c0 vista','A prazo']) {const o=document.createElement('option');o.value=o.textContent=value;tipoVenda.appendChild(o)}
const clienteId={value:''}, clientesConsumoProprioVenda=new Set(['6','10','1078','1086']);
let vendaEdicaoVenda=null;
function vendaEdicaoConsumoProprio(){return vendaEdicaoVenda?.tipo_pagamento==='consumo_proprio'}
function atualizarOpcoesConsumoProprio(){FUNCTION
for(const id of ['6','10','1078','1086']) {
 clienteId.value=id;atualizarOpcoesConsumoProprio();
 if(tipoVenda.options.length!==4)throw Error('family option missing');
 tipoVenda.value='consumo_proprio';clienteId.value='1124';atualizarOpcoesConsumoProprio();
 if(tipoVenda.options.length!==3||tipoVenda.value!=='')throw Error('unauthorized selection remains');
}
clienteId.value='';atualizarOpcoesConsumoProprio();if(tipoVenda.options.length!==3||tipoVenda.value!=='')throw Error('anonymous option');
vendaEdicaoVenda={id:1497,tipo_pagamento:'A prazo'};clienteId.value='6';atualizarOpcoesConsumoProprio();
if(tipoVenda.options.length!==3)throw Error('normal sale conversion unlocked');
const o=document.createElement('option');o.value='consumo_proprio';tipoVenda.appendChild(o);tipoVenda.value='consumo_proprio';tipoVenda.disabled=true;
vendaEdicaoVenda={id:1496,tipo_pagamento:'consumo_proprio'};clienteId.value='1145';atualizarOpcoesConsumoProprio();
if(tipoVenda.value!=='consumo_proprio'||!tipoVenda.disabled)throw Error('historical sale changed');
return true;
""".replace('FUNCTION',function)
        self.assertTrue(SuspensionJavaScriptTests().evaluate(code))
