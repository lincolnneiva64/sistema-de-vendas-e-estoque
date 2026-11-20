"""Central read evaluator: exact fractions and frozen operational evidence."""
from copy import deepcopy
from decimal import Decimal as D
from fractions import Fraction
from types import SimpleNamespace
from unittest import TestCase
from datetime import timedelta
from uuid import uuid4

from django.core.paginator import Paginator
from django.db import connection
from django.test import TestCase as DBTestCase
from django.test.utils import CaptureQueriesContext

from estoque.models import ItemCompra, ItemVenda, Produto, Venda, EventoVenda
from .models import DetalheMovimentoEstoque, MovimentoObservado
from .tests_extrato_cronologico import CronologiaFixture

from .saldo_documental import aplicar_saldos_documentais, provar_atribuicao, provar_entrada_legada, estados_anteriores
from .tests_saldo_documental import Cronologia


def atribuicao(q='2', fator=24, base='PCT', antes='1', acao='saida', depois=None):
    delta = (D(q).quantize(D('.001')) / D(fator)).quantize(D('.001')) if fator else D(q)
    sinal = 1 if acao in ('entrada', 'devolucao', 'cancelamento') else -1
    m = SimpleNamespace(pk=1, sequencia=1, antes=D(antes),
        depois=D(depois) if depois is not None else D(antes) + sinal * delta)
    return SimpleNamespace(movimento=m, quantidade_entrada=D(q), fator=D(fator) if fator else None,
        unidade_base=base, unidade_entrada='UN' if fator else base,
        acao=acao, conversao='divisao' if fator else 'nenhuma', operacao_id='teste', ordem=1)


def evento(d, pk=1):
    prova = provar_atribuicao(d)
    return dict(pk=pk, documento=pk, fonte='digital', operacao_historica=prova,
        fluxo_documental_exato=prova['exato'], quantidade=d.quantidade_entrada,
        unidade=d.unidade_entrada)


def certificado(valor=1, base='PCT', pk=0):
    return dict(pk=pk, fonte='regularizacao', saldo_certificado=dict(valor=D(valor),
        unidade=base, origem='Referência física aplicada', proposta=pk+1))


def calcular(linhas, numero=1, tamanho=50):
    fonte = Cronologia(linhas)
    pagina = Paginator(fonte, tamanho).page(numero)
    aplicar_saldos_documentais(pagina, fonte)
    return pagina


