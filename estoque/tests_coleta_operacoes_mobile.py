"""One mobile write at a time; only synthetic fixtures and disposable databases."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection, close_old_connections
from django.test import Client, TransactionTestCase
from django.urls import reverse

from .tests_coleta_lista_mobile import ColetaFixture
from .tests_coleta_lista_browser import ColetaBrowserTests


class ColetaOperacoesBrowserTests(ColetaFixture, StaticLiveServerTestCase):
    navegador = ColetaBrowserTests.navegador
    abrir = ColetaBrowserTests.abrir
    tocar = ColetaBrowserTests.tocar
    iniciar = ColetaBrowserTests.iniciar
    pegar = ColetaBrowserTests.pegar
    resumo = ColetaBrowserTests.resumo

    def preparar(self):
        t = self.navegador(); self.abrir(t); self.iniciar(t)
        t.evaluate("window.alertasTeste=[];window.alert=m=>alertasTeste.push(m);true")
        return t

    def liberar_rascunho(self, t):
        self.tocar(t, '#btnContinuarEditandoRascunhoLista')
        t.wait("document.querySelector('#modalContinuarEditandoRascunhoLista').classList.contains('aberto')")
        self.tocar(t, '#btnConfirmarContinuarEditandoRascunhoLista')
        t.wait("!document.querySelector('#modalContinuarEditandoRascunhoLista').classList.contains('aberto')")

    def instrumentar(self, t, campo, modo='reter'):
        t.evaluate("""window.fetchTesteOperacoes=fetch;window.respostaRetida=null;window.contagemGravacoes=0;
          window.fetch=async(...a)=>{
            const body=a[1]?.body;
            if(body instanceof FormData && (body.has('lista_payload')||body.has('acao_coleta_mobile')))contagemGravacoes++;
            const r=await fetchTesteOperacoes(...a);
            if(body instanceof FormData && body.has("""+json.dumps(campo)+""")){
              window.respostaRetida=await r.clone().json();
              const modo="""+json.dumps(modo)+""";
              if(modo==='perder')throw new TypeError('Resposta ficticia perdida');
              if(modo==='sem_versao'){
                const d={...respostaRetida};delete d.coleta;
                return new Response(JSON.stringify(d),{status:r.status,headers:{'Content-Type':'application/json'}});
              }
              await new Promise(resolve=>window.liberarResposta=resolve);
            }
            return r;
          };
          window.finalizarTesteOperacoes=coletaLista.finalizar;
          coletaLista.finalizar=()=>{
            sessionStorage.setItem('ack-coleta',JSON.stringify({ocupado:coletaLista.ocupado,
              versao:coletaLista.dados?.estado.versao,atualizada:coletaLista.dados?.atualizada}));
            return finalizarTesteOperacoes();
          };true""")

    def tentar_durante_gravacao(self, t, selector):
        self.assertTrue(t.evaluate('coletaLista.ocupado'))
        self.assertTrue(t.evaluate('document.querySelector('+json.dumps(selector)+').disabled'))
        antes = t.evaluate('contagemGravacoes')
        # Even a synthetic click bypassing the disabled property is refused by the capture guard.
        t.evaluate('document.querySelector('+json.dumps(selector)+
                   ").dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));true")
        self.assertEqual(t.evaluate('contagemGravacoes'), antes)

    def conferir_ack(self, t):
        self.lista.refresh_from_db()
        ack = t.evaluate("JSON.parse(sessionStorage.getItem('ack-coleta'))")
        self.assertTrue(ack['ocupado'])
        self.assertEqual(ack['versao'], self.lista.coleta_mobile['versao'])
        self.assertEqual(ack['atualizada'], self.lista.atualizado_em.isoformat())

    def test_rascunho_pendente_bloqueia_peguei_e_atualiza_versao_antes_de_liberar(self):
        t = self.preparar(); self.instrumentar(t, 'lista_payload')
        self.tocar(t, '#btnSalvarRascunhoListaMobile'); t.wait('!!respostaRetida')
        self.lista.refresh_from_db()
        self.assertEqual(t.evaluate('coletaLista.dados.estado.versao'), self.lista.coleta_mobile['versao']-1)
        pegar = f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[0].pk}"] .sugestao-adicionar-lista-mobile'
        self.tentar_durante_gravacao(t, pegar)
        # The existing Continue Editing control must not release an unrelated pending save.
        t.evaluate("document.querySelector('#btnConfirmarContinuarEditandoRascunhoLista').dispatchEvent(new MouseEvent('click',{bubbles:true}));true")
        self.assertTrue(t.evaluate('coletaLista.ocupado'))
        t.evaluate('liberarResposta();true'); t.wait('!coletaLista.ocupado')
        self.conferir_ack(t)
        self.liberar_rascunho(t)
        t.evaluate('window.fetch=fetchTesteOperacoes;true')
        self.pegar(t); self.resumo(t, 1, 1, '5,20', '30,15')
        self.assertTrue(t.evaluate('coletaLista.pronto()'))
        self.sem_operacoes()

    def test_salvar_alteracoes_pendente_bloqueia_peguei_ate_ack_e_redirect(self):
        t = self.preparar(); self.instrumentar(t, 'lista_payload')
        self.tocar(t, '#btnSalvarAlteracoesListaMobile'); t.wait('!!respostaRetida')
        self.tentar_durante_gravacao(t, '#mobileSugestaoProdutos .sugestao-adicionar-lista-mobile')
        t.evaluate('liberarResposta();true')
        t.wait('location.pathname==='+json.dumps(reverse('estoque:compras_listas_fornecedor')))
        self.conferir_ack(t)
        self.abrir(t); self.pegar(t); self.resumo(t, 1, 1, '5,20', '30,15')
        self.sem_operacoes()

    def test_peguei_pendente_bloqueia_salvamentos_e_outro_peguei(self):
        t = self.preparar(); self.instrumentar(t, 'acao_coleta_mobile')
        self.tocar(t, f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[0].pk}"] .sugestao-adicionar-lista-mobile')
        t.wait('!!respostaRetida')
        for selector in ('#btnSalvarRascunhoListaMobile', '#btnSalvarAlteracoesListaMobile',
                         f'#mobileSugestaoProdutos [data-produto-id="{self.produtos[1].pk}"] .sugestao-adicionar-lista-mobile'):
            self.tentar_durante_gravacao(t, selector)
        t.evaluate('liberarResposta();true'); t.wait('!coletaLista.ocupado')
        self.resumo(t, 1, 1, '5,20', '30,15')
        self.assertFalse(t.evaluate("document.querySelector('#btnSalvarRascunhoListaMobile').disabled"))
        self.assertTrue(t.evaluate('coletaLista.pronto()'))
        t.evaluate('window.fetch=fetchTesteOperacoes;true')
        self.pegar(t, self.produtos[1]); self.resumo(t, 0, 2, '0,00', '35,35')
        self.sem_operacoes()

    def conferir_erro_sem_reenvio(self, t):
        t.wait('!!alertasTeste.length && !coletaLista.ocupado')
        self.assertFalse(t.evaluate("document.querySelector('#btnSalvarRascunhoListaMobile').disabled"))
        self.assertFalse(t.evaluate('coletaLista.pronto()'))
        antes = t.evaluate('contagemGravacoes')
        t.evaluate("document.querySelector('#btnSalvarRascunhoListaMobile').click();true")
        self.assertEqual(t.evaluate('contagemGravacoes'), antes)

    def test_resposta_perdida_libera_controles_sem_reutilizar_versao_antiga(self):
        t = self.preparar(); self.instrumentar(t, 'lista_payload', 'perder')
        self.tocar(t, '#btnSalvarRascunhoListaMobile'); self.conferir_erro_sem_reenvio(t)
        self.lista.refresh_from_db()
        self.assertEqual(t.evaluate('coletaLista.dados.estado.versao'), self.lista.coleta_mobile['versao']-1)
        self.abrir(t); self.pegar(t); self.resumo(t, 1, 1, '5,20', '30,15')
        self.sem_operacoes()

    def test_sucesso_sem_confirmacao_da_versao_nao_libera_nova_gravacao(self):
        t = self.preparar(); self.instrumentar(t, 'lista_payload', 'sem_versao')
        self.tocar(t, '#btnSalvarRascunhoListaMobile'); self.conferir_erro_sem_reenvio(t)
        self.assertIn('versão', t.evaluate("document.querySelector('[data-coleta-aviso]').textContent"))
        self.sem_operacoes()

    def test_409_real_no_salvamento_preserva_campos_e_exige_reabrir(self):
        t = self.preparar(); self.instrumentar(t, 'acao_ausente')
        self.acao('mover', produto=str(self.produtos[1].pk), estado='pego')
        antes = t.evaluate("document.querySelector('#mobileSugestaoProdutos .sugestao-qtd-input').value")
        self.tocar(t, '#btnSalvarAlteracoesListaMobile'); self.conferir_erro_sem_reenvio(t)
        self.assertIn('outra tela', t.evaluate("document.querySelector('[data-coleta-aviso]').textContent"))
        self.assertEqual(t.evaluate("document.querySelector('#mobileSugestaoProdutos .sugestao-qtd-input').value"), antes)
        self.lista.refresh_from_db()
        self.assertEqual(list(self.lista.coleta_mobile['itens'].values()).count('pego'), 1)
        self.sem_operacoes()


@skipUnless(connection.vendor == 'postgresql', 'Concorrência requer PostgreSQL descartável')
class ColetaOperacoesConcorrentesTests(ColetaFixture, TransactionTestCase):
    def test_duas_sessoes_mantem_um_sucesso_e_um_409(self):
        self.acao('iniciar_pendente'); estado = self.estado(); barreira = Barrier(2)
        outro = get_user_model().objects.create_superuser('outro-ficticio', '', 'teste')
        clientes = [Client(), Client()]
        for c, u in zip(clientes, (self.usuario, outro)): c.force_login(u)
        def enviar(par):
            c, p = par; close_old_connections()
            try:
                barreira.wait(timeout=15)
                return c.post(self.url, {'acao_coleta_mobile': json.dumps(dict(estado,
                    acao='mover', produto=str(p.pk), estado='pego'))}).status_code
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(enviar, zip(clientes, self.produtos)))
        self.assertEqual(sorted(resultados), [200, 409])
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.coleta_mobile['versao'], estado['versao']+1)
        self.assertEqual(list(self.lista.coleta_mobile['itens'].values()).count('pego'), 1)
        self.sem_operacoes()
