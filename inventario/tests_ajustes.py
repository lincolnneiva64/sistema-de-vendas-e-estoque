"""Fictitious stock only; real PostgreSQL transactions and browser checks."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import PermissionDenied
from django.db import connection, close_old_connections, transaction, DatabaseError
from django.test import TestCase, TransactionTestCase, override_settings, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Produto, Funcionario, MovimentacaoEstoqueManual
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from estoque.views import _baixar_estoque_produto
from offline.browser_support import Chrome
from . import ajustes, services
from .models import (Inventario, RodadaContagem, PropostaAjuste, AplicacaoAjuste,
                     EventoAjuste, DecisaoAjuste, MovimentoObservado, ConfirmacaoContagem)
from .tests import produto
from .tests_browser import CHROME


class AjustesFixture:
    def setUp(self):
        super().setUp()
        self.usuario = get_user_model().objects.create_superuser('gestor-ajustes', '', 'teste')
        # Reserve a synthetic protected #1; never load operational data.
        Inventario.objects.create(titulo='Reserva de referência fictícia', criterio='manual', criado_por=self.usuario)
        Inventario.objects.get_or_create(pk=1, defaults=dict(titulo='Referência protegida fictícia',
            criterio='manual', criado_por=self.usuario))
        self.produto = produto(nome='Condicionador fictício', quantidade=10)
        self.funcionario = Funcionario.objects.create(nome='Contador fictício')
        self.inv = services.criar_inventario(self.usuario, 'Conferência demonstrativa · dados fictícios', 'manual', [self.produto.pk])
        self.rodada = self.confirmar(self.inv, 8)
        self.url = reverse('inventario:ajustes', args=[self.inv.pk])
        self.client.force_login(self.usuario)

    def confirmar(self, inventario, contado):
        rodada = inventario.itens.order_by('-pk').first().rodadas.get(encerrada_em__isnull=True)
        acesso, _ = services.gerar_acesso(self.usuario, inventario.pk, self.funcionario.pk, [rodada.pk])
        services.salvar_contagem(acesso.pk, rodada.pk, 0, str(contado), '0', True)
        return self.fresca(rodada.pk)

    def fresca(self, pk=None):
        return RodadaContagem.objects.select_related('item__produto').get(pk=pk or self.rodada.pk)

    def avaliar(self, rodada=None):
        return ajustes.avaliar(self.fresca(rodada.pk if rodada else None))

    def aprovar(self, rodada=None):
        rodada = rodada or self.rodada
        a = self.avaliar(rodada)
        return ajustes.decidir(self.usuario, self.inv.pk, rodada.pk, a['token'],
                              'Verificação física e lançamentos comprovados no teste.', confirmacao=True)

    def segunda(self):
        from .conversao import snapshot
        from .models import ItemInventario
        p = produto(nome='Estabilizante fictício', quantidade=20)
        p.refresh_from_db()
        item = ItemInventario.objects.create(inventario=self.inv, produto=p, cadastro=snapshot(p))
        r = RodadaContagem.objects.create(item=item, numero=1)
        self.confirmar(self.inv, 18)
        return self.fresca(r.pk)


class PreparacaoTests(AjustesFixture, TestCase):
    def test_preparar_somente_select_e_diferenca_nao_substitui_estoque(self):
        with CaptureQueriesContext(connection) as queries:
            r = self.client.get(self.url, {'preparar': 1})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertContains(r, 'Aplicação real desabilitada no servidor')
        self.assertEqual(self.avaliar()['resultante'], 8)
        self.assertFalse(PropostaAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_aprovacao_separada_idempotente_com_evidencia_congelada(self):
        p = self.aprovar()
        self.assertEqual(self.aprovar().pk, p.pk)
        self.assertEqual(PropostaAjuste.objects.count(), 1)
        self.assertEqual(EventoAjuste.objects.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(DecisaoAjuste.objects.exists())
        self.assertEqual(p.confirmacao_id, self.rodada.confirmacoes.get().pk)

    def test_rejeitar_aprovada_auditado_e_nao_reaprova_mesma_evidencia(self):
        p = self.aprovar()
        a = self.avaliar()
        ajustes.decidir(self.usuario, self.inv.pk, self.rodada.pk, a['token'], 'Dúvida física', False, True)
        p.refresh_from_db(); self.assertEqual(p.estado, 'rejeitado')
        self.assertEqual(EventoAjuste.objects.count(), 2)
        with self.assertRaises(ValueError):
            self.aprovar()

    def test_revisao_expressa_justificativa_e_token_exigidos(self):
        a = self.avaliar()
        for kwargs in (dict(confirmacao=False), dict(confirmacao=True, justificativa=''),
                       dict(confirmacao=True, previa=a['token']+'x')):
            args = dict(previa=a['token'], justificativa='Evidência', confirmacao=True)
            args.update(kwargs)
            with self.assertRaises(ValueError):
                ajustes.decidir(self.usuario, self.inv.pk, self.rodada.pk, **args)
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp()+1300):
            with self.assertRaises(ValueError):
                ajustes.ler_token(a['token'])

    def test_negar_outro_inventario_rodada_e_permissao(self):
        a = self.avaliar()
        contador = get_user_model().objects.create_user('sem-permissao')
        with self.assertRaises(PermissionDenied):
            ajustes.decidir(contador, self.inv.pk, self.rodada.pk, a['token'], 'Teste', True, True)
        self.client.force_login(contador)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'acao': 'aprovar'}).status_code, 403)
        self.client.logout(); self.assertEqual(self.client.get(self.url).status_code, 403)
        with self.assertRaises(ValueError):
            ajustes.decidir(self.usuario, 1, self.rodada.pk, a['token'], 'Teste', True, True)

    def test_flag_off_bloqueia_servidor_inclusive_requisicao_forjada_e_legado(self):
        p = self.aprovar(); previa = ajustes.previa_aplicacao(self.inv.pk, [p])
        with self.assertRaisesMessage(ValueError, 'desabilitada'):
            ajustes.aplicar(self.usuario, self.inv.pk, previa, True)
        r = self.client.post(self.url, {'acao': 'aplicar', 'previa': previa, 'confirmacao': 'sim'})
        self.assertContains(r, 'desabilitada no servidor')
        with self.assertRaises(ValueError):
            services.aprovar(self.usuario, self.rodada.pk, 'Legado')
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            with self.assertRaisesMessage(ValueError, 'Fluxo antigo desativado'):
                services.aprovar(self.usuario, self.rodada.pk, 'Legado')
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_ativar_nao_executa_automaticamente(self):
        self.aprovar()
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(DecisaoAjuste.objects.exists())

    def test_csrf_post_e_metodos(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.usuario)
        self.assertEqual(client.post(self.url, {'acao':'aplicar'}).status_code, 403)
        self.assertEqual(self.client.put(self.url).status_code, 405)

    def test_revisor_pode_consultar_mas_nao_aprovar_rejeitar_ou_aplicar(self):
        p=self.aprovar(); a=self.avaliar()
        usuario=get_user_model().objects.create_user('revisor-sem-aprovacao')
        usuario.user_permissions.add(Permission.objects.get(codename='revisar_inventario'))
        self.client.force_login(usuario)
        pagina=self.client.get(self.url)
        self.assertEqual(pagina.status_code,200)
        self.assertNotContains(pagina,'Aprovar sem alterar estoque')
        for acao in ('aprovar','rejeitar','aplicar'):
            with self.subTest(acao=acao):
                r=self.client.post(self.url,dict(acao=acao,rodada=self.rodada.pk,
                    previa=a['token'] if acao!='aplicar' else ajustes.previa_aplicacao(self.inv.pk,[p]),
                    justificativa='Tentativa sem permissão',revisao_fisica='sim',confirmacao='sim'))
                self.assertEqual(r.status_code,403)
        p.refresh_from_db();self.assertEqual(p.estado,'aprovado')
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())

    def test_conferencia_um_prepara_aprova_sem_alterar_ok_ou_estoque(self):
        self.rodada.item.inventario_id = 1; self.rodada.item.save(update_fields=['inventario'])
        antes = list(ConfirmacaoContagem.objects.values())
        a = self.avaliar()
        self.assertFalse(a['bloqueio'])
        self.assertEqual(a['resultante'], 8)
        p = ajustes.decidir(self.usuario, 1, self.rodada.pk, a['token'],
                            'Revisão gerencial fictícia', confirmacao=True)
        self.assertEqual(p.estado, 'aprovado')
        self.assertEqual(antes, list(ConfirmacaoContagem.objects.values()))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        with self.assertRaisesMessage(ValueError, 'desabilitada'):
            ajustes.aplicar(self.usuario, 1, ajustes.previa_aplicacao(1, [p]), True)

    def test_conferencia_um_movimento_posterior_nao_e_liberado_pelo_numero(self):
        self.rodada.item.inventario_id = 1; self.rodada.item.save(update_fields=['inventario'])
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=9)
        self.assertTrue(self.avaliar()['bloqueio'])
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_conferencia_um_previa_antiga_recusada_apos_movimento(self):
        self.rodada.item.inventario_id = 1; self.rodada.item.save(update_fields=['inventario'])
        a = self.avaliar()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=9)
        with self.assertRaises(ValueError):
            ajustes.decidir(self.usuario, 1, self.rodada.pk, a['token'],
                            'Prévia desatualizada', confirmacao=True)
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_cadastro_modificado_bloqueia_sem_converter(self):
        for campo, valor in (('fator_conversao', 24), ('unidade_venda_1', 'KG'), ('nome', 'Alterado')):
            with transaction.atomic():
                Produto.objects.filter(pk=self.produto.pk).update(**{campo: valor})
                self.assertTrue(self.avaliar()['bloqueio'])
                transaction.set_rollback(True)

    def test_recontagem_ou_confirmacao_alterada_invalida_aprovacao(self):
        a = self.avaliar()
        services.recontar(self.usuario, self.rodada.pk, 'Nova verificação')
        self.assertTrue(self.avaliar()['bloqueio'])
        with self.assertRaises(ValueError):
            ajustes.decidir(self.usuario, self.inv.pk, self.rodada.pk, a['token'], 'Teste', True, True)

    def test_snapshot_ok_incompleto_nao_aprova(self):
        ConfirmacaoContagem.objects.filter(rodada=self.rodada).update(esperado=11)
        self.assertIn('confirmação original', self.avaliar()['bloqueio'])

    def test_venda_compra_devolucao_manual_pos_ok_nunca_inferidos(self):
        for origem in ('venda', 'compra', 'devolucao_venda', 'ajuste_manual', 'nao_identificada'):
            with transaction.atomic():
                conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', 9, 'Movimento fictício', usuario=self.usuario)
                movimento = MovimentoObservado.objects.get(produto=self.produto)
                MovimentoObservado.objects.filter(pk=movimento.pk).update(origem=origem)
                self.assertTrue(self.avaliar()['bloqueio'])
                if origem != 'nao_identificada':
                    services.conciliar(self.usuario, self.rodada.pk, movimento.pk, 'Conciliação textual fictícia')
                    self.assertIn('unidade histórica', self.avaliar()['bloqueio'])
                transaction.set_rollback(True)

    def test_coincidencia_numerica_nao_esconde_movimentos(self):
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', 9, 'Saída teste')
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', 10, 'Entrada teste')
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_resumo_lote_inclui_pendentes_aptos_sem_aprovacao_separada(self):
        p = self.aprovar(); r2 = self.segunda()
        r = self.client.get(self.url, {'confirmar':'todos'})
        self.assertEqual(r.context['resumo']['aptos'], 2)
        self.assertEqual(r.context['resumo']['pendentes'], 1)
        self.assertEqual(len(r.context['confirmacoes']), 2)
        self.assertContains(r, 'por inteiro ou nenhuma alteração')


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL descartável obrigatório')
@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class AplicacaoTests(AjustesFixture, TestCase):
    def aplicar(self, propostas=None):
        propostas = propostas or [self.aprovar()]
        return ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, propostas), True)

    def test_individual_auditoria_idempotencia_e_observador_unico(self):
        p = self.aprovar()
        primeira = self.aplicar([p])[0]; segunda = self.aplicar([p])[0]
        self.assertEqual(primeira.pk, segunda.pk)
        self.assertEqual((primeira.antes, primeira.diferenca, primeira.depois), (10, -2, 8))
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 1)
        m = MovimentoObservado.objects.get(produto=self.produto)
        self.assertEqual(m.origem, 'ajuste_manual')
        self.assertEqual(m.referencia, f'Movimentação manual #{primeira.decisao.movimentacao_id}')
        self.assertEqual(self.fresca().esperado, 10)
        self.assertEqual(self.fresca().contado_base, 8)

    def test_lote_duas_propostas_e_repeticao(self):
        p1 = self.aprovar(); p2 = self.aprovar(self.segunda())
        a = self.aplicar([p1,p2]); b = self.aplicar([p1,p2])
        self.assertEqual({r.pk for r in a}, {r.pk for r in b})
        self.assertEqual(len({r.lote for r in a}), 1)
        self.assertEqual(DecisaoAjuste.objects.count(), 2)

    def test_falha_no_segundo_desfaz_todo_lote_inclusive_observador(self):
        p1 = self.aprovar(); p2 = self.aprovar(self.segunda())
        original = ajustes.conferir_ou_ajustar_estoque
        chamadas=[]
        def falhar(*args, **kwargs):
            chamadas.append(1)
            if len(chamadas)==2: raise ValueError('Falha simulada')
            return original(*args, **kwargs)
        with patch.object(ajustes, 'conferir_ou_ajustar_estoque', side_effect=falhar):
            with self.assertRaisesMessage(ValueError, 'Falha simulada'):
                self.aplicar([p1,p2])
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(DecisaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        self.assertEqual(self.fresca().item.produto.quantidade, 10)
        self.assertEqual(PropostaAjuste.objects.filter(estado='aprovado').count(), 2)

    def test_alteracao_entre_preparar_aprovar_e_aplicar_bloqueia(self):
        p = self.aprovar()
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', 9, 'Venda fictícia')
        with self.assertRaises(ValueError): self.aplicar([p])
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_venda_entre_preparacao_e_aprovacao_invalida_previa(self):
        a=self.avaliar()
        with transaction.atomic(): _baixar_estoque_produto(self.produto.pk,1,unidade='CX')
        with self.assertRaises(ValueError):
            ajustes.decidir(self.usuario,self.inv.pk,self.rodada.pk,a['token'],'Físico',True,True)
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_compra_entre_preparacao_e_confirmacao_bloqueia_lote_inteiro(self):
        from estoque.models import Compra, ItemCompra
        from .rastreabilidade import identificar
        p1=self.aprovar(); r2=self.segunda(); p2=self.aprovar(r2)
        c=Compra.objects.create(data_compra=timezone.localdate(),estoque_entrada_realizada=True,
            estoque_entrada_realizada_em=timezone.now())
        ItemCompra.objects.create(compra=c,produto=self.produto,quantidade=1,unidade='CX',preco_unitario=0,valor_total=0)
        with transaction.atomic():
            produto=Produto.objects.select_for_update().get(pk=self.produto.pk)
            Produto.objects.filter(pk=produto.pk).update(quantidade=11)
            identificar(produto.pk,produto.quantidade,11,'compra',f'Compra #{c.pk}')
        with self.assertRaises(ValueError): self.aplicar([p1,p2])
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertEqual(self.fresca(r2.pk).item.produto.quantidade,20)

    def test_falha_na_auditoria_e_atribuicao_observador_reverte_escrita(self):
        p=self.aprovar()
        with patch('inventario.ajustes.AplicacaoAjuste.objects.create',side_effect=RuntimeError('Falha auditoria')):
            with self.assertRaises(RuntimeError): self.aplicar([p])
        self.assertEqual(self.fresca().item.produto.quantidade,10)
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
        self.assertFalse(MovimentoObservado.objects.exists())
        with patch('inventario.rastreabilidade.identificar'):
            with self.assertRaisesMessage(ValueError,'observador não confirmado'): self.aplicar([p])
        self.assertEqual(self.fresca().item.produto.quantidade,10)

    def test_protecao_auditoria_ausente_impede_aplicacao(self):
        p=self.aprovar()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE inventario_aplicacaoajuste DISABLE TRIGGER inventario_aplicacao_imutavel')
            with self.assertRaisesMessage(ValueError,'Proteções da auditoria'): self.aplicar([p])
            transaction.set_rollback(True)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_novo_ok_e_recontagem_invalida_token_aprovado(self):
        p = self.aprovar()
        services.recontar(self.usuario, self.rodada.pk, 'Contagem posterior')
        with self.assertRaises(ValueError): self.aplicar([p])

    def test_confirmacao_expressa_e_permissao_exigidas(self):
        p = self.aprovar(); t=ajustes.previa_aplicacao(self.inv.pk, [p])
        with self.assertRaises(ValueError): ajustes.aplicar(self.usuario,self.inv.pk,t,False)
        u=get_user_model().objects.create_user('invasor')
        with self.assertRaises(PermissionDenied): ajustes.aplicar(u,self.inv.pk,t,True)
        with self.assertRaises(ValueError): ajustes.aplicar(self.usuario,1,t,True)

    def test_auditoria_protegida_inclusive_update_delete_em_lote(self):
        p = self.aprovar(); a = self.aplicar([p])[0]
        with self.assertRaises(ValueError): a.delete()
        with self.assertRaises(ValueError): a.save()
        for model in (AplicacaoAjuste, EventoAjuste, PropostaAjuste):
            with self.assertRaises(DatabaseError), transaction.atomic():
                model.objects.all().delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            PropostaAjuste.objects.filter(pk=p.pk).update(justificativa='Tentativa de adulteração')
        with self.assertRaises(DatabaseError), transaction.atomic():
            AplicacaoAjuste.objects.filter(pk=a.pk).update(depois=999)

    def test_lote_com_rejeitado_nao_aplica_ninguem(self):
        p1=self.aprovar(); r2=self.segunda(); a=self.avaliar(r2)
        p2=ajustes.decidir(self.usuario,self.inv.pk,r2.pk,a['token'],'Rejeição fictícia',False,True)
        with self.assertRaises(ValueError): self.aplicar([p1,p2])
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_previa_lote_parcialmente_aplicada_exige_nova_confirmacao(self):
        p1=self.aprovar();p2=self.aprovar(self.segunda())
        previa=ajustes.previa_aplicacao(self.inv.pk,[p1,p2])
        self.aplicar([p1])
        with self.assertRaisesMessage(ValueError,'parcialmente executado'):
            ajustes.aplicar(self.usuario,self.inv.pk,previa,True)
        self.assertEqual(AplicacaoAjuste.objects.count(),1)
        self.aplicar([p2])
        self.assertEqual(AplicacaoAjuste.objects.count(),2)

    def test_limite_de_500_nao_pode_ser_contornado_por_post_ou_token_anterior(self):
        from .models import ItemInventario
        p=self.aprovar(); preparacao=self.avaliar()['token']
        previa=ajustes.previa_aplicacao(self.inv.pk,[p])
        produtos=Produto.objects.bulk_create([Produto(nome=f'Fictício limite {i}',quantidade=10,
            preco_compra=1,preco_vista=2,preco_prazo=3,unidade_compra='UN') for i in range(500)])
        ItemInventario.objects.bulk_create([ItemInventario(inventario=self.inv,produto=produto,
            cadastro=self.rodada.item.cadastro) for produto in produtos])
        with self.assertRaisesMessage(ValueError,'500 produtos'):
            ajustes.decidir(self.usuario,self.inv.pk,self.rodada.pk,preparacao,'Revisão fictícia',True,True)
        with self.assertRaisesMessage(ValueError,'500 produtos'):
            ajustes.aplicar(self.usuario,self.inv.pk,previa,True)
        r=self.client.post(self.url,{'acao':'aplicar','previa':previa,'confirmacao':'sim'})
        self.assertContains(r,'500 produtos')
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertFalse(MovimentacaoEstoqueManual.objects.exists())


@skipUnless(connection.vendor == 'postgresql', 'Bloqueios reais')
@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConcorrenciaAjustesTests(AjustesFixture, TransactionTestCase):
    def thread(self, func):
        close_old_connections()
        try: return func()
        finally: close_old_connections()

    def test_duas_pessoas_aplicam_uma_unica_vez(self):
        p = self.aprovar(); token = ajustes.previa_aplicacao(self.inv.pk,[p]); barreira=Barrier(2)
        outro=get_user_model().objects.create_superuser('outro-gestor','','teste')
        def executar(usuario):
            barreira.wait(10)
            return ajustes.aplicar(usuario,self.inv.pk,token,True)[0].pk
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.thread,lambda:executar(self.usuario))
            b=pool.submit(self.thread,lambda:executar(outro))
            self.assertEqual(a.result(20), b.result(20))
        self.assertEqual(AplicacaoAjuste.objects.count(),1)
        self.assertEqual(self.fresca().item.produto.quantidade,8)

    def test_venda_entre_leitura_e_update_aguarda_lock_sem_perda(self):
        p=self.aprovar(); token=ajustes.previa_aplicacao(self.inv.pk,[p])
        bloqueou, liberar, iniciou, terminou=Event(),Event(),Event(),Event()
        original=ajustes.conferir_ou_ajustar_estoque
        def segurar(*args,**kwargs):
            bloqueou.set()
            if not liberar.wait(10): raise AssertionError('Timeout')
            return original(*args,**kwargs)
        def venda():
            iniciou.set()
            with transaction.atomic(): _baixar_estoque_produto(self.produto.pk,1,unidade='CX')
            terminou.set()
        with patch.object(ajustes,'conferir_ou_ajustar_estoque',side_effect=segurar), ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.thread,lambda:ajustes.aplicar(self.usuario,self.inv.pk,token,True))
            self.assertTrue(bloqueou.wait(10))
            b=pool.submit(self.thread,venda)
            try:
                self.assertTrue(iniciou.wait(10)); self.assertFalse(terminou.wait(.3))
            finally: liberar.set()
            a.result(20); b.result(20)
        self.assertEqual(self.fresca().item.produto.quantidade,7)

    def test_compra_concorrente_antes_da_validacao_bloqueia_ajuste(self):
        from estoque.models import Compra, ItemCompra
        from .rastreabilidade import identificar
        p=self.aprovar();previa=ajustes.previa_aplicacao(self.inv.pk,[p])
        comprou,liberar,iniciou,terminou=Event(),Event(),Event(),Event()
        def compra():
            with transaction.atomic():
                produto=Produto.objects.select_for_update().get(pk=self.produto.pk)
                c=Compra.objects.create(data_compra=timezone.localdate(),estoque_entrada_realizada=True,
                    estoque_entrada_realizada_em=timezone.now())
                ItemCompra.objects.create(compra=c,produto=produto,quantidade=1,unidade='CX',preco_unitario=0,valor_total=0)
                Produto.objects.filter(pk=produto.pk).update(quantidade=11)
                identificar(produto.pk,produto.quantidade,11,'compra',f'Compra #{c.pk}')
                comprou.set()
                if not liberar.wait(10): raise AssertionError('Timeout compra')
        def aplicar():
            iniciou.set()
            try:
                ajustes.aplicar(self.usuario,self.inv.pk,previa,True)
            except ValueError:
                return 'bloqueado'
            finally: terminou.set()
            return 'aplicado'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.thread,compra)
            self.assertTrue(comprou.wait(10))
            b=pool.submit(self.thread,aplicar)
            try:
                self.assertTrue(iniciou.wait(10));self.assertFalse(terminou.wait(.3))
            finally: liberar.set()
            a.result(20);self.assertEqual(b.result(20),'bloqueado')
        self.assertEqual(self.fresca().item.produto.quantidade,11)
        self.assertFalse(AplicacaoAjuste.objects.exists())


@skipUnless(Path(CHROME).exists(), 'Chrome local')
class AjustesBrowserTests(AjustesFixture, StaticLiveServerTestCase):
    def test_previa_desktop_mobile_aprovacao_e_bloqueio(self):
        self.aprovar()
        r2=self.segunda()
        r2.item.produto.nome='Trigo fictício · revisão pendente';r2.item.produto.save(update_fields=['nome'])
        self.assertEqual(self.client.get(self.url).status_code,200)
        with TemporaryDirectory(prefix='ajustes-chrome-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                conferencia=reverse('inventario:detalhe',args=[self.inv.pk])
                tab.call('Page.navigate',{'url':self.live_server_url+conferencia})
                tab.wait("!!document.querySelector('a[href$=\"/ajustes/\"]')")
                tab.evaluate("document.querySelector('a[href$=\"/ajustes/\"]').click(); true")
                tab.wait("!!document.querySelector('.lista-ajustes')")
                self.assertTrue(tab.evaluate(f"!!document.querySelector('.navegacao-contextual a[href=\"{conferencia}\"]')"))
                destino=Path('artifacts/ajustes');destino.mkdir(parents=True,exist_ok=True)
                for largura,altura in ((1366,1000),(390,844),(320,800)):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':largura,'height':altura,'deviceScaleFactor':1,'mobile':largura<768})
                    tab.call('Page.navigate',{'url':self.live_server_url+self.url+'?confirmar=todos'})
                    tab.wait("!!document.querySelector('.confirmacao-aplicacao')")
                    self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    self.assertTrue(tab.evaluate("!!document.querySelector('#confirmar-aplicacao button:disabled')"))
                    self.assertTrue(tab.evaluate("document.querySelectorAll('.proposta-ajuste').length===2"))
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('details')].every(e=>!e.open)"))
                    (destino/f'ajustes-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']))
            finally: chrome.stop()

    def test_atualizar_individual_pelo_navegador_descartavel(self):
        with TemporaryDirectory(prefix='ajustes-fluxo-') as profile:
            chrome=Chrome(CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                tab.call('Page.navigate',{'url':self.live_server_url+self.url})
                tab.wait("!!document.querySelector('.proposta-ajuste .button')")
                self.assertFalse(MovimentacaoEstoqueManual.objects.exists())
                tab.evaluate("document.querySelector('.proposta-ajuste .button').click();true")
                tab.wait("!!document.querySelector('#confirmar-aplicacao button:disabled')")
                with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
                    tab.call('Page.navigate',{'url':self.live_server_url+self.url+'?confirmar='+str(self.rodada.pk)})
                    tab.wait("!!document.querySelector('#confirmar-aplicacao button:not(:disabled)')")
                    destino=Path('artifacts/ajustes');destino.mkdir(parents=True,exist_ok=True)
                    tab.evaluate("document.querySelector('#confirmar-aplicacao').scrollIntoView(); true")
                    (destino/'confirmacao-individual-mobile.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png'})['data']))
                    tab.evaluate("document.querySelector('#confirmar-aplicacao button').click();true")
                    tab.wait("!!document.querySelector('[data-estado=Aplicado]')")
                self.assertEqual(AplicacaoAjuste.objects.count(),1)
                self.assertEqual(self.fresca().item.produto.quantidade,8)
            finally: chrome.stop()


    def test_lote_desktop_duplo_clique_confirmacao_unica(self):
        self._confirmar_lote(1366, 1000)

    def test_lote_mobile_duplo_clique_confirmacao_unica(self):
        self._confirmar_lote(390, 844)

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def _confirmar_lote(self, largura, altura):
        self.segunda()
        with TemporaryDirectory(prefix='ajustes-lote-') as profile:
            chrome = Chrome(CHROME, profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name': 'sessionid',
                    'value': self.client.cookies['sessionid'].value, 'url': self.live_server_url})
                tab.call('Emulation.setDeviceMetricsOverride', dict(width=largura, height=altura,
                    deviceScaleFactor=1, mobile=largura < 768))
                tab.call('Page.navigate', {'url': self.live_server_url+self.url+'?confirmar=todos'})
                tab.wait("!!document.querySelector('#confirmar-aplicacao button:not(:disabled)')")
                self.assertEqual(tab.evaluate("document.querySelectorAll('#confirmar-aplicacao li').length"), 2)
                self.assertFalse(tab.evaluate("!!document.querySelector('input[type=checkbox]')"))
                destino = Path('artifacts/ajustes'); destino.mkdir(parents=True, exist_ok=True)
                tab.evaluate("document.querySelector('#confirmar-aplicacao').scrollIntoView(); true")
                (destino/f'confirmacao-lote-{largura}.png').write_bytes(base64.b64decode(
                    tab.call('Page.captureScreenshot', {'format':'png'})['data']))
                tab.evaluate("document.querySelector('#confirmar-aplicacao button').click(); document.querySelector('#confirmar-aplicacao button')?.click(); true")
                tab.wait("document.querySelectorAll('[data-estado=Aplicado]').length===2")
                self.assertEqual(AplicacaoAjuste.objects.count(), 2)
                self.assertEqual(MovimentacaoEstoqueManual.objects.count(), 2)
                self.assertEqual(EventoAjuste.objects.filter(acao='confirmado_gestor').count(), 2)
            finally:
                chrome.stop()
