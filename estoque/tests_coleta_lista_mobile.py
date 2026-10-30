"""Synthetic collection fixtures, independent of operational databases."""
import importlib
import json
from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, close_old_connections
from django.db.migrations.state import ProjectState
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse

from .models import (ListaCompraFornecedor, ItemListaCompraFornecedor, Produto,
                     Fornecedor, Compra, MovimentoFinanceiro, ContaPagar)
from .services import coleta_lista


class ColetaFixture:
    def setUp(self):
        super().setUp()
        self.usuario = get_user_model().objects.create_superuser('coleta-ficticia', '', 'teste')
        self.client.force_login(self.usuario)
        self.fornecedor = Fornecedor.objects.create(nome='Fornecedor fictício')
        self.lista = ListaCompraFornecedor.objects.create(fornecedor=self.fornecedor,
            data_lista=date(2026,10,30), data_inicio_periodo=date(2026,10,1),
            data_fim_periodo=date(2026,10,30), total_lista=Decimal('35.35'))
        self.produtos = [Produto.objects.create(nome=f'Produto fictício {n:02}', quantidade=10,
            preco_compra=preco, preco_vista=20, preco_prazo=21, unidade_compra='CX',
            unidade_venda_1='CX') for n,preco in enumerate(('10.05','5.20'),1)]
        for p,q in zip(self.produtos,(3,1)):
            ItemListaCompraFornecedor.objects.create(lista=self.lista,produto=p,
                quantidade_sugerida=q,quantidade_final=q,unidade='CX',preco_compra=p.preco_compra,
                preco_unitario=p.preco_compra,total=p.preco_compra*q)
        self.url = reverse('estoque:compras_lista_fornecedor_editar',args=[self.lista.pk])

    def estado(self):
        self.lista.refresh_from_db()
        return dict(versao=coleta_lista.versao(self.lista),atualizada=self.lista.atualizado_em.isoformat())

    def acao(self,acao,**extra):
        dados=dict(self.estado(),acao=acao,**extra)
        return self.client.post(self.url,{'acao_coleta_mobile':json.dumps(dados)},HTTP_ACCEPT='application/json')

    def payload(self,mobile=True):
        self.lista.refresh_from_db()
        linhas=[dict(produtoId=str(i.produto_id),sugestao=str(i.quantidade_final),unidade=i.unidade,
            precoCompra=str(i.preco_compra),precoUnitario=str(i.preco_unitario),total=str(i.total))
            for i in self.lista.itens.all() if not mobile or (self.lista.coleta_mobile or {}).get('itens',{}).get(str(i.produto_id))!='removido']
        payload=dict(fornecedorId=str(self.fornecedor.pk),dataInicio='2026-10-01',dataFim='2026-10-30',linhas=linhas)
        if mobile:
            payload['coletaMobile']=dict(self.estado(),itens={l['produtoId']:
                self.lista.coleta_mobile['itens'][l['produtoId']] for l in linhas})
        return payload

    def salvar(self,payload,gerar=False):
        return self.client.post(self.url,{'lista_payload':json.dumps(payload),'gerar_compra':'1' if gerar else '0'},
            HTTP_ACCEPT='application/json')

    def sem_operacoes(self):
        self.assertFalse(Compra.objects.exists())
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        self.assertFalse(ContaPagar.objects.exists())
        for p in self.produtos:
            p.refresh_from_db(); self.assertEqual(p.quantidade,10); self.assertEqual(p.preco_vista,20)


