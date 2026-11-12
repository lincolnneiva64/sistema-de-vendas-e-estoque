"""Targeted physical-reference scenarios; exclusively fictitious PostgreSQL data."""
from concurrent.futures import ThreadPoolExecutor
import base64
import re
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from uuid import uuid4

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import (Produto, Compra, ItemCompra, Venda, ItemVenda,
                            MovimentacaoEstoqueManual)
from . import ajustes, services, contagem_admin, confirmacao_ajustes
from .models import (AplicacaoAjuste, DetalheMovimentoEstoque, EventoAjuste,
                     MovimentoObservado, PropostaAjuste, RodadaContagem)
from .tests_ajustes import AjustesFixture


class Tabela(HTMLParser):
    def __init__(self):
        super().__init__()
        self.colunas = 0
        self.cabecalho = False
        self.abertos = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'thead':
            self.cabecalho = True
        if tag == 'th' and self.cabecalho:
            self.colunas += 1
        if tag == 'details' and 'open' in attrs:
            self.abertos.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'thead':
            self.cabecalho = False


class Operacoes:
    def aplicar_marco(self, rodada=None):
        rodada = rodada or self.rodada
        avaliacao = self.avaliar(rodada)
        self.assertFalse(avaliacao['bloqueio'], avaliacao['bloqueio'])
        previa = ajustes.previa_atualizacao(rodada.item.inventario_id, [avaliacao])
        registros = ajustes.atualizar(self.usuario, rodada.item.inventario_id, previa, True)
        self.assertTrue(confirmacao_ajustes.resultado(rodada.item.inventario_id, previa)['ok'])
        return registros[0]

    def documento(self, entrada, quantidade, unidade='CX', fator=None):
        q = Decimal(str(quantidade))
        base = (q / Decimal(str(fator))).quantize(Decimal('.001')) if fator else q
        self.produto.refresh_from_db()
        antes = self.produto.quantidade
        depois = antes + base if entrada else antes - base
        if entrada:
            doc = Compra.objects.create(data_compra=timezone.localdate(), operador='Compra fictícia',
                estoque_entrada_realizada=True, estoque_entrada_realizada_em=timezone.now())
            item = ItemCompra.objects.create(compra=doc, produto=self.produto, quantidade=q,
                unidade=unidade, preco_unitario=0, valor_total=0)
        else:
            doc = Venda.objects.create(data_venda=timezone.localdate(), operador='Venda fictícia')
            item = ItemVenda.objects.create(venda=doc, produto=self.produto, quantidade=q,
                unidade=unidade, preco_unitario=0, valor_total=0, estoque_antes=antes,
                estoque_movimentado=base, estoque_depois=depois, estoque_unidade_snapshot='CX')
        with transaction.atomic():
            from .rastreabilidade import identificar
            Produto.objects.filter(pk=self.produto.pk).update(quantidade=depois)
            origem = 'compra' if entrada else 'venda'
            identificar(self.produto.pk, antes, depois, origem,
                f'{"Compra" if entrada else "Venda"} #{doc.pk}')
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            if fator:
                DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
                    acao='entrada' if entrada else 'saida', documento_tipo=origem,
                    documento_id=str(doc.pk), item_referencia=str(item.pk),
                    executor=self.usuario, executor_nome=self.usuario.username, contexto='autenticado',
                    unidade_base='CX', quantidade_entrada=q, unidade_entrada=unidade,
                    conversao='divisao', fator=Decimal(str(fator)))
        return m


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class MarcoFisicoTests(Operacoes, AjustesFixture, TestCase):
    def test_primeira_contagem_ignora_inconsistencia_antes_do_ok_escolhido(self):
        # Unknown write between an old OK and the responsible person's new OK.
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=35)
        r = self.fresca()
        token = contagem_admin.autorizacao(self.usuario, self.inv, r)
        nova = contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, '7', '0', tipo='nova')
        r = self.fresca(nova.pk)
        self.assertEqual(r.confirmacoes.count(), 1)
        self.assertEqual(self.fresca().confirmacoes.count(), 1)
        services.revisar(self.usuario, r.pk, 'Contagem física validada pelo responsável.')
        registro = self.aplicar_marco(r)
        self.assertEqual((registro.antes, registro.diferenca, registro.depois), (35, -28, 7))
        self.assertEqual(MovimentoObservado.objects.filter(produto=self.produto).count(), 2)
        self.assertEqual(registro.evidencia['preparacao']['regra'], 'marco_fisico_v1')

    def test_compra_venda_posteriores_sem_duplicacao(self):
        self.documento(True, 5)
        self.documento(False, 2)
        # Return identified by its immutable attribution and actual stock write.
        with transaction.atomic():
            Produto.objects.filter(pk=self.produto.pk).update(quantidade=14)
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            m.origem, m.referencia = 'devolucao_venda', 'Devolução fictícia documentada'
            m.save(update_fields=['origem', 'referencia'])
            DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
                acao='devolucao', documento_tipo='venda', documento_id='devolucao-ficticia',
                executor=self.usuario, executor_nome=self.usuario.username, contexto='autenticado',
                unidade_base='CX', quantidade_entrada=1, unidade_entrada='CX', conversao='nenhuma')
        registro = self.aplicar_marco()
        self.assertEqual((registro.antes, registro.depois), (14, 12))
        self.assertEqual(len(registro.evidencia['preparacao']['documentos']), 3)
        self.assertEqual(self.fresca().contado_base, 8)

    def test_mesma_regra_para_unidades_discretas_e_continuas(self):
        from .tests import produto
        for unidade, contado in [('UN', '0'), ('KG', '2.125'), ('MIL', '0.5'), ('FD', '3')]:
            with self.subTest(unidade=unidade):
                p = produto(nome='Mercadoria fictícia ' + unidade, quantidade=10,
                    unidade_venda_1=unidade, unidade_compra=unidade,
                    vende_fracionado=False, unidade_venda_2='', fator_conversao=1)
                # No old physical anchor; deliberately inconsistent legacy write.
                Produto.objects.filter(pk=p.pk).update(quantidade=27)
                inv = services.criar_inventario(self.usuario, 'Primeiro marco', 'manual', [p.pk])
                r = self.confirmar(inv, contado)
                registro = self.aplicar_marco(r)
                self.assertEqual(registro.depois, Decimal(contado))
                self.assertEqual(registro.unidade, unidade)

    def test_zero_fisico_e_diferenca_zero_estabelecem_marco_sem_movimento(self):
        r = self.fresca()
        token = contagem_admin.autorizacao(self.usuario, self.inv, r)
        contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, '0', '0')
        primeiro = self.aplicar_marco(self.fresca())
        self.assertEqual(primeiro.depois, 0)
        inv = services.criar_inventario(self.usuario, 'Zero confirmado', 'manual', [self.produto.pk])
        r = self.confirmar(inv, 0)
        antes = (MovimentoObservado.objects.count(), MovimentacaoEstoqueManual.objects.count())
        registro = self.aplicar_marco(r)
        self.assertEqual((registro.antes, registro.depois, registro.diferenca), (0, 0, 0))
        self.assertTrue(registro.sem_ajuste)
        self.assertEqual(antes, (MovimentoObservado.objects.count(), MovimentacaoEstoqueManual.objects.count()))
        self.assertTrue(EventoAjuste.objects.filter(proposta=registro.proposta, acao='marco_sem_ajuste').exists())
        avaliacao = ajustes.preparar_resultado(inv)['linhas'][0]
        self.assertEqual((avaliacao['estado'], avaliacao['saldo_aplicado']), ('Aplicado', 0))
        historico = self.client.get(reverse('inventario:historico_produto', args=[inv.pk, r.pk]),
            {'periodo': 'completo'})
        self.assertEqual(historico.status_code, 200)
        self.assertContains(historico, 'Marco físico estabelecido sem alteração de quantidade')
        with self.assertRaises(ValueError):
            services.recontar(self.usuario, r.pk, 'Não editar um marco já aplicado.')
        painel = self.client.get(reverse('inventario:ajustes', args=[inv.pk]))
        self.assertEqual(painel.status_code, 200)
        self.assertContains(painel, 'Marco sem alteração de quantidade')
        detalhe = self.client.get(reverse('inventario:detalhe', args=[inv.pk]))
        self.assertNotContains(detalhe, 'data-abrir-edicao-admin=')

    def test_lote_misto_zero_e_ajuste_reverte_integralmente_e_reenvia(self):
        from unittest.mock import patch
        r = self.fresca()
        token = contagem_admin.autorizacao(self.usuario, self.inv, r)
        contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, '10', '0')
        outra = self.segunda()
        linhas = [self.avaliar(), self.avaliar(outra)]
        previa = ajustes.previa_atualizacao(self.inv.pk, linhas)
        with patch('inventario.ajustes.conferir_ou_ajustar_estoque', side_effect=ValueError('Falha fictícia')):
            with self.assertRaisesMessage(ValueError, 'Falha fictícia'):
                ajustes.atualizar(self.usuario, self.inv.pk, previa, True)
        self.assertFalse(PropostaAjuste.objects.filter(estado='aplicado').exists())
        self.assertFalse(EventoAjuste.objects.filter(acao='marco_sem_ajuste').exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        a = ajustes.atualizar(self.usuario, self.inv.pk, previa, True)
        b = ajustes.atualizar(self.usuario, self.inv.pk, previa, True)
        self.assertEqual({x.pk for x in a}, {x.pk for x in b})
        self.assertEqual(len(a), 2)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        self.assertTrue(confirmacao_ajustes.resultado(self.inv.pk, previa)['ok'])

    def test_novo_marco_substitui_anterior_e_recusa_conferencia_antiga(self):
        antiga = self.aprovar()
        inv = services.criar_inventario(self.usuario, 'Nova conferência', 'manual', [self.produto.pk])
        nova = self.confirmar(inv, 6)
        self.assertEqual(self.aplicar_marco(nova).depois, 6)
        with self.assertRaisesMessage(ValueError, 'mais recente'):
            ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, [antiga]), True)
        # A subsequent conference creates another reference normally.
        outra = services.criar_inventario(self.usuario, 'Outra conferência', 'manual', [self.produto.pk])
        self.assertEqual(self.aplicar_marco(self.confirmar(outra, 4)).depois, 4)
        self.assertEqual(AplicacaoAjuste.objects.count(), 2)

    def test_conversao_fisica_congelada_em_movimentos_e_avulsos(self):
        self.documento(True, 24, 'UN', fator=12)
        self.documento(False, 6, 'UN', fator=12)
        registro = self.aplicar_marco()
        self.assertEqual(registro.depois, Decimal('9.500'))
        self.assertEqual(registro.diferenca, -2)
        # A separate physical count uses fractional packaging and loose units.
        inv = services.criar_inventario(self.usuario, 'Avulsos', 'manual', [self.produto.pk])
        r = inv.itens.get().rodadas.get()
        token = contagem_admin.autorizacao(self.usuario, inv, r)
        contagem_admin.confirmar(self.usuario, inv.pk, r.pk, token, '0.5', '1')
        registro = self.aplicar_marco(self.fresca(r.pk))
        self.assertEqual(registro.depois, Decimal('.583'))
        self.assertEqual(Decimal(registro.evidencia['preparacao']['representacao_fisica']['total_unidades']), 7)

    def test_venda_de_uma_avulsa_usa_projecao_operacional_comprovada(self):
        self.documento(False, 1, 'UN', fator=12)
        self.assertEqual(self.aplicar_marco().depois, Decimal('7.917'))

    def test_validacao_da_aplicacao_nova_e_interpretacao_antiga(self):
        from copy import copy, deepcopy
        from .contexto_ajustes import carregar
        from . import revisao_historico
        registro = self.aplicar_marco()
        r = self.fresca()
        with carregar([r]) as lote:
            revisao_historico.carregar(lote)
            a = lote.aplicacoes_revisao[f'Movimentação manual #{registro.decisao.movimentacao_id}']
            m = lote.movimentos[self.produto.pk][-1]
            revisao_historico.validar_aplicacao(r, m, a, lote)
            # The same persisted legacy structure, without a rule-version key,
            # retains its original delta interpretation. Never rewrite a row.
            antigo = copy(a)
            antigo.proposta = copy(a.proposta)
            antigo.proposta.evidencia = deepcopy(a.proposta.evidencia)
            antigo.proposta.evidencia.pop('regra')
            antigo.proposta.evidencia.pop('diferenca_ok')
            antigo.proposta.assinatura = ajustes.assinatura(antigo.proposta.evidencia)
            antigo.evidencia = dict(a.evidencia, preparacao=antigo.proposta.evidencia)
            revisao_historico.validar_aplicacao(r, m, antigo, lote)
            posicao, comprovados = revisao_historico.validar(r, r.item.produto, lote)
            self.assertEqual(posicao, m.sequencia)
            self.assertIn(m.pk, comprovados)

    def test_movimento_posterior_desconhecido_nao_e_zero_e_previa_expira(self):
        p = self.aprovar()
        self.documento(False, 1)
        with self.assertRaises(ValueError):
            ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, [p]), True)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=20)
        self.assertEqual(self.avaliar()['estado'], 'Bloqueado')
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_layout_sete_colunas_detalhes_recolhidos_e_numeros_visiveis(self):
        response = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(response.status_code, 200)
        tabela = Tabela()
        tabela.feed(response.content.decode())
        self.assertEqual(tabela.colunas, 7)
        self.assertFalse(tabela.abertos)
        self.assertContains(response, 'Saldo registrado no OK')
        self.assertContains(response, 'Estimado')
        self.aplicar_marco()
        response = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(response, 'Ajuste aplicado')
        self.assertContains(response, 'Saldo resultante na aplicação')

    def test_layout_navegador_desktop_e_mobile(self):
        from offline.browser_support import Chrome
        from .tests_browser import CHROME
        response = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        html = response.content.decode()
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
        html = re.sub(r'<link\b[^>]*>', '', html)
        css = Path('static/inventario/revisao.css').read_text(encoding='utf-8')
        html = html.replace('</head>', '<style>' + css + '</style></head>')
        pasta = Path('diagnostics/marco-fisico')
        pasta.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='marco-fisico-chrome-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*']})
                for largura in (1366, 390, 320):
                    with self.subTest(largura=largura):
                        tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura,
                            height=900, deviceScaleFactor=1, mobile=largura < 800))
                        tab.call('Page.navigate', {'url': 'data:text/html;base64,' +
                            base64.b64encode(html.encode()).decode()})
                        tab.wait('!!document.querySelector(".resumo-linha")')
                        self.assertEqual(tab.evaluate('document.querySelectorAll(".tabela-resumo thead th").length'), 7)
                        self.assertFalse(tab.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        self.assertTrue(tab.evaluate('[...document.querySelectorAll("details")].every(d=>!d.open)'))
                        tab.evaluate('document.querySelector(".resumo-inventario").scrollIntoView(); true')
                        captura = tab.call('Page.captureScreenshot', {'format': 'png'})
                        (pasta / f'compacto-{largura}.png').write_bytes(base64.b64decode(captura['data']))
            finally:
                chrome.stop()


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class MarcoConcorrenciaTests(Operacoes, AjustesFixture, TransactionTestCase):
    def test_duas_aplicacoes_concorrentes_gravam_uma_vez(self):
        p = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [p])
        barreira = Barrier(2)
        def executar():
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                return ajustes.aplicar(self.usuario, self.inv.pk, previa, True)[0].pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda _: executar(), range(2)))
        self.assertEqual(a, b)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        self.assertEqual(self.fresca().item.produto.quantidade, 8)

    def test_duas_aplicacoes_zero_concorrentes_sem_movimento(self):
        r = self.fresca()
        token = contagem_admin.autorizacao(self.usuario, self.inv, r)
        contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, '10', '0')
        proposta = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        barreira = Barrier(2)
        def executar():
            close_old_connections()
            try:
                barreira.wait(timeout=10)
                return ajustes.aplicar(self.usuario, self.inv.pk, previa, True)[0].pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda _: executar(), range(2)))
        self.assertEqual(a, b)
        self.assertEqual(EventoAjuste.objects.filter(acao='marco_sem_ajuste').count(), 1)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())

    def test_venda_concorrente_preservada_antes_de_revalidar_aplicacao(self):
        p = self.aprovar()
        previa = ajustes.previa_aplicacao(self.inv.pk, [p])
        venda_gravada, liberar = Event(), Event()
        def vender():
            close_old_connections()
            try:
                with transaction.atomic():
                    Produto.objects.select_for_update().get(pk=self.produto.pk)
                    self.documento(False, 1)
                    venda_gravada.set()
                    if not liberar.wait(10):
                        raise RuntimeError('Prazo da fixture concorrente excedido.')
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            tarefa = pool.submit(vender)
            self.assertTrue(venda_gravada.wait(10))
            liberar.set()
            tarefa.result(timeout=10)
        # The stale authorization cannot overwrite a sale committed after approval.
        with self.assertRaises(ValueError):
            ajustes.aplicar(self.usuario, self.inv.pk, previa, True)
        self.assertEqual(self.fresca().item.produto.quantidade, 9)
        self.assertEqual(self.aplicar_marco().depois, 7)
