"""Current table arithmetic against fictitious, isolated PostgreSQL evidence."""
import base64
import re
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Produto, Funcionario, Venda, ItemVenda, Compra, ItemCompra
from . import services, views, ajustes
from .models import MovimentoObservado, DetalheMovimentoEstoque, ConfirmacaoContagem
from .painel import resumo_painel
from .tests import produto
from .tests_marco_fisico import Tabela


class MatematicaTabelaTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('tabela-ficticia', '', 'teste')
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.client.force_login(self.user)

    def conferir(self, estoque, contado, unidade='MIL', fracionado=False, fator=1, avulsos='0'):
        self.produto = produto(nome='Mercadoria fictícia', quantidade=D(estoque), unidade_compra=unidade,
            unidade_venda_1=unidade, vende_fracionado=fracionado,
            unidade_venda_2='UN' if fracionado else '', fator_conversao=fator)
        self.inv = services.criar_inventario(self.user, 'Conferência fictícia', 'manual', [self.produto.pk])
        self.r = self.inv.itens.get().rodadas.get()
        acesso, _ = services.gerar_acesso(self.user, self.inv.pk, self.funcionario.pk, [self.r.pk])
        services.salvar_contagem(acesso.pk, self.r.pk, 0, contado, avulsos, True)
        self.r.refresh_from_db()

    def consultar(self):
        with CaptureQueriesContext(connection) as consultas:
            linhas = views.linhas_revisao(self.inv)
            resumo = resumo_painel(linhas)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in consultas))
        return linhas[0], resumo

    def movimentar(self, quantidade, entrada=False, unidade=None, fator=None, atribuir=False):
        q = D(quantidade)
        base = (q / D(fator)).quantize(D('.001')) if fator else q
        self.produto.refresh_from_db()
        antes = self.produto.quantidade
        depois = antes + base if entrada else antes - base
        unidade = unidade or self.produto.unidade_venda_1
        if entrada:
            doc = Compra.objects.create(data_compra=timezone.localdate(), estoque_entrada_realizada=True,
                estoque_entrada_realizada_em=timezone.now())
            item = ItemCompra.objects.create(compra=doc, produto=self.produto,
                quantidade=q, unidade=unidade, preco_unitario=0, valor_total=0)
        else:
            doc = Venda.objects.create(data_venda=timezone.localdate(), operador='Operador fictício')
            item = ItemVenda.objects.create(venda=doc, produto=self.produto,
                quantidade=q, unidade=unidade, preco_unitario=0, valor_total=0,
                estoque_antes=antes, estoque_depois=depois, estoque_movimentado=base,
                estoque_unidade_snapshot=self.produto.unidade_venda_1)
        with transaction.atomic():
            from .rastreabilidade import identificar
            Produto.objects.filter(pk=self.produto.pk).update(quantidade=depois)
            identificar(self.produto.pk, antes, depois, 'compra' if entrada else 'venda',
                f'{"Compra" if entrada else "Venda"} #{doc.pk}')
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            if atribuir:
                DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
                    acao='entrada' if entrada else 'saida', documento_tipo='compra' if entrada else 'venda',
                    documento_id=str(doc.pk), item_referencia=str(item.pk), executor=self.user,
                    executor_nome=self.user.username, contexto='autenticado',
                    unidade_base=self.produto.unidade_venda_1, quantidade_entrada=q,
                    unidade_entrada=unidade, conversao='divisao' if fator else 'nenhuma', fator=fator)
        return m

    def assert_matematica(self, linha, estoque, contado, diferenca, percentual, estimativa):
        c = linha['comparacao_atual']
        self.assertEqual((c['estoque'], c['contado'], c['diferenca']), (D(estoque), D(contado), D(diferenca)))
        self.assertEqual(c['diferenca'], c['contado'] - c['estoque'])
        self.assertEqual(c['percentual'], D(percentual) if percentual is not None else None)
        self.assertEqual(c['estimativa'], D(estimativa))

    def test_um_mil_contra_um_mil(self):
        self.conferir('1', '1')
        linha, resumo = self.consultar()
        self.assert_matematica(linha, '1', '1', '0', '0', '0')
        self.assertEqual(linha['resultado_label'], 'Sem diferença')
        self.assertEqual(resumo['corretos'], 1)

    def test_zero_contra_zero(self):
        self.conferir('0', '0')
        linha, resumo = self.consultar()
        self.assert_matematica(linha, '0', '0', '0', None, '0')
        self.assertEqual(resumo['corretos'], 1)
        self.assertIn('zero', linha['percentual_motivo'])

    def test_dois_mil_contra_meio_mil(self):
        self.conferir('2', '0.5')
        linha, resumo = self.consultar()
        self.assert_matematica(linha, '2', '.5', '-1.5', '-75', '-15')
        self.assertEqual(linha['resultado_label'], 'Falta')
        self.assertEqual(resumo['impacto'], 15)
        self.assertEqual(resumo['base_financeira'], 20)

    def test_venda_posterior_projeta_um_mil_para_sete_decimos(self):
        self.conferir('1', '1')
        self.movimentar('.3')
        linha, _ = self.consultar()
        self.assert_matematica(linha, '.7', '1', '.3', str(D('.3')/D('.7')*100), '3')
        self.assertEqual(linha['comparacao_operacional']['diferenca'], 0)
        self.assertTrue(linha['comparacao_atual']['projetada'])
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(pagina, 'Contagem original no OK')

    def test_compra_e_venda_posteriores_percentual_sobre_estoque_exibido(self):
        self.conferir('2', '.5')
        self.movimentar('1', entrada=True)
        self.movimentar('.5')
        linha, resumo = self.consultar()
        self.assert_matematica(linha, '2.5', '.5', '-2', '-80', '-20')
        self.assertEqual(resumo['percentual_financeiro'], 80)
        self.assertEqual(linha['diferenca'], D('-1.5'))  # Original OK is intact.
        self.assertEqual(linha['percentual_quantidade'], -80)

    def test_outro_tipo_de_unidade_e_conversao_congelada(self):
        self.conferir('2', '1', unidade='CX', fracionado=True, fator=12)
        self.movimentar('6', unidade='UN', fator=12, atribuir=True)
        linha, _ = self.consultar()
        self.assert_matematica(linha, '1.5', '1', '-.5', str((-D('.5')/D('1.5'))*100), '-5')
        self.assertEqual(linha['comparacao_atual']['unidade'], 'CX')
        self.assertEqual(linha['comparacao_atual']['diferenca_texto'], '−0,5 CX')
        self.assertEqual(linha['contado_equivalencia']['texto'], '12 UN')

    def test_unidade_alterada_nao_converte_pelo_cadastro_atual(self):
        self.conferir('1', '1')
        Produto.objects.filter(pk=self.produto.pk).update(unidade_venda_1='UN')
        linha, _ = self.consultar()
        self.assertIsNone(linha['comparacao_atual']['diferenca'])
        self.assertIsNone(linha['estimativa'])
        self.assertEqual(linha['resultado_label'], 'Não comparável')

    def test_movimento_sem_prova_exibe_comparacao_sem_inventar_ajuste(self):
        self.conferir('1', '1')
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=D('.7'))
        linha, resumo = self.consultar()
        c = linha['comparacao_atual']
        self.assertEqual(c['estoque'], D('.7'))
        self.assertEqual(c['contado'], 1)
        self.assertEqual(c['diferenca'], D('.3'))
        self.assertEqual(c['percentual'], D('.3')/D('.7')*100)
        self.assertEqual(c['estimativa'], 3)
        self.assertIsNone(linha['comparacao_operacional']['diferenca'])
        self.assertTrue(c['limite'])
        self.assertTrue(resumo['parcial'])
        self.assertEqual(linha['diferenca'], 0)

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_aplicado_preserva_contagem_e_diferenca_historica(self):
        self.conferir('2', '.5')
        r = type(self.r).objects.select_related('item__produto').get(pk=self.r.pk)
        a = ajustes.avaliar(r)
        ajustes.atualizar(self.user, self.inv.pk, ajustes.previa_atualizacao(self.inv.pk, [a]), True)
        self.movimentar('.2')
        antes = ConfirmacaoContagem.objects.get(rodada=self.r)
        linha, _ = self.consultar()
        self.assert_matematica(linha, '.3', '.5', '.2', str(D('.2')/D('.3')*100), '2')
        self.assertEqual(linha['diferenca'], D('-1.5'))
        self.assertEqual(linha['marco'].diferenca, D('-1.5'))
        self.assertEqual(ConfirmacaoContagem.objects.get(pk=antes.pk).contado_base, D('.5'))
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(pagina, 'Diferença original no OK')
        tabela = Tabela()
        tabela.feed(pagina.content.decode())
        self.assertEqual(tabela.colunas, 7)
        self.assertFalse(tabela.abertos)

    def test_kg_zero_com_saldo_positivo_e_sem_custo(self):
        self.conferir('2.5', '0', unidade='KG')
        self.inv.itens.update(custo_base=None)
        linha, resumo = self.consultar()
        self.assertEqual(linha['comparacao_atual']['diferenca'], D('-2.5'))
        self.assertEqual(linha['percentual_quantidade'], -100)
        self.assertIsNone(linha['estimativa'])
        self.assertEqual(resumo['sem_custo'], 1)

    def test_layout_navegador_desktop_mobile_sete_colunas_e_detalhes_recolhidos(self):
        from offline.browser_support import Chrome
        from .tests_browser import CHROME
        self.conferir('1', '1')
        self.movimentar('.3')
        pagina = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', pagina.content.decode(), flags=re.S)
        html = re.sub(r'<link\b[^>]*>', '', html)
        html = html.replace('</head>', '<style>' + Path('static/inventario/revisao.css').read_text(encoding='utf-8') + '</style></head>')
        pasta = Path('diagnostics/matematica-tabela')
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='matematica-tabela-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                for largura in (1366, 390, 320):
                    with self.subTest(largura=largura):
                        tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=900,
                            deviceScaleFactor=1, mobile=largura < 800))
                        tab.call('Page.navigate', {'url': 'data:text/html;base64,' + base64.b64encode(html.encode()).decode()})
                        tab.wait('!!document.querySelector(".resumo-linha")')
                        self.assertEqual(tab.evaluate('document.querySelectorAll(".tabela-resumo thead th").length'), 7)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.assertTrue(tab.evaluate('[...document.querySelectorAll("details")].every(d=>!d.open)'))
                        self.assertIn('Contagem original no OK', tab.evaluate('document.querySelector(".contado-coluna").innerText'))
                        tab.evaluate('document.querySelector(".resumo-inventario").scrollIntoView();true')
                        captura = tab.call('Page.captureScreenshot', {'format': 'png'})
                        (pasta / f'tabela-{largura}.png').write_bytes(base64.b64decode(captura['data']))
            finally:
                chrome.stop()
