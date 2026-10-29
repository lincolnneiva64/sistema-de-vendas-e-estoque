"""Synthetic purchase decisions: never connect an operational database."""
import json
from decimal import Decimal
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, Client
from django.db import connection, close_old_connections
from django.urls import reverse

from .models import (Compra, ItemCompra, Produto, Fornecedor, ContaFinanceira,
                     MovimentoFinanceiro, GrupoProdutoVinculado, MembroGrupoProduto)
from .services import decisoes_precos_compra as decisoes


class DecisoesFixture:
    def setUp(self):
        super().setUp()
        self.usuario = get_user_model().objects.create_superuser('decisoes-ficticias', '', 'teste')
        self.client.force_login(self.usuario)
        self.fornecedor = Fornecedor.objects.create(nome='Fornecedor fictício')
        ContaFinanceira.objects.create(nome='Banco/Pix', tipo='banco')
        self.produto = Produto.objects.create(nome='Skol fictícia', quantidade=10, preco_compra=36,
            preco_vista=45, preco_prazo=46, unidade_compra='CX', unidade_venda_1='CX')
        self.compra = Compra.objects.create(fornecedor=self.fornecedor, data_compra='2026-10-29',
            tipo_pagamento='avista', status='rascunho', total=555)
        ItemCompra.objects.create(compra=self.compra, produto=self.produto, quantidade=15,
            unidade='CX', preco_unitario=37, valor_total=555)
        self.finalizar_url = reverse('estoque:compra_finalizar', args=[self.compra.pk])
        self.preparar_url = reverse('estoque:compra_salvar_pre_revisao', args=[self.compra.pk])

    def payload(self, **extra):
        dados = dict(fornecedor_id=str(self.fornecedor.pk), data_compra='2026-10-29',
            tipo_pagamento='avista', origem_banco='555', fluxo_mobile_compra='1',
            acao_compra='confirmar_financeiro', observacao='Preservar observação fictícia')
        dados.update({'produto_id[]':[str(self.produto.pk)], 'quantidade[]':['15'],
            'unidade[]':['CX'], 'preco_unitario[]':['37'],
            'atualizar_custo_produto_ids[]':[str(self.produto.pk)]})
        dados.update(extra)
        return dados

    def previa(self, payload=None):
        resposta = self.client.post(self.preparar_url, payload or self.payload())
        self.assertEqual(resposta.status_code, 200, resposta.content)
        return resposta.json()['revisao_precos']['previa']

    def decisao(self, escolha='manter', payload=None):
        dados = payload or self.payload()
        dados.update(previa_decisao_precos=self.previa(dados), decisao_precos_compra='revisado',
            decisoes_precos_itens=json.dumps({f'p:{self.produto.pk}':escolha}))
        return dados

    def sem_efeitos(self):
        self.compra.refresh_from_db(); self.produto.refresh_from_db()
        self.assertEqual(self.compra.status, 'rascunho')
        self.assertFalse(self.compra.estoque_entrada_realizada)
        self.assertEqual(self.produto.quantidade, Decimal('10'))
        self.assertEqual(self.produto.preco_compra, Decimal('36'))
        self.assertEqual(self.produto.preco_vista, Decimal('45'))
        self.assertEqual(MovimentoFinanceiro.objects.count(), getattr(self, 'movimentos_iniciais', 0))


