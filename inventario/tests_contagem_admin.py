from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import Funcionario, Produto, MovimentacaoEstoqueManual
from . import services, contagem_admin, ajustes
from .conversao import snapshot
from .models import (Inventario, ItemInventario, RodadaContagem, ConfirmacaoContagem,
    EventoInventario, AcessoInventario, AtribuicaoContagem, MovimentoObservado, PropostaAjuste)
from .tests import produto
from .tests_browser import CHROME
from offline.browser_support import Chrome


class AdminFixture:
    def setUp(self):
        self.usuario = get_user_model().objects.create_superuser('admin-sintetico', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador sintético')
        self.agua = produto(nome='Agua Oxig Vol 40 12/90Ml', unidade_compra='DZ', fator_conversao=12)
        self.barbeador = produto(nome='Barbeador Probak 1/28Un', unidade_compra='CX', fator_conversao=28)
        self.inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=[self.agua.pk, self.barbeador.pk])
        self.rodadas = list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('item__produto_id'))
        self.url = reverse('inventario:detalhe', args=[self.inv.pk])
        self.client.force_login(self.usuario)

    def token(self, rodada=None, usuario=None):
        self.inv.refresh_from_db()
        rodada = rodada or self.rodadas[0]
        rodada.refresh_from_db()
        return contagem_admin.autorizacao(usuario or self.usuario, self.inv, rodada)

    def corrigir(self, principal='1', avulsos='0', rodada=None, token=None, usuario=None):
        rodada = rodada or self.rodadas[0]
        return contagem_admin.confirmar(usuario or self.usuario, self.inv.pk, rodada.pk,
            token or self.token(rodada, usuario), principal, avulsos)

    def enviar_funcionario(self):
        acesso, token = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk,
            [r.pk for r in self.rodadas])
        for indice, rodada in enumerate(self.rodadas):
            services.salvar_contagem(acesso.pk, rodada.pk, 0, '12' if indice == 0 else '1', '0', True)
        self.inv.refresh_from_db()
        services.finalizar_conferencia(acesso.pk, self.inv.versao)
        self.inv.refresh_from_db()
        return acesso, token

    def estoque(self):
        return (list(Produto.objects.order_by('pk').values()),
            list(MovimentacaoEstoqueManual.objects.values()), list(MovimentoObservado.objects.values()))


