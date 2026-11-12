"""General extract rules with synthetic examples; never operational documents."""
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

from django.db.models import F
from django.test import TestCase, override_settings
from django.urls import reverse

from estoque.models import ItemCompra, ItemVenda, Produto
from . import ajustes
from .models import AplicacaoAjuste, DetalheMovimentoEstoque, MovimentoObservado
from .tests_extrato_cronologico import CronologiaFixture
from .tests_ajustes import AjustesFixture
from . import tests_fluxo_compartilhado as fluxo


class ReferenciasExtratoTests(CronologiaFixture, TestCase):
    def resposta(self, **params):
        resposta = self.client.get(self.url, params)
        self.assertEqual(resposta.status_code, 200)
        return resposta

    def compra_com_referencia(self, quantidade=10, unidade='PCT', incremento=None, fator=None):
        item = ItemCompra.objects.get(compra=self.compra, produto=self.produto)
        ItemCompra.objects.filter(pk=item.pk).update(quantidade=quantidade, unidade=unidade)
        incremento = quantidade if incremento is None else incremento
        m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1, antes=5,
            depois=5+incremento, origem='compra', referencia=f'Compra #{self.compra.pk}',
            registrado_em=self.inicio+timedelta(seconds=1))
        DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
            acao='entrada', documento_tipo='compra', documento_id=str(self.compra.pk),
            item_referencia=str(item.pk), executor=self.gestor, executor_nome='Gestor fictício',
            contexto='autenticado', unidade_base='PCT', quantidade_entrada=quantidade,
            unidade_entrada=unidade, conversao='divisao' if fator else 'nenhuma', fator=fator)
        return item, m

    def test_unidades_diferentes_uma_operacao_numeros_originais_preservados(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=10, unidade='UN',
            estoque_antes=20, estoque_movimentado=1, estoque_depois=19, estoque_unidade_snapshot='PCT')
        m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1, antes=20,
            depois=19, origem='venda', referencia=f'Venda #{self.posterior.venda_id}',
            registrado_em=self.ok.replace(hour=16, minute=19)+timedelta(seconds=1))
        for nome in ('Saco Chopp Gde 10/1Ct', 'Saco 3 kg'):
            with self.subTest(exemplo_ficticio=nome):
                Produto.objects.filter(pk=self.produto.pk).update(nome=nome)
                resposta = self.resposta()
                linhas = list(resposta.context['extrato'])
                self.assertFalse(any(r['fonte']=='digital' for r in linhas))
                venda = next(r for r in linhas if r['fonte']=='venda' and r['pk']==self.posterior.pk)
                self.assertEqual([e.pk for e in venda['evidencias_operacionais']], [m.pk])
                self.assertEqual(venda['operacao_documental']['quantidade'], 10)
                self.assertEqual(venda['registro_original']['antes'], 20)
                self.assertEqual(venda['registro_original']['depois'], 19)
                self.assertEqual(venda['registro_original']['unidade'], 'PCT')
                self.assertContains(resposta, 'Registrado anterior: 20')
                self.assertContains(resposta, 'Baixa registrada: 1')

    def test_compra_com_referencia_inicia_saldo_e_evidencia_nao_e_somada(self):
        item, m = self.compra_com_referencia()
        linhas = list(self.resposta().context['extrato'])
        self.assertFalse(any(r['fonte']=='digital' for r in linhas))
        compra = next(r for r in linhas if r['fonte']=='compra')
        self.assertEqual((compra['saldo_calculado']['anterior'], compra['saldo_calculado']['valor']), (5, 15))
        self.assertIn(f'Registro #{m.pk}', compra['saldo_calculado']['origem'])
        primeira = next(r for r in linhas if r['fonte']=='venda' and r['pk']==self.anterior.pk)
        self.assertEqual(primeira['saldo_calculado']['valor'], 14)

    def test_paginacao_reconstroi_mesmo_trecho_de_compra(self):
        self.compra_com_referencia(100)
        for i in range(52):
            self.registrar_venda(self.ok.replace(hour=20)+timedelta(minutes=i))
        linhas = []
        for numero in (1, 2):
            linhas.extend(self.resposta(extrato_pagina=numero).context['extrato'])
        vendas = [r for r in linhas if r['fonte']=='venda']
        self.assertEqual(len(vendas), 55)
        for i, venda in enumerate(vendas):
            self.assertEqual(venda['saldo_calculado']['anterior'], 105-i)
            self.assertEqual(venda['saldo_calculado']['valor'], 104-i)
        self.assertEqual(len({r['detalhe_id'] for r in linhas}), len(linhas))

    def test_referencia_registrada_sobrevive_a_conversao_nao_correspondente(self):
        self.compra_com_referencia(1, 'UN', D('.083'), 12)
        linhas = list(self.resposta().context['extrato'])
        compra = next(r for r in linhas if r['fonte']=='compra')
        self.assertIsNone(compra['saldo_calculado'])
        self.assertEqual(compra['registro_original']['depois'], D('5.083'))
        primeira = next(r for r in linhas if r['fonte']=='venda' and r['pk']==self.anterior.pk)
        self.assertEqual(primeira['saldo_calculado']['valor'], D('4.083'))
        self.assertIn('compra #', primeira['saldo_calculado']['origem'])

    def test_observacoes_ambiguas_continuam_separadas(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=10, unidade='UN',
            estoque_antes=20, estoque_movimentado=1, estoque_depois=19, estoque_unidade_snapshot='PCT')
        for sequencia, antes in ((1, 20), (2, 19)):
            MovimentoObservado.objects.create(produto=self.produto, sequencia=sequencia,
                antes=antes, depois=antes-1, origem='venda', referencia=f'Venda #{self.posterior.venda_id}',
                registrado_em=self.ok.replace(hour=16, minute=19)+timedelta(seconds=sequencia))
        linhas = list(self.resposta().context['extrato'])
        self.assertEqual(sum(r['fonte']=='digital' for r in linhas), 2)


