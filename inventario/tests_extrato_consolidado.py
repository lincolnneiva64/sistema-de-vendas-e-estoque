"""Synthetic regressions matching the investigated shapes, never production IDs."""
from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from estoque.models import (Compra, EventoVenda, ItemCompra, ItemVenda, ItemVendaRemovido,
                            MovimentacaoEstoqueManual, Venda)
from .models import MovimentoObservado
from .tests import produto
from .tests_historico import HistoricoFixture
from . import tests_saldos_historicos as evidencia


class ConsolidadoTests(HistoricoFixture, TestCase):
    venda_snapshot = evidencia.SaldosHistoricosTests.venda_snapshot

    def setUp(self):
        super().setUp()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=41)
        self.anterior=self.venda_snapshot(-1,23,6,17)
        self.posterior=self.venda_snapshot(1,58,7,51)
        self.ultima=self.venda_snapshot(2,51,3,48)
        self.outro=produto(nome='Outro SKU fictício')

    def resposta(self, **params):
        r=self.client.get(self.url,params)
        self.assertEqual(r.status_code,200)
        return r

    def edicao_outro(self, item, tipo='quantidade_item_alterada', resumo=True):
        ItemVenda.objects.create(venda_id=item.venda_id,produto=self.outro,quantidade=1,
            unidade='UN',preco_unitario=0,valor_total=0)
        descricao=f'Quantidade alterada na nota: {self.outro.nome}. De 2 UN para 1 UN.'
        if tipo=='item_removido_da_nota':
            descricao=f'Item removido da nota: {self.outro.nome}, quantidade 1 UN.'
            ItemVendaRemovido.objects.create(venda_id=item.venda_id,produto=self.outro,
                produto_nome_snapshot=self.outro.nome,quantidade_snapshot=1,unidade_snapshot='UN',
                preco_unitario_snapshot=0,valor_total_snapshot=0,estoque_devolvido=True)
        edicao=EventoVenda.objects.create(venda_id=item.venda_id,tipo_evento=tipo,descricao=descricao)
        if resumo:
            EventoVenda.objects.create(venda_id=item.venda_id,tipo_evento='alteracao_separacao_atendida',
                descricao='Ajustes da separacao aplicados pela edicao da venda.')
        return edicao

    def linha(self,r,item):
        return next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==item.pk)

    def test_resumo_com_edicao_atribuida_a_outro_sku_libera_snapshot_e_ponte(self):
        self.edicao_outro(self.posterior)
        r=self.resposta()
        self.assertEqual(self.linha(r,self.posterior)['saldo'],51)
        self.assertEqual(r.context['extrato'].saldo_inicial['valor'],17)

    def test_resumo_isolado_ou_texto_desconhecido_nao_basta(self):
        for texto in ('Alteracoes apos separacao atendidas.','Resumo incompleto'):
            e=EventoVenda.objects.create(venda_id=self.posterior.venda_id,
                tipo_evento='alteracao_separacao_atendida',descricao=texto)
            self.assertIsNone(self.linha(self.resposta(),self.posterior)['saldo'])
            e.delete()

    def test_resumo_anterior_a_edicao_nao_a_comprova(self):
        e=self.edicao_outro(self.posterior)
        EventoVenda.objects.filter(venda_id=self.posterior.venda_id,
            tipo_evento='alteracao_separacao_atendida').update(criado_em=e.criado_em-timedelta(seconds=1))
        self.assertIsNone(self.linha(self.resposta(),self.posterior)['saldo'])

    def test_segundo_resumo_exige_nova_edicao_atribuida(self):
        self.edicao_outro(self.posterior)
        EventoVenda.objects.create(venda_id=self.posterior.venda_id,
            tipo_evento='alteracao_separacao_atendida',descricao='Alteracoes apos separacao atendidas.')
        self.assertIsNone(self.linha(self.resposta(),self.posterior)['saldo'])

    def test_alteracao_do_proprio_produto_continua_bloqueada_com_resumo(self):
        self.edicao_outro(self.posterior)
        EventoVenda.objects.create(venda_id=self.posterior.venda_id,tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {self.produto.nome}. De 6 CX para 7 CX.')
        self.assertIsNone(self.linha(self.resposta(),self.posterior)['saldo'])

    def test_nome_ambiguo_e_evento_desconhecido_continuam_bloqueados(self):
        self.outro.nome=self.produto.nome
        self.outro.save(update_fields=['nome'])
        self.edicao_outro(self.posterior)
        self.assertIsNone(self.linha(self.resposta(),self.posterior)['saldo'])

    def test_trigopan_e_condicionador_simulados_aberturas_genericas(self):
        for antes,entrada,nome in ((2,100,'Estabilizante fictício'),(4,40,'Condicionador fictício')):
            with self.subTest(nome=nome):
                ItemCompra.objects.filter(compra=self.compra).update(quantidade=entrada,unidade='UN')
                ItemVenda.objects.filter(pk=self.anterior.pk).update(quantidade=6,unidade='UN',
                    estoque_antes=antes+6,estoque_depois=antes,estoque_unidade_snapshot='UN')
                ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=7,unidade='UN',
                    estoque_antes=antes+entrada,estoque_depois=antes+entrada-7,estoque_unidade_snapshot='UN')
                self.assertEqual(self.resposta().context['extrato'].saldo_inicial['valor'],antes)

    def nova_compra(self,dia,quantidade):
        instante=self.inicio+timedelta(days=dia)
        c=Compra.objects.create(data_compra=instante.date(),estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=instante)
        item=ItemCompra.objects.create(compra=c,produto=self.produto,quantidade=quantidade,
            unidade='FD',preco_unitario=0,valor_total=0)
        return item

    def test_trigo_fd_tres_compras_edicoes_alheias_e_conversao_nao_comprovada(self):
        ItemVenda.objects.filter(produto=self.produto).delete()
        ItemCompra.objects.filter(compra=self.compra).update(quantidade=3,unidade='FD')
        self.rodada.ok_em=self.inicio+timedelta(days=10)
        self.rodada.esperado=D('.2');self.rodada.contado_base=0;self.rodada.save()
        self.venda_snapshot(-1,1,1,0,'FD')
        alterada=self.venda_snapshot(1,3,1,2,'FD')
        EventoVenda.objects.create(venda_id=alterada.venda_id,tipo_evento='item_adicionado_na_nota',
            descricao=f'Item adicionado na nota: {self.produto.nome}, quantidade 1 FD.')
        self.venda_snapshot(2,2,D('.5'),D('1.5'),'FD')
        kg=self.venda_snapshot(3,D('1.5'),D('.3'),D('1.2'),'FD')
        ItemVenda.objects.filter(pk=kg.pk).update(quantidade=3,unidade='KG')
        self.venda_snapshot(4,D('1.2'),1,D('.2'),'FD')
        compra_intermediaria=self.nova_compra(5,1)
        venda_anterior=self.venda_snapshot(6,D('1.2'),1,D('.2'),'FD')
        self.edicao_outro(venda_anterior)
        compra_recente=self.nova_compra(6.1,2)
        venda_posterior=self.venda_snapshot(7,D('2.2'),1,D('1.2'),'FD')
        self.edicao_outro(venda_posterior,resumo=False)
        venda_final=self.venda_snapshot(8,D('1.2'),1,D('.2'),'FD')
        self.edicao_outro(venda_final,tipo='item_removido_da_nota')
        r=self.resposta(periodo='tres_compras', visual='a')
        compras={l['pk']:l for l in r.context['extrato'] if l['fonte']=='compra'}
        self.assertEqual(compras[compra_intermediaria.pk]['saldo'],D('1.2'))
        self.assertEqual(compras[compra_recente.pk]['saldo'],D('2.2'))
        self.assertIsNone(next(l for l in compras.values() if l['documento']==self.compra.pk)['saldo'])
        self.assertEqual(self.linha(r,venda_anterior)['saldo'],D('.2'))
        self.assertEqual(self.linha(r,venda_final)['saldo'],D('.2'))
        self.assertIsNone(self.linha(r,kg)['saldo'])
        self.assertEqual(r.context['esperado'],D('.2'))

    def manual(self,dia=1.5):
        m=MovimentacaoEstoqueManual.objects.create(produto=self.produto,tipo='ajuste',
            estoque_antes=51,estoque_depois=51,diferenca=0,motivo='Evidência fictícia')
        MovimentacaoEstoqueManual.objects.filter(pk=m.pk).update(criado_em=self.inicio+timedelta(days=dia))
        m.refresh_from_db();return m

    def test_manual_exibido_sem_unidade_inferida_e_interrompe_continuidade(self):
        m=self.manual()
        r=self.resposta()
        linha=next(l for l in r.context['extrato'] if l['fonte']=='manual')
        self.assertEqual(linha['classe'],'manual')
        self.assertIsNone(linha['entrada']);self.assertIsNone(linha['saldo'])
        self.assertTrue(self.linha(r,self.ultima)['saldo_pontual'])
        self.assertContains(r,f'Ajuste manual #{m.pk}')
        with CaptureQueriesContext(connection) as queries:
            detalhe=self.client.get(linha['url'])
        self.assertContains(detalhe,'Unidade histórica não gravada')
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.client.logout();self.assertEqual(self.client.get(linha['url']).status_code,403)
        self.client.force_login(get_user_model().objects.create_user('contador-sem-gestao'))
        self.assertEqual(self.client.get(linha['url']).status_code,403)
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.post(linha['url']).status_code,405)
        outro=self.manual();MovimentacaoEstoqueManual.objects.filter(pk=outro.pk).update(produto=self.outro)
        self.assertEqual(self.client.get(reverse('inventario:movimento_manual',args=[self.inv.pk,self.rodada.pk,outro.pk])).status_code,404)

    def test_manual_observador_dedup_somente_referencia_e_saldos_exatos(self):
        m=self.manual()
        obs=MovimentoObservado.objects.create(produto=self.produto,sequencia=1,antes=51,depois=51,
            origem='ajuste_manual',referencia=f'Movimentação manual #{m.pk}',registrado_em=m.criado_em)
        r=self.resposta()
        self.assertEqual(len([l for l in r.context['extrato'] if l['fonte']=='manual']),1)
        self.assertFalse(any(l['fonte']=='digital' for l in r.context['extrato']))
        for campo,valor in (('referencia','Referência desconhecida'),('depois',52)):
            with self.subTest(campo=campo):
                MovimentoObservado.objects.filter(pk=obs.pk).update(**{campo:valor})
                self.assertTrue(any(l['fonte']=='digital' for l in self.resposta().context['extrato']))
                MovimentoObservado.objects.filter(pk=obs.pk).update(referencia=f'Movimentação manual #{m.pk}',depois=51)

    def test_manobra_fora_do_periodo_nao_oculta_observador_dentro(self):
        m=self.manual(-5)
        MovimentoObservado.objects.create(produto=self.produto,sequencia=1,antes=51,depois=51,
            origem='ajuste_manual',referencia=f'Movimentação manual #{m.pk}',registrado_em=self.inicio+timedelta(hours=1))
        self.assertTrue(any(l['fonte']=='digital' for l in self.resposta().context['extrato']))

    def test_angest_simulado_sem_compra_recente_amplia_para_manual_antigo(self):
        ItemCompra.objects.filter(produto=self.produto).delete()
        self.manual(-45)
        r=self.resposta()
        self.assertContains(r,'últimos 30 dias')
        self.assertContains(r,'Ampliar para o histórico completo')
        completo=self.client.get(self.url+r.context['completo_url'])
        self.assertTrue(any(l['fonte']=='manual' for l in completo.context['extrato']))
        self.assertIsNone(completo.context['extrato'].saldo_inicial)

    def test_trigo_saco_simulado_manual_nao_usa_cadastro_atual(self):
        self.produto.unidade_compra='SC';self.produto.fator_conversao=25;self.produto.save()
        m=self.manual()
        r=self.resposta()
        linha=next(l for l in r.context['extrato'] if l['fonte']=='manual')
        self.assertEqual(linha['unidade'],'')
        self.assertIsNone(linha['saldo'])

    def test_ok_terminal_unico_apos_ultima_pagina_e_referencia_fixa(self):
        MovimentoObservado.objects.bulk_create([MovimentoObservado(produto=self.produto,sequencia=i,
            antes=10,depois=9,origem='nao_identificada',registrado_em=self.inicio+timedelta(minutes=i)) for i in range(1,61)])
        primeiro=self.resposta()
        self.assertNotContains(primeiro,'data-evento="ok-conferencia"')
        ultimo=self.resposta(extrato_pagina=2)
        self.assertContains(ultimo,'data-evento="ok-conferencia"',count=1)
        historico=self.resposta(compra=self.compra.pk,intervalo='entre_compras')
        self.assertEqual(historico.context['referencia'],self.agora)

    def test_ok_posterior_a_periodo_personalizado_e_identificado(self):
        dia=self.inicio.date().isoformat()
        r=self.resposta(periodo='personalizado',data_inicial=dia,data_final=dia)
        self.assertContains(r,'Referência posterior ao período investigado')
        self.assertEqual(r.context['esperado'],10)
