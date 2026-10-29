from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .models import ConfirmacaoContagem
from .tests import produto
from .tests_browser import CHROME


class OrganizacaoFixture:
    def setUp(self):
        super().setUp()
        self.usuario = get_user_model().objects.create_superuser('organizacao-contagem', '', 'teste')
        self.produtos = [produto(nome=nome, unidade_compra='UN', vende_fracionado=False)
                         for nome in ['A de teste', 'B de teste', 'C de teste']]
        self.inventario = services.criar_inventario(self.usuario, 'Contagem de teste', 'manual',
                                                   [p.pk for p in self.produtos])
        self.rodadas = list(self.inventario.itens.order_by('produto__nome').values_list('rodadas__pk', flat=True))
        funcionario = Funcionario.objects.create(nome='Contador de teste')
        self.acesso, self.token = services.gerar_acesso(
            self.usuario, self.inventario.pk, funcionario.pk, self.rodadas)
        services.salvar_contagem(self.acesso.pk, self.rodadas[2], 0, '8', None, True)


class OrganizacaoHTTPTests(OrganizacaoFixture, TestCase):
    def test_grupos_iniciais_iguais_ao_estado_do_servidor(self):
        self.client.post(reverse('inventario:convite', args=[self.token]))
        resposta = self.client.get(reverse('inventario:mobile'))
        self.assertEqual(resposta.context['progresso'], dict(total=3, conferidos=1, faltam=2))
        html = resposta.content.decode()
        pendentes = html.split('<section id="produtos-pendentes"')[1].split('</section>')[0]
        conferidos = html.split('<div id="lista-produtos-conferidos">')[1].split('</div></details>')[0]
        self.assertIn(self.produtos[0].nome, pendentes)
        self.assertIn(self.produtos[1].nome, pendentes)
        self.assertNotIn(self.produtos[2].nome, pendentes)
        self.assertIn(self.produtos[2].nome, conferidos)
        self.assertNotIn(self.produtos[0].nome, conferidos)
        self.assertNotIn('open', html.split('<details id="produtos-conferidos"')[1].split('>')[0])
        self.assertEqual(html.count('class="contagem"'), 3)

    def test_guardar_sem_ok_continua_pendente_apos_recarregar(self):
        self.client.post(reverse('inventario:convite', args=[self.token]))
        resposta = self.client.post(reverse('inventario:contar', args=[self.rodadas[1]]),
            dict(acao='salvar', versao=0, embalagens='6'), HTTP_ACCEPT='application/json')
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.json()['produto']['conferido'])
        pagina = self.client.get(reverse('inventario:mobile'))
        self.assertEqual(pagina.context['progresso'], dict(total=3, conferidos=1, faltam=2))
        b = next(p for p in pagina.context['produtos'] if p['id'] == self.rodadas[1])
        self.assertEqual(b['embalagens'], '6')
        self.assertFalse(b['conferido'])
        self.assertTrue(b['guardado'])
        self.assertFalse(ConfirmacaoContagem.objects.filter(rodada_id=self.rodadas[1]).exists())


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class OrganizacaoBrowserTests(OrganizacaoFixture, StaticLiveServerTestCase):
    def test_ok_contadores_foco_recontagem_e_reload(self):
        with TemporaryDirectory(prefix='organizacao-contagem-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                t = chrome.tab()
                t.call('Page.navigate', {'url': self.live_server_url + reverse('inventario:convite', args=[self.token])})
                t.wait("!!document.querySelector('form button')")
                t.evaluate("document.querySelector('form button').click();true")
                t.wait('window.inventarioContagemPronta===true')
                for largura in [320, 390]:
                    t.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=844, deviceScaleFactor=1, mobile=True))
                    self.assertTrue(t.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                t.evaluate(f"window.a=document.querySelector('[data-produto-id=\"{self.rodadas[0]}\"]');window.b=document.querySelector('[data-produto-id=\"{self.rodadas[1]}\"]');window.c=document.querySelector('[data-produto-id=\"{self.rodadas[2]}\"]');window.marcoSemReload=1;true")
                self.assertTrue(t.evaluate("!document.querySelector('#produtos-conferidos').open && document.querySelector('[data-total-pendentes]').textContent==='2'"))
                t.evaluate("b.elements.embalagens.value='3';b.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));b.querySelector('[value=salvar]').click();true")
                t.wait("b.elements.embalagens.readOnly && !b.querySelector('[value=ok]').disabled")
                self.assertTrue(t.evaluate("b.closest('article').parentElement.id==='produtos-pendentes' && document.querySelector('[data-total-pendentes]').textContent==='2'"))
                # A resposta do servidor, e não o clique, determina a mudança de grupo.
                t.evaluate("window.fetchReal=fetch;window.fetch=async(...args)=>{if(String(args[0])===a.action)await new Promise(resolve=>window.liberarOK=resolve);return fetchReal(...args)};a.elements.embalagens.value='2';a.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));a.querySelector('[value=ok]').focus();a.querySelector('[value=ok]').scrollIntoView({block:'center'});a.querySelector('[value=ok]').click();true")
                t.wait('!!window.liberarOK')
                self.assertTrue(t.evaluate("a.closest('article').parentElement.id==='produtos-pendentes' && document.querySelector('[data-total-pendentes]').textContent==='2'"))
                t.evaluate('window.liberarOK();true')
                t.wait("a.closest('article').parentElement.id==='lista-produtos-conferidos' && !a.querySelector('[data-editar-contagem]').disabled")
                self.assertTrue(t.evaluate("marcoSemReload===1 && !document.querySelector('#produtos-conferidos').open && document.querySelector('[data-total-pendentes]').textContent==='1' && [...document.querySelectorAll('[data-total-conferidos]')].every(e=>e.textContent==='2')"))
                self.assertTrue(t.evaluate("document.activeElement.closest('form')===b && document.activeElement.getBoundingClientRect().top>=0 && document.activeElement.getBoundingClientRect().bottom<=innerHeight && b.elements.embalagens.value==='3'"))
                t.evaluate("window.fetch=fetchReal;document.querySelector('#produtos-conferidos').open=true;c.querySelector('[data-editar-contagem]').click();c.elements.embalagens.value='9';c.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));true")
                # Uma edição local não apaga o OK que o servidor ainda mantém.
                self.assertTrue(t.evaluate("document.querySelector('[data-total-conferidos]').textContent==='2' && c.elements.embalagens.value==='9'"))
                t.evaluate("c.querySelector('[value=salvar]').click();true")
                t.wait("c.closest('article').parentElement.id==='produtos-pendentes' && c.elements.embalagens.readOnly")
                self.assertTrue(t.evaluate("document.querySelector('[data-total-pendentes]').textContent==='2' && b.elements.embalagens.value==='3'"))
                t.evaluate("c.querySelector('[value=ok]').click();true")
                t.wait("c.closest('article').parentElement.id==='lista-produtos-conferidos' && !c.querySelector('[data-editar-contagem]').disabled")
                t.evaluate("c.querySelector('[data-editar-contagem]').click();c.elements.embalagens.value='10';c.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));c.querySelector('[value=ok]').click();true")
                t.wait("c.elements.versao.value==='4' && !c.querySelector('[data-editar-contagem]').disabled")
                t.call('Page.reload')
                t.wait('window.inventarioContagemPronta===true')
                self.assertTrue(t.evaluate("!document.querySelector('#produtos-conferidos').open && document.querySelectorAll('#produtos-pendentes form.contagem').length===1 && document.querySelectorAll('#lista-produtos-conferidos form.contagem').length===2 && document.querySelector('[data-total-pendentes]').textContent==='1' && document.querySelector('[data-total-conferidos]').textContent==='2'"))
            finally:
                chrome.stop()
        self.assertEqual(list(ConfirmacaoContagem.objects.filter(rodada_id=self.rodadas[2]).order_by('pk').values_list('embalagens', flat=True)), [8, 9, 10])
        self.assertFalse(ConfirmacaoContagem.objects.filter(rodada_id=self.rodadas[1]).exists())
        for p in self.produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)