class ContagemAdminTests(AdminFixture, TestCase):
    def test_corrige_ok_enviado_sem_reabrir_ou_alterar_estoque(self):
        acesso, _ = self.enviar_funcionario()
        original = list(ConfirmacaoContagem.objects.order_by('pk').values())
        acesso_antes = AcessoInventario.objects.values().get(pk=acesso.pk)
        estoque = self.estoque()
        finalizacao = self.inv.finalizada_em
        corrigida = self.corrigir()
        self.assertEqual(corrigida.contado_base, 1)
        self.assertEqual(corrigida.avulsos, 0)
        self.assertEqual(original, list(ConfirmacaoContagem.objects.filter(pk__in=[c['id'] for c in original]).order_by('pk').values()))
        self.assertEqual(acesso_antes, AcessoInventario.objects.values().get(pk=acesso.pk))
        self.assertEqual(estoque, self.estoque())
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.finalizada_em, finalizacao)
        self.assertEqual(RodadaContagem.objects.count(), 2)
        evento = self.inv.eventos.get(acao=contagem_admin.ACAO)
        self.assertEqual(evento.usuario, self.usuario)
        self.assertEqual(evento.rodada_id, corrigida.pk)
        self.assertIsNotNone(evento.criado_em)
        self.assertEqual(Decimal(evento.dados['antes']['embalagens']), 12)
        self.assertEqual(Decimal(evento.dados['depois']['contado_base']), 1)
        self.assertEqual(evento.dados['produto_id'], self.agua.pk)
        self.assertFalse(evento.dados['estoque_alterado'])
        novo = ConfirmacaoContagem.objects.get(pk=evento.dados['confirmacao_id'])
        self.assertIsNone(novo.acesso_id)
        self.assertEqual(novo.responsavel_nome, self.usuario.username)
        self.assertEqual(corrigida.revisao_motivo, '')
        resposta = self.client.get(self.url)
        linha = next(l for l in resposta.context['linhas'] if l['rodada'].pk == corrigida.pk)
        self.assertEqual(linha['diferenca'], Decimal('-9'))
        self.assertEqual(linha['resultado'], 'falta')
        self.assertContains(resposta, 'OK administrativo: admin-sintetico')

    def test_recontagem_ciclo_2_sem_atribuicao_preserva_outros_29(self):
        for i in range(28):
            p = produto(nome=f'Outro produto {i:02}', unidade_compra='CX')
            p.refresh_from_db()
            item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p), custo_base=p.preco_compra)
            self.rodadas.append(RodadaContagem.objects.create(item=item, numero=1))
        self.enviar_funcionario()
        nova = services.recontar(self.usuario, self.rodadas[1].pk, 'Recontagem sintética já solicitada')
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.ciclo, 2)
        outros = list(RodadaContagem.objects.exclude(item=nova.item).order_by('pk').values())
        atribuicoes = list(AtribuicaoContagem.objects.values())
        acessos = list(AcessoInventario.objects.values())
        originais = list(ConfirmacaoContagem.objects.values())
        estoque = self.estoque()
        antes_rodadas = RodadaContagem.objects.count()
        self.corrigir('2', '14', rodada=nova)
        self.assertEqual(antes_rodadas, RodadaContagem.objects.count())
        self.assertEqual(outros, list(RodadaContagem.objects.exclude(item=nova.item).order_by('pk').values()))
        self.assertEqual(atribuicoes, list(AtribuicaoContagem.objects.values()))
        self.assertEqual(acessos, list(AcessoInventario.objects.values()))
        self.assertEqual(originais, list(ConfirmacaoContagem.objects.filter(pk__in=[c['id'] for c in originais]).values()))
        self.assertEqual(estoque, self.estoque())
        nova.refresh_from_db()
        self.assertEqual(nova.contado_base, Decimal('2.5'))
        self.assertIsNotNone(nova.ok_em)
        _, pendencias = services.estado_conclusao(self.inv)
        self.assertFalse(any(self.barbeador.nome in p for p in pendencias))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.ciclo, 2)
        self.assertIsNone(self.inv.finalizada_em)

    def test_embalagens_avulsos_unidades_discretas_continuas_e_validacao(self):
        self.assertEqual(self.corrigir('1', '6').contado_base, Decimal('1.5'))
        for principal, avulsos in [('', '0'), ('-1', '0'), ('1.5', '0'), ('1', ''), ('1', '0.5'), ('NaN', '0'), ('1000000000', '0')]:
            with self.subTest(principal=principal, avulsos=avulsos):
                with self.assertRaises(ValueError): self.corrigir(principal, avulsos)
        p = produto(nome='Peso sintético', unidade_compra='KG', vende_fracionado=False)
        outra = services.criar_inventario(self.usuario, criterio='manual', selecionados=[p.pk])
        r = outra.itens.get().rodadas.get()
        token = contagem_admin.autorizacao(self.usuario, outra, r)
        with self.assertRaises(ValueError): contagem_admin.confirmar(self.usuario, outra.pk, r.pk, token, '1.5', '1')
        r = contagem_admin.confirmar(self.usuario, outra.pk, r.pk, token, '1.5', None)
        self.assertEqual(r.contado_base, Decimal('1.5'))

    def test_duplo_envio_idempotente_e_valores_diferentes_recusados(self):
        token = self.token()
        primeiro = self.corrigir(token=token)
        self.assertEqual(self.corrigir(token=token).pk, primeiro.pk)
        with self.assertRaises(ValueError): self.corrigir('2', token=token)
        self.assertEqual(self.inv.eventos.filter(acao=contagem_admin.ACAO).count(), 1)
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        self.corrigir('3')
        self.corrigir(token=token)  # Lost response retry must not overwrite a newer correction.
        primeiro.refresh_from_db()
        self.assertEqual(primeiro.contado_base, 3)

    def test_rascunho_concorrente_e_outro_usuario_recusados(self):
        token = self.token()
        acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodadas[0].pk])
        services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 0, '2', '0')
        with self.assertRaises(ValueError): self.corrigir(token=token)
        outro = get_user_model().objects.create_superuser('outro-admin', '', 'teste')
        with self.assertRaises(PermissionDenied): self.corrigir(token=self.token(), usuario=outro)

    def test_funcionario_nao_sobrescreve_ok_admin_mas_continua_outro_produto(self):
        acesso, token = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [r.pk for r in self.rodadas])
        self.corrigir()
        with self.assertRaisesMessage(PermissionDenied, 'administrador'):
            services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 1, '2', '0', True)
        services.salvar_contagem(acesso.pk, self.rodadas[1].pk, 0, '1', '0', True)
        self.assertIsNotNone(services.acesso_token(token))
        self.inv.refresh_from_db()
        services.finalizar_conferencia(acesso.pk, self.inv.versao)
        self.inv.refresh_from_db(); self.assertIsNotNone(self.inv.finalizada_em)

    def test_rollback_integral_se_auditoria_falhar(self):
        antes = list(RodadaContagem.objects.values())
        inv = Inventario.objects.values().get(pk=self.inv.pk)
        with patch('inventario.services.evento', side_effect=RuntimeError('falha sintética')):
            with self.assertRaises(RuntimeError): self.corrigir()
        self.assertEqual(antes, list(RodadaContagem.objects.values()))
        self.assertEqual(inv, Inventario.objects.values().get(pk=self.inv.pk))
        self.assertFalse(ConfirmacaoContagem.objects.exists())

    def test_cancelada_encerrada_cadastro_alterado_token_adulterado_e_outro_ciclo(self):
        token = self.token()
        with self.assertRaises(ValueError): self.corrigir(token=token+'x')
        self.agua.fator_conversao = 24; self.agua.save(update_fields=['fator_conversao'])
        with self.assertRaises(ValueError): self.corrigir(token=token)
        self.agua.fator_conversao = 12; self.agua.save(update_fields=['fator_conversao'])
        services.recontar(self.usuario, self.rodadas[0].pk, 'Sintético')
        with self.assertRaises(ValueError): self.corrigir(token=token)
        Inventario.objects.filter(pk=self.inv.pk).update(ciclo=2)
        with self.assertRaisesMessage(ValueError, 'ciclo'): self.corrigir(token=token)
        services.cancelar_conferencia(self.usuario, self.inv.pk)
        with self.assertRaises(PermissionDenied): self.corrigir(token=token)

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_ajuste_aplicado_bloqueia_sem_reverter_e_proposta_antiga_invalida(self):
        self.enviar_funcionario()
        r = RodadaContagem.objects.select_related('item__produto').get(pk=self.rodadas[0].pk)
        a = ajustes.avaliar(r)
        proposta = ajustes.decidir(self.usuario, self.inv.pk, r.pk, a['token'], 'Sintético', confirmacao=True)
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        self.corrigir()
        with self.assertRaises(ValueError): ajustes.aplicar(self.usuario, self.inv.pk, previa, True)
        r.refresh_from_db(); a = ajustes.avaliar(r)
        proposta = ajustes.decidir(self.usuario, self.inv.pk, r.pk, a['token'], 'Sintético', confirmacao=True)
        ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, [proposta]), True)
        antes = self.estoque()
        with self.assertRaisesMessage(ValueError, 'aplicado'): self.corrigir()
        self.assertEqual(antes, self.estoque())
        self.assertNotContains(self.client.get(self.url), 'action="'+reverse('inventario:editar_contagem_admin', args=[self.inv.pk, r.pk])+'"')

    def test_http_csrf_permissoes_post_e_confirmacao_sem_justificativa(self):
        endpoint = reverse('inventario:editar_contagem_admin', args=[self.inv.pk, self.rodadas[0].pk])
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.usuario)
        self.assertEqual(csrf.post(endpoint, {}).status_code, 403)
        dados = {'autorizacao':self.token(), 'embalagens':'1', 'avulsos':'0', 'confirmacao':'sim'}
        sem = get_user_model().objects.create_user('gestor-sem-revisao')
        sem.user_permissions.add(Permission.objects.get(content_type__app_label='inventario', codename='gerenciar_inventario'))
        self.client.force_login(sem)
        self.assertNotContains(self.client.get(self.url), 'Editar contagem')
        self.assertEqual(self.client.post(endpoint, dados).status_code, 403)
        self.client.force_login(self.usuario)
        resposta = self.client.post(endpoint, dados, HTTP_ACCEPT='application/json')
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()['destino'], self.url+'#titulo-resumo')
        self.assertEqual(self.client.post(endpoint, {**dados,'embalagens':'2'}, HTTP_ACCEPT='application/json').status_code, 409)

    def test_renderizacao_sem_n_mais_um_com_dezenas_de_produtos_confirmados(self):
        def medir():
            self.client.get(self.url)  # Warm permission caches/session.
            with CaptureQueriesContext(connection) as queries:
                resposta = self.client.get(self.url)
            return len(queries), resposta
        self.corrigir()
        pequeno, _ = medir()
        for i in range(28):
            p = produto(nome=f'Produto adicional {i:02}')
            p.refresh_from_db()
            item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p))
            r = RodadaContagem.objects.create(item=item, numero=1)
            self.corrigir(rodada=r)
        grande, resposta = medir()
        self.assertLessEqual(grande, pequeno + 2)
        self.assertContains(resposta, 'data-edicao-admin', count=30)