class AvaliadorHistoricoTests(TestCase):
    def test_zero_comprovado_nao_e_zero_presumido(self):
        r = evento(atribuicao('5', None, 'FD', '0', 'entrada'))
        calcular([r])
        self.assertEqual(r['saldo_calculado']['anterior'], 0)
        self.assertEqual(r['saldo_calculado']['valor'], 5)
        desconhecido = dict(pk=2, fonte='digital', antes=0, depois=5)
        calcular([desconhecido])
        self.assertIsNone(desconhecido['saldo_calculado'])

    def test_compra_legada_nao_normaliza_null_nem_origem_desconhecida(self):
        linha = dict(documento=261, quantidade=D(3), unidade='FD')
        m = SimpleNamespace(pk=1, sequencia=1, antes=D(0), depois=D(3),
            origem='compra', referencia='Compra #261')
        self.assertEqual(provar_entrada_legada(linha, m)['antes'], 0)
        for campo, valor in (('antes', None), ('origem', 'nao_identificada'),
                             ('referencia', 'Compra #262'), ('depois', D(4))):
            ruim = deepcopy(m)
            setattr(ruim, campo, valor)
            self.assertIsNone(provar_entrada_legada(linha, ruim))

    def test_fatores_e_unidades_sem_tolerancia(self):
        for base in ('PCT', 'CX', 'DZ', 'FD'):
            for fator in (6, 12, 24):
                with self.subTest(base=base, fator=fator):
                    r = evento(atribuicao(fator=fator, base=base))
                    calcular([certificado(base=base), r])
                    self.assertEqual(r['saldo_calculado']['_exato'], 1-Fraction(2, fator))
                    self.assertEqual(r['saldo_operacional_calculado']['valor'],
                        1-(D(2)/D(fator)).quantize(D('.001')))
        self.assertIsNone(provar_atribuicao(atribuicao(q='1', fator=12, depois='.916')))

    def test_un_sem_conversao_e_fator_ausente_nao_e_inferido(self):
        r = evento(atribuicao('2', None, 'UN', '11'))
        calcular([certificado(11, 'UN'), r])
        self.assertEqual(r['saldo_calculado']['valor'], 9)
        d = atribuicao()
        d.fator = None
        self.assertIsNone(provar_atribuicao(d))

    def test_arredondamento_half_even_efetivo(self):
        for q, esperado in (('1', D('.062')), ('3', D('.188'))):
            d = atribuicao(q, 16)
            prova = provar_atribuicao(d)
            self.assertEqual(prova['operacional'], -esperado)
            self.assertEqual(prova['exato'], -Fraction(D(q))/16)

    def test_fracoes_nao_acumulam_arredondamento_documental(self):
        linhas = [certificado()]
        antes = D(1)
        for i in range(12):
            d = atribuicao(antes=str(antes))
            linhas.append(evento(d, i+1))
            antes = d.movimento.depois
        calcular(linhas)
        self.assertEqual(linhas[-1]['saldo_calculado']['_exato'], 0)
        self.assertEqual(linhas[-1]['saldo_operacional_calculado']['valor'], D('.004'))
        self.assertTrue(linhas[-1]['saldo_divergente'])

    def test_paginacao_mesmas_fracoes_e_origem_fisica(self):
        linhas = [certificado()]
        antes = D(1)
        for i in range(8):
            d = atribuicao(q='1', fator=12, antes=str(antes))
            linhas.append(evento(d, i+1))
            antes = d.movimento.depois
        completo = deepcopy(linhas)
        calcular(completo)
        for numero in range(1, 4):
            p = calcular(linhas, numero, 3)
            for r in p:
                self.assertEqual(r.get('saldo_calculado'), completo[r['pk']].get('saldo_calculado'))

    def test_prefixo_sem_fisica_preserva_quantidade_exata(self):
        r = evento(atribuicao('1', 12, 'PCT', '5', 'entrada'))
        doc, op = estados_anteriores([r])
        self.assertEqual(doc['_exato'], 5+Fraction(1, 12))
        self.assertEqual(op['valor'], D('5.083'))

    def test_troca_de_unidade_nao_perde_fracao_do_novo_trecho(self):
        primeiro = evento(atribuicao('1', 12, 'PCT', '5'))
        segundo = evento(atribuicao('1', 12, 'PCT', '4.917'), 2)
        calcular([certificado(11, 'UN'), primeiro, segundo])
        self.assertIsNone(primeiro['saldo_calculado'])
        self.assertEqual(segundo['saldo_calculado']['_exato'], 5-Fraction(2, 12))

    def test_edicao_devolucao_cancelamento_eventos_distintos(self):
        original = evento(atribuicao('4', None, 'UN', '10'), 1)
        edicao = evento(atribuicao('1', None, 'UN', '6', 'devolucao'), 2)
        cancelamento = evento(atribuicao('3', None, 'UN', '7', 'cancelamento'), 3)
        nota = dict(pk=4, fonte='venda', documento_atual=True, informacao_sem_efeito=True,
                    quantidade=D(3), unidade='UN')
        calcular([certificado(10, 'UN'), original, edicao, cancelamento, nota])
        self.assertEqual(original['saldo_calculado']['valor'], 6)
        self.assertEqual(edicao['saldo_calculado']['valor'], 7)
        self.assertEqual(cancelamento['saldo_calculado']['valor'], 10)
        self.assertIsNone(nota['saldo_calculado'])

    def test_ajuste_incluido_uma_vez_e_multiplas_referencias(self):
        ajuste = certificado(11, 'UN')
        ajuste.update(antes=D(9), depois=D(11), diferenca_aplicada=D(2))
        r = evento(atribuicao('2', None, 'UN', '11'))
        segunda = certificado(7, 'UN', 2)
        calcular([ajuste, r, segunda])
        self.assertEqual(r['saldo_calculado']['valor'], 9)
        self.assertIsNone(segunda['saldo_calculado'])
        self.assertEqual(estados_anteriores([segunda, r, ajuste])[0]['valor'], 7)

    def test_lacuna_e_unidade_incompativel_so_recuperam_adiante(self):
        desconhecido = dict(pk=2, fonte='digital', antes=1, depois=2)
        depois = dict(pk=3, fonte='venda', quantidade=D(1), unidade='UN')
        ultima = evento(atribuicao('1', None, 'UN', '11'), 5)
        linhas = [certificado(), desconhecido, depois, certificado(11, 'UN', 4), ultima]
        calcular(linhas)
        self.assertIsNone(desconhecido['saldo_calculado'])
        self.assertIsNone(depois['saldo_calculado'])
        self.assertEqual(ultima['saldo_calculado']['valor'], 10)


