"""Percurso visual completo de unidade inválida até compra concluída; somente fixtures."""
import base64
from pathlib import Path
from django.urls import reverse
from estoque.models import Compra, MovimentoFinanceiro
from estoque.tests_decisoes_precos_browser import DecisoesBrowserTests


class UnidadeFinalBrowserTests(DecisoesBrowserTests):
    def capturar_unidade(self, t, nome):
        pasta = Path('amostras/unidade-final')
        pasta.mkdir(parents=True, exist_ok=True)
        clip = t.evaluate("(() => {const r=document.querySelector('#secaoItensCompra').getBoundingClientRect();return {x:Math.max(0,r.x+scrollX),y:Math.max(0,r.y+scrollY),width:r.width,height:r.height,scale:1}})()")
        resultado = t.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True,'clip':clip})
        (pasta / (nome+'.png')).write_bytes(base64.b64decode(resultado['data']))

    def percurso(self, largura):
        self.produto.unidade_compra = 'UN'
        self.produto.unidade_venda_1 = 'UN'
        self.produto.save()
        self.compra.itens.update(unidade='2,95')
        t = self.navegador()
        t.call('Emulation.setDeviceMetricsOverride', dict(width=largura,height=900,deviceScaleFactor=1,mobile=largura<820))
        self.abrir(t)
        t.evaluate("window.linhaUnidade=document.querySelector('#tabelaItensCompra tbody .linha-item');window.campoUnidade=linhaUnidade.querySelector('[name=\"unidade[]\"]');true")
        self.assertEqual(t.evaluate('campoUnidade.value'), '')
        self.assertEqual(t.evaluate('[...campoUnidade.options].map(o=>o.value)'), ['', 'UN'])
        self.assertNotIn('2,95', t.evaluate("document.querySelector('#secaoItensCompra').innerText"))
        self.assertIn('Unidade inválida. Selecione uma unidade cadastrada.', t.evaluate("linhaUnidade.querySelector('.aviso-unidade-compra').textContent"))
        self.assertFalse(t.evaluate("!!document.querySelector('#avisoUnidadesCompra')"))
        self.assertFalse(t.evaluate("!!linhaUnidade.querySelector('.compra-unidade-label')"))
        valores = t.evaluate("[linhaUnidade.querySelector('[name=\"quantidade[]\"]').value,linhaUnidade.querySelector('.campo-preco-unitario-compra').value,linhaUnidade.querySelector('.valor-total-item').textContent,document.querySelector('#previaDecisaoPrecosCompra').value]")
        # Tentativa real de concluir: deve focalizar Unidade, sem abrir financeiro.
        self.tocar(t, '#btnSalvarCompra')
        self.assertTrue(t.evaluate('document.activeElement===campoUnidade'))
        self.assertFalse(t.evaluate("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')"))
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(), 0)
        self.capturar_unidade(t, f'{largura}-invalida')
        t.call('Input.dispatchKeyEvent',dict(type='keyDown',key='ArrowDown',code='ArrowDown',windowsVirtualKeyCode=40))
        t.call('Input.dispatchKeyEvent',dict(type='keyUp',key='ArrowDown',code='ArrowDown',windowsVirtualKeyCode=40))
        self.assertEqual(t.evaluate('campoUnidade.value'), 'UN')
        self.assertTrue(t.evaluate("linhaUnidade.querySelector('.aviso-unidade-compra').hidden"))
        self.assertFalse(t.evaluate("!!document.querySelector('#avisoUnidadesCompra')"))
        self.assertEqual(valores, t.evaluate("[linhaUnidade.querySelector('[name=\"quantidade[]\"]').value,linhaUnidade.querySelector('.campo-preco-unitario-compra').value,linhaUnidade.querySelector('.valor-total-item').textContent,document.querySelector('#previaDecisaoPrecosCompra').value]"))
        self.capturar_unidade(t, f'{largura}-corrigida')
        if largura < 820:
            self.tocar(t, '#tabelaItensCompra .btn-salvar-edicao-item-mobile')
        if largura < 820:
            self.revisar(t)
            self.tocar(t, '#btnRevisarPrecosAgoraCompra')
            self.tocar(t, '#btnContinuarSemAtualizarPrecosCompra')
        else:
            self.tocar(t, '#btnSalvarCompra')
            t.wait("document.querySelector('#modalConferenciaPrecosCompra').classList.contains('visivel')")
            self.tocar(t, '#btnContinuarSemAtualizarPrecosCompra')
            t.wait("document.querySelector('#origemDinheiroCompra').classList.contains('visivel')")
            t.evaluate("document.querySelector('#origemCaixaCompra').value='0,00';document.querySelector('#origemReservaCompra').value='0,00';document.querySelector('#origemBancoCompra').value='555,00';document.querySelector('#origemBancoCompra').dispatchEvent(new Event('input',{bubbles:true}));true")
            self.tocar(t, '#btnConfirmarFechamentoCompra')
        compra = self.conferir_final(t)
        self.assertEqual(compra.itens.get().unidade, 'UN')
        self.assertEqual(compra.total, 555)
        self.assertFalse(compra.revisao_precos_pendente)
        self.client.post(reverse('estoque:compra_finalizar', args=[compra.pk]), self.payload())
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 25)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.get(compra=compra).valor, 555)
        print(f'UNIDADE_FINAL_VISUAL largura={largura} corrigida=UN quantidade=15 custo=37 total=555 movimento_unico=1', flush=True)

    def test_percurso_desktop(self):
        self.percurso(1280)

    def test_percurso_mobile(self):
        self.percurso(390)