class ContagemAdminConcorrenciaTests(AdminFixture, TransactionTestCase):
    def disputar(self, operacoes):
        barreira = Barrier(2)
        def executar(operacao):
            try:
                usuario = get_user_model().objects.get(pk=self.usuario.pk)
                barreira.wait(timeout=10)
                try:
                    operacao(usuario)
                    return 'ok'
                except (ValueError, PermissionDenied):
                    return 'conflito'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(executar, operacoes))

    def test_admin_e_funcionario_concorrentes_nao_perdem_confirmacoes(self):
        acesso, _ = services.gerar_acesso(self.usuario, self.inv.pk, self.funcionario.pk, [self.rodadas[0].pk])
        token = self.token()
        resultado = self.disputar([
            lambda u: contagem_admin.confirmar(u, self.inv.pk, self.rodadas[0].pk, token, '1', '0'),
            lambda u: services.salvar_contagem(acesso.pk, self.rodadas[0].pk, 0, '12', '0', True),
        ])
        self.assertCountEqual(resultado, ['ok', 'conflito'])
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        r = RodadaContagem.objects.get(pk=self.rodadas[0].pk)
        self.assertEqual(r.contado_base, 1 if resultado[0] == 'ok' else 12)
        self.assertEqual(Produto.objects.get(pk=self.agua.pk).quantidade, 10)

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_admin_e_aplicacao_de_ajuste_concorrentes(self):
        self.enviar_funcionario()
        r = RodadaContagem.objects.select_related('item__produto').get(pk=self.rodadas[0].pk)
        avaliacao = ajustes.avaliar(r)
        proposta = ajustes.decidir(self.usuario, self.inv.pk, r.pk, avaliacao['token'], 'Sintético', confirmacao=True)
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        token = self.token()
        resultado = self.disputar([
            lambda u: contagem_admin.confirmar(u, self.inv.pk, r.pk, token, '1', '0'),
            lambda u: ajustes.aplicar(u, self.inv.pk, previa, True),
        ])
        self.assertCountEqual(resultado, ['ok','conflito'])
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), int(resultado[1] == 'ok'))
        self.assertEqual(Produto.objects.get(pk=self.agua.pk).quantidade, 12 if resultado[1] == 'ok' else 10)

    def concorrentes(self, valores):
        token = self.token()
        barreira = Barrier(2)
        def executar(valor):
            try:
                usuario = get_user_model().objects.get(pk=self.usuario.pk)
                barreira.wait(timeout=10)
                try:
                    contagem_admin.confirmar(usuario, self.inv.pk, self.rodadas[0].pk, token, valor, '0')
                    return 'ok'
                except ValueError:
                    return 'conflito'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(executar, valores))

    def test_duas_confirmacoes_iguais_gravam_uma_vez(self):
        self.assertEqual(self.concorrentes(['1','1']), ['ok','ok'])
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        self.assertEqual(EventoInventario.objects.filter(acao=contagem_admin.ACAO).count(), 1)

    def test_duas_confirmacoes_diferentes_nao_sobrescrevem(self):
        self.assertCountEqual(self.concorrentes(['1','2']), ['ok','conflito'])
        self.assertEqual(ConfirmacaoContagem.objects.count(), 1)
        self.assertEqual(self.estoque()[0][0]['quantidade'], 10)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class ContagemAdminBrowserTests(AdminFixture, StaticLiveServerTestCase):
    def test_modal_desktop_mobile_total_erro_cancelar_e_duplo_clique(self):
        self.enviar_funcionario()
        with TemporaryDirectory(prefix='contagem-admin-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+self.url})
                tab.wait("window.inventarioContagemAdminPronta === true")
                for width in (1280, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                    tab.evaluate("window.formAdmin=document.querySelector('form[data-base=DZ]');document.querySelector('[data-abrir-edicao-admin=\"'+formAdmin.closest('dialog').id+'\"]').click();formAdmin.elements.embalagens.value='1';formAdmin.elements.avulsos.value='6';formAdmin.dispatchEvent(new Event('input',{bubbles:true}));true")
                    self.assertIn('1,5 DZ', tab.evaluate("formAdmin.querySelector('output').textContent"))
                    self.assertEqual(tab.evaluate("formAdmin.querySelectorAll('textarea').length"), 0)
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertTrue(tab.evaluate("formAdmin.closest('dialog').matches(':modal')"))
                    self.assertIn('Agua Oxig', tab.evaluate("formAdmin.closest('dialog').querySelector('h2').textContent"))
                    self.assertTrue(tab.evaluate("(()=>{const r=formAdmin.closest('dialog').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&Math.abs((r.left+r.right)/2-document.documentElement.clientWidth/2)<2})()"), str(tab.evaluate("({width:innerWidth,rect:formAdmin.closest('dialog').getBoundingClientRect().toJSON(),margin:getComputedStyle(formAdmin.closest('dialog')).margin})")))
                    tab.evaluate("formAdmin.querySelector('[data-fechar-edicao-admin]').click();true")
                    self.assertFalse(tab.evaluate("formAdmin.closest('dialog').open"))
                tab.evaluate("document.querySelector('[data-abrir-edicao-admin=\"'+formAdmin.closest('dialog').id+'\"]').click();true")
                # A failed request must preserve quantities and keep the dialog open.
                tab.evaluate("window.fetchOriginal=window.fetch;window.fetch=async()=>({ok:false,headers:new Headers({'content-type':'application/json'}),json:async()=>({erro:'Conflito sintetico: tente novamente.'})});formAdmin.querySelector('button[type=submit]').click();true")
                tab.wait("formAdmin.querySelector('[data-mensagem-admin]').textContent.includes('Conflito')")
                self.assertTrue(tab.evaluate("formAdmin.closest('dialog').matches(':modal')"))
                self.assertEqual(tab.evaluate('formAdmin.elements.avulsos.value'), '6')
                self.assertEqual(tab.evaluate('formAdmin.elements.embalagens.value'), '1')
                tab.evaluate('window.fetch=window.fetchOriginal;true')
                tab.evaluate("formAdmin.dataset.fator='16';formAdmin.elements.embalagens.value='0';formAdmin.elements.avulsos.value='1';formAdmin.dispatchEvent(new Event('input',{bubbles:true}));true")
                self.assertEqual(tab.evaluate("formAdmin.querySelector('output').textContent"), '0,062 DZ')
                tab.evaluate("formAdmin.elements.avulsos.value='3';formAdmin.dispatchEvent(new Event('input',{bubbles:true}));true")
                self.assertEqual(tab.evaluate("formAdmin.querySelector('output').textContent"), '0,188 DZ')
                tab.evaluate("formAdmin.dataset.fator='12.00';formAdmin.elements.embalagens.value='1';true")
                tab.evaluate("formAdmin.elements.avulsos.value='0';formAdmin.dispatchEvent(new Event('input',{bubbles:true}));formAdmin.querySelector('button[type=submit]').click();formAdmin.querySelector('button[type=submit]').click();true")
                try:
                    tab.wait("document.body.textContent.includes('OK administrativo: admin-sintetico')")
                except AssertionError:
                    self.fail(str(tab.evaluate("({url:location.href,mensagem:document.querySelector('[data-mensagem-admin]')?.textContent,formularios:[...document.querySelectorAll('[data-edicao-admin]')].map(f=>({valido:f.checkValidity(),campos:[...f.elements].filter(e=>['embalagens','avulsos'].includes(e.name)).map(e=>({nome:e.name,valor:e.value,validacao:e.validationMessage}))}))})")))
                self.assertEqual(EventoInventario.objects.filter(acao=contagem_admin.ACAO).count(), 1)
                self.assertEqual(ConfirmacaoContagem.objects.count(), 3)
                self.assertEqual(RodadaContagem.objects.get(pk=self.rodadas[0].pk).contado_base, 1)
                self.assertEqual(Produto.objects.get(pk=self.agua.pk).quantidade, 10)
            finally:
                chrome.stop()

    def test_modal_sem_avulsos_escape_e_foco(self):
        p = produto(nome='Produto unitario sintetico', unidade_compra='UN', vende_fracionado=False)
        inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=[p.pk])
        with TemporaryDirectory(prefix='contagem-admin-browser-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab(); tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate', {'url':self.live_server_url+reverse('inventario:detalhe', args=[inv.pk])})
                tab.wait("window.inventarioContagemAdminPronta === true")
                tab.evaluate("window.abrirAdmin=document.querySelector('[data-abrir-edicao-admin]');abrirAdmin.focus();abrirAdmin.click();window.formAdmin=document.querySelector('[data-edicao-admin]');true")
                self.assertIsNone(tab.evaluate("formAdmin.elements.avulsos || null"))
                self.assertTrue(tab.evaluate("document.activeElement===formAdmin.elements.embalagens"))
                tab.evaluate("formAdmin.elements.embalagens.value='3';formAdmin.dispatchEvent(new Event('input',{bubbles:true}));true")
                self.assertEqual(tab.evaluate("formAdmin.querySelector('output').textContent"), '3 UN')
                tab.call('Input.dispatchKeyEvent', {'type':'keyDown','key':'Escape','code':'Escape','windowsVirtualKeyCode':27})
                tab.call('Input.dispatchKeyEvent', {'type':'keyUp','key':'Escape','code':'Escape','windowsVirtualKeyCode':27})
                tab.wait("!formAdmin.closest('dialog').open")
                self.assertTrue(tab.evaluate('document.activeElement===abrirAdmin'))
                self.assertFalse(ConfirmacaoContagem.objects.filter(rodada__item__inventario=inv).exists())
            finally:
                chrome.stop()
