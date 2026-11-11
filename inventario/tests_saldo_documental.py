"""Fictitious documentary balances. No database, services or migrations."""
from copy import deepcopy
from decimal import Decimal as D
from unittest import TestCase

from django.core.paginator import Paginator

from .saldo_documental import aplicar_saldos_documentais


class Cronologia(list):
    def reverse(self):
        return Cronologia(reversed(self))

    def __getitem__(self, chave):
        valor = super().__getitem__(chave)
        return Cronologia(valor) if isinstance(chave, slice) else valor

    def iterator(self, chunk_size=100):
        yield from self


def venda(numero, quantidade, saldo=None, base='UN', **campos):
    linha = dict(pk=numero, documento=numero, fonte='venda', origem='avista',
        quantidade=D(str(quantidade)), unidade=base, cancelada=False,
        saldo=D(str(saldo)) if saldo is not None else None,
        saldo_unidade=base, saldo_tipo='Snapshot registrado' if saldo is not None else '',
        entrada=None, saida=D(str(quantidade)))
    linha.update(campos)
    return linha


def projetar(linhas, pagina=1, tamanho=50):
    fonte = Cronologia(linhas)
    resultado = Paginator(fonte, tamanho).page(pagina)
    aplicar_saldos_documentais(resultado, fonte)
    return resultado