class DecisoesCompraTests(DecisoesFixture, TestCase):
    def test_post_direto_sem_decisao_preserva_formulario_sem_efeitos(self):
        for nome in ('compra_editar', 'compra_finalizar'):
            for acao in ('finalizar', 'confirmar_financeiro'):
                # finalizar/avista only saves a draft; all real writes occur at confirmar.
                r = self.client.post(reverse('estoque:'+nome, args=[self.compra.pk]),
                                     self.payload(acao_compra=acao))
                self.assertEqual(r.status_code, (200 if nome == 'compra_finalizar' else 302) if acao == 'finalizar' else 409)
                self.sem_efeitos()
                if acao != 'finalizar':
                    self.assertContains(r, 'Preservar observação fictícia', status_code=409)
                    self.assertContains(r, '37,00', status_code=409)

    def test_compra_nova_sem_decisao_nao_produz_efeitos(self):
        antes = Compra.objects.count()
        r = self.client.post(reverse('estoque:compras_nova'), self.payload())
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(Compra.objects.count(), antes)
        self.sem_efeitos()

    def test_marcador_legado_nao_substitui_decisao(self):
        r = self.client.post(self.finalizar_url, self.payload(revisao_precos_pendente='1'))
        self.assertEqual(r.status_code, 409)
        self.sem_efeitos()
        r=self.client.post(self.finalizar_url,self.payload(**{'atualizar_custo_produto_ids[]':[]}))
        self.assertEqual(r.status_code,409)
        self.sem_efeitos()

    def test_sem_diferenca_dispensa_previa(self):
        dados = self.payload(**{'preco_unitario[]':['36'], 'origem_banco':'540'})
        r = self.client.post(self.finalizar_url, dados)
        self.assertEqual(r.status_code, 302, r.content)
        self.compra.refresh_from_db()
        self.assertEqual(self.compra.status, 'finalizada')

    def test_manter_finaliza_sem_alterar_venda_e_reenvio_nao_duplica(self):
        dados = self.decisao()
        for _ in range(2):
            self.assertEqual(self.client.post(self.finalizar_url, dados).status_code, 302)
        self.produto.refresh_from_db();self.compra.refresh_from_db()
        self.assertEqual(self.produto.preco_compra, Decimal('37'))
        self.assertEqual(self.produto.preco_vista, Decimal('45'))
        self.assertEqual(self.produto.quantidade, Decimal('25'))
        self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(), 1)

    def test_alterar_exige_precos_efetivos_e_preserva_selecao(self):
        dados = self.decisao('alterar')
        self.assertEqual(self.client.post(self.finalizar_url, dados).status_code, 409)
        self.sem_efeitos()
        dados.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)],
            'atualizar_preco_venda_nomes[]':['preco_vista'], 'atualizar_preco_venda_valores[]':['47']})
        self.assertEqual(self.client.post(self.finalizar_url, dados).status_code, 302)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_vista, Decimal('47'))
        self.assertEqual(self.produto.preco_prazo, Decimal('46'))

    def test_adiar_deriva_pendencia_da_decisao_nao_do_booleano(self):
        dados = self.payload(previa_decisao_precos=self.previa(), decisao_precos_compra='adiar',
                             revisao_precos_pendente='0')
        self.assertEqual(self.client.post(self.finalizar_url, dados).status_code, 302)
        self.compra.refresh_from_db();self.produto.refresh_from_db()
        item = self.compra.itens.get()
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertEqual(item.preco_compra_anterior, Decimal('36'))
        self.assertEqual(self.produto.preco_vista, Decimal('45'))

    def test_previa_adulterada_expirada_outro_usuario_itens_e_preco_desatualizados(self):
        original = self.decisao()
        for extra in ({'previa_decisao_precos':'invalida'}, {'quantidade[]':['16'], 'origem_banco':'592'},
                      {'decisoes_precos_itens':'{}'}, {'decisoes_precos_itens':'[]'}):
            self.assertEqual(self.client.post(self.finalizar_url, {**original, **extra}).status_code, 409)
            self.sem_efeitos()
        import time
        passado=time.time()-1300
        with patch('django.core.signing.time.time', return_value=passado):
            expirada=decisoes.preparar(self.compra,self.usuario)['previa']
        self.assertEqual(self.client.post(self.finalizar_url, {**original,'previa_decisao_precos':expirada}).status_code,409)
        outro = get_user_model().objects.create_superuser('outro-ficticio', '', 'teste')
        self.client.force_login(outro)
        self.assertEqual(self.client.post(self.finalizar_url, original).status_code, 409)
        self.client.force_login(self.usuario)
        self.produto.preco_prazo=Decimal('49');self.produto.save(update_fields=['preco_prazo'])
        r = self.client.post(self.finalizar_url, original, HTTP_X_COMPRA_DECISAO='1')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()['revisar_precos'])
        self.sem_efeitos()

    def test_custo_desatualizado_nao_mexe_em_estoque_ou_financeiro(self):
        dados = self.decisao()
        self.produto.preco_compra=Decimal('38');self.produto.save(update_fields=['preco_compra'])
        self.assertEqual(self.client.post(self.finalizar_url, dados).status_code, 409)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_compra, Decimal('38'))
        self.assertEqual(self.produto.quantidade, Decimal('10'))
        self.assertFalse(MovimentoFinanceiro.objects.exists())

    def test_rascunho_antigo_com_snapshot_de_custo_ainda_exige_decisao(self):
        from .services.precos_compra import aplicar_precos_compra
        item=self.compra.itens.get()
        aplicar_precos_compra(item, {'preco_compra':Decimal('37')}, operador=self.usuario)
        self.assertEqual(self.client.post(self.finalizar_url,self.payload()).status_code,409)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade,Decimal('10'))
        self.assertFalse(MovimentoFinanceiro.objects.exists())
        dados=self.decisao()
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        self.assertEqual(self.compra.itens.get().alteracoes_precos['preco_compra']['anterior'],'36.00')

    def test_comando_sem_permissao_nao_altera_nem_finaliza(self):
        dados=self.decisao('alterar')
        dados.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)],
            'atualizar_preco_venda_nomes[]':['preco_vista'],'atualizar_preco_venda_valores[]':['47']})
        sem_permissao=get_user_model().objects.create_user('sem-permissao-ficticio')
        self.client.force_login(sem_permissao)
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,403)
        self.sem_efeitos()

    def test_adiar_rascunho_antigo_preserva_custo_original_na_pendencia(self):
        from .services.precos_compra import aplicar_precos_compra
        aplicar_precos_compra(self.compra.itens.get(), {'preco_compra':Decimal('37')}, operador=self.usuario)
        dados=self.payload(previa_decisao_precos=self.previa(),decisao_precos_compra='adiar')
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        item=self.compra.itens.get();self.compra.refresh_from_db()
        self.assertEqual(item.preco_compra_anterior,Decimal('36'))
        self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertEqual(len(self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()['itens']),1)

    def test_grupo_exige_uma_decisao_e_preserva_custos_individuais(self):
        grupo = GrupoProdutoVinculado.objects.create(nome='Grupo fictício')
        outro = Produto.objects.create(nome='Outro fictício', quantidade=10, preco_compra=20,
            preco_vista=45, preco_prazo=46, unidade_compra='CX', unidade_venda_1='CX')
        for p in (self.produto, outro):MembroGrupoProduto.objects.create(grupo=grupo,produto=p)
        dados = self.payload(**{'produto_id[]':[str(self.produto.pk),str(outro.pk)],
            'quantidade[]':['15','1'],'preco_unitario[]':['37','21'],'unidade[]':['CX','CX'],
            'atualizar_custo_produto_ids[]':[str(self.produto.pk),str(outro.pk)],'origem_banco':'576'})
        dados.update(previa_decisao_precos=self.previa(dados), decisao_precos_compra='revisado',
                     decisoes_precos_itens=json.dumps({f'g:{grupo.pk}':'manter'}))
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        outro.refresh_from_db();self.produto.refresh_from_db()
        self.assertEqual((self.produto.preco_compra,outro.preco_compra),(Decimal('37'),Decimal('21')))
        self.assertEqual((self.produto.preco_vista,outro.preco_vista),(Decimal('45'),Decimal('45')))

    def test_pendencia_antiga_exige_decisao_e_manter_nao_altera_precos(self):
        # Legacy records need no backfill; their current GET issues the fresh preview.
        self.compra.status='finalizada';self.compra.revisao_precos_pendente=True
        self.compra.estoque_entrada_realizada=True;self.compra.save()
        item=self.compra.itens.get();item.preco_compra_anterior=36;item.save()
        consulta=reverse('estoque:revisao_precos_posterior_pendentes')
        salvar=reverse('estoque:revisao_precos_posterior_salvar')
        dados=dict(compra_id=self.compra.pk,item_id=item.pk,produto_id=self.produto.pk)
        self.assertEqual(self.client.post(salvar,dados).status_code,400)
        dados['previa_decisao_precos']=self.client.get(consulta).json()['itens'][0]['previa_decisao_precos']
        self.assertEqual(self.client.post(salvar,dados).status_code,400)
        dados['decisao_precos']='alterar'
        self.assertEqual(self.client.post(salvar,dados).status_code,400)
        dados['decisao_precos']='manter'
        self.assertEqual(self.client.post(salvar,dados).status_code,200)
        self.assertEqual(self.client.post(salvar,dados).status_code,409)
        self.produto.refresh_from_db();item.refresh_from_db()
        self.assertEqual(self.produto.preco_vista,Decimal('45'))
        self.assertTrue(item.revisao_preco_concluida)

    def test_grupo_alterar_reutiliza_confirmacao_seletiva_e_auditoria(self):
        from .services.precos_vinculados import diagnosticar_grupo
        grupo=GrupoProdutoVinculado.objects.create(nome='Grupo fictício')
        outro=Produto.objects.create(nome='Outro do grupo',quantidade=10,preco_compra=20,
            preco_vista=45,preco_prazo=46,unidade_compra='CX',unidade_venda_1='CX')
        for p in (self.produto,outro):MembroGrupoProduto.objects.create(grupo=grupo,produto=p)
        dados=self.payload(**{'produto_id[]':[str(self.produto.pk),str(outro.pk)],
            'quantidade[]':['15','1'],'preco_unitario[]':['37','21'],'unidade[]':['CX','CX'],
            'atualizar_custo_produto_ids[]':[str(self.produto.pk),str(outro.pk)],'origem_banco':'576'})
        dados.update(previa_decisao_precos=self.previa(dados),decisao_precos_compra='revisado',
            decisoes_precos_itens=json.dumps({f'g:{grupo.pk}':'alterar'}))
        pid=str(self.produto.pk)
        dados.update({'atualizar_preco_venda_produto_ids[]':[pid],
            'atualizar_preco_venda_nomes[]':['preco_vista'],'atualizar_preco_venda_valores[]':['47'],
            'versao_grupo_produto_'+pid:'0','confirmar_precos_produto_'+pid:'1',
            'assinatura_precos_produto_'+pid:diagnosticar_grupo(list(grupo.produtos.all()),validar_precos=False)['assinatura_precos'],
            'destinatarios_precos_produto_'+pid:[pid,str(outro.pk)]})
        for invalido in ({'confirmar_precos_produto_'+pid:''}, {'assinatura_precos_produto_'+pid:'invalida'}):
            with patch('estoque.views._atualizar_custos_produtos_compra') as custos:
                self.assertEqual(self.client.post(self.finalizar_url,{**dados,**invalido}).status_code,409)
                custos.assert_not_called()
            self.sem_efeitos()
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        self.produto.refresh_from_db();outro.refresh_from_db()
        self.assertEqual((self.produto.preco_vista,outro.preco_vista),(Decimal('47'),Decimal('47')))
        self.assertEqual((self.produto.preco_compra,outro.preco_compra),(Decimal('37'),Decimal('21')))
        self.compra.refresh_from_db()
        self.assertEqual(len(self.compra.alteracoes_precos_vinculados),1)

    def test_previa_carrega_produtos_em_lote(self):
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as pequeno:
            decisoes.preparar(self.compra,self.usuario)
        for n in range(20):
            p=Produto.objects.create(nome=f'Fictício em lote {n}',preco_compra=1,preco_vista=3,preco_prazo=3)
            ItemCompra.objects.create(compra=self.compra,produto=p,quantidade=1,preco_unitario=2,valor_total=2)
        with CaptureQueriesContext(connection) as maior:
            previa=decisoes.preparar(self.compra,self.usuario)
        self.assertEqual(len(previa['itens']),21)
        self.assertLessEqual(len(maior),len(pequeno))

    def test_posterior_desatualizada_e_alterar_com_mesmos_precos_nao_concluem(self):
        dados=self.payload(previa_decisao_precos=self.previa(),decisao_precos_compra='adiar')
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        item=self.compra.itens.get()
        consulta=reverse('estoque:revisao_precos_posterior_pendentes')
        salvar=reverse('estoque:revisao_precos_posterior_salvar')
        dados=dict(compra_id=self.compra.pk,item_id=item.pk,produto_id=self.produto.pk,
            decisao_precos='alterar',previa_decisao_precos=self.client.get(consulta).json()['itens'][0]['previa_decisao_precos'])
        dados.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)],
            'atualizar_preco_venda_nomes[]':['preco_vista'],'atualizar_preco_venda_valores[]':['45']})
        self.assertEqual(self.client.post(salvar,dados).status_code,400)
        self.produto.refresh_from_db();self.produto.preco_prazo=49;self.produto.save(update_fields=['preco_prazo'])
        dados={k:v for k,v in dados.items() if not k.startswith('atualizar_preco_venda_')}
        dados['decisao_precos']='manter'
        self.assertEqual(self.client.post(salvar,dados).status_code,400)
        item.refresh_from_db();self.assertFalse(item.revisao_preco_concluida)
        dados['previa_decisao_precos']=self.client.get(consulta).json()['itens'][0]['previa_decisao_precos']
        self.assertEqual(self.client.post(salvar,dados).status_code,200)
        self.produto.refresh_from_db();self.assertEqual(self.produto.preco_prazo,49)