class ColetaListaTests(ColetaFixture,TestCase):
    def test_criacao_desktop_e_mobile_registram_origem_sem_converter_antigas(self):
        payload=self.payload(False)
        url=reverse('estoque:compras_lista_fornecedor_gravar')
        r=self.client.post(url,{'lista_payload':json.dumps(payload)},HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code,200)
        self.assertEqual(ListaCompraFornecedor.objects.get(pk=r.json()['lista_id']).coleta_mobile,{})
        payload['coletaMobileNova']=True
        r=self.client.post(url,{'lista_payload':json.dumps(payload)},HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code,200)
        nova=ListaCompraFornecedor.objects.get(pk=r.json()['lista_id'])
        self.assertTrue(nova.coleta_mobile['iniciada'])
        self.assertEqual(set(nova.coleta_mobile['itens'].values()),{'pego'})
        self.sem_operacoes()

    def test_nova_planejada_exige_inicio_preserva_todos_os_campos(self):
        antes=list(self.lista.itens.values())
        self.assertEqual(self.lista.coleta_mobile,{})
        self.assertEqual(self.acao('iniciar_pego').status_code,409)
        self.assertEqual(self.acao('iniciar_pendente').status_code,200)
        self.lista.refresh_from_db()
        self.assertEqual(set(self.lista.coleta_mobile['itens'].values()),{'pendente'})
        self.assertEqual(antes,list(self.lista.itens.values()))
        self.sem_operacoes()

    def test_antiga_exige_escolha_explicita_e_nao_repete(self):
        self.lista.coleta_mobile=None;self.lista.save(update_fields=['coleta_mobile'])
        self.client.get(self.url);self.lista.refresh_from_db();self.assertIsNone(self.lista.coleta_mobile)
        self.assertEqual(self.acao('iniciar_pego').status_code,200)
        self.lista.refresh_from_db();anterior=self.lista.coleta_mobile
        self.assertEqual(set(anterior['itens'].values()),{'pego'})
        self.assertEqual(self.acao('iniciar_pendente').status_code,409)
        self.lista.refresh_from_db();self.assertEqual(self.lista.coleta_mobile,anterior)

    def test_antiga_pode_iniciar_com_todos_pendentes(self):
        self.lista.coleta_mobile=None;self.lista.save(update_fields=['coleta_mobile'])
        self.assertEqual(self.acao('iniciar_pendente').status_code,200)
        self.lista.refresh_from_db()
        self.assertEqual(set(self.lista.coleta_mobile['itens'].values()),{'pendente'})
        self.sem_operacoes()

    def test_parcial_reabre_salva_desktop_e_nao_reinicia(self):
        self.acao('iniciar_pendente');pk=str(self.produtos[0].pk)
        self.assertEqual(self.acao('mover',produto=pk,estado='pego').status_code,200)
        self.assertEqual(self.salvar(self.payload()).status_code,200)
        self.client.get(self.url)
        self.assertEqual(self.salvar(self.payload(mobile=False)).status_code,200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.coleta_mobile['itens'],{pk:'pego',str(self.produtos[1].pk):'pendente'})
        self.assertEqual(self.acao('mover',produto=pk,estado='pendente').status_code,200)
        self.sem_operacoes()

    def test_remocao_e_edicao_preservam_estado_dos_demais(self):
        self.acao('iniciar_pendente');self.acao('mover',produto=str(self.produtos[1].pk),estado='removido')
        payload=self.payload();payload['linhas'][0].update(sugestao='4',precoCompra='11.25',total='45')
        self.assertEqual(self.salvar(payload).status_code,200)
        self.lista.refresh_from_db();self.assertEqual(self.lista.itens.count(),1)
        i=self.lista.itens.get();self.assertEqual(i.quantidade_final,4);self.assertEqual(i.total,45)
        self.assertEqual(self.lista.coleta_mobile['itens'],{str(self.produtos[0].pk):'pendente'})
        self.sem_operacoes()

    def test_versao_antiga_reenvio_e_produto_alheio_nao_sobrescrevem(self):
        dados=dict(self.estado(),acao='iniciar_pendente')
        self.assertEqual(self.client.post(self.url,{'acao_coleta_mobile':json.dumps(dados)}).status_code,200)
        self.assertEqual(self.client.post(self.url,{'acao_coleta_mobile':json.dumps(dados)}).status_code,409)
        payload=self.payload();self.acao('mover',produto=str(self.produtos[0].pk),estado='pego')
        self.assertEqual(self.salvar(payload).status_code,409)
        self.assertEqual(self.acao('mover',produto='999999',estado='pego').status_code,409)
        self.assertEqual(self.acao('mover',produto=str(self.produtos[0].pk),estado='invalido').status_code,409)
        self.sem_operacoes()

    def test_estado_invalido_e_sem_inicio_rejeitados(self):
        self.assertEqual(self.acao('mover',produto=str(self.produtos[0].pk),estado='pego').status_code,409)
        self.acao('iniciar_pendente');payload=self.payload()
        payload['coletaMobile']['itens'][str(self.produtos[0].pk)]=[]
        self.assertEqual(self.salvar(payload).status_code,409)
        self.sem_operacoes()

    def test_geracao_mobile_so_confirmados_preserva_pendentes_e_nao_duplica(self):
        self.acao('iniciar_pendente')
        self.assertEqual(self.salvar(self.payload(),True).status_code,409)
        self.acao('mover',produto=str(self.produtos[0].pk),estado='pego')
        self.assertEqual(self.salvar(self.payload(),True).status_code,302)
        c=Compra.objects.get();self.assertEqual(c.status,'rascunho')
        self.assertEqual(list(c.itens.values_list('produto_id',flat=True)),[self.produtos[0].pk])
        self.assertEqual(c.total,Decimal('30.15'));self.assertEqual(self.lista.itens.count(),2)
        self.assertEqual(self.salvar(self.payload(),True).status_code,302)
        self.assertEqual(Compra.objects.count(),1)
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_desktop_antigo_nao_exige_coleta_e_gera_todos(self):
        self.lista.coleta_mobile=None;self.lista.save(update_fields=['coleta_mobile'])
        self.assertEqual(self.salvar(self.payload(False),True).status_code,302)
        self.assertEqual(Compra.objects.get().itens.count(),2)
        self.lista.refresh_from_db();self.assertIsNone(self.lista.coleta_mobile)

    def test_permissoes_csrf_e_finalizadas(self):
        cliente=Client(enforce_csrf_checks=True);cliente.force_login(self.usuario)
        self.assertEqual(cliente.post(self.url,{'acao_coleta_mobile':'{}'}).status_code,403)
        user=get_user_model().objects.create_user('sem-permissao',password='teste')
        self.client.force_login(user);self.assertEqual(self.acao('iniciar_pendente').status_code,403)
        self.client.force_login(self.usuario)
        Compra.objects.create(lista_fornecedor=self.lista,fornecedor=self.fornecedor,data_compra=date(2026,10,30),status='finalizada')
        self.assertEqual(self.acao('iniciar_pendente').status_code,302)
        self.lista.refresh_from_db();self.assertEqual(self.lista.coleta_mobile,{})


