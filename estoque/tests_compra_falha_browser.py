from estoque.tests_decisoes_precos_browser import DecisoesBrowserTests


class CompraFalhaBrowserTests(DecisoesBrowserTests):
    def test_falha_libera_botao_preserva_revisao_e_reenvia(self):
        t = self.navegador()
        self.abrir(t)
        self.revisar(t)
        self.tocar(t, '#btnRevisarPrecosAgoraCompra')
        t.evaluate("""window.fetchReal280 = window.fetch; window.falhas280 = 0;
          window.fetch = async (...args) => {
            if (args[1]?.headers?.['X-Compra-Decisao'] === '1' && falhas280++ === 0)
              return new Response(JSON.stringify({ok:false,erro:'Falha sintética de finalização'}), {status:500,headers:{'Content-Type':'application/json'}});
            return fetchReal280(...args);
          }; true""")
        self.tocar(t, '#btnContinuarSemAtualizarPrecosCompra')
        t.wait("!document.querySelector('#erroSalvarPreRevisaoCompra').hidden")
        self.assertTrue(t.evaluate("!document.querySelector('#btnSalvarCompra').disabled"))
        self.assertEqual(t.evaluate("document.querySelector('#decisaoPrecosCompra').value"), 'revisado')
        self.assertTrue(t.evaluate("!!document.querySelector('#previaDecisaoPrecosCompra').value"))
        self.tocar(t, '#btnSalvarCompra')
        self.conferir_final(t)

    def test_unidade_corrigida_remove_aviso_desktop_mobile(self):
        self.compra.itens.update(unidade='2,95')
        t = self.navegador()
        for width, mobile in [(1280, False), (390, True)]:
            t.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=mobile))
            self.abrir(t)
            self.assertTrue(t.evaluate("!document.querySelector('#tabelaItensCompra tbody .aviso-unidade-compra').hidden"))
            base = t.evaluate("document.querySelector('#tabelaItensCompra tbody [name=\"produto_id[]\"]').selectedOptions[0].dataset.unidadeEstoque")
            import json
            t.evaluate("const u=document.querySelector('#tabelaItensCompra tbody [name=\"unidade[]\"]');u.value=" + json.dumps(base) + ";u.dispatchEvent(new Event('input',{bubbles:true}));true")
            self.assertTrue(t.evaluate("document.querySelector('#tabelaItensCompra tbody .aviso-unidade-compra').hidden"))
            self.assertFalse(t.evaluate("!!document.querySelector('[data-item-provisorio=\"1\"] .aviso-unidade-compra:not([hidden])')"))
            self.assertTrue(t.evaluate("document.querySelector('#tabelaItensCompra tbody [name=\"unidade[]\"]').checkValidity()"))
            t.evaluate("document.querySelector('#tabelaItensCompra tbody [name=\"unidade[]\"]').value='2,95';document.querySelector('#tabelaItensCompra tbody [name=\"unidade[]\"]').dispatchEvent(new Event('input',{bubbles:true}));true")
            self.assertFalse(t.evaluate("document.querySelector('#tabelaItensCompra tbody [name=\"unidade[]\"]').checkValidity()"))
    def test_timeout_libera_botao_sem_apagar_revisao(self):
        t = self.navegador()
        self.abrir(t)
        self.revisar(t)
        self.tocar(t, '#btnRevisarPrecosAgoraCompra')
        t.evaluate("""window.fetchReal280 = window.fetch; window.timerReal280 = window.setTimeout;
          window.setTimeout = (fn, ms, ...args) => timerReal280(fn, ms === 30000 ? 100 : ms, ...args);
          window.fetch = (...args) => {
            if (args[1]?.headers?.['X-Compra-Decisao'] === '1')
              return new Promise((resolve, reject) => args[1].signal.addEventListener('abort', () => reject(new DOMException('Tempo esgotado', 'AbortError'))));
            return fetchReal280(...args);
          }; true""")
        self.tocar(t, '#btnContinuarSemAtualizarPrecosCompra')
        t.wait("!document.querySelector('#erroSalvarPreRevisaoCompra').hidden")
        self.assertIn('demorou', t.evaluate("document.querySelector('#erroSalvarPreRevisaoCompra').textContent"))
        self.assertTrue(t.evaluate("!document.querySelector('#btnSalvarCompra').disabled"))
        self.assertEqual(t.evaluate("document.querySelector('#decisaoPrecosCompra').value"), 'revisado')
        self.compra.refresh_from_db()
        self.assertFalse(self.compra.estoque_entrada_realizada)
