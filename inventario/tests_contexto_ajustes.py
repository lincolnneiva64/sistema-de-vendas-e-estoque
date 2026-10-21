"""Compare batched evidence with the original per-document validator."""
from datetime import timedelta

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from estoque.models import EventoVenda, ItemVenda
from estoque.views import _baixar_estoque_produto
from . import ajustes
from .contexto_ajustes import carregar
from .models import MovimentoObservado
from .tests_conciliacao_documental import ConciliacaoDocumentalTests


class ConciliacaoLoteTests(ConciliacaoDocumentalTests):
    # Re-run the existing invalid-reference, editing, cancellation, duplicate,
    # stale-preview and rollback cases through the batched read path as well.
    def avaliar(self, rodada=None):
        r = self.fresca(rodada.pk if rodada else None)
        with carregar([r]) as lote:
            return ajustes.avaliar(r, _lote=lote)

    def test_evidencia_identica_e_validacao_sem_novas_consultas(self):
        self.vender(1); self.vender(1)
        rodada = self.fresca()
        antiga = ajustes.avaliar(rodada)
        with carregar([rodada]) as lote, CaptureQueriesContext(connection) as queries:
            nova = ajustes.avaliar(rodada, _lote=lote)
        self.assertEqual(len(queries), 0)
        for campo in ('assinatura','evidencia','documentos','bloqueio','resultante'):
            self.assertEqual(nova[campo], antiga[campo])

    def test_contexto_nao_sobrevive_a_operacao_e_rele_eventos(self):
        venda,_,_ = self.vender(1)
        rodada = self.fresca()
        with carregar([rodada]) as lote:
            self.assertFalse(ajustes.avaliar(rodada,_lote=lote)['bloqueio'])
        with self.assertRaisesMessage(ValueError,'Contexto do lote'):
            ajustes.avaliar(rodada,_lote=lote)
        EventoVenda.objects.create(venda=venda,tipo_evento='evento_ambiguo')
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_limite_de_eventos_e_por_venda(self):
        venda1,_,_ = self.vender(1)
        venda2,_,_ = self.vender(1)
        EventoVenda.objects.bulk_create([EventoVenda(venda=v,tipo_evento='venda_gravada')
            for v in (venda1,venda2) for _ in range(500)])
        self.assertFalse(self.avaliar()['bloqueio'])
        EventoVenda.objects.create(venda=venda2,tipo_evento='venda_gravada')
        self.assertTrue(self.avaliar()['bloqueio'])
        self.assertTrue(ajustes.avaliar(self.fresca())['bloqueio'])

    def test_contagem_observador_inclui_registros_fora_do_corte(self):
        _,_,m = self.vender(1)
        m.refresh_from_db()
        MovimentoObservado.objects.create(produto=self.produto,sequencia=0,antes=11,depois=10,
            origem='venda',referencia=m.referencia,registrado_em=m.registrado_em-timedelta(days=1))
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_mais_documentos_nao_acrescenta_consultas(self):
        self.vender(1)
        rodada = self.fresca()
        with CaptureQueriesContext(connection) as poucas:
            with carregar([rodada]) as lote:
                ajustes.avaliar(rodada,_lote=lote)
        self.vender(1); self.vender(1)
        rodada = self.fresca()
        with CaptureQueriesContext(connection) as muitas:
            with carregar([rodada]) as lote:
                self.assertFalse(ajustes.avaliar(rodada,_lote=lote)['bloqueio'])
        self.assertEqual(len(muitas),len(poucas))

    def test_venda_compartilhada_por_dois_produtos_preserva_evidencias(self):
        outra = self.segunda()
        venda,_,_ = self.vender(1)
        with transaction.atomic():
            snap = _baixar_estoque_produto(outra.item.produto_id,1,unidade='CX')
            ItemVenda.objects.create(venda=venda,produto_id=outra.item.produto_id,
                quantidade=1,unidade='CX',preco_unitario=0,valor_total=0,**snap)
            m = MovimentoObservado.objects.filter(produto_id=outra.item.produto_id).latest('sequencia')
            MovimentoObservado.objects.filter(pk=m.pk).update(referencia=f'Venda #{venda.pk}')
        rodadas = [self.fresca(),self.fresca(outra.pk)]
        antes = [ajustes.avaliar(r) for r in rodadas]
        with carregar(rodadas) as lote, CaptureQueriesContext(connection) as consultas:
            depois = [ajustes.avaliar(r,_lote=lote) for r in rodadas]
        self.assertEqual(len(consultas),0)
        for a,b in zip(antes,depois):
            self.assertFalse(b['bloqueio'])
            self.assertEqual((a['assinatura'],a['evidencia']),(b['assinatura'],b['evidencia']))