class ProjecaoExtratoTests(AjustesFixture, TestCase):
    comprar = fluxo.FluxoCompartilhadoTests.comprar
    vender = fluxo.FluxoCompartilhadoTests.vender

    def resposta(self, pagina=1):
        return self.client.get(reverse('inventario:historico_produto',
            args=[self.inv.pk, self.rodada.pk]), {'periodo':'completo', 'extrato_pagina':pagina})

    def test_ok_sem_aplicacao_projeta_compra_e_venda_sem_alterar_saldos(self):
        compra, item, _ = self.comprar()
        venda, vendido, _ = self.vender(1)
        self.produto.refresh_from_db()
        antes = self.produto.quantidade
        resposta = self.resposta()
        self.assertEqual(resposta.status_code, 200)
        linhas = list(resposta.context['extrato'])
        entrada = next(r for r in linhas if r['fonte']=='compra' and r['pk']==item.pk)
        saida = next(r for r in linhas if r['fonte']=='venda' and r['pk']==vendido.pk)
        self.assertEqual((entrada['projecao_fisica']['antes'], entrada['projecao_fisica']['depois']), (8, 11))
        self.assertEqual((saida['projecao_fisica']['antes'], saida['projecao_fisica']['depois']), (11, 10))
        self.assertEqual(saida['registro_original']['depois'], 12)
        self.assertContains(resposta, 'Projeção física:')
        self.assertContains(resposta, 'Referência: OK de')
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, antes)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_projecao_usa_prefixo_em_outra_pagina(self):
        self.comprar(100)
        for _ in range(51):
            self.vender(1)
        resposta = self.resposta(2)
        vendas = [r for r in resposta.context['extrato'] if r['fonte']=='venda']
        self.assertTrue(vendas)
        self.assertEqual(vendas[-1]['projecao_fisica']['depois'], 57)

    def test_lacuna_preserva_projecao_anterior_e_interrompe_so_posteriores(self):
        _, item, _ = self.comprar()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=F('quantidade')+1)
        _, vendido, _ = self.vender(1)
        linhas = list(self.resposta().context['extrato'])
        compra = next(r for r in linhas if r['fonte']=='compra' and r['pk']==item.pk)
        venda = next(r for r in linhas if r['fonte']=='venda' and r['pk']==vendido.pk)
        self.assertEqual(compra['projecao_fisica']['depois'], 11)
        self.assertNotIn('projecao_fisica', venda)
        self.assertIn('projecao_aviso', venda)

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_ajuste_proprio_nao_e_subtraido_novamente_da_projecao(self):
        proposta = self.aprovar()
        ajustes.aplicar(self.usuario, self.inv.pk,
            ajustes.previa_aplicacao(self.inv.pk, [proposta]), True)
        _, vendido, _ = self.vender(1)
        linhas = list(self.resposta().context['extrato'])
        venda = next(r for r in linhas if r['fonte']=='venda' and r['pk']==vendido.pk)
        self.assertEqual(venda['projecao_fisica']['antes'], 8)
        self.assertEqual(venda['projecao_fisica']['depois'], 7)
