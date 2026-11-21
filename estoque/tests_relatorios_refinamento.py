"""Presentation regression: in-memory templates and isolated Edge, no database/server."""
import base64
import copy
import json
from datetime import date
from decimal import Decimal
from django.core.serializers.json import DjangoJSONEncoder
from django.template import Context, Engine
from estoque import tests_relatorios_layout as layout
from estoque.analise_evolucao import analisar_historico
from estoque.templatetags.relatorios import valor_evolucao

ROOT = layout.ROOT


class RefinamentoProdutoTests(layout.CartoesResumoLayoutTests):
    def cenario_skol(self):
        inicio, hoje = date(2026,9,7), date(2026,11,20)
        dias = [dict(data_venda=d, faturamento=Decimal(v), quantidade=n, sem_valor=0)
                for d,v,n in [(inicio,'27',11),(date(2026,10,1),'34.5',14),
                              (date(2026,10,31),'31',11),(date(2026,11,1),'20',10)]]
        cobertura = [date.fromordinal(n) for n in range(inicio.toordinal(),hoje.toordinal()+1)]
        analise = analisar_historico(dias,date(2026,9,1),hoje,hoje,'mensal',
            inicio_registros=inicio,precisao=Decimal('.001'),dias_cobertos=cobertura)
        analise.update(medida='quantidade',unidade='DZ',casas=3,cobertura_comercial=True)
        return json.loads(json.dumps(analise,cls=DjangoJSONEncoder))

    def renderizar_produto(self, analise, largura):
        analise = copy.deepcopy(analise)
        for campo in ('inicio', 'fim', 'inicio_registros'):
            if isinstance(analise.get(campo), str):
                analise[campo] = date.fromisoformat(analise[campo])
        source = (ROOT/'estoque/templates/estoque/relatorios/graficos.html').read_text(encoding='utf-8')
        source = source.replace("{% include 'estoque/relatorios/evolucao_controles.html' %}", '')
        template = Engine(libraries={'relatorios':'estoque.templatetags.relatorios','l10n':'django.templatetags.l10n'}).from_string(source)
        html = template.render(Context({'produto_relatorio':True, 'medida':analise['medida'], 'unidade':analise['unidade'],
            'resumo':{'analise_evolucao':analise,'grafico_evolucao':analise}}, use_l10n=False))
        self.preparar(largura)
        self.tab.evaluate('document.querySelector("main").innerHTML='+json.dumps(html))
        self.tab.evaluate((ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8'))

    def test_skol_evidencia_preservada_tres_metricas_sem_alterar_json(self):
        original=self.cenario_skol()
        for medida in ('quantidade','faturamento','vendas'):
            analise=copy.deepcopy(original)
            analise.update(medida=medida,unidade='DZ' if medida=='quantidade' else 'vendas' if medida=='vendas' else '')
            for largura in (320,390,768,1366):
                with self.subTest(medida=medida,largura=largura):
                    self.renderizar_produto(analise,largura)
                    textos=self.tab.evaluate('[...document.querySelectorAll(".grafico-barra-dia")].map(e=>e.innerText)')
                    self.assertIn('Comparação do período realizado',textos[2])
                    self.assertIn('-42,03%',textos[2])
                    self.assertIn('-53,40%',textos[2])
                    self.assertIn('projeção',textos[2])
                    self.assertIn('30/11/2026',textos[2])
                    self.assertIn('01/10/2026 a 20/10/2026',textos[2])
                    self.assertIn('01/10/2026 a 31/10/2026',textos[2])
                    self.assertNotIn('24 dias comprovados',textos[0])
                    self.assertIn('não realizado',textos[0])
                    if medida=='quantidade':
                        self.assertIn('27 DZ',textos[0]); self.assertIn('33,75 DZ',textos[0])
                        self.assertIn('65,5 DZ',textos[1]); self.assertIn('30,53 DZ',textos[2])
                    self.assertEqual(self.tab.evaluate('JSON.parse(document.getElementById("relatorios-barras-dados").textContent)'),analise)
                    self.assertLessEqual(self.tab.evaluate('document.documentElement.scrollWidth'),largura)
                    for caixa in self.tab.evaluate('[...document.querySelectorAll(".grafico-barra-dia")].map(e=>({data:e.querySelector(".grafico-barra-data").getBoundingClientRect().bottom,comp:e.querySelector(".grafico-barra-variacao").getBoundingClientRect().top,indic:e.querySelector(".grafico-barra-indicadores").getBoundingClientRect().top,fim:e.getBoundingClientRect().bottom,rodape:e.querySelector(".grafico-barra-indicadores").getBoundingClientRect().bottom}))'):
                        self.assertLessEqual(caixa['data'],caixa['comp'])
                        self.assertLess(caixa['indic'],caixa['fim'])
                        self.assertLessEqual(caixa['rodape'],caixa['fim'])
                    if medida=='quantidade' and largura in (390,1366):
                        destino=ROOT/'diagnostics/relatorios-refinamento-20261121'/('skol-'+str(largura)+'.png')
                        destino.parent.mkdir(parents=True,exist_ok=True)
                        destino.write_bytes(base64.b64decode(self.tab.call('Page.captureScreenshot',{'captureBeyondViewport':True})['data']))
                    self.tab.evaluate('document.querySelector(".grafico-barra-indicadores details").open=true')
                    self.tab.evaluate('(async()=>{await new Promise(r=>setTimeout(r,100));})()')
                    self.assertIn('24 dias comprovados',self.tab.evaluate('document.querySelector(".grafico-barra-dia").innerText'))

    def test_quantidade_significativa_e_estimativa_separadas_todas_unidades(self):
        analise=self.cenario_skol()
        for unidade in ('DZ','PCT','CX','FD','UN','KG'):
            analise['unidade']=unidade
            analise['itens'][0]['valor']='-30.526'
            self.renderizar_produto(analise,390)
            self.assertEqual(self.tab.evaluate('document.querySelector(".grafico-barra-valor").innerText'),'-30,526 '+unidade)
            self.assertEqual(valor_evolucao(Decimal('30.526'),analise),'30,526 '+unidade)
            self.assertEqual(valor_evolucao(Decimal('65.500'),analise),'65,5 '+unidade)
            self.assertIn('33,75 '+unidade,self.tab.evaluate('document.querySelector(".grafico-barra-fantasma-valor").innerText'))

    def test_comparar_restaura_posicao_e_foco(self):
        self.renderizar_produto(self.cenario_skol(),390)
        self.tab.evaluate('''(()=>{
            const storage={};
            Object.defineProperty(window,'sessionStorage',{value:{
                getItem:k=>storage[k]??null,setItem:(k,v)=>storage[k]=v,removeItem:k=>delete storage[k]
            },configurable:true});
            document.querySelector('main').insertAdjacentHTML('afterbegin',
                '<div style="height:1400px"></div><form id="evolucao-controles" data-produto="true">'+
                '<input name="modo" value="mensal"><input name="medida" value="quantidade">'+
                '<span data-evolucao-dia></span><span data-evolucao-equivalente></span><button type="submit">Comparar</button></form>');
        })()''')
        script=(ROOT/'static/estoque/relatorios-filtros.js').read_text(encoding='utf-8')
        self.tab.evaluate(script)
        self.tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
        self.tab.evaluate('''(()=>{
            const form=document.getElementById('evolucao-controles');
            form.querySelector('button').focus({preventScroll:true});window.scrollTo(0,500);
            form.addEventListener('submit',e=>e.preventDefault());
            form.dispatchEvent(new Event('submit',{cancelable:true}));
            window.scrollTo(0,0);document.activeElement.blur();
        })()''')
        self.tab.evaluate(script)
        self.tab.evaluate('(async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()')
        self.assertAlmostEqual(self.tab.evaluate('scrollY'),500,delta=1)
        self.assertEqual(self.tab.evaluate('document.activeElement.textContent'),'Comparar')
