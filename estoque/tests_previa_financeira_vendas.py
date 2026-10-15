"""Regressao da previa; executar com sistema.settings_test_compras (SQLite isolado)."""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import re
import subprocess
import tempfile

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Cliente, ContaReceber


class PreviaFinanceiraEndpointTests(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create(nome="Aurora Unica", whatsapp="5591999999999")
        ContaReceber.objects.create(
            cliente=self.cliente, data_emissao=timezone.localdate(),
            data_vencimento=timezone.localdate() - timedelta(days=2),
            valor_original=Decimal("125.50"), valor_em_aberto=Decimal("125.50"),
        )

    def buscar(self, **params):
        return self.client.get(reverse("estoque:clientes_autocomplete"), {
            "contexto": "venda_rapida", **params,
        }).json()["clientes"]

    def test_unico_carrega_financeiro_sem_alterar_contas(self):
        antes = list(ContaReceber.objects.values())
        cliente = self.buscar(q="Aurora")[0]
        self.assertEqual(cliente["id"], self.cliente.pk)
        self.assertEqual(cliente["financeiro"]["contas_abertas_total"], "125.50")
        self.assertEqual(cliente["financeiro"]["contas_vencidas_qtd"], 1)
        self.assertTrue(cliente["whatsapp_cobranca"]["url"])
        self.assertEqual(antes, list(ContaReceber.objects.values()))

    def test_varios_nao_calculam_previa_mesmo_com_prioridade_visual(self):
        Cliente.objects.create(nome="Outra Aurora")
        clientes = self.buscar(q="Aurora")
        self.assertEqual(len(clientes), 2)
        self.assertTrue(all(c["financeiro"]["contas_abertas_qtd"] == 0 for c in clientes))

    def test_vazio_inexistente_e_confirmacao(self):
        self.assertEqual(self.buscar(q="inexistente"), [])
        self.assertEqual(self.buscar(q="")[0]["financeiro"]["contas_abertas_qtd"], 0)
        self.assertEqual(self.buscar(cliente_id=self.cliente.pk)[0]["financeiro"]["contas_abertas_qtd"], 1)


class PreviaFinanceiraJavascriptTests(SimpleTestCase):
    def test_fluxos_no_chrome_com_respostas_controladas(self):
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        if not chrome.exists():
            self.skipTest("Chrome indisponivel")
        template = (Path(__file__).parent / "templates/estoque/vendas_layout_teste.html").read_text(encoding="utf-8")
        nomes = [
            "criarLinhaFinanceira", "criarLinkWhatsappCobranca", "atualizarResumoFinanceiroCliente",
            "atualizarCabecalhoCliente", "clienteSelecionadoValido", "preencherResumoCliente",
            "buscarClientes", "limparClienteSelecionado", "suspenderResumoClienteSelecionado", "renderizarSugestoesClientes",
        ]
        funcoes = "\n".join(re.search(r"^(?:async )?function " + nome + r"\([^\n]*\{.*?^\}", template, re.M | re.S)[0] for nome in nomes)
        inicio = template.index('clienteBusca.addEventListener("input",')
        listener = template[inicio:template.index('clienteBusca.addEventListener("focus",', inicio)]
        inicio_teclado = template.index('clienteBusca.addEventListener("keydown",')
        listener += template[inicio_teclado:template.index('  produto.addEventListener(', inicio_teclado)]
        setup = r'''
const ids = ['clienteBusca','clienteId','vencimentoVenda','clienteResumo','clienteResumoFinanceiro',
 'clienteResumoNome','clienteResumoPrazo','clienteResumoLimite','clienteResumoStatus','clienteResumoStatusCard',
 'clienteResumoWhatsapp','clienteSugestoesBox'];
for (const id of ids) { const el = document.createElement(id === 'clienteBusca' ? 'input' : 'div'); el.id=id; document.body.append(el); window[id]=el; }
window.salesOffline = {active:false};
let clienteSelecionado=null, clienteResumoSuspenso=false, clienteIndexAtivo=-1, clientesSugestoes=[];
let clienteBuscaAbortController=null, clienteBuscaTimer=null;
const clienteBuscaCache=new Map(), clientesAutocompleteUrl='/mock';
const numeroCliente=Number, formatarMoedaCliente=String, formatarLimiteCliente=String, normalizarStatusCliente=String;
const normalizarTextoCliente=s=>s.trim().toLowerCase();
const montarLinkReceberCliente=()=>'/receber', navegarResumoFinanceiroCliente=()=>{}, abrirPreviewCobranca=()=>{};
const guardarClienteSelecionado=()=>{}, esquecerClienteSelecionado=()=>{}, atualizarPrazoEVencimentoCliente=()=>{};
const atualizarHistoricoProdutoCliente=()=>{}, ocultarHistoricoProdutoCliente=()=>{}, atualizarClienteSelecionadoDoServidor=()=>{};
const renderizarClientesDoCache=()=>false, ordenarClientesAutocomplete=x=>x;
const focarProximoCampo=()=>{}, tipoVenda={};
let requests=[];
window.fetch=(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}));
const cliente=(id,nome)=>({id,nome,financeiro:{contas_abertas_qtd:1,contas_abertas_total:'125.50',contas_vencidas_qtd:1,contas_vencidas_total:'125.50'},whatsapp_cobranca:{url:'/cobrar'}});
const a=cliente(1,'Aurora'), b=cliente(2,'Beatriz');
const assert=(ok,msg)=>{if(!ok)throw Error(msg)};
const edit=texto=>{clienteBusca.value=texto;clienteBusca.dispatchEvent(new Event('input'));clearTimeout(clienteBuscaTimer)};
const responder=async(req,clientes)=>{req.resolve({ok:true,json:async()=>({clientes})});await new Promise(r=>setTimeout(r,0))};
'''
        casos = r'''
(async()=>{try {
 edit('Aurora'); let p=buscarClientes('Aurora',{forcarServidor:true});await responder(requests.at(-1),[a]);await p;
 assert(clienteResumo.classList.contains('visivel'),'previa unica ausente');
 assert(!clienteId.value&&!clienteSelecionado,'previa selecionou cliente');
 assert(clienteResumoFinanceiro.querySelectorAll('a').length===0,'previa tem links ativos');
 assert(clienteResumoFinanceiro.querySelectorAll('[aria-disabled=true]').length===3,'acoes nao desabilitadas');
 clienteSugestoesBox.querySelector('.cliente-sugestao-item').click();assert(clienteSelecionadoValido(),'confirmacao por clique falhou');
 assert(clienteResumoFinanceiro.querySelectorAll('a').length===3,'confirmacao nao liberou acoes');
 edit('Aurora');p=buscarClientes('Aurora',{forcarServidor:true});await responder(requests.at(-1),[a]);await p;
 clienteBusca.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));
 assert(clienteSelecionadoValido(),'confirmacao por Enter falhou');
 edit('Aur');assert(!clienteSelecionadoValido()&&!clienteId.value,'edicao manteve confirmacao');
 assert(!clienteResumo.classList.contains('visivel'),'edicao manteve previa');
 edit('Aurora');p=buscarClientes('Aurora',{forcarServidor:true});await responder(requests.at(-1),[a,b]);await p;
 assert(!clienteResumo.classList.contains('visivel'),'varios mostraram previa');
 edit('Aurora');let velha=buscarClientes('Aurora',{forcarServidor:true}), reqVelha=requests.at(-1);
 edit('Beatriz');p=buscarClientes('Beatriz',{forcarServidor:true});await responder(requests.at(-1),[b]);await p;
 await responder(reqVelha,[a]);await velha;
 assert(clienteResumoNome.textContent==='Beatriz','resposta atrasada sobrescreveu previa');
 edit('Aurora');velha=buscarClientes('Aurora',{forcarServidor:true});reqVelha=requests.at(-1);
 edit('');await responder(reqVelha,[a]);await velha;
 assert(!clienteResumo.classList.contains('visivel')&&!clienteId.value,'limpeza falhou');
 edit('Aurora');velha=buscarClientes('Aurora',{forcarServidor:true});reqVelha=requests.at(-1);
 edit('Beatriz');edit('Aurora');await responder(reqVelha,[a]);await velha;
 assert(!clienteResumo.classList.contains('visivel'),'resposta antiga A-B-A aceita');
 const antes=requests.length;
 for(const texto of ['Au','Aur','Aurora']) {
   clienteBusca.value=texto;clienteBusca.dispatchEvent(new Event('input'));
 }
 assert(requests.length===antes,'debounce disparou imediatamente');
 await new Promise(r=>setTimeout(r,160));
 assert(requests.length===antes+1,'debounce nao agrupou digitacao');
 await responder(requests.at(-1),[a]);
 assert(clienteResumo.classList.contains('visivel'),'debounce nao mostrou previa');
 p=buscarClientes('Aurora',{forcarServidor:true});const antesConfirmar=requests.at(-1);
 clienteSugestoesBox.querySelector('.cliente-sugestao-item').click();
 await responder(antesConfirmar,[b]);await p;
 assert(clienteSelecionadoValido()&&clienteResumoNome.textContent==='Aurora','resposta alterou confirmacao');
 document.body.dataset.result='PASS';
}catch(e){document.body.dataset.result='FAIL: '+e.message}})();
'''
        with tempfile.TemporaryDirectory(prefix="previa-vendas-") as pasta:
            html = Path(pasta) / "test.html"
            html.write_text('<html><body><script>' + setup + funcoes + listener + casos + '</script></body></html>', encoding="utf-8")
            resultado = subprocess.run([
                str(chrome), "--headless", "--disable-gpu", "--no-first-run", "--no-sandbox", "--in-process-gpu",
                "--disable-background-networking", "--user-data-dir=" + str(Path(pasta) / "profile"),
                "--dump-dom", "--virtual-time-budget=4000", html.as_uri(),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            self.assertIn('data-result="PASS"', resultado.stdout, resultado.stdout + resultado.stderr[-1500:])
