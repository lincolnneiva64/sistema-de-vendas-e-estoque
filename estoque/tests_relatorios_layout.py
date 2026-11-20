"""Layout dos cartões reais em Edge isolado; sem settings operacionais ou banco.

python -m unittest estoque.tests_relatorios_layout -v
"""
import json
import os
import subprocess
from pathlib import Path
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from django.template import Context, Engine
from offline.browser_support import Chrome


ROOT = Path(__file__).resolve().parents[1]
BROWSER = os.environ.get('OFFLINE_TEST_CHROME', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


@unittest.skipUnless(Path(BROWSER).is_file(), 'Configure OFFLINE_TEST_CHROME para Edge/Chrome')
class CartoesResumoLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from django.conf import settings
        if not settings.configured:
            settings.configure(USE_I18N=False, USE_TZ=False)
        cls.perfil = TemporaryDirectory(prefix='relatorios-cartoes-', ignore_cleanup_errors=True)
        cls.browser = Chrome(BROWSER, cls.perfil.name)
        original = subprocess.Popen
        with patch('offline.browser_support.subprocess.Popen', side_effect=lambda cmd, **kwargs:
                   original([cmd[0], '--no-sandbox', *cmd[1:]], **kwargs)):
            cls.browser.start()
        cls.css = (ROOT/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        source = (ROOT/'estoque/templates/estoque/relatorios/vendas.html').read_text(encoding='utf-8')
        start = source.index('<dl class="relatorios-indicadores">')
        cls.template = Engine(libraries={'relatorios': 'estoque.templatetags.relatorios'}).from_string(
            '{% load relatorios %}'+source[start:source.index('</dl>', start)+5])

    @classmethod
    def tearDownClass(cls):
        cls.browser.stop()
        cls.perfil.cleanup()

    def setUp(self):
        self.tab = self.browser.tab()

    def tearDown(self):
        self.tab.call('Page.close')
        self.tab.socket.close()

    def preparar(self, largura, grande=False):
        valor = Decimal('1234567.89') if grande else Decimal('43064.76')
        resumo = {'faturamento': valor, 'quantidade': 9876, 'ticket_medio': Decimal('1234.56'),
                  'vista': {'valor': valor if grande else Decimal('34642.36'), 'percentual': Decimal('80.44'), 'quantidade': 1234},
                  'prazo': {'valor': valor if grande else Decimal('8422.40'), 'percentual': Decimal('19.56'), 'quantidade': 234}}
        html = self.template.render(Context({'resumo': resumo}, use_l10n=False))
        self.tab.call('Emulation.setDeviceMetricsOverride', {'width': largura, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': largura < 600})
        self.tab.evaluate('document.head.innerHTML='+json.dumps('<meta name="viewport" content="width=device-width, initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;font:16px system-ui}'+self.css+'</style>')+';')
        self.tab.evaluate('document.body.innerHTML='+json.dumps('<main class="relatorios relatorios-resumo">'+html+'</main>')+';')
        return self.tab.evaluate('''(()=>{
            const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
            return {pageWidth:document.documentElement.scrollWidth,viewport:innerWidth,
              cards:[...document.querySelectorAll('.relatorios-indicadores > div')].map(c=>{
                const valor=c.querySelector('dd'),titulo=c.querySelector('dt');
                const range=document.createRange();range.selectNodeContents(valor);
                return {box:rect(c),valor:rect(valor),texto:rect(range),titulo:rect(titulo),
                    linhas:range.getClientRects().length,conteudo:valor.textContent,
                    scroll:valor.scrollWidth,client:valor.clientWidth,font:parseFloat(getComputedStyle(valor).fontSize),
                    legendas:[...c.querySelectorAll('.indicador-legenda span')].map(rect)};
              })};
        })()''')

    def conferir_sem_cortes(self, medidas):
        self.assertEqual(len(medidas['cards']), 5)
        self.assertLessEqual(medidas['pageWidth'], medidas['viewport'])
        alturas = [c['box']['height'] for c in medidas['cards']]
        self.assertLess(max(alturas)-min(alturas), 1)
        self.assertLessEqual(max(alturas), 160)
        for c in medidas['cards']:
            self.assertEqual(c['linhas'], 1, c['conteudo'])
            self.assertLessEqual(c['scroll'], c['client']+1, c['conteudo'])
            self.assertGreaterEqual(c['font'], 18)
            self.assertGreaterEqual(c['texto']['left'], c['box']['left'])
            self.assertLessEqual(c['texto']['right'], c['box']['right'])
        offsets = [c['valor']['top']-c['box']['top'] for c in medidas['cards']]
        self.assertLess(max(offsets)-min(offsets), 1)
        for a, b in zip(medidas['cards'], medidas['cards'][1:]):
            self.assertTrue(a['box']['right'] <= b['box']['left'] or a['box']['bottom'] <= b['box']['top'])

    def test_desktop_cinco_alinhados_e_reais_na_mesma_linha(self):
        for largura in (1100, 1280, 1366, 1920):
            with self.subTest(largura=largura):
                medidas = self.preparar(largura)
                self.conferir_sem_cortes(medidas)
                self.assertEqual(len({c['box']['top'] for c in medidas['cards']}), 1)
                self.assertEqual(medidas['cards'][0]['conteudo'], 'R$ 43.064,76')
                self.assertEqual(medidas['cards'][3]['conteudo'], 'R$ 34.642,36')

    def test_mobile_tablet_e_valores_maiores_sem_cortes(self):
        for largura in (320, 360, 390, 400, 559, 560, 768, 1024, 1100, 1366):
            with self.subTest(largura=largura):
                self.conferir_sem_cortes(self.preparar(largura, grande=True))

    def test_quantidade_centralizada_e_legendas_alinhadas(self):
        for largura in (320, 768, 1366):
            with self.subTest(largura=largura):
                cards = self.preparar(largura)['cards']
                numero = cards[1]
                self.assertAlmostEqual(numero['texto']['left']+numero['texto']['width']/2,
                                       numero['valor']['left']+numero['valor']['width']/2, delta=1)
                vista, prazo = cards[3], cards[4]
                self.assertEqual(len(vista['legendas']), 2)
                self.assertEqual(len(prazo['legendas']), 2)
                for indice in (0, 1):
                    self.assertAlmostEqual(vista['legendas'][indice]['top']-vista['box']['top'],
                                           prazo['legendas'][indice]['top']-prazo['box']['top'], delta=1)

    def test_comparativo_semanal_real_sinais_datas_e_sem_cortes(self):
        from datetime import date
        from estoque.analise_evolucao import analisar
        dias = [{'data_venda': date(2026, 11, 9), 'faturamento': Decimal('100')},
                {'data_venda': date(2026, 11, 16), 'faturamento': Decimal('75')}]
        analise = analisar(dias, date(2026, 11, 9), date(2026, 11, 19),
            date(2026, 11, 19), 'semanal', 0,
            referencia_dias=[{'data_venda': date(2026, 11, 2), 'faturamento': Decimal('80')}])
        self.conferir_grafico(analise)

    def test_comparativo_mensal_real_visivel_e_destacado(self):
        from datetime import date
        from estoque.analise_evolucao import analisar
        analise = analisar([{'data_venda': date(2026, 11, 20), 'faturamento': Decimal('75')}],
            date(2026, 11, 1), date(2026, 11, 20), date(2026, 11, 20), 'mensal', 0)
        comp = dict(tipo='parcial', rotulo='Parcial equivalente', inicio='2026-11-01', fim='2026-11-20',
            atual=Decimal('75'), anterior=Decimal('100'), anterior_inicio='2026-10-01',
            anterior_fim='2026-10-20', inclui_hoje=True)
        analise['itens'][0].update(comparacao=comp, diferenca=Decimal('-25'), variacao=Decimal('-25.00'))
        analise['comparacao_disponivel'] = True
        analise['equivalente'] = dict(atual=Decimal(75), anterior=Decimal(100), diferenca=Decimal(-25),
            variacao=Decimal('-25.00'), atual_inicio=date(2026, 11, 1), atual_fim=date(2026, 11, 20),
            anterior_inicio=date(2026, 10, 1), anterior_fim=date(2026, 10, 20), inclui_hoje=True)
        self.conferir_grafico(analise, mensal=True)

    def test_par_mensal_valores_maiores_e_ancora_estavel_apos_desenhar(self):
        from datetime import date
        from estoque.analise_evolucao import analisar
        analise = analisar([], date(2026, 11, 1), date(2026, 11, 20), date(2026, 11, 20), 'mensal', 0)
        analise['equivalente'] = dict(atual=Decimal('43206.21'), anterior=Decimal('46869.81'),
            diferenca=Decimal('-3663.60'), variacao=Decimal('-7.82'),
            atual_inicio=date(2026, 11, 1), atual_fim=date(2026, 11, 20),
            anterior_inicio=date(2026, 10, 1), anterior_fim=date(2026, 10, 20), inclui_hoje=True)
        analise['comparacao_disponivel'] = True
        self.conferir_grafico(analise, mensal=True, grande=True)

    def test_indicadores_3_6_12_24_meses_paginacao_previsao_e_metodo_mobile(self):
        from datetime import date
        from estoque.analise_evolucao import analisar_historico
        # Actual report template/CSS/JS, rendered in memory; no HTTP server.
        hoje = date(2026,11,20)
        dados = []
        for ordinal in range(date(2024,11,1).toordinal(),hoje.toordinal()+1):
            dados.append(dict(data_venda=date.fromordinal(ordinal),faturamento=Decimal('100'),quantidade=1,sem_valor=0))
        source = (ROOT/'estoque/templates/estoque/relatorios/graficos.html').read_text(encoding='utf-8')
        source = source.replace("{% include 'estoque/relatorios/evolucao_controles.html' %}", '')
        template = Engine(libraries={'relatorios':'estoque.templatetags.relatorios','l10n':'django.templatetags.l10n'}).from_string(source)
        script = (ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        for meses in (3,6,12,24):
            ano,mes = divmod(hoje.year*12+hoje.month-meses,12)
            inicio = date(ano,mes+1,1)
            analise = analisar_historico(dados,inicio,hoje,hoje,'mensal',inicio_registros=date(2024,11,1))
            html = template.render(Context({'resumo':{'analise_evolucao':analise,'grafico_evolucao':analise,'participacoes':[]},
                'inicio':inicio,'fim':hoje},use_l10n=False))
            for largura in (320,390,768,1366):
                with self.subTest(meses=meses,largura=largura):
                    self.preparar(largura)
                    self.tab.evaluate('document.querySelector("main").innerHTML='+json.dumps(html))
                    self.tab.evaluate(script)
                    vistos=[]
                    escala=None
                    while True:
                        self.assertLessEqual(self.tab.evaluate("document.querySelectorAll('.grafico-barra-dia').length"),6)
                        medidas=self.tab.evaluate('''[...document.querySelectorAll('.grafico-barra-dia')].map(i=>{
                            const r=e=>e.getBoundingClientRect(); const m=i.querySelector('.grafico-barra-indicadores');
                            return {data:i.dataset.data,texto:m.innerText,corte:m.scrollWidth>m.clientWidth+1,
                                top:r(m).top,variacao:r(i.querySelector('.grafico-barra-variacao')).bottom,
                                bottom:r(m).bottom,limite:r(i).bottom,
                                valor:JSON.parse(document.getElementById('relatorios-barras-dados').textContent).itens.find(p=>p.inicio===i.dataset.data).valor,
                                altura:r(i.querySelector('.grafico-barra')).height,
                                fontes:[...m.querySelectorAll('strong')].map(e=>parseFloat(getComputedStyle(e).fontSize)),
                                previsao:[...m.querySelectorAll('.indicadores-previsao')].map(e=>getComputedStyle(e).color)};})''')
                        for m in medidas:
                            vistos.append(m['data'])
                            self.assertIn('Ticket médio:',m['texto'])
                            self.assertIn('Média diária:',m['texto'])
                            self.assertFalse(m['corte'])
                            self.assertGreaterEqual(m['top'],m['variacao']+7)
                            self.assertLessEqual(m['bottom'],m['limite'])
                            fator=m['altura']/float(m['valor'])
                            if escala is None:escala=fator
                            self.assertAlmostEqual(fator,escala,delta=.001)
                            for fonte in m['fontes']:self.assertGreaterEqual(fonte,16)
                            for cor in m['previsao']:self.assertEqual(cor,'rgb(159, 29, 45)')
                        if self.tab.evaluate("document.querySelector('[data-barras-proximo]').disabled"):break
                        self.tab.evaluate("document.querySelector('[data-barras-proximo]').click()")
                    self.assertEqual(len(vistos),meses)
                    self.assertEqual(len(set(vistos)),meses)
                    self.assertEqual(vistos[-1],'2026-11-01')
                    texto=self.tab.evaluate("document.querySelector('.indicadores-metodo').innerText")
                    self.assertIn('Como foi estimada',texto)
                    self.tab.evaluate("document.querySelector('.indicadores-metodo').open=true")
                    self.tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
                    self.assertIn('O restante de hoje',self.tab.evaluate("document.querySelector('.indicadores-metodo').innerText"))
                    self.assertTrue(self.tab.evaluate("(()=>{const m=document.querySelector('.indicadores-metodo');return m.getBoundingClientRect().bottom<=m.closest('li').getBoundingClientRect().bottom;})()"))

    def test_fantasmas_legenda_escala_zero_cobertura_e_sem_sobreposicao(self):
        from datetime import date
        from estoque.analise_evolucao import analisar_historico
        def dias(a,b,valor):
            return [dict(data_venda=date.fromordinal(n),faturamento=Decimal(valor) if n==a.toordinal() else Decimal(0),
                quantidade=1,sem_valor=0) for n in range(a.toordinal(),b.toordinal()+1)]
        a,b=date(2026,9,7),date(2026,9,30)
        teorico=dias(a,b,'100')
        casos=(('teorico',teorico,date(2026,9,1),b,date(2026,11,20),a,125),
               ('zero',dias(a,b,'0'),date(2026,9,1),b,date(2026,11,20),a,0),
               ('lacuna',[d for d in teorico if d['data_venda']!=date(2026,9,15)],date(2026,9,1),b,date(2026,11,20),a,None),
               ('completo',dias(date(2026,10,1),date(2026,10,31),'100'),date(2026,10,1),date(2026,10,31),date(2026,11,20),date(2026,10,1),None),
               ('sem_historico',[],date(2026,9,1),b,date(2026,11,20),None,None),
               ('previsao',dias(date(2026,11,1),date(2026,11,19),'100'),date(2026,11,1),date(2026,11,20),date(2026,11,20),date(2026,11,1),Decimal('152.63')))
        source=(ROOT/'estoque/templates/estoque/relatorios/graficos.html').read_text(encoding='utf-8').replace("{% include 'estoque/relatorios/evolucao_controles.html' %}",'')
        template=Engine(libraries={'relatorios':'estoque.templatetags.relatorios','l10n':'django.templatetags.l10n'}).from_string(source)
        script=(ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        for nome,dados,inicio,fim,hoje,primeiro,estimativa in casos:
            analise=analisar_historico(dados,inicio,fim,hoje,'mensal',inicio_registros=primeiro)
            html=template.render(Context({'resumo':{'analise_evolucao':analise,'grafico_evolucao':analise,'participacoes':[]},
                'inicio':inicio,'fim':fim},use_l10n=False))
            for largura in (320,390,768,1366):
                with self.subTest(caso=nome,largura=largura):
                    self.preparar(largura)
                    self.tab.evaluate('document.querySelector("main").innerHTML='+json.dumps(html))
                    self.tab.evaluate(script)
                    m=self.tab.evaluate('''(()=>{
                        const item=document.querySelector('.grafico-barra-dia'), real=item.querySelector('.grafico-barra'),
                            ghost=item.querySelector('.grafico-barra-fantasma'), legend=document.querySelector('.grafico-legenda-mensal');
                        const r=e=>{const x=e.getBoundingClientRect();return {top:x.top,bottom:x.bottom,left:x.left,right:x.right,height:x.height,width:x.width};};
                        return {real:r(real),hidden:real.hidden,ghost:ghost?r(ghost):null,estilo:ghost?getComputedStyle(ghost).borderStyle:null,
                            label:ghost?ghost.getAttribute('aria-label'):'',legenda:legend.innerText,legend:r(legend),
                            janela:r(document.querySelector('.grafico-barras-janela')),zero:r(document.querySelector('.grafico-zero-linha')),
                            data:r(item.querySelector('.grafico-barra-data')),page:document.documentElement.scrollWidth,
                            realEstilo:getComputedStyle(real).borderStyle};})()''')
                    self.assertLessEqual(m['page'],largura+1)
                    self.assertIn('Barra sólida: Faturamento realizado.',m['legenda'])
                    self.assertIn('Valores estimados não representam vendas realizadas.',m['legenda'])
                    self.assertLessEqual(m['legend']['bottom'],m['janela']['top'])
                    if estimativa is None:
                        self.assertIsNone(m['ghost'])
                    else:
                        self.assertIsNotNone(m['ghost'])
                        self.assertLess(m['ghost']['width'],m['real']['width'])
                        self.assertGreaterEqual(m['ghost']['left'],m['real']['right'])
                        self.assertAlmostEqual(m['ghost']['bottom'],m['zero']['top'],delta=1)
                        self.assertIn('Previsão de fechamento' if nome=='previsao' else 'Equivalente teórico',m['label'])
                        self.assertGreaterEqual(m['data']['top'],m['ghost']['bottom'])
                        if estimativa:
                            self.assertEqual(m['estilo'],'dashed')
                            self.assertGreaterEqual(m['ghost']['height'],180)
                            self.assertAlmostEqual(m['real']['height']/100,m['ghost']['height']/float(estimativa),delta=.001)
                        else:
                            self.assertEqual(m['ghost']['height'],0)
                    if nome=='sem_historico':self.assertTrue(m['hidden'])
                    else:self.assertAlmostEqual(m['real']['bottom'],m['zero']['top'],delta=1)
                    self.assertNotEqual(m['realEstilo'],'dashed')

    def test_rotulos_dos_tres_meses_centrados_na_barra_e_estimativas_verticais(self):
        from datetime import date
        from estoque.analise_evolucao import analisar_historico
        primeiro,hoje=date(2026,9,7),date(2026,11,20)
        valores={primeiro:Decimal('55699.17'),date(2026,10,1):Decimal('71692.85'),date(2026,11,1):Decimal('43206.21')}
        dados=[dict(data_venda=date.fromordinal(n),faturamento=valores.get(date.fromordinal(n),Decimal(0)),quantidade=1,sem_valor=0)
            for n in range(primeiro.toordinal(),hoje.toordinal())]
        analise=analisar_historico(dados,date(2026,9,1),hoje,hoje,'mensal',inicio_registros=primeiro)
        source=(ROOT/'estoque/templates/estoque/relatorios/graficos.html').read_text(encoding='utf-8').replace("{% include 'estoque/relatorios/evolucao_controles.html' %}",'')
        template=Engine(libraries={'relatorios':'estoque.templatetags.relatorios','l10n':'django.templatetags.l10n'}).from_string(source)
        html=template.render(Context({'resumo':{'analise_evolucao':analise,'grafico_evolucao':analise,'participacoes':[]},
            'inicio':date(2026,9,1),'fim':hoje},use_l10n=False))
        script=(ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        for largura in (320,390,768,1024,1366):
            with self.subTest(largura=largura):
                self.preparar(largura)
                self.tab.evaluate('document.querySelector("main").innerHTML='+json.dumps(html))
                self.tab.evaluate(script)
                medidas=self.tab.evaluate('''[...document.querySelectorAll('.grafico-barra-dia')].map(i=>{
                    const r=e=>{const b=e.getBoundingClientRect();return {left:b.left,right:b.right,top:b.top,bottom:b.bottom,width:b.width,height:b.height};};
                    const v=i.querySelector('.grafico-barra-valor'),b=i.querySelector('.grafico-barra'),
                        f=i.querySelector('.grafico-barra-fantasma'),n=i.querySelector('.grafico-barra-fantasma-valor');
                    return {item:r(i),valor:r(v),barra:r(b),texto:v.innerText,
                        fantasma:f?r(f):null,estimado:n?r(n):null,estimadoTexto:n?n.innerText:null,
                        escrita:n?getComputedStyle(n).writingMode:null,transform:n?getComputedStyle(n).transform:null,
                        data:r(i.querySelector('.grafico-barra-data'))};})''')
                self.assertEqual([m['texto'] for m in medidas],['R$ 55.699,17','R$ 71.692,85','R$ 43.206,21'])
                self.assertEqual([m['estimadoTexto'] for m in medidas],['R$ 69.623,96',None,'R$ 65.946,32'])
                for m in medidas:
                    self.assertAlmostEqual((m['valor']['left']+m['valor']['right'])/2,
                        (m['barra']['left']+m['barra']['right'])/2,delta=.5)
                    self.assertLessEqual(m['valor']['bottom'],m['barra']['top']+1)
                    self.assertGreaterEqual(m['valor']['left'],m['item']['left'])
                    self.assertLessEqual(m['valor']['right'],m['item']['right'])
                    if m['fantasma']:
                        self.assertGreaterEqual(m['estimado']['left'],m['fantasma']['right']+3)
                        self.assertLessEqual(m['estimado']['right'],m['item']['right'])
                        self.assertLessEqual(m['estimado']['bottom'],m['data']['top'])
                        self.assertTrue(m['valor']['right']<=m['estimado']['left'] or m['valor']['bottom']<=m['estimado']['top'])
                        self.assertEqual(m['escrita'],'vertical-rl')
                        self.assertEqual(m['transform'],'matrix(-1, 0, 0, -1, 0, 0)')

    def conferir_grafico(self, analise, mensal=False, grande=False):
        from datetime import date
        from estoque.graficos_relatorios import barras_evolucao
        source = (ROOT/'estoque/templates/estoque/relatorios/graficos.html').read_text(encoding='utf-8')
        # Controls are covered by full Django requests; measure the actual chart
        # fragment and its actual script without starting an HTTP server.
        source = source.replace("{% include 'estoque/relatorios/evolucao_controles.html' %}", '')
        template = Engine(libraries={'relatorios': 'estoque.templatetags.relatorios',
            'l10n': 'django.templatetags.l10n'}).from_string(source)
        html = template.render(Context({'resumo': {'analise_evolucao': analise,
            'grafico_evolucao': barras_evolucao(analise), 'participacoes': []},
            'inicio': date(2026, 11, 9), 'fim': date(2026, 11, 19)}, use_l10n=False))
        script = (ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        for largura in (320, 390, 768, 1366):
            self.preparar(largura)
            self.tab.evaluate('document.querySelector("main").insertAdjacentHTML("beforeend",'+json.dumps(html)+')')
            self.tab.evaluate("document.querySelector('main').insertAdjacentHTML('afterbegin','<div style=\"height:1400px\"></div>');location.hash='';location.hash='evolucao-vendas';")
            self.tab.evaluate(script)
            # Compare frames after the one intentional initial framing, rather
            # than requiring the pre-JavaScript anchor position to be retained.
            self.tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
            antes = self.tab.evaluate('scrollY')
            self.assertGreaterEqual(self.tab.evaluate('document.getElementById("titulo-evolucao-vendas").getBoundingClientRect().top'), 0)
            self.tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,80));})()')
            self.assertAlmostEqual(self.tab.evaluate('scrollY'), antes, delta=2)
            linhas = self.tab.evaluate('''[...document.querySelectorAll('.grafico-barra-dia')].map(c=>{
                const d=c.querySelector('.grafico-barra-data').getBoundingClientRect();
                const v=c.querySelector('.grafico-barra-variacao');
                const t=c.querySelector('.grafico-barra-valor');
                const valores=[...v.querySelectorAll('.comparacao-valor')];
                return {texto:v.innerText,data:c.querySelector('.grafico-barra-data').innerText,
                    total:c.querySelector('.grafico-barra-valor').innerText,
                    totalFonte:parseFloat(getComputedStyle(t).fontSize),totalPeso:parseFloat(getComputedStyle(t).fontWeight),
                    totalCortado:t.scrollWidth>t.clientWidth+1,
                    coluna:c.getBoundingClientRect().left,linha:c.getBoundingClientRect().top,
                    antes:d.bottom, top:v.getBoundingClientRect().top,
                    bottom:v.getBoundingClientRect().bottom, limite:c.getBoundingClientRect().bottom,
                    corte:v.scrollWidth>v.clientWidth+1,
                    valores:valores.map(e=>({peso:parseFloat(getComputedStyle(e).fontWeight),
                        fonte:parseFloat(getComputedStyle(e).fontSize),cor:getComputedStyle(e).color})),
                    dataFonte:parseFloat(getComputedStyle(c.querySelector('.grafico-barra-data')).fontSize)};})''')
            if not mensal:
                self.assertIn('+R$ 20,00', linhas[0]['texto'])
                self.assertIn('+25,00%', linhas[0]['texto'])
                self.assertIn('Completa vs completa', linhas[0]['texto'])
                self.assertEqual(linhas[0]['valores'][0]['cor'], 'rgb(23, 97, 62)')
            ultima = linhas[-1]
            self.assertIn('-R$ 3.663,60' if grande else '-R$ 25,00', ultima['texto'])
            self.assertIn('-7,82%' if grande else '-25,00%', ultima['texto'])
            if mensal:
                self.assertEqual(len(linhas), 2)
                self.assertEqual(linhas[0]['linha'], linhas[1]['linha'])
                self.assertLess(linhas[0]['coluna'], linhas[1]['coluna'])
                self.assertIn('01/10/2026 a 20/10/2026', linhas[0]['data'])
                self.assertIn('Outubro', linhas[0]['data'])
                self.assertIn('Novembro', linhas[1]['data'])
                self.assertNotIn('Sem período comparável', linhas[0]['texto'])
                if grande:
                    self.assertEqual(linhas[0]['total'], 'R$ 46.869,81')
                    self.assertEqual(linhas[1]['total'], 'R$ 43.206,21')
            else:
                self.assertIn('Parcial equivalente', ultima['texto'])
                self.assertIn('09/11/2026 a 12/11/2026', ultima['texto'])
            self.assertEqual(ultima['valores'][0]['cor'], 'rgb(165, 32, 44)')
            for linha in linhas:
                self.assertGreaterEqual(linha['totalFonte'], 14)
                self.assertGreaterEqual(linha['totalPeso'], 700)
                self.assertFalse(linha['totalCortado'])
                for valor in linha['valores']:
                    self.assertGreaterEqual(valor['peso'], 700)
                    self.assertGreaterEqual(valor['fonte'], 16)
                self.assertGreaterEqual(linha['dataFonte'], 13)
                self.assertLessEqual(linha['antes'], linha['top'])
                self.assertLessEqual(linha['bottom'], linha['limite'])
                self.assertFalse(linha['corte'])
