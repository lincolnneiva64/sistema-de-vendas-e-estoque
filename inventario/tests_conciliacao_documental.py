"""Synthetic documents and stock; never operational IDs or fixed production data."""
from decimal import Decimal
from unittest.mock import patch
from datetime import timedelta

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import close_old_connections
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from django.utils import timezone
from estoque.models import Venda, ItemVenda, EventoVenda, Produto
from estoque.views import _baixar_estoque_produto
from . import ajustes
from .models import (MovimentoObservado, ConfirmacaoContagem, RodadaContagem,
                     AplicacaoAjuste, PropostaAjuste)
from .tests_ajustes import AjustesFixture
from .tests import produto


class ConciliacaoDocumentalTests(AjustesFixture, TestCase):
    def vender(self, quantidade=6):
        self.produto.refresh_from_db()
        venda = Venda.objects.create(data_venda=timezone.localdate())
        with transaction.atomic():
            snap = _baixar_estoque_produto(self.produto.pk, Decimal(quantidade),
                unidade=self.produto.unidade_venda_1)
            item = ItemVenda.objects.create(venda=venda, produto=self.produto,
                quantidade=quantidade, unidade=self.produto.unidade_venda_1,
                preco_unitario=0, valor_total=0, **snap)
            m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
            MovimentoObservado.objects.filter(pk=m.pk).update(referencia=f'Venda #{venda.pk}')
        return venda, item, m

    def preparar_conferencia_um(self):
        self.rodada.item.inventario_id = 1
        self.rodada.item.save(update_fields=['inventario'])
        self.inv.pk = 1
        # Synthetic count of six, with original confirmation matching the round.
        valores = dict(contado=6, contado_base=6, embalagens=6)
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(**valores)
        ConfirmacaoContagem.objects.filter(rodada=self.rodada).update(**valores)

    def test_venda_documentada_conferencia_um_propoe_zero_sem_escrever(self):
        self.preparar_conferencia_um()
        venda, item, m = self.vender()
        a = self.avaliar()
        self.assertFalse(a['bloqueio'])
        self.assertEqual((a['esperado'], a['contado'], a['atual'], a['resultante']), (10, 6, 4, 0))
        self.assertEqual(a['evidencia']['documentos'][0]['item'], item.pk)
        p = self.aprovar()
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 4)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        with self.assertRaisesMessage(ValueError, 'desabilitada'):
            ajustes.aplicar(self.usuario, 1, ajustes.previa_aplicacao(1, [p]), True)
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            a = ajustes.aplicar(self.usuario, 1, ajustes.previa_aplicacao(1, [p]), True)[0]
            self.assertEqual((a.antes, a.depois), (4, 0))

    def test_edicao_outro_produto_comprovado_nao_invalida_snapshot(self):
        venda, item, m = self.vender()
        outro = produto(nome='Outro SKU independente', quantidade=20)
        ItemVenda.objects.create(venda=venda, produto=outro, quantidade=1,
            unidade='CX', preco_unitario=0, valor_total=0)
        EventoVenda.objects.create(venda=venda, tipo_evento='item_adicionado_na_nota',
            descricao=f'Item adicionado na nota: {outro.nome}, quantidade 1.000 CX.')
        self.assertFalse(self.avaliar()['bloqueio'])

    def test_edicoes_ambiguas_ou_proprio_produto_bloqueiam(self):
        venda, item, m = self.vender()
        for tipo, descricao in [('quantidade_item_alterada',
                f'Quantidade alterada na nota: {self.produto.nome}. De 5 para 6'),
                ('edicao_desconhecida', 'Sem item comprovado')]:
            e = EventoVenda.objects.create(venda=venda,tipo_evento=tipo,descricao=descricao)
            self.assertTrue(self.avaliar()['bloqueio'])
            e.delete()

    def test_unidade_quantidade_snapshot_ou_referencia_incompativeis(self):
        venda, item, m = self.vender()
        for campo, valor in [('estoque_unidade_snapshot','KG'), ('quantidade',5),
                              ('estoque_depois',3), ('estoque_antes',11)]:
            with transaction.atomic():
                ItemVenda.objects.filter(pk=item.pk).update(**{campo:valor})
                self.assertTrue(self.avaliar()['bloqueio'])
                transaction.set_rollback(True)
        MovimentoObservado.objects.filter(pk=m.pk).update(referencia='Venda sem número')
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_movimento_duplicado_ou_data_distante_bloqueia(self):
        venda,item,m = self.vender()
        Venda.objects.filter(pk=venda.pk).update(criado_em=m.registrado_em-timedelta(minutes=10))
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_cancelamento_ou_item_duplicado_bloqueia(self):
        venda,item,m = self.vender()
        with transaction.atomic():
            Venda.objects.filter(pk=venda.pk).update(cancelada=True)
            self.assertTrue(self.avaliar()['bloqueio'])
            transaction.set_rollback(True)
        ItemVenda.objects.create(venda=venda,produto=self.produto,quantidade=1,
            unidade=item.unidade,preco_unitario=0,valor_total=0)
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_nova_venda_apos_aprovacao_exige_nova_previa(self):
        self.vender()
        p = self.aprovar()
        self.vender(1)
        self.assertFalse(self.avaliar()['bloqueio'])
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            with self.assertRaises(ValueError):
                ajustes.aplicar(self.usuario,self.inv.pk,ajustes.previa_aplicacao(self.inv.pk,[p]),True)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_sem_diferenca_nao_cria_proposta(self):
        valores=dict(contado=10,contado_base=10,embalagens=10)
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(**valores)
        ConfirmacaoContagem.objects.filter(rodada=self.rodada).update(**valores)
        a=self.avaliar()
        self.assertEqual(a['estado'],'Sem ajuste')
        self.assertNotIn('token',a)
        self.assertFalse(PropostaAjuste.objects.exists())

    def test_aprovacao_revalida_edicao_documental_sem_mudanca_saldo(self):
        venda,item,m = self.vender()
        p = self.aprovar()
        EventoVenda.objects.create(venda=venda,tipo_evento='edicao_desconhecida',descricao='Ambígua')
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            with self.assertRaises(ValueError):
                ajustes.aplicar(self.usuario,self.inv.pk,ajustes.previa_aplicacao(self.inv.pk,[p]),True)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_visualizacao_nao_expira_proposta(self):
        venda,item,m = self.vender()
        p = self.aprovar()
        EventoVenda.objects.create(venda=venda,tipo_evento='nota_visualizada',descricao='Nota visualizada.')
        self.assertEqual(p.assinatura,self.avaliar()['assinatura'])

    @override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
    def test_lote_com_documento_e_falha_reverte_integralmente(self):
        self.vender()
        p1 = self.aprovar(); p2 = self.aprovar(self.segunda())
        original = ajustes.conferir_ou_ajustar_estoque
        chamadas = []
        def falhar(*args, **kwargs):
            chamadas.append(1)
            if len(chamadas)==2:
                raise ValueError('Falha integral fictícia')
            return original(*args, **kwargs)
        with patch.object(ajustes, 'conferir_ou_ajustar_estoque', side_effect=falhar):
            with self.assertRaises(ValueError):
                ajustes.aplicar(self.usuario,self.inv.pk,ajustes.previa_aplicacao(self.inv.pk,[p1,p2]),True)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade,4)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.assertEqual(PropostaAjuste.objects.filter(estado='aprovado').count(),2)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class ConcorrenciaDocumentalTests(AjustesFixture, TransactionTestCase):
    vender = ConciliacaoDocumentalTests.vender

    def test_documento_em_edicao_concorrente_recusa_sem_escrita(self):
        venda,item,m = self.vender()
        p=self.aprovar()
        travou,liberar=Event(),Event()
        def editar():
            close_old_connections()
            try:
                with transaction.atomic():
                    Venda.objects.select_for_update().get(pk=venda.pk)
                    travou.set()
                    if not liberar.wait(15):
                        raise AssertionError('Timeout fictício')
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            f=pool.submit(editar)
            self.assertTrue(travou.wait(10))
            try:
                with self.assertRaisesMessage(ValueError,'concorrente'):
                    ajustes.aplicar(self.usuario,self.inv.pk,ajustes.previa_aplicacao(self.inv.pk,[p]),True)
            finally:
                liberar.set()
            f.result(20)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade,4)
        self.assertFalse(AplicacaoAjuste.objects.exists())
