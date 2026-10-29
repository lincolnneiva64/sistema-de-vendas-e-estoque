"""Last OK and reload with two or several synthetic products, without sending."""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from estoque.models import Funcionario
from offline.browser_support import Chrome
from . import services
from .tests import produto
from .tests_browser import CHROME


@skipUnless(Path(CHROME).is_file(), 'Navegador indisponível')
class OrganizacaoCompletaMobileTests(StaticLiveServerTestCase):
    def verificar_grupos(self, tab, total, conferidos):
        estado = tab.evaluate("""(() => {
            const grupo=document.querySelector('#produtos-conferidos'),
                botao=document.querySelector('#finalizar-conferencia button');
            return {pendentes:document.querySelectorAll('#produtos-pendentes form.contagem').length,
                conferidos:document.querySelectorAll('#lista-produtos-conferidos form.contagem').length,
                total:document.querySelectorAll('form.contagem').length,
                resumo:document.querySelector('#progresso-contagem').innerText,
                titulo:grupo.querySelector('summary').innerText,
                moldura:grupo.getBoundingClientRect().height>0 && !grupo.hidden,
                envio:!!botao && !botao.disabled && botao.getBoundingClientRect().height>0,
                separado:!grupo.contains(botao),
                overflow:document.documentElement.scrollWidth>innerWidth};
        })()""")
        self.assertEqual((estado['pendentes'], estado['conferidos'], estado['total']),
                         (total-conferidos, conferidos, total))
        self.assertIn(f'Faltam conferir: {total-conferidos} produtos', estado['resumo'])
        self.assertIn(f'Já conferidos: {conferidos} produtos', estado['resumo'])
        self.assertEqual(estado['titulo'], f'Produtos já conferidos ({conferidos})')
        self.assertTrue(estado['moldura'])
        self.assertTrue(estado['envio'])
        self.assertTrue(estado['separado'])
        self.assertFalse(estado['overflow'])

    def exercitar(self, total, largura):
        usuario=get_user_model().objects.create_superuser('organizacao-completa', '', 'teste')
        produtos=[produto(nome=f'Produto fictício {i:02}', unidade_compra='UN', vende_fracionado=False)
                  for i in range(total)]
        inventario=services.criar_inventario(usuario, 'Organização fictícia', 'manual', [p.pk for p in produtos])
        rodadas=list(inventario.itens.order_by('produto__nome').values_list('rodadas__pk', flat=True))
        funcionario=Funcionario.objects.create(nome='Funcionário fictício')
        acesso,token=services.gerar_acesso(usuario, inventario.pk, funcionario.pk, rodadas)
        with TemporaryDirectory(prefix='organizacao-completa-', ignore_cleanup_errors=True) as profile:
            chrome=Chrome(CHROME, profile).start()
            try:
                t=chrome.tab()
                t.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=844,deviceScaleFactor=1,mobile=True))
                t.call('Emulation.setTouchEmulationEnabled', dict(enabled=True,maxTouchPoints=1))

                def tocar(seletor):
                    ponto=t.evaluate("(() => {const e=document.querySelector("+json.dumps(seletor)+
                        ");e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
                    t.call('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[ponto]))
                    t.call('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))

                t.call('Page.navigate',dict(url=self.live_server_url+reverse('inventario:convite',args=[token])))
                t.wait("!!document.querySelector('form button')")
                tocar('form button')
                t.wait('window.inventarioContagemPronta===true')
                self.verificar_grupos(t,total,0)
                for i,rodada in enumerate(rodadas):
                    seletor=f'[data-produto-id="{rodada}"]'
                    t.evaluate("window.atual=document.querySelector("+json.dumps(seletor)+
                        ");atual.elements.embalagens.value='4';atual.elements.embalagens.dispatchEvent(new Event('input',{bubbles:true}));true")
                    tocar(seletor+' [value=ok]')
                    t.wait(f"document.querySelectorAll('#lista-produtos-conferidos form.contagem').length==={i+1} && !atual.querySelector('[data-editar-contagem]').disabled")
                    self.verificar_grupos(t,total,i+1)
                    self.assertFalse(t.evaluate("document.querySelector('#produtos-conferidos').open"))
                inventario.refresh_from_db();acesso.refresh_from_db()
                self.assertIsNone(inventario.finalizada_em)
                self.assertIsNone(acesso.enviada_em)
                self.assertTrue(t.evaluate("['#produtos-conferidos>summary','#finalizar-conferencia button'].every(s=>{const r=document.querySelector(s).getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})"))
                self.capturar(t,f'{total}-produtos-{largura}-ultimo-ok')
                tocar('#produtos-conferidos>summary')
                self.assertTrue(t.evaluate("document.querySelector('#produtos-conferidos').open && [...document.querySelectorAll('[data-editar-contagem]')].every(b=>!b.hidden&&!b.disabled)"))
                self.capturar(t,f'{total}-produtos-{largura}-abertos')
                t.call('Page.reload')
                t.wait('window.inventarioContagemPronta===true')
                self.verificar_grupos(t,total,total)
                self.assertFalse(t.evaluate("document.querySelector('#produtos-conferidos').open"))
                self.capturar(t,f'{total}-produtos-{largura}-reload')
                tocar('#produtos-conferidos>summary')
                seletor=f'[data-produto-id="{rodadas[0]}"]'
                tocar(seletor+' [data-editar-contagem]')
                tocar(seletor+' [value=salvar]')
                t.wait("document.querySelectorAll('#produtos-pendentes form.contagem').length===1")
                self.verificar_grupos(t,total,total-1)
                tocar(seletor+' [value=ok]')
                t.wait(f"document.querySelectorAll('#lista-produtos-conferidos form.contagem').length==={total}")
                self.verificar_grupos(t,total,total)
                inventario.refresh_from_db();acesso.refresh_from_db()
                self.assertIsNone(inventario.finalizada_em)
                self.assertIsNone(acesso.enviada_em)
            finally:
                chrome.stop()
        for p in produtos:
            p.refresh_from_db()
            self.assertEqual(p.quantidade,10)

    def test_dois_produtos_ultimo_ok_reload_e_recontagem(self):
        self.exercitar(2,390)

    def test_oito_produtos_ultimo_ok_reload_e_recontagem(self):
        self.exercitar(8,320)

    @staticmethod
    def capturar(tab,nome):
        pasta=Path('amostras/organizacao-mobile-20261029')
        pasta.mkdir(parents=True,exist_ok=True)
        (pasta/(nome+'.png')).write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',dict(format='png'))['data']))
