"""Historical cost acceptance and refusal on guarded disposable PostgreSQL."""
from datetime import datetime, date, UTC, timedelta
from decimal import Decimal as D
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque.models import ItemVenda, ItemVendaRemovido, Venda, EventoVenda, Compra, ItemCompra
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from estoque.services.analise_classes_produtos import analise_consolidada
from estoque.services.relatorios_produtos import itens_comerciais
from estoque.services.relatorios_produtos_indicadores import indicadores_produto
from estoque.services.relatorios import resumo_vendas
from inventario.models import (Inventario, ItemInventario, RodadaContagem,
    ConfirmacaoContagem, MovimentoObservado, DetalheMovimentoEstoque, EventoInventario)
from inventario.tests import produto

WHEN = datetime(2026,11,10,15,tzinfo=UTC)


class CustosHistoricosTests(PostgreSQLIsoladoTests, TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('custos-local',is_staff=True,is_superuser=True)
        self.p = produto(id=223,nome='Trigo histórico fictício',categoria='Farinhas',
            unidade_compra='FD',unidade_venda_2='KG',fator_conversao=999,
            preco_compra=9999,preco_vista=10000,preco_prazo=10001)

    def confirmar(self, p=None, fator='10', base='FD', secundaria='KG', instante=None, sequencia=0):
        p=p or self.p
        inv=Inventario.objects.create(titulo='Evidência fictícia',criterio='manual',criado_por=self.admin)
        item=ItemInventario.objects.create(inventario=inv,produto=p,custo_base=42,
            cadastro=dict(base=base,secundaria=secundaria,fator=fator,fracionado=True))
        rodada=RodadaContagem.objects.create(item=item,numero=1,esperado=0)
        return ConfirmacaoContagem.objects.create(rodada=rodada,versao_entrada=1,
            embalagens=0,avulsos=0,contado=0,contado_base=0,esperado=0,
            sequencia_ok=sequencia,ok_em=instante or WHEN-timedelta(days=2))

    def vender(self,q='1',fator='10',custo='42',unidade='KG',base='FD',p=None,when=WHEN,valor=None):
        p=p or self.p
        q=D(q);baixa=(q/D(fator)).quantize(D('.001'))
        receita=D(valor) if valor is not None else q*D('5.60')
        venda=Venda.objects.create(data_venda=when.date(),tipo_pagamento='Pix',total=receita)
        item=ItemVenda.objects.create(venda=venda,produto=p,quantidade=q,unidade=unidade,
            preco_unitario=receita/q,valor_total=receita,estoque_antes=10,
            estoque_depois=10-baixa,estoque_movimentado=baixa,estoque_unidade_snapshot=base,
            custo_unitario_snapshot=D(custo),custo_total_snapshot=(q/D(fator)*D(custo)).quantize(D('.01')),
            custo_unidade_snapshot=base,custo_origem_snapshot='preco_compra_atual')
        seq=MovimentoObservado.objects.filter(produto=p).count()+1
        m=MovimentoObservado.objects.create(produto=p,sequencia=seq,antes=10,depois=10-baixa,
            registrado_em=when,origem='venda',referencia=f'Venda #{venda.pk}')
        return item,m

    def analisar(self,**kwargs):
        return analise_consolidada(date(2026,11,1),date(2026,11,24),hoje=date(2026,11,26),**kwargs)

    def test_trigo_223_nove_kg_e_mesma_decisao_nos_tres_relatorios_sem_escrita(self):
        c=self.confirmar()
        itens=[self.vender(q=q,when=WHEN+timedelta(days=d))[0] for q,d in [('1',0),('6',1),('2',10)]]
        model_list=(ItemVenda,Venda,MovimentoObservado,ConfirmacaoContagem,ItemInventario)
        before={m._meta.label:list(m.objects.order_by('pk').values()) for m in model_list}
        with CaptureQueriesContext(connection) as sql:
            r=self.analisar()
            individual=indicadores_produto(itens_comerciais().filter(produto=self.p))
            geral=resumo_vendas(date(2026,11,1),date(2026,11,24),hoje=date(2026,11,26))['rentabilidade']
        line=r['classes'][0]['produtos'][0]
        self.assertEqual((line['quantidade'],line['faturamento'],line['custo'],line['custo_medio'],line['lucro'],line['margem']),
                         (D('0.9'),D('50.40'),D('37.80'),D('42.00'),D('12.60'),D('25.00')))
        self.assertEqual((individual['cmv'],individual['lucro'],individual['margem']),(D('37.80'),D('12.60'),D('25')))
        self.assertEqual((geral['cmv'],geral['lucro'],geral['margem']),(D('37.80'),D('12.60'),D('25')))
        self.assertEqual(len(r['validacoes_custo']),3)
        self.assertTrue(all(v['evidencia']['confirmacao_id']==c.pk for v in r['validacoes_custo'].values()))
        self.assertEqual(before,{m._meta.label:list(m.objects.order_by('pk').values()) for m in model_list})
        self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','DECLARE')) for q in sql))

    def test_fator_atual_e_delta_plausivel_nao_substituem_confirmacao(self):
        self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def compra(self, p, custo='42', dia=date(2026,10,1), status='finalizada', unidade='FD'):
        c=Compra.objects.create(data_compra=dia,status=status)
        return ItemCompra.objects.create(compra=c,produto=p,unidade=unidade,quantidade=1,
            preco_unitario=D(custo),valor_total=D(custo))

    def test_custo_gerencial_concilia_snapshot_compra_e_fator_sem_contagem_anterior(self):
        p=produto(unidade_compra='PCT',unidade_venda_2='UN',fator_conversao=6)
        i,_=self.vender(p=p,q='4',base='PCT',unidade='UN',fator='6',custo='28.14',valor='24')
        purchase=self.compra(p,custo='28.14',unidade='PCT')
        r=self.analisar();v=r['validacoes_custo'][i.pk]
        self.assertEqual(v['custo'],D('18.76'))
        self.assertEqual(v['evidencia']['item_compra_id'],purchase.pk)
        self.assertFalse(v['evidencia']['prova_fisica'])
        individual=indicadores_produto(itens_comerciais().filter(produto=p))
        geral=resumo_vendas(date(2026,11,1),date(2026,11,24),hoje=date(2026,11,26))['rentabilidade']
        self.assertEqual(individual['cmv'],D('18.76'))
        self.assertEqual(geral['cmv'],D('18.76'))
        self.assertEqual(r['classes'][0]['produtos'][0]['quantidade'],D(4)/6)

    def test_compra_posterior_cancelada_preco_diferente_ou_outra_unidade_nao_valida_custo(self):
        for changed in (dict(dia=date(2026,11,11)),dict(status='cancelada'),dict(custo='99'),dict(unidade='CX')):
            with self.subTest(changed=changed):
                p=produto(unidade_compra='FD',unidade_venda_2='KG',fator_conversao=10)
                i,_=self.vender(p=p)
                self.compra(p,**changed)
                self.assertIsNone(self.analisar()['validacoes_custo'][i.pk]['custo'])

    def test_embalagem_principal_independe_de_vendas_avulsas_ou_mistas(self):
        p=produto(unidade_compra='PCT',unidade_venda_2='UN',fator_conversao=6)
        self.compra(p,custo='28.14',unidade='PCT')
        self.vender(p=p,q='6',base='PCT',unidade='UN',fator='6',custo='28.14')
        first=self.analisar()['classes'][0]['produtos'][0]
        self.assertEqual((first['unidade'],first['quantidade'],first['custo_medio']),('PCT',1,D('28.14')))
        self.vender(p=p,q='1',base='PCT',unidade='PCT',fator='1',custo='28.14')
        second=self.analisar()['classes'][0]['produtos'][0]
        self.assertEqual((second['unidade'],second['quantidade'],second['custo_medio']),('PCT',2,D('28.14')))

    def test_renomeacao_embalagem_com_mesma_capacidade_preserva_custos(self):
        p=produto(unidade_compra='CT',unidade_venda_2='UN',fator_conversao=100)
        self.confirmar(p=p,base='FD',secundaria='UN',fator='100')
        self.vender(p=p,q='2.5',base='FD',unidade='FD',fator='1',custo='38.50')
        self.vender(p=p,q='.5',base='CT',unidade='CT',fator='1',custo='38.67')
        g=self.analisar()['classes'][0]['produtos'][0]
        self.assertEqual((g['unidade'],g['quantidade'],g['custo']),('CT',3,D('115.59')))
        self.assertEqual(g['custo_medio'],D('38.53'))
        self.assertEqual({a['unidade'] for a in g['apresentacoes']},{'FD','CT'})

    def test_divergencia_historica_nao_vira_equivalencia_comercial_pelo_cadastro_atual(self):
        self.p.fator_conversao=10
        self.p.save(update_fields=['fator_conversao'])
        self.confirmar(fator='20')
        self.vender(q='1',fator='10')
        g=self.analisar()['classes'][0]['produtos'][0]
        self.assertEqual((g['unidade'],g['quantidade']),('KG',1))
        self.assertIsNone(g['custo_medio'])

    def selecionar(self, *, documentar=True, quantidade_evento=1, instante=None):
        instante=instante or WHEN-timedelta(days=1)
        inv=Inventario.objects.create(titulo='Seleção original fictícia',criterio='manual',criado_por=self.admin)
        Inventario.objects.filter(pk=inv.pk).update(criado_em=instante-timedelta(minutes=1))
        item=ItemInventario.objects.create(inventario=inv,produto=self.p,custo_base=9999,
            cadastro=dict(base='FD',secundaria='KG',fator='10',fracionado=True))
        evento=None
        if documentar:
            evento=EventoInventario.objects.create(inventario=inv,acao='criado',dados=dict(quantidade=quantidade_evento))
            EventoInventario.objects.filter(pk=evento.pk).update(criado_em=instante)
        return item,evento

    def test_selecao_original_documentada_prova_fator_sem_exigir_contagem(self):
        item,evento=self.selecionar();i,_=self.vender()
        r=self.analisar();v=r['validacoes_custo'][i.pk]
        self.assertEqual(v['custo'],D('4.20'))
        self.assertEqual(v['evidencia']['evento_criacao_inventario_id'],evento.pk)
        self.assertEqual(v['evidencia']['item_inventario_id'],item.pk)
        self.assertIsNone(v['evidencia']['confirmacao_id'])
        self.assertEqual(v['evidencia']['criterio'],'movimento_e_selecao_documentada_v1')

    def test_selecao_sem_evento_de_criacao_nao_usa_so_data_do_inventario(self):
        self.selecionar(documentar=False);self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_selecao_com_quantidade_original_incompativel_nao_e_aceita(self):
        self.selecionar(quantidade_evento=2);self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_selecao_posterior_nao_recupera_custo_anterior(self):
        self.selecionar(instante=WHEN+timedelta(days=1));self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_multiplos_eventos_criado_deixam_origem_ambigua(self):
        item,_=self.selecionar()
        EventoInventario.objects.create(inventario=item.inventario,acao='criado',dados=dict(quantidade=1))
        self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_confirmacao_posterior_sozinha_nao_prova_passado(self):
        self.confirmar(instante=WHEN+timedelta(days=1))
        self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_sem_movimento_documentado_nao_recupera(self):
        self.confirmar();_,m=self.vender();m.delete()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_fator_contraditorio_nao_escolhe_o_que_fecha_a_conta(self):
        self.confirmar(fator='12');self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_confirmacao_seguinte_contraditoria_bloqueia_intervalo_incerto(self):
        self.confirmar();self.confirmar(fator='12',instante=WHEN+timedelta(days=1))
        self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_mesmo_instante_com_fatores_contraditorios_bloqueia(self):
        self.confirmar(fator='12');self.confirmar(fator='10');self.vender()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_movimentos_multiplos_nao_duplicam_custo(self):
        self.confirmar();item,m=self.vender()
        MovimentoObservado.objects.create(produto=self.p,sequencia=2,antes=m.antes,depois=m.depois,
            registrado_em=WHEN,origem='venda',referencia=m.referencia)
        r=self.analisar();self.assertEqual(r['total']['com_custo'],0)
        self.assertEqual(r['total']['faturamento'],D('5.60'))

    def test_itens_iguais_na_mesma_venda_nao_compartilham_um_movimento(self):
        self.confirmar();i,_=self.vender()
        i.pk=None;i.save()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_edicao_impede_reutilizar_prova_anterior(self):
        self.confirmar();i,_=self.vender()
        EventoVenda.objects.create(venda=i.venda,tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {self.p.nome}. De 1 para 2')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def inclusao(self, item, *, quantidade=None):
        return EventoVenda.objects.create(venda=item.venda,tipo_evento='item_adicionado_na_nota',
            descricao=f'Item adicionado na nota: {self.p.nome}, quantidade '
                f'{quantidade or item.quantidade} {item.unidade}, preco unitario R$ '
                f'{item.preco_unitario:.2f}, valor acrescentado R$ {item.valor_total:.2f}.')

    def test_inclusao_original_documentada_concilia_sem_ser_tratada_como_edicao(self):
        self.confirmar();i,_=self.vender(q='2');evento=self.inclusao(i)
        r=self.analisar()
        self.assertEqual(r['total']['custo'],D('8.40'))
        self.assertEqual(r['validacoes_custo'][i.pk]['evidencia']['evento_inclusao_id'],evento.pk)

    def total_documentado(self):
        selection, _ = self.selecionar()
        ItemInventario.objects.filter(pk=selection.pk).update(custo_base=D('71.67'),
            cadastro=dict(base='FD',secundaria='',fator='1',fracionado=False))
        i, m = self.vender(q='1',custo='71.67',valor='9.70')
        event = self.inclusao(i)
        return i, m, event

    def test_total_documentado_sem_conversao_retroativa_concilia_relatorios(self):
        i, m, event = self.total_documentado()
        r = self.analisar()
        v = r['validacoes_custo'][i.pk]
        self.assertEqual(v['custo'],D('7.17'))
        self.assertEqual(v['evidencia']['evento_inclusao_id'],event.pk)
        self.assertFalse(v['evidencia']['conversao_historica_comprovada'])
        line = r['classes'][0]['produtos'][0]
        self.assertEqual((line['unidade'],line['quantidade']),('KG',D('1')))
        geral=resumo_vendas(date(2026,11,1),date(2026,11,24),hoje=date(2026,11,26))['rentabilidade']
        individual=indicadores_produto(itens_comerciais().filter(pk=i.pk))
        for value in (geral,individual):
            self.assertEqual((value['cmv'],value['lucro'],value['margem']),
                (D('7.17'),D('2.53'),D('26.08')))
        self.assertEqual(r['total']['lucro'],D('2.53'))
        self.assertEqual(ItemVenda.objects.get(pk=i.pk).custo_total_snapshot,D('7.17'))

    def test_total_documentado_recusa_custo_incompativel(self):
        i,_,_=self.total_documentado()
        ItemVenda.objects.filter(pk=i.pk).update(custo_total_snapshot=D('7.18'))
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_total_documentado_recusa_quantidade_editada(self):
        i,_,_=self.total_documentado()
        self.alteracao(i,'1','2')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_total_documentado_recusa_movimento_divergente(self):
        _,m,_=self.total_documentado()
        MovimentoObservado.objects.filter(pk=m.pk).update(depois=D('9.800'))
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_total_documentado_recusa_inclusao_ambigua(self):
        i,_,_=self.total_documentado()
        self.inclusao(i)
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_total_documentado_preserva_bloqueio_de_fator_explicito_contraditorio(self):
        self.total_documentado()
        self.confirmar(fator='12')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_inclusao_documentada_com_quantidade_contraditoria_permanece_bloqueada(self):
        self.confirmar();i,_=self.vender(q='2');self.inclusao(i,quantidade='3')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_formato_de_inclusao_sem_valor_total_explicito_confere_quantidade_e_preco(self):
        self.confirmar();i,_=self.vender(q='2');evento=self.inclusao(i)
        EventoVenda.objects.filter(pk=evento.pk).update(descricao=evento.descricao.replace(
            f', valor acrescentado R$ {i.valor_total:.2f}.','.'))
        self.assertEqual(self.analisar()['total']['custo'],D('8.40'))

    def test_inclusao_sem_total_documentado_nao_aceita_receita_incompativel(self):
        self.confirmar();i,_=self.vender(q='2');evento=self.inclusao(i)
        EventoVenda.objects.filter(pk=evento.pk).update(descricao=evento.descricao.replace(
            f', valor acrescentado R$ {i.valor_total:.2f}.','.'))
        ItemVenda.objects.filter(pk=i.pk).update(valor_total=D('20.00'))
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def alteracao(self,item,antes,depois,unidade_antes=None):
        return EventoVenda.objects.create(venda=item.venda,tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {self.p.nome}. De {D(antes):.3f} '
                f'{unidade_antes or item.unidade} para {D(depois):.3f} {item.unidade}. '
                'Total sera recalculado ao final da edicao.')

    def test_edicao_documentada_sem_mudar_projecao_fisica_e_custo_preservado(self):
        self.confirmar();i,_=self.vender(q='.996')
        ItemVenda.objects.filter(pk=i.pk).update(quantidade=D('1'),valor_total=D('5.60'),custo_total_snapshot=D('4.20'))
        Venda.objects.filter(pk=i.venda_id).update(total=D('5.60'))
        evento=self.alteracao(i,'.996','1')
        r=self.analisar();v=r['validacoes_custo'][i.pk]
        self.assertEqual(v['custo'],D('4.20'))
        self.assertEqual(v['evidencia']['eventos_ajuste_sem_efeito_fisico'],[evento.pk])

    def test_edicao_com_alteracao_fisica_nao_reaproveita_movimento_unico(self):
        self.confirmar();i,_=self.vender(q='2');self.alteracao(i,'1','2')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_edicoes_sem_sequencia_documental_continua_sao_bloqueadas(self):
        self.confirmar();i,_=self.vender(q='1')
        self.alteracao(i,'.990','.995');self.alteracao(i,'.996','1')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_edicao_de_unidade_nao_e_um_ajuste_sem_efeito_fisico(self):
        self.confirmar();i,_=self.vender(q='1');self.alteracao(i,'.996','1',unidade_antes='UN')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def editar_com_movimento(self,antes='1',depois='3',origem='venda',intercalar=False):
        self.confirmar();i,m=self.vender(q=antes)
        delta=(D(depois)-D(antes))/D('10')
        inicio=m.depois-(D('1') if intercalar else D('0'))
        if intercalar:
            MovimentoObservado.objects.create(produto=self.p,sequencia=2,antes=m.depois,depois=inicio,
                registrado_em=WHEN+timedelta(seconds=10),origem='venda',referencia='Venda #999999')
        fim=inicio-delta
        baixa=(D(depois)/10).quantize(D('.001'))
        ItemVenda.objects.filter(pk=i.pk).update(quantidade=D(depois),valor_total=D(depois)*D('5.60'),
            custo_total_snapshot=(D(depois)*D('4.20')).quantize(D('.01')),
            estoque_antes=fim+baixa,estoque_depois=fim,estoque_movimentado=baixa)
        MovimentoObservado.objects.create(produto=self.p,sequencia=3 if intercalar else 2,
            antes=inicio,depois=fim,registrado_em=WHEN+timedelta(seconds=20),
            origem=origem,referencia=m.referencia)
        self.alteracao(i,antes,depois)
        return i

    def test_sequencia_documentada_de_acrescimo_recupera_custo_uma_vez(self):
        i=self.editar_com_movimento()
        before=list(ItemVenda.objects.values())
        r=self.analisar();v=r['validacoes_custo'][i.pk]
        self.assertEqual((r['total']['custo'],r['total']['com_custo']),(D('12.60'),1))
        self.assertEqual(v['evidencia']['criterio'],'sequencia_edicao_e_conversao_historica_v1')
        self.assertEqual(len(v['evidencia']['movimento_ids']),2)
        self.assertEqual(before,list(ItemVenda.objects.values()))

    def test_sequencia_documentada_de_reducao_preserva_o_custo_atual(self):
        self.editar_com_movimento(antes='2',depois='1.5',origem='devolucao_venda')
        self.assertEqual(self.analisar()['total']['custo'],D('6.30'))

    def test_snapshot_rebaseado_nao_e_confundido_com_antes_original(self):
        self.editar_com_movimento(intercalar=True)
        self.assertEqual(self.analisar()['total']['custo'],D('12.60'))

    def test_sequencia_com_sinal_de_movimento_incompativel_permanece_bloqueada(self):
        self.editar_com_movimento(origem='devolucao_venda')
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def reincluir(self, *, devolver=True, arquivar=True):
        self.confirmar();i,m=self.vender(q='2')
        MovimentoObservado.objects.create(produto=self.p,sequencia=2,antes=m.depois,
            depois=m.antes if devolver else m.antes-D('.1'),registrado_em=WHEN+timedelta(seconds=10),
            origem='devolucao_venda',referencia=m.referencia)
        rem=EventoVenda.objects.create(venda=i.venda,tipo_evento='item_removido_da_nota',
            descricao=f'Item removido da nota: {self.p.nome}, quantidade 2.000 KG, valor abatido R$ 11.20.')
        EventoVenda.objects.filter(pk=rem.pk).update(criado_em=WHEN+timedelta(seconds=12))
        if arquivar:
            ItemVendaRemovido.objects.create(venda=i.venda,produto=self.p,produto_nome_snapshot=self.p.nome,
                quantidade_snapshot=2,unidade_snapshot='KG',preco_unitario_snapshot=D('5.60'),
                valor_total_snapshot=D('11.20'),item_venda_original_id=i.pk+10000,
                estoque_antes=m.antes,estoque_depois=m.depois,estoque_movimentado=D('.2'),
                estoque_unidade_snapshot='FD',estoque_devolvido=True,
                estoque_devolvido_em=WHEN+timedelta(seconds=11))
        MovimentoObservado.objects.create(produto=self.p,sequencia=3,antes=10,depois=D('9.8'),
            registrado_em=WHEN+timedelta(seconds=15),origem='venda',referencia=m.referencia)
        add=self.inclusao(i)
        EventoVenda.objects.filter(pk=add.pk).update(criado_em=WHEN+timedelta(seconds=16))
        return i

    def test_remocao_integral_e_reinclusao_contam_so_custo_do_item_atual(self):
        i=self.reincluir();r=self.analisar()
        self.assertEqual((r['total']['custo'],r['total']['itens']),(D('8.40'),1))
        ev=r['validacoes_custo'][i.pk]['evidencia']
        self.assertEqual(ev['criterio'],'remocao_compensada_e_reinclusao_historica_v1')
        self.assertEqual(len(ev['movimento_ids']),3)
        self.assertEqual(len(ev['evento_ids']),2)

    def test_remocao_sem_devolucao_integral_nao_valida_reinclusao(self):
        self.reincluir(devolver=False)
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_remocao_sem_snapshot_arquivado_nao_valida_reinclusao(self):
        self.reincluir(arquivar=False)
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_reinclusao_exige_arquivo_unico_com_identidade_e_devolucao_confirmadas(self):
        self.reincluir();archive=ItemVendaRemovido.objects.get()
        for changed in ({'item_venda_original_id':None},{'estoque_devolvido':False},{'status':'revertido'}):
            with self.subTest(changed=changed):
                ItemVendaRemovido.objects.filter(pk=archive.pk).update(**changed)
                self.assertEqual(self.analisar()['total']['com_custo'],0)
                archive.save()
        archive.pk=None;archive.save()
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_produto_consolidado_mantem_media_por_unidade_e_cobertura_parcial(self):
        p=produto(nome='Micos Uva sintético',categoria='Refrigerantes e Afins',
            unidade_compra='PCT',unidade_venda_2='UN',fator_conversao=999)
        self.vender(p=p,q='1.5',unidade='PCT',base='PCT',fator='1',custo='28.14',valor='60')
        self.vender(p=p,q='4',unidade='UN',base='PCT',fator='6',custo='28.14',valor='24')
        r=self.analisar();g=r['classes'][0]['produtos'][0]
        self.assertEqual(r['quantidade_produtos'],1)
        self.assertEqual((g['faturamento'],g['custo'],g['receita_coberta'],g['lucro']),
            (D('84'),D('42.21'),D('60'),D('17.79')))
        self.assertFalse(g['completa'])
        self.assertIsNone(g['quantidade']);self.assertIsNone(g['preco_medio'])
        self.assertEqual(g['custo_medio'],D('28.14'))
        self.assertEqual(g['lucro_unitario'],D('11.86'))
        units={a['unidade']:a for a in g['apresentacoes']}
        self.assertEqual((units['PCT']['quantidade'],units['PCT']['custo_medio'],units['PCT']['preco_medio']),
            (D('1.5'),D('28.14'),D('40')))
        self.assertEqual((units['UN']['quantidade'],units['UN']['preco_medio']),(D('4'),D('6')))
        self.assertIsNone(units['UN']['lucro'])

    def test_nomes_iguais_nao_agrupam_ids_distintos(self):
        for _ in range(2):
            p=produto(nome='Açúcar sintético',categoria='Estivas',unidade_compra='FD',unidade_venda_2='KG')
            self.vender(p=p,q='1',unidade='FD',base='FD',fator='1')
            self.vender(p=p,q='2',unidade='KG',base='FD',fator='10')
        r=self.analisar();produtos=r['classes'][0]['produtos']
        self.assertEqual(len(produtos),2)
        self.assertEqual(len({p['produto_id'] for p in produtos}),2)
        self.assertTrue(all(len(p['apresentacoes'])==2 for p in produtos))

    def test_quantidades_visiveis_no_produto_e_controle_no_nome_sem_medias_mistas(self):
        import re
        from html import unescape
        self.confirmar();self.vender(q='9')
        p=produto(nome='Micos teste visual',categoria='Refrigerantes e Afins')
        self.vender(p=p,q='1.5',unidade='PCT',base='PCT',fator='1',custo='28.14',valor='54')
        self.vender(p=p,q='4',unidade='UN',base='PCT',fator='6',custo='28.14',valor='24')
        self.client.force_login(self.admin)
        response=self.client.get(reverse('estoque:relatorios_classes_produtos'),
            dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-24'))
        self.assertEqual(response.status_code,200)
        html=response.content.decode()
        def cells(pid):
            row=re.search(r'<tr class="ac-produto ac-produto-grupo" data-ac-produto-id="'+str(pid)+r'">(.*?)</tr>',html,re.S)[1]
            return re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>',row,re.S)
        def text(value):return ' '.join(unescape(re.sub('<[^>]+>',' ',value)).split())
        multi=cells(p.pk)
        self.assertIn('data-ac-produto-botao',multi[0])
        self.assertNotIn('data-ac-produto-botao',multi[1])
        self.assertEqual(text(multi[1]),'1,5 PCT + 4 UN')
        self.assertIn('Ver por unidade'.lower(),text(multi[5]).lower())
        self.assertIn('28,14',text(multi[3]))
        self.assertIn('Parcial · quantidade coberta',text(multi[3]))
        self.assertIn('7,86',text(multi[7]))
        self.assertIn('Parcial · quantidade coberta',text(multi[7]))
        self.assertNotIn('Base das médias',html)
        self.assertNotIn('ac-medias',html)
        self.assertIn('Motivo do custo a conferir',text(multi[0]))
        single=list(map(text,cells(self.p.pk)))
        self.assertEqual(single[1:],[ '0,9 FD','R$ 37,80','R$ 42,00 /FD','R$ 50,40','R$ 56,00 /FD','R$ 12,60','R$ 14,00 /FD','25,00%'])
        self.assertEqual(html.count(f'class="ac-produto ac-produto-grupo" data-ac-produto-id="{p.pk}"'),1)

    def test_todas_as_ordenacoes_movimentam_produtos_com_suas_apresentacoes(self):
        from estoque.services.analise_classes_produtos import ORDENS
        self.vender(q='1',unidade='FD',base='FD',fator='1')
        self.vender(q='2')
        p=produto(nome='Outro produto fictício',categoria=self.p.categoria)
        self.vender(p=p,q='1',unidade='PCT',base='PCT',fator='1')
        for ordem in ORDENS:
            with self.subTest(ordem=ordem):
                r=self.analisar(ordem=ordem)
                groups=r['classes'][0]['produtos']
                self.assertEqual(len(groups),2)
                self.assertTrue(all(all(a['produto_id']==g['produto_id'] for a in g['apresentacoes']) for g in groups))
                self.assertEqual(sum(g['faturamento'] for g in groups),r['total']['faturamento'])

    def test_multiplas_inclusoes_nao_compartilham_o_mesmo_movimento(self):
        self.confirmar();i,_=self.vender(q='2');self.inclusao(i);self.inclusao(i)
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_atribuicao_incompativel_nao_pode_cair_no_legado(self):
        self.confirmar();i,m=self.vender()
        DetalheMovimentoEstoque.objects.create(movimento=m,operacao_id=uuid4(),ordem=1,
            documento_tipo='venda',documento_id=str(i.venda_id),item_referencia=str(i.pk),
            acao='saida',contexto='sistema',executor_nome='Teste',unidade_base='FD',
            unidade_entrada='KG',quantidade_entrada=i.quantidade,conversao='divisao',fator=12)
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_custo_congelado_contraditorio_nao_e_recalculado(self):
        self.confirmar();i,_=self.vender()
        ItemVenda.objects.filter(pk=i.pk).update(custo_total_snapshot=D('4.21'))
        self.assertEqual(self.analisar()['total']['com_custo'],0)

    def test_pacotes_avulsos_e_unidades_diferentes_sem_medias_mistas(self):
        for label,fator in [('Cervejas','15'),('Refrigerantes e Afins','6')]:
            p=produto(nome=label+' fictício',categoria=label,unidade_compra='PCT',
                unidade_venda_2='UN',fator_conversao=999)
            self.confirmar(p=p,base='PCT',secundaria='UN',fator=fator)
            self.vender(p=p,unidade='UN',base='PCT',fator=fator,q=fator,custo='30',valor='60')
            self.vender(p=p,unidade='PCT',base='PCT',fator='1',q='2',custo='30',valor='100')
        r=self.analisar()
        for classe in r['classes']:
            self.assertIsNone(classe['custo_medio'])
            units={q['unidade']:q for q in classe['quantidades']}
            self.assertEqual(units['PCT']['custo_medio'],D('30'))
            self.assertEqual(units['PCT']['custo'],D('60'))
            self.assertEqual(units['UN']['custo'],D('30'))
        self.assertEqual(r['total']['custo'],D('180'))

    def test_media_parcial_usa_somente_quantidade_coberta(self):
        self.confirmar();self.vender(q='2')
        i,_=self.vender(q='8');ItemVenda.objects.filter(pk=i.pk).update(custo_total_snapshot=None)
        line=self.analisar()['classes'][0]['produtos'][0]
        self.assertEqual((line['quantidade'],line['quantidade_coberta'],line['custo_medio']),
                         (D('10'),D('2'),D('4.20')))
        self.assertFalse(line['completa'])
        unit=line['quantidades'][0]
        self.assertEqual((unit['custo'],unit['custo_medio'],unit['quantidade_coberta']),
                         (D('8.40'),D('4.20'),D('2')))

    def test_consultas_em_lote_nao_crescem_com_numero_de_itens(self):
        self.confirmar();self.vender()
        with CaptureQueriesContext(connection) as small:self.analisar()
        for _ in range(20):self.vender()
        with CaptureQueriesContext(connection) as large:self.analisar()
        self.assertEqual(len(small),len(large))
        self.assertLessEqual(len(large),9)

    def test_ordem_colunas_e_detalhamento_da_classe(self):
        self.confirmar();self.vender()
        self.client.force_login(self.admin)
        response=self.client.get(reverse('estoque:relatorios_classes_produtos'),
            dict(periodo='personalizado',data_inicial='2026-11-01',data_final='2026-11-24'))
        self.assertEqual(response.status_code,200)
        html=response.content.decode()
        head=html.split('<thead>')[1].split('</thead>')[0]
        labels=['Produto','Quantidade vendida','CMV gerencial','Custo médio','Faturamento comercial',
                'Preço médio','Lucro bruto gerencial','Lucro por unidade','Margem bruta']
        positions=[head.index('>'+label+'</th>') for label in labels]
        self.assertEqual(positions,sorted(positions))
        self.assertIn('Custo médio: R$ 4,20/KG',html)
        self.assertIn('CMV: R$ 4,20',html)