class SaldoDocumentalTests(TestCase):
    def test_snapshot_divergente_visivel_sem_reiniciar_calculo(self):
        atual = venda(2, 2, referencia_antes=D(20), referencia_baixa=D(2),
            referencia_depois=D(18), referencia_unidade='UN', referencia_habilitada=False)
        posterior = venda(3, 1)
        projetar([venda(1, 1, 10), atual, posterior])
        self.assertEqual(atual['saldo_calculado']['valor'], D(8))
        self.assertEqual(atual['saldo_registrado_operacional']['valor'], D(18))
        self.assertTrue(atual['saldo_divergente'])
        self.assertEqual(posterior['saldo_calculado']['valor'], D(7))

    def test_28_menos_13_sem_somar_evidencia_digital_27(self):
        anterior = venda(996, 14, 28)
        atual = venda(1049, 13, documento_atual=True,
            evidencias_operacionais=[dict(antes=D(28), depois=D(1))])
        originais = deepcopy(atual)
        projetar([anterior, atual])
        self.assertEqual(atual['saldo_calculado']['valor'], D(15))
        self.assertEqual(atual['saldo_calculado']['anterior'], D(28))
        self.assertEqual({k: atual[k] for k in originais}, originais)
        self.assertIsNone(atual['saldo'])

    def test_considera_compra_e_devolucao_intermediarias(self):
        compra = dict(pk=1, documento=1, fonte='compra', quantidade=D(3), unidade='UN')
        devolucao = dict(pk=2, documento=2, fonte='remocao', retorno_efetivo=True,
                        quantidade=D(2), unidade='UN')
        atual = venda(1049, 13)
        projetar([venda(996, 1, 28), compra, devolucao, atual])
        self.assertEqual(compra['saldo_calculado']['valor'], D(31))
        self.assertEqual(devolucao['saldo_calculado']['valor'], D(33))
        self.assertEqual(atual['saldo_calculado']['valor'], D(20))

    def test_operacao_nao_identificada_interrompe_ate_nova_referencia(self):
        atual, posterior = venda(1049, 13), venda(2001, 2)
        desconhecido = dict(pk=9, fonte='digital', origem='nao_identificada', antes=D(28), depois=D(20))
        projetar([venda(996, 1, 28), desconhecido, atual, venda(2000, 1, 9), posterior])
        self.assertIsNone(atual['saldo_calculado'])
        self.assertEqual(posterior['saldo_calculado']['valor'], D(7))

    def test_nao_mistura_unidades_nem_reutiliza_base_anterior(self):
        pacote, seguinte = venda(1049, 1, base='PCT'), venda(1050, 1)
        projetar([venda(996, 1, 28), pacote, seguinte])
        self.assertIsNone(pacote['saldo_calculado'])
        self.assertIsNone(seguinte['saldo_calculado'])

    def test_ajuste_sem_unidade_interrompe_e_com_unidade_reinicia(self):
        desconhecido = dict(pk=684, fonte='manual', origem='ajuste', ajuste_manual=dict(
            antes=D(1), depois=D('0.491'), unidade='', diferenca=D('-0.509')))
        conhecido = dict(pk=685, fonte='manual', origem='ajuste', ajuste_manual=dict(
            antes=D(5), depois=D(4), unidade='UN', diferenca=D(-1)))
        primeira, segunda = venda(1049, 1), venda(1050, 1)
        projetar([venda(996, 1, 28), desconhecido, primeira, conhecido, segunda])
        self.assertIsNone(primeira['saldo_calculado'])
        self.assertEqual(segunda['saldo_calculado']['valor'], D(3))
        self.assertEqual(desconhecido['ajuste_manual']['depois'], D('0.491'))

    def test_so_regularizacao_aplicada_altera_referencia(self):
        ok = dict(pk=1, fonte='conferencia', contado=D(0))
        regular = dict(pk=2, documento=2, fonte='regularizacao', saldo=D(6), saldo_unidade='UN')
        primeira, segunda = venda(1049, 13), venda(1050, 2)
        projetar([venda(996, 1, 28), ok, primeira, regular, segunda])
        self.assertEqual(primeira['saldo_calculado']['valor'], D(15))
        self.assertEqual(segunda['saldo_calculado']['valor'], D(4))

    def test_paginacao_nao_troca_ancora_nem_pula_movimentos(self):
        # New snapshots must not replace the documentary calculation, including
        # when the divergent snapshot is on the previous page.
        linhas = [venda(996, 1, 28), venda(1049, 13, 77), venda(1050, 2)]
        completo = deepcopy(linhas)
        projetar(completo)
        pagina = projetar(linhas, pagina=2, tamanho=2)
        self.assertEqual(pagina[0]['saldo_calculado']['valor'], D(13))
        self.assertEqual(pagina[0]['saldo_calculado'], completo[2]['saldo_calculado'])
        self.assertEqual(linhas[1]['saldo'], D(77))
        self.assertEqual(completo[1]['saldo_calculado']['valor'], D(15))
        self.assertTrue(completo[1]['saldo_divergente'])
        self.assertEqual(completo[1]['saldo_registrado_operacional']['valor'], D(77))

    def test_lacuna_em_pagina_anterior_nao_e_ignorada(self):
        desconhecido = dict(pk=9, fonte='digital', origem='nao_identificada')
        linhas = [venda(996, 1, 28), desconhecido, venda(1049, 13)]
        pagina = projetar(linhas, pagina=2, tamanho=2)
        self.assertIsNone(pagina[0]['saldo_calculado'])

    def test_snapshot_nulo_inconsistente_ou_sem_unidade_nao_e_ancora(self):
        for campo, valor in [('referencia_antes', None), ('referencia_depois', D(99)),
                             ('referencia_unidade', ''), ('referencia_habilitada', False)]:
            inicial = venda(996, 2, referencia_antes=D(30), referencia_baixa=D(2),
                referencia_depois=D(28), referencia_unidade='UN', referencia_habilitada=True)
            inicial[campo] = valor
            atual = venda(1049, 13)
            projetar([inicial, atual])
            self.assertIsNone(atual['saldo_calculado'])

    def test_correcao_com_unidade_e_posterior_conhecido_reinicia_entre_paginas(self):
        manual = dict(pk=10, documento=10, fonte='manual', origem='ajuste',
                      referencia_depois=D(4), referencia_unidade='UN')
        pagina = projetar([venda(996, 1, 28), manual, venda(1049, 1)], pagina=2, tamanho=2)
        self.assertEqual(pagina[0]['saldo_calculado']['valor'], D(3))

    def test_quantidade_zero_documentada_preserva_saldo(self):
        sem_saida = venda(1049, 0)
        projetar([venda(996, 1, 28), sem_saida])
        self.assertEqual(sem_saida['saldo_calculado']['valor'], D(28))

    def test_paginacao_reutiliza_preparacao_manual_da_pagina_visivel(self):
        from unittest.mock import Mock
        manual = dict(pk=10, documento=10, fonte='manual', origem='ajuste',
                      referencia_depois=D(4), referencia_unidade='')
        linhas = [venda(996, 1, 28), manual, venda(1049, 1)]
        fonte = Cronologia(linhas)
        pagina = Paginator(fonte, 2).page(2)
        def preparar(trecho):
            for r in trecho:
                if r['fonte'] == 'manual':
                    r['ajuste_manual'] = dict(depois=D(4), unidade='UN')
        callback = Mock(side_effect=preparar)
        aplicar_saldos_documentais(pagina, fonte, preparar_linhas=callback)
        callback.assert_called_once()
        self.assertEqual(pagina[0]['saldo_calculado']['valor'], D(3))

    def test_saldo_registrado_em_base_diferente_nao_converte_quantidade_da_nota(self):
        inicial = venda(996, 1, base='PCT', referencia_antes=D(40), referencia_baixa=D(12),
            referencia_depois=D(28), referencia_unidade='UN', referencia_habilitada=True)
        atual = venda(1049, 13)
        projetar([inicial, atual])
        self.assertEqual(inicial['saldo_registrado_operacional']['valor'], D(28))
        self.assertEqual(atual['saldo_calculado']['valor'], D(15))
        self.assertEqual(inicial['quantidade'], D(1))

    def test_classificacao_nota_atual_nao_invalida_referencia_independente(self):
        inicial = venda(996, 1, documento_atual=True, referencia_habilitada=True,
            referencia_antes=D(29), referencia_baixa=D(1), referencia_depois=D(28),
            referencia_unidade='UN')
        atual = venda(1049, 13)
        projetar([inicial, atual])
        self.assertEqual(atual['saldo_calculado']['valor'], D(15))
        inicial['referencia_habilitada'] = False
        projetar([inicial, atual])
        self.assertIsNone(atual['saldo_calculado'])

    def test_remocao_sem_devolucao_nao_interrompe_mas_desconhecido_sim(self):
        informativo = dict(pk=10, fonte='remocao', origem='removido', devolvido=False)
        linhas = [venda(996, 1, 28), informativo, venda(1049, 13)]
        projetar(linhas)
        self.assertEqual(linhas[-1]['saldo_calculado']['valor'], D(15))
        pagina = projetar(deepcopy(linhas), pagina=2, tamanho=2)
        self.assertEqual(pagina[0]['saldo_calculado']['valor'], D(15))
        linhas[1] = dict(pk=10, fonte='digital', origem='nao_identificada', antes=D(28), depois=D(28))
        projetar(linhas)
        self.assertIsNone(linhas[-1]['saldo_calculado'])

    def test_cancelada_so_e_neutra_com_snapshot_de_efeito_zero(self):
        cancelada = venda(10, 3, cancelada=True, devolvido=False,
            referencia_baixa=D(0), referencia_antes=D(28), referencia_depois=D(28))
        linhas = [venda(996, 1, 28), cancelada, venda(1049, 13)]
        projetar(linhas)
        self.assertEqual(linhas[-1]['saldo_calculado']['valor'], D(15))
        cancelada['referencia_baixa'] = None
        projetar(linhas)
        self.assertIsNone(linhas[-1]['saldo_calculado'])
