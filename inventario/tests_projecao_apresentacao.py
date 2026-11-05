"""Read-only rendering and static desktop/mobile preview; no web server/login."""
import base64
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse
from .conversao import snapshot
from .models import ItemInventario
from .tests_ajustes import AjustesFixture
from .tests_projecao_fisica import AplicacaoProjecaoTests


class ApresentacaoFisicaTests(AjustesFixture, TestCase):
    preparar = AplicacaoProjecaoTests.preparar

    def preparar_pagina(self):
        for nome, fator, saldo, pacotes, avulsos in [
                ('Amstel fictícia 12/350ml', 12, '7.084', 7, 1),
                ('Coca-Cola fictícia 6/1L', 6, '4', 3, 5),
                ('Fanta fictícia 6/2L', 6, '3.500', 3, 2)]:
            p, rodada = self.preparar(fator, saldo, pacotes, avulsos)
            p.nome = nome
            p.save(update_fields=['nome'])
            ItemInventario.objects.filter(pk=rodada.item_id).update(cadastro=snapshot(p))
        return self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))

    def test_textos_fisicos_e_pendencias_sem_aplicacao(self):
        from .models import AplicacaoAjuste
        r = self.preparar_pagina()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '3 PCT + 3 UN = 21 UN')
        self.assertContains(r, '3 PCT + 2 UN = 20 UN')
        self.assertContains(r, 'Falta 1 UN', count=2)
        self.assertContains(r, 'sem falta física comprovada')
        self.assertContains(r, 'Pendente de regularização')
        self.assertNotContains(r, '−1,002 UN')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_previa_estatica_desktop_e_celular(self):
        from offline.browser_support import Chrome
        from .tests_browser import CHROME
        self.assertTrue(Path(CHROME).is_file(), 'Chrome local necessário para validar o layout.')
        r = self.preparar_pagina()
        html = r.content.decode('utf-8')
        root = Path(__file__).resolve().parents[1]
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S | re.I)
        html = re.sub(r'<input\b[^>]*name="(?:csrfmiddlewaretoken|token|previa)"[^>]*>', '', html)
        html = re.sub(r'href="/static/([^"]+)"',
                      lambda m: 'href="' + (root/'static'/m[1]).as_uri() + '"', html)
        html = html.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" '
            'content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39; file:; '
            'img-src data:; form-action &#39;none&#39;">')
        html = html.replace('<body>', '<body><p style="padding:16px;background:#fff6d5">'
            'PRÉVIA ESTÁTICA — DADOS FICTÍCIOS. Nenhum ajuste pode ser executado por este arquivo.</p>')
        destino = root/'diagnostics'/'conferencia-projecao'
        destino.mkdir(parents=True, exist_ok=True)
        previa = destino/'previa.html'
        previa.write_text(html, encoding='utf-8')
        with TemporaryDirectory(prefix='conferencia-estatica-') as perfil:
            chrome = Chrome(CHROME, perfil).start()
            try:
                tab = chrome.tab()
                for largura in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=1000,
                        deviceScaleFactor=1, mobile=largura < 768))
                    tab.call('Page.navigate', dict(url=previa.as_uri()))
                    tab.wait("document.readyState === 'complete' && !!document.querySelector('.tabela-resumo')")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertIn('Falta 1 UN', tab.evaluate('document.body.innerText'))
                    self.assertNotIn('−1,002 UN', tab.evaluate('document.body.innerText'))
                    tab.evaluate("document.querySelector('.tabela-resumo').scrollIntoView(); true")
                    captura = tab.call('Page.captureScreenshot', dict(format='png', captureBeyondViewport=False))
                    (destino/f'comparacao-{largura}.png').write_bytes(base64.b64decode(captura['data']))
                    tab.evaluate("document.querySelectorAll('.representacao-fisica').forEach(e=>e.open=true); true")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            finally:
                chrome.stop()