@skipUnless(connection.vendor == 'postgresql', 'Exige bloqueios PostgreSQL no banco descartável')
class DecisoesConcorrenciaTests(DecisoesFixture, TransactionTestCase):
    def enviar_duas(self, url, dados):
        clientes=[Client(),Client()]
        for c in clientes:c.force_login(self.usuario)
        barreira=Barrier(2)
        def enviar(c):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                return c.post(url,dados).status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(enviar,clientes))

    def test_duas_finalizacoes_uma_entrada_e_um_financeiro(self):
        self.assertEqual(self.enviar_duas(self.finalizar_url,self.decisao()),[302,302])
        self.produto.refresh_from_db();self.compra.refresh_from_db()
        self.assertEqual(self.produto.quantidade,Decimal('25'))
        self.assertEqual(self.compra.status,'finalizada')
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)

    def test_duas_revisoes_uma_aplicacao(self):
        dados=self.payload(previa_decisao_precos=self.previa(),decisao_precos_compra='adiar')
        self.assertEqual(self.client.post(self.finalizar_url,dados).status_code,302)
        self.compra.refresh_from_db()
        item=self.compra.itens.get()
        token=self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()['itens'][0]['previa_decisao_precos']
        dados=dict(compra_id=self.compra.pk,item_id=item.pk,produto_id=self.produto.pk,
                   decisao_precos='alterar',previa_decisao_precos=token)
        dados.update({'atualizar_preco_venda_produto_ids[]':[str(self.produto.pk)],
                      'atualizar_preco_venda_nomes[]':['preco_vista'],
                      'atualizar_preco_venda_valores[]':['47']})
        self.assertEqual(sorted(self.enviar_duas(reverse('estoque:revisao_precos_posterior_salvar'),dados)),[200,409])
        item.refresh_from_db();self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_vista,Decimal('47'))
        self.assertEqual(item.alteracoes_precos['preco_vista']['anterior'],'45.00')
        self.assertTrue(item.revisao_preco_concluida)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=self.compra).count(),1)
