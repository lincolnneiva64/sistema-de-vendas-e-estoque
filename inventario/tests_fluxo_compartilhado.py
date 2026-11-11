"""Focused regressions on disposable stock and documents only."""
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from django.db import close_old_connections, connection, transaction
from django.db.models import F
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Compra, ItemCompra, Produto, EdicaoCompra
from . import ajustes, contagem_admin, conversao_fisica, encerramento, services, views
from .contexto_ajustes import carregar
from .conversao import cadastro_compativel
from .models import AplicacaoAjuste, DetalheMovimentoEstoque, MovimentoObservado, RodadaContagem
from .tests import produto
from .tests_ajustes import AjustesFixture
from . import tests_conciliacao_documental as documental


class FluxoCompartilhadoTests(AjustesFixture, TestCase):
    vender = documental.ConciliacaoDocumentalTests.vender

    def comprar(self, quantidade=3, unidade='CX', incremento=None, detalhe=False):
        incremento = Decimal(str(quantidade if incremento is None else incremento))
        compra = Compra.objects.create(data_compra=timezone.localdate(),
            estoque_entrada_realizada=True, estoque_entrada_realizada_em=timezone.now())
        item = ItemCompra.objects.create(compra=compra, produto=self.produto,
            quantidade=quantidade, unidade=unidade, preco_unitario=0, valor_total=0)
        Produto.objects.filter(pk=self.produto.pk).update(quantidade=F('quantidade')+incremento)
        m = MovimentoObservado.objects.filter(produto=self.produto).latest('sequencia')
        MovimentoObservado.objects.filter(pk=m.pk).update(origem='compra', referencia=f'Compra #{compra.pk}')
        m.refresh_from_db()
        if detalhe:
            DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(), ordem=1,
                acao='entrada', documento_tipo='compra', documento_id=str(compra.pk),
                item_referencia=str(item.pk), executor=self.usuario, executor_nome='Gestor fictício',
                contexto='autenticado', unidade_base='CX', quantidade_entrada=quantidade,
                unidade_entrada=unidade, conversao='divisao', fator=12)
        return compra, item, m

    def test_compra_venda_revisao_e_aplicacao_preservam_ok_e_saldo_posterior(self):
        self.comprar()
        self.vender(1)
        r = self.fresca()
        antes = list(r.confirmacoes.values())
        a = ajustes.avaliar(r)
        self.assertFalse(a['bloqueio'])
        self.assertEqual((a['esperado'], a['contado'], a['atual'], a['resultante']), (10, 8, 12, 10))
        with carregar([r]) as lote:
            b = ajustes.avaliar(r, _lote=lote)
        self.assertEqual(a['evidencia'], b['evidencia'])
        services.revisar(self.usuario, r.pk, 'Documentos conferidos fisicamente.')
        self.assertFalse(r.conciliacoes.exists())
        linha = next(l for l in views.linhas_revisao(self.inv) if l['rodada'].pk == r.pk)
        self.assertTrue(all(m['comprovado_documentalmente'] for m in linha['movimentos']))
        p = self.aprovar()
        token = ajustes.previa_aplicacao(self.inv.pk, [p])
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True):
            primeiro = ajustes.aplicar(self.usuario, self.inv.pk, token, True)
            segundo = ajustes.aplicar(self.usuario, self.inv.pk, token, True)
        self.assertEqual(primeiro[0].pk, segundo[0].pk)
        self.assertEqual(AplicacaoAjuste.objects.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 10)
        self.assertEqual(list(r.confirmacoes.values()), antes)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)

    def test_compra_convertida_exige_fator_historico_congelado(self):
        self.comprar(6, 'UN', '.5', detalhe=True)
        a = self.avaliar()
        self.assertFalse(a['bloqueio'])
        self.assertEqual(a['resultante'], Decimal('8.5'))
        self.assertEqual(a['documentos'][0]['atribuicao']['conversao'], 'divisao')

    def test_compras_em_lote_nao_criam_consultas_por_produto_ou_documento(self):
        self.comprar()
        r = self.fresca()
        with CaptureQueriesContext(connection) as poucas:
            with carregar([r]) as lote:
                ajustes.avaliar(r, _lote=lote)
        self.comprar(); self.comprar()
        r = self.fresca()
        with CaptureQueriesContext(connection) as muitas:
            with carregar([r]) as lote:
                with CaptureQueriesContext(connection) as validacao:
                    a = ajustes.avaliar(r, _lote=lote)
        self.assertFalse(a['bloqueio'])
        self.assertEqual(len(validacao), 0)
        self.assertEqual(len(poucas), len(muitas))

    def test_compra_sem_conversao_comprovada_identifica_impedimento(self):
        c, _, m = self.comprar(6, 'UN', '.5')
        a = self.avaliar()
        self.assertIn(f'Compra #{c.pk}', a['bloqueio'])
        self.assertIn(f'movimento #{m.pk}', a['bloqueio'])
        self.assertIn(f'produto #{self.produto.pk}', a['bloqueio'])
        self.assertEqual((a['esperado'], a['contado'], a['diferenca']), (10, 8, -2))

    def test_recebimento_cancelado_editado_duplicado_ou_divergente_bloqueia(self):
        c, i, m = self.comprar()
        for campo, valor in (('cancelada', True), ('estoque_entrada_realizada', False)):
            original = getattr(c, campo)
            Compra.objects.filter(pk=c.pk).update(**{campo: valor})
            self.assertTrue(self.avaliar()['bloqueio'])
            Compra.objects.filter(pk=c.pk).update(**{campo: original})
        ItemCompra.objects.filter(pk=i.pk).update(quantidade=4)
        self.assertTrue(self.avaliar()['bloqueio'])
        ItemCompra.objects.filter(pk=i.pk).update(quantidade=3)
        EdicaoCompra.objects.create(compra=c, operacao=uuid4(), motivo='Edição fictícia',
            payload_hash='a'*64, antes={}, depois={})
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_movimento_repetido_nao_e_somado_duas_vezes(self):
        _, _, m = self.comprar()
        MovimentoObservado.objects.create(produto=self.produto, sequencia=m.sequencia+1,
            antes=m.antes, depois=m.depois, origem=m.origem, referencia=m.referencia,
            registrado_em=timezone.now())
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_conciliacao_textual_nao_libera_origem_sem_prova(self):
        _, _, m = self.comprar()
        MovimentoObservado.objects.filter(pk=m.pk).update(origem='ajuste_manual',
            referencia='Movimentação manual fictícia sem comprovante')
        services.conciliar(self.usuario, self.rodada.pk, m.pk, 'Texto não substitui documento.')
        self.assertTrue(self.avaliar()['bloqueio'])
        with self.assertRaisesMessage(ValueError, f'movimento #{m.pk}'):
            services.revisar(self.usuario, self.rodada.pk, 'Revisão física.')
        linha = next(l for l in views.linhas_revisao(self.inv) if l['rodada'].pk == self.rodada.pk)
        self.assertEqual(linha['situacao'], 'Revisão necessária')

    def test_compra_posterior_a_aprovacao_exige_nova_previa(self):
        p = self.aprovar()
        self.comprar()
        with override_settings(INVENTARIO_AJUSTES_HABILITADOS=True), self.assertRaises(ValueError):
            ajustes.aplicar(self.usuario, self.inv.pk, ajustes.previa_aplicacao(self.inv.pk, [p]), True)
        self.assertFalse(AplicacaoAjuste.objects.exists())

    def test_mudanca_descritiva_nao_bloqueia_mas_conversao_sim(self):
        Produto.objects.filter(pk=self.produto.pk).update(nome='Nome revisado', codigo='NOVO', categoria='Outra')
        self.produto.refresh_from_db()
        self.assertTrue(cadastro_compativel(self.produto, self.rodada.item.cadastro))
        self.assertFalse(self.avaliar()['bloqueio'])
        Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=24)
        self.assertTrue(self.avaliar()['bloqueio'])

    def test_zero_e_divergencia_grande_preservam_ok_sem_finalizar(self):
        for valor in ('0', '999'):
            r = self.fresca()
            self.inv.refresh_from_db()
            token = contagem_admin.autorizacao(self.usuario, self.inv, r)
            contagem_admin.confirmar(self.usuario, self.inv.pk, r.pk, token, valor, '0')
            a = self.avaliar()
            self.assertFalse(a['bloqueio'])
            self.assertEqual(a['contado'], Decimal(valor))
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)
        self.assertEqual(self.fresca().confirmacoes.count(), 3)

    def test_encerramento_usa_relacao_fisica_confirmada_e_e_idempotente(self):
        p = produto(nome='Pacote fictício', vende_fracionado=False, unidade_venda_2='')
        inv = services.criar_inventario(self.usuario, criterio='manual', selecionados=[p.pk])
        r = RodadaContagem.objects.select_related('item__produto').get(item__inventario=inv)
        token = contagem_admin.autorizacao(self.usuario, inv, r)
        conversao_fisica.confirmar(self.usuario, inv.pk, r.pk, token, '12')
        contagem_admin.confirmar(self.usuario, inv.pk, r.pk, token, '1', '5')
        inv.refresh_from_db()
        previa = encerramento.previa(self.usuario, inv)
        encerramento.finalizar(self.usuario, inv.pk, previa['token'], 'sim')
        self.assertTrue(encerramento.finalizar(self.usuario, inv.pk, previa['token'], 'sim')['ja_finalizada'])
        inv.refresh_from_db(); p.refresh_from_db()
        self.assertIsNotNone(inv.finalizada_em)
        self.assertEqual(p.quantidade, 10)

    def test_encerramento_impedido_exibe_motivo_produto_e_acao(self):
        RodadaContagem.objects.filter(pk=self.rodada.pk).update(contado_base=9)
        resposta = self.client.get(reverse('inventario:detalhe', args=[self.inv.pk]))
        self.assertContains(resposta, 'Conferência ainda não pode ser finalizada')
        self.assertContains(resposta, self.produto.nome)
        self.assertContains(resposta, 'Revise o produto indicado')
        self.assertContains(resposta, 'contagem inconsistente')


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class CompraConcorrenteTests(AjustesFixture, TransactionTestCase):
    comprar = FluxoCompartilhadoTests.comprar

    def test_compra_em_edicao_recusa_aplicacao_sem_escrita_parcial(self):
        compra, _, _ = self.comprar()
        proposta = self.aprovar()
        token = ajustes.previa_aplicacao(self.inv.pk, [proposta])
        travada, liberar = Event(), Event()

        def editar():
            close_old_connections()
            try:
                with transaction.atomic():
                    Compra.objects.select_for_update().get(pk=compra.pk)
                    travada.set()
                    if not liberar.wait(10):
                        raise RuntimeError('Teste não liberou o documento.')
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            futuro = executor.submit(editar)
            try:
                self.assertTrue(travada.wait(10))
                with self.assertRaisesMessage(ValueError, 'alteração concorrente'):
                    ajustes.aplicar(self.usuario, self.inv.pk, token, True)
            finally:
                liberar.set()
            futuro.result(timeout=10)
        self.assertFalse(AplicacaoAjuste.objects.exists())
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, 13)
