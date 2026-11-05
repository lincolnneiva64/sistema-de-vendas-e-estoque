"""Synthetic PostgreSQL evidence. Never use operational stock or documents."""
from uuid import uuid4

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from estoque.models import EventoVenda, Produto
from estoque.services.estoque_manual import conferir_ou_ajustar_estoque
from . import ajustes, services, views, revisao_historico
from .contexto_ajustes import carregar
from .models import RodadaContagem, ConfirmacaoContagem, AplicacaoAjuste, MovimentoObservado
from .tests_ajustes import AjustesFixture
from .tests_conciliacao_documental import ConciliacaoDocumentalTests


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class RevisaoHistoricoTests(AjustesFixture, TestCase):
    vender = ConciliacaoDocumentalTests.vender

    def linha(self):
        return next(l for l in views.linhas_revisao(self.inv) if l['rodada'].pk == self.rodada.pk)

    def aplicar(self):
        r = self.fresca()
        return ajustes.atualizar(self.usuario, self.inv.pk,
            ajustes.previa_atualizacao(self.inv.pk, [ajustes.avaliar(r)]), True)[0]

    def test_sem_movimentos_distingue_revisao_administrativa_sem_gravar(self):
        for campo, valor in (('esperado', 8),):
            RodadaContagem.objects.filter(pk=self.rodada.pk).update(**{campo: valor})
            ConfirmacaoContagem.objects.filter(rodada=self.rodada).update(**{campo: valor})
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=8)
        # Use fresh OK to anchor the synthetic observer update.
        r = self.fresca()
        seq = services.sequencia(self.produto.pk)
        RodadaContagem.objects.filter(pk=r.pk).update(sequencia_ok=seq)
        ConfirmacaoContagem.objects.filter(rodada=r).update(sequencia_ok=seq)
        for revisao, texto in [(None, 'não registrada'), (seq+1, 'desatualizada'), (seq, 'registrada')]:
            RodadaContagem.objects.filter(pk=r.pk).update(revisao_sequencia=revisao)
            with CaptureQueriesContext(connection) as queries:
                l = self.linha()
            self.assertEqual(l['situacao'], 'Correto')
            self.assertIn(texto, l['bloqueio'])
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
            self.assertEqual(self.fresca().revisao_sequencia, revisao)

    def test_venda_documentada_aceita_sem_conciliacao_textual(self):
        self.vender(1)
        linha = self.linha()
        self.assertNotEqual(linha['situacao'], 'Revisão necessária')
        self.assertTrue(linha['movimentos'][0]['comprovado_documentalmente'])
        self.assertFalse(linha['movimentos'][0]['conciliado'])

    def test_venda_cancelada_ou_editada_continua_bloqueando(self):
        venda, _, _ = self.vender(1)
        EventoVenda.objects.create(venda=venda, tipo_evento='edicao_desconhecida')
        self.assertEqual(self.linha()['situacao'], 'Revisão necessária')

    def test_cancelamento_snapshot_e_referencia_invalidos_nao_sao_aceitos(self):
        from django.db import transaction
        from estoque.models import Venda, ItemVenda
        venda, item, movimento = self.vender(1)
        for modelo, pk, campos in [(Venda, venda.pk, dict(cancelada=True)),
                (ItemVenda, item.pk, dict(estoque_depois=0)),
                (MovimentoObservado, movimento.pk, dict(referencia='Venda sem identidade'))]:
            with self.subTest(campos=campos), transaction.atomic():
                modelo.objects.filter(pk=pk).update(**campos)
                self.assertEqual(self.linha()['situacao'], 'Revisão necessária')
                transaction.set_rollback(True)

    def test_aplicacao_auditada_e_venda_posterior_continuam_validadas(self):
        self.aplicar()
        self.assertNotEqual(self.linha()['situacao'], 'Revisão necessária')
        self.vender(1)
        self.assertNotEqual(self.linha()['situacao'], 'Revisão necessária')
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)

    def test_aplicacao_em_lote_tambem_e_reconhecida(self):
        self.segunda()
        preparacao = ajustes.preparar_resultado(self.inv)
        ajustes.atualizar(self.usuario, self.inv.pk,
            ajustes.previa_atualizacao(self.inv.pk, preparacao['aptas']), True)
        linhas = views.linhas_revisao(self.inv)
        self.assertTrue(all(l['situacao'] != 'Revisão necessária' for l in linhas))
        self.assertTrue(all(l['movimentos'][-1]['comprovado_documentalmente'] for l in linhas))

    def test_aplicacao_nao_oculta_movimento_posterior_sem_evidencia(self):
        self.aplicar()
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7,
            motivo='Manual genérico fictício', usuario=self.usuario)
        self.assertEqual(self.linha()['situacao'], 'Revisão necessária')

    def test_sinalizacao_nao_resolvida_independe_da_auditoria(self):
        self.aplicar()
        services.sinalizar_fisico(self.rodada.pk, 'Dúvida fictícia', str(uuid4()), usuario=self.usuario)
        self.assertEqual(self.linha()['situacao'], 'Revisão necessária')

    def test_auditoria_incompleta_incompativel_nao_e_aceita(self):
        self.aplicar()
        r = self.fresca()
        for dano in ('aplicacao', 'evento', 'inventario', 'assinatura', 'saldo', 'usuario', 'confirmacao', 'referencia'):
            with self.subTest(dano=dano), carregar([r]) as lote:
                revisao_historico.carregar(lote)
                a = next(iter(lote.aplicacoes_revisao.values()))
                m = lote.movimentos[r.item.produto_id][-1]
                if dano == 'aplicacao': lote.aplicacoes_revisao.clear()
                if dano == 'evento': lote.eventos_aplicacao[a.proposta_id] = []
                if dano == 'inventario': lote.eventos_inventario_aplicacao[r.pk] = []
                if dano == 'assinatura': a.proposta.assinatura = 'invalida'
                if dano == 'saldo': a.depois += 1
                if dano == 'usuario': a.usuario_id += 100
                if dano == 'confirmacao': a.proposta.confirmacao.contado_base += 1
                if dano == 'referencia': m.referencia = 'Movimentação manual #999999'
                with self.assertRaises(ValueError):
                    revisao_historico.validar(r, r.item.produto, lote)

    def test_validacao_em_lote_sem_consultas_por_produto(self):
        self.aplicar()
        self.vender(1)
        r = self.fresca()
        with carregar([r]) as lote:
            revisao_historico.carregar(lote)
            with CaptureQueriesContext(connection) as queries:
                revisao_historico.validar(r, r.item.produto, lote)
            self.assertEqual(len(queries), 0)

    def test_mais_produtos_nao_multiplica_consultas_de_auditoria(self):
        def medir():
            rodadas = [self.fresca(r.pk) for r in RodadaContagem.objects.filter(item__inventario=self.inv)]
            with CaptureQueriesContext(connection) as queries:
                with carregar(rodadas) as lote:
                    revisao_historico.carregar(lote)
                    for r in rodadas:
                        revisao_historico.validar(r, r.item.produto, lote)
            return len(queries)
        self.aplicar()
        poucas = medir()
        outra = self.segunda()
        r = self.fresca(outra.pk)
        ajustes.atualizar(self.usuario, self.inv.pk,
            ajustes.previa_atualizacao(self.inv.pk, [ajustes.avaliar(r)]), True)
        self.assertEqual(medir(), poucas)

    def test_custo_adicional_fixo_tres_consultas_e_get_somente_leitura(self):
        from django.urls import reverse
        self.aplicar()
        r = self.fresca()
        with CaptureQueriesContext(connection) as antes:
            with carregar([r]) as lote:
                with self.assertRaises(ValueError):
                    services.validar_cadeia(r, r.item.produto, _lote=lote)
        with CaptureQueriesContext(connection) as depois:
            with carregar([r]) as lote:
                revisao_historico.carregar(lote)
                revisao_historico.validar(r, r.item.produto, lote)
        self.assertEqual(len(depois), len(antes) + 3)
        print(f'Consultas no mesmo cenário aplicado: antes={len(antes)}, depois={len(depois)}; validação por produto=0.')
        with CaptureQueriesContext(connection) as leitura:
            resposta = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Movimento comprovado')
        self.assertNotContains(resposta, 'Revisão física necessária')
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in leitura))

    def test_pendencia_posterior_permanece_visivel_mesmo_com_ajuste_aplicado(self):
        from django.urls import reverse
        self.aplicar()
        conferir_ou_ajustar_estoque(self.produto.pk, 'ajuste', novo_estoque=7,
            motivo='Movimento posterior não comprovado', usuario=self.usuario)
        resposta = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(resposta, 'Ajuste aplicado')
        self.assertContains(resposta, 'Revisão necessária')

    def test_descontinuidade_e_origem_desconhecida_permanecem_bloqueadas(self):
        self.aplicar()
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=7)
        self.assertEqual(self.linha()['situacao'], 'Revisão necessária')
        MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia').delete()
        self.assertEqual(self.linha()['situacao'], 'Revisão necessária')