class ColetaMigrationTests(TransactionTestCase):
    def test_migration_distingue_legadas_de_novas_em_tabela_descartavel(self):
        # Exercise only the added column, in a dedicated disposable test table.
        from django.apps import apps
        state=ProjectState.from_apps(apps)
        key=('estoque','listacomprafornecedor')
        state.models[key].options['db_table']='teste_coleta_migration'
        state.remove_field('estoque','listacomprafornecedor','coleta_mobile')
        Old=state.apps.get_model('estoque','ListaCompraFornecedor')
        with connection.schema_editor() as editor: editor.create_model(Old)
        try:
            old=Old.objects.create(data_lista=date.today(),data_inicio_periodo=date.today(),data_fim_periodo=date.today())
            migration=importlib.import_module('estoque.migrations.0137_lista_coleta_mobile').Migration
            for operation in migration.operations:
                after=state.clone();operation.state_forwards('estoque',after)
                with connection.schema_editor() as editor: operation.database_forwards('estoque',editor,state,after)
                state=after
            New=state.apps.get_model('estoque','ListaCompraFornecedor')
            self.assertIsNone(New.objects.get(pk=old.pk).coleta_mobile)
            new=New.objects.create(data_lista=date.today(),data_inicio_periodo=date.today(),data_fim_periodo=date.today())
            self.assertEqual(new.coleta_mobile,{})
        finally:
            with connection.schema_editor() as editor: editor.delete_model(state.apps.get_model('estoque','ListaCompraFornecedor'))


@skipUnless(connection.vendor=='postgresql','Concorrência requer PostgreSQL descartável')
class ColetaConcorrenciaTests(ColetaFixture,TransactionTestCase):
    def test_duas_escolhas_iniciais_nao_reiniciam_coleta(self):
        self.lista.coleta_mobile=None;self.lista.save(update_fields=['coleta_mobile'])
        dados=self.estado();barreira=Barrier(2)
        clientes=[Client(),Client()]
        for c in clientes:c.force_login(self.usuario)
        def enviar(par):
            c,acao=par;close_old_connections()
            try:
                barreira.wait(timeout=15)
                return c.post(self.url,{'acao_coleta_mobile':json.dumps(dict(dados,acao=acao))}).status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados=list(pool.map(enviar,zip(clientes,['iniciar_pego','iniciar_pendente'])))
        self.assertEqual(sorted(resultados),[200,409])
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.coleta_mobile['versao'],1)
        self.assertEqual(len(set(self.lista.coleta_mobile['itens'].values())),1)
        self.sem_operacoes()