@skipUnless(Path(CHROME).is_file(), 'Chrome indisponível')
class ConclusaoVisualMobileTests(StaticLiveServerTestCase):
    def test_dois_oks_destacam_revisao_sem_enviar_e_preservam_recontagem(self):
        usuario = get_user_model().objects.create_superuser('revisao-visual', '', 'teste')
        produtos = [produto(nome=nome, unidade_compra='UN', vende_fracionado=False)
                    for nome in ['Primeiro teste', 'Segundo teste']]
        inventario = services.criar_inventario(usuario, 'Revisão visual', 'manual',
                                               [p.pk for p in produtos])
        rodadas = list(inventario.itens.order_by('produto__nome').values_list('rodadas__pk', flat=True))
        funcionario = Funcionario.objects.create(nome='Revisor de teste')
        acesso, token = services.gerar_acesso(usuario, inventario.pk, funcionario.pk, rodadas)
        with TemporaryDirectory(prefix='revisao-visual-contagem-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                t = chrome.tab()
                t.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=True))
                t.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=1))

                def tocar(seletor):
                    import json
                    ponto = t.evaluate("(()=>{const e=document.querySelector(" + json.dumps(seletor)
                        + ");e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
                        + "return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
                    t.call('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=[ponto]))
                    t.call('Input.dispatchTouchEvent', dict(type='touchEnd', touchPoints=[]))

                t.call('Page.navigate', dict(url=self.live_server_url + reverse('inventario:convite', args=[token])))
                t.wait("!!document.querySelector('form button')")
                tocar('form button')
                t.wait('window.inventarioContagemPronta===true')
                self.assertFalse(t.evaluate("document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel')"))
                for indice, rodada in enumerate(rodadas):
                    seletor = f'[data-produto-id="{rodada}"]'
                    t.evaluate(f"window.atual=document.querySelector('{seletor}');atual.elements.embalagens.value='2';atual.elements.embalagens.dispatchEvent(new Event('input',{{bubbles:true}}));true")
                    # O destaque só pode surgir depois da confirmação do último OK.
                    if indice == 1:
                        t.evaluate("window.fetchReal=fetch;window.fetch=async(...args)=>{if(String(args[0])===atual.action)await new Promise(resolve=>window.liberarOK=resolve);return fetchReal(...args)};true")
                    tocar(seletor + ' [value=ok]')
                    if indice == 1:
                        t.wait('!!window.liberarOK')
                        self.assertFalse(t.evaluate("document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel')"))
                        t.evaluate('window.liberarOK();true')
                    t.wait(f"document.querySelectorAll('#lista-produtos-conferidos form.contagem').length==={indice + 1} && !atual.querySelector('[data-editar-contagem]').disabled")
                t.evaluate('window.fetch=fetchReal;true')
                self.assertTrue(t.evaluate("document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel') && !document.querySelector('#produtos-conferidos').open && !document.querySelector('#nenhum-produto-pendente').hidden && document.querySelector('#nenhum-produto-pendente').textContent==='Todos os produtos conferidos. Revise os itens abaixo antes de finalizar.'"))
                self.assertTrue(t.evaluate("['#produtos-conferidos>summary','#finalizar-conferencia button'].every(s=>{const r=document.querySelector(s).getBoundingClientRect();return r.height>0&&r.top>=0&&r.bottom<=innerHeight})"))
                inventario.refresh_from_db()
                acesso.refresh_from_db()
                self.assertIsNone(inventario.finalizada_em)
                self.assertIsNone(acesso.enviada_em)
                tocar('#produtos-conferidos>summary')
                self.assertTrue(t.evaluate("document.querySelector('#produtos-conferidos').open && document.querySelectorAll('#lista-produtos-conferidos article').length===2 && !document.querySelector('#produtos-conferidos').contains(document.querySelector('#finalizar-conferencia'))"))
                tocar(f'[data-produto-id="{rodadas[0]}"] [data-editar-contagem]')
                tocar(f'[data-produto-id="{rodadas[0]}"] [value=salvar]')
                t.wait("document.querySelectorAll('#produtos-pendentes form.contagem').length===1")
                self.assertTrue(t.evaluate("!document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel') && document.querySelector('#nenhum-produto-pendente').hidden"))
                tocar(f'[data-produto-id="{rodadas[0]}"] [value=ok]')
                t.wait("document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel')")
                t.call('Page.reload')
                t.wait('window.inventarioContagemPronta===true')
                self.assertTrue(t.evaluate("document.querySelector('#produtos-conferidos').classList.contains('revisao-disponivel') && !document.querySelector('#produtos-conferidos').open && document.querySelector('[data-total-pendentes]').textContent==='0' && document.querySelector('[data-total-conferidos]').textContent==='2'"))
                for largura in [320, 390]:
                    t.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=844, deviceScaleFactor=1, mobile=True))
                    self.assertTrue(t.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                tocar('#finalizar-conferencia button')
                t.wait("document.querySelector('#estado-conferencia').textContent.includes('finalizada')")
                self.assertTrue(t.evaluate("document.querySelector('#finalizar-conferencia button').disabled && [...document.querySelectorAll('[data-editar-contagem]')].every(e=>e.hidden)"))
                t.call('Page.reload')
                t.wait('window.inventarioContagemPronta===true')
                self.assertTrue(t.evaluate("!!document.querySelector('#produtos-conferidos>summary') && !document.querySelector('#finalizar-conferencia')"))
                tocar('#produtos-conferidos>summary')
                self.assertTrue(t.evaluate("document.querySelectorAll('#lista-produtos-conferidos article').length===2 && [...document.querySelectorAll('form.contagem fieldset')].every(e=>e.disabled)"))
            finally:
                chrome.stop()
        for p in produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade, 10)