class AvaliadorDjangoTests(CronologiaFixture, DBTestCase):
    """Actual route, middleware, SQL union, batching and both presentations."""

    def resposta(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return r

    def detalhe(self, item, m, q, base, fator=None, acao='saida'):
        return DetalheMovimentoEstoque.objects.create(movimento=m, operacao_id=uuid4(),
            ordem=1, acao=acao, documento_tipo='compra' if acao=='entrada' else 'venda',
            documento_id=str(item.compra_id if acao=='entrada' else item.venda_id),
            item_referencia=str(item.pk), executor=self.gestor, executor_nome='Teste',
            contexto='autenticado', unidade_base=base, quantidade_entrada=q,
            unidade_entrada='UN' if fator else base, fator=fator,
            conversao='divisao' if fator else 'nenhuma')

    def entrada_zero_fd(self, detalhar, deslocamento=0):
        item = ItemCompra.objects.get(compra=self.compra, produto=self.produto)
        ItemCompra.objects.filter(pk=item.pk).update(quantidade=5, unidade='FD')
        m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=0, depois=5, origem='compra', referencia=f'Compra #{self.compra.pk}',
            registrado_em=self.inicio+timedelta(seconds=deslocamento))
        if detalhar:
            self.detalhe(item, m, 5, 'FD', acao='entrada')
        for visual in ('a', 'b'):
            r = self.resposta(visual=visual)
            self.assertEqual(r.context['extrato'].saldo_inicial['valor'], 0)
            self.assertEqual(r.context['extrato'].saldo_inicial['unidade'], 'FD')
            linha = next(l for l in r.context['extrato'] if l['fonte']=='compra')
            self.assertEqual(linha['saldo_calculado']['valor'], 5)

    def test_abertura_zero_fd_comprovada_antes_da_apresentacao(self):
        self.entrada_zero_fd(True)

    def test_abertura_zero_compra_legada_unica_sem_conversao(self):
        self.entrada_zero_fd(False)

    def test_evidencia_anterior_ao_limite_inicial_nao_e_descartada(self):
        self.entrada_zero_fd(False, -1)

    def test_fatores_congelados_independentes_do_cadastro_atual(self):
        for base, fator in (('PCT', 6), ('CX', 12), ('DZ', 24), ('FD', 24)):
            with self.subTest(base=base, fator=fator):
                d = atribuicao('2', fator, base, '10')
                ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=2, unidade='UN',
                    estoque_antes=10, estoque_movimentado=-provar_atribuicao(d)['operacional'],
                    estoque_depois=d.movimento.depois, estoque_unidade_snapshot=base)
                self.posterior.venda.refresh_from_db()
                m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
                    antes=10, depois=d.movimento.depois, origem='venda',
                    referencia=f'Venda #{self.posterior.venda_id}', registrado_em=self.posterior.venda.criado_em)
                self.detalhe(self.posterior, m, 2, base, fator)
                Produto.objects.filter(pk=self.produto.pk).update(fator_conversao=99)
                r = self.resposta()
                linha = next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)
                self.assertEqual(linha['saldo_calculado']['_exato'], 10-Fraction(2, fator))
                self.assertEqual(linha['saldo_operacional_calculado']['valor'], d.movimento.depois)
                self.assertFalse(any(l['fonte']=='digital' for l in r.context['extrato']))
                self.assertContains(r, 'Conversão histórica comprovada')
                DetalheMovimentoEstoque.objects.filter(movimento=m).delete()
                m.delete()

    def test_snapshot_sem_fator_continua_operacional_sem_inventar_conversao(self):
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=2, unidade='UN',
            estoque_antes=0, estoque_movimentado=D('.083'), estoque_depois=D('-.083'),
            estoque_unidade_snapshot='PCT')
        # Negative snapshots are not certified by the legacy snapshot validator.
        r = self.resposta()
        linha = next(l for l in r.context['extrato'] if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        self.assertIsNone(linha.get('operacao_historica'))
        self.assertIsNone(linha['saldo_operacional_calculado'])

    def test_carga_em_lote_somente_leitura(self):
        def consultar():
            with CaptureQueriesContext(connection) as queries:
                self.resposta()
            # PostgreSQL runner may check its disposable identity on connect.
            self.assertTrue(all(q['sql'].lstrip().upper().startswith(
                ('SELECT', '(SELECT', 'DECLARE', 'CLOSE', 'FETCH')) for q in queries),
                [q['sql'] for q in queries if not q['sql'].lstrip().upper().startswith(
                    ('SELECT', '(SELECT', 'DECLARE', 'CLOSE', 'FETCH'))])
            return len(queries)
        poucas = consultar()
        for n in range(30):
            self.registrar_venda(self.ok + timedelta(minutes=n+1))
        muitas = consultar()
        self.assertLessEqual(muitas, poucas+2)

    def test_edicao_e_cancelamento_atribuidos_nao_duplicam_nota(self):
        self.posterior.venda.refresh_from_db()
        instante = self.posterior.venda.criado_em
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=3, unidade='PCT',
            estoque_antes=10, estoque_movimentado=3, estoque_depois=7, estoque_unidade_snapshot='PCT')
        EventoVenda.objects.create(venda_id=self.posterior.venda_id,
            tipo_evento='quantidade_item_alterada',
            descricao=f'Quantidade alterada na nota: {self.produto.nome}. De 4 PCT para 3 PCT.')
        eventos = []
        for seq, antes, depois, q, acao, origem in (
            (1, 10, 6, 4, 'saida', 'venda'),
            (2, 6, 7, 1, 'devolucao', 'devolucao_venda'),
            (3, 7, 10, 3, 'cancelamento', 'devolucao_venda')):
            hora = instante+timedelta(seconds=seq)
            m = MovimentoObservado.objects.create(produto=self.produto, sequencia=seq,
                antes=antes, depois=depois, origem=origem,
                referencia=f'Venda #{self.posterior.venda_id}', registrado_em=hora)
            self.detalhe(self.posterior, m, q, 'PCT', acao=acao)
            eventos.append(m)
        Venda.objects.filter(pk=self.posterior.venda_id).update(cancelada=True,
            estoque_devolvido_cancelamento=True,
            estoque_devolvido_cancelamento_em=eventos[-1].registrado_em)
        r = self.resposta()
        linhas = list(r.context['extrato'])
        digitais = [l for l in linhas if l['fonte']=='digital']
        self.assertEqual([l['saldo_calculado']['valor'] for l in digitais], [6, 7, 10])
        self.assertEqual([l['operacao_historica']['operacional'] for l in digitais], [-4, 1, 3])
        nota = next(l for l in linhas if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        resumo = next(l for l in linhas if l['fonte']=='cancelamento')
        self.assertTrue(nota['informacao_sem_efeito'])
        self.assertTrue(resumo['informacao_sem_efeito'])
        self.assertIsNone(resumo['saldo_calculado'])

    def test_nota_divergente_nao_duplica_operando_atribuido_original(self):
        self.posterior.venda.refresh_from_db()
        ItemVenda.objects.filter(pk=self.posterior.pk).update(quantidade=3, unidade='PCT',
            estoque_antes=10, estoque_movimentado=4, estoque_depois=6, estoque_unidade_snapshot='PCT')
        m = MovimentoObservado.objects.create(produto=self.produto, sequencia=1,
            antes=10, depois=6, origem='venda', referencia=f'Venda #{self.posterior.venda_id}',
            registrado_em=self.posterior.venda.criado_em)
        self.detalhe(self.posterior, m, 4, 'PCT')
        # No mutable edit event is needed to recognize the immutable operand.
        r = self.resposta()
        linhas = list(r.context['extrato'])
        nota = next(l for l in linhas if l['fonte']=='venda' and l['pk']==self.posterior.pk)
        movimento = next(l for l in linhas if l['fonte']=='digital')
        self.assertTrue(nota['informacao_sem_efeito'])
        self.assertIsNone(nota['saldo_calculado'])
        self.assertEqual(movimento['saldo_calculado']['valor'], 6)
