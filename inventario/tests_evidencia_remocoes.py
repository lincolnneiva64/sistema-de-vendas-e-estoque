"""Pure attribution checks: no settings, database, users or stock writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as Obj
import unittest

from .evidencia_remocoes import comprovar
from .evidencia_saldos import conflito_eventos


class AtribuicaoRemocoesTests(unittest.TestCase):
    def setUp(self):
        self.hora = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.registro = dict(id=10, venda_id=1, produto_id=3, produto_nome_snapshot='Oleo fictício',
            item_venda_original_id=30, quantidade_snapshot=D('2'), unidade_snapshot='UN',
            valor_total_snapshot=D('10'), status='removido', criado_em=self.hora,
            revertido_em=None, estoque_devolvido=False, estoque_devolvido_em=None, ajuste_origem_id=None)
        self.evento = dict(pk=20, tipo_evento='item_removido_da_nota', criado_em=self.hora+timedelta(seconds=1),
            descricao='Item removido da nota: Oleo fictício, quantidade 2.000 UN, valor abatido R$ 10.00. '
            'Registro de remocao #10. Total da venda: R$ 30.00 -> R$ 20.00. Sem devolucao de estoque para este item.')
        self.itens = [Obj(pk=1, produto_id=1, produto=Obj(nome='Alvo fictício')),
                      Obj(pk=2, produto_id=2, produto=Obj(nome='Ovo fictício'))]

    def avaliar(self, registros=None, eventos=None):
        return comprovar(1, 1, {'alvo fictício'}, self.itens,
            eventos if eventos is not None else [self.evento],
            registros if registros is not None else [self.registro])

    def test_identidade_explicita_corrobora_outro_produto_sem_mutar_entrada(self):
        original = deepcopy(self.registro)
        prova = self.avaliar()
        self.assertEqual(prova[0]['registro'], 10)
        self.assertEqual(conflito_eventos([self.evento], set(), remocoes_comprovadas={20}), '')
        self.assertEqual(self.registro, original)

    def test_proprio_produto_permanece_bloqueado(self):
        self.registro['produto_id'] = 1
        with self.assertRaises(ValueError): self.avaliar()

    def test_ausente_nome_divergente_id_ausente_e_dados_incompativeis(self):
        with self.assertRaises(ValueError): self.avaliar(registros=[])
        for campos in (dict(venda_id=2), dict(produto_id=None), dict(produto_nome_snapshot='Nome divergente'),
                dict(item_venda_original_id=None), dict(quantidade_snapshot=D(3)),
                dict(unidade_snapshot='PCT'), dict(valor_total_snapshot=D(11)),
                dict(status='revertido'), dict(revertido_em=self.hora),
                dict(estoque_devolvido=True), dict(ajuste_origem_id=8)):
            with self.subTest(campos=campos):
                with self.assertRaises(ValueError): self.avaliar(registros=[dict(self.registro, **campos)])
        self.evento['descricao'] = self.evento['descricao'].replace('Registro de remocao #10.', '')
        with self.assertRaises(ValueError): self.avaliar()

    def test_duplicidade_original_e_nome_ambiguo(self):
        for outro in (dict(self.registro, id=11),
                dict(self.registro, id=11, item_venda_original_id=31, produto_id=4)):
            with self.subTest(outro=outro):
                with self.assertRaises(ValueError): self.avaliar(registros=[self.registro, outro])

    def test_eventos_repetidos_nao_reutilizam_mesmo_registro(self):
        with self.assertRaises(ValueError): self.avaliar(eventos=[self.evento, dict(self.evento, pk=21)])

    def test_item_original_presente_ou_produto_reinserido_bloqueia(self):
        for item in (Obj(pk=30, produto_id=4, produto=Obj(nome='Outro')),
                     Obj(pk=31, produto_id=3, produto=Obj(nome='Oleo fictício'))):
            self.itens.append(item)
            with self.assertRaises(ValueError): self.avaliar()
            self.itens.pop()

    def test_evento_distante_ou_anterior_bloqueia(self):
        for delta in (-1, 6):
            self.evento['criado_em'] = self.hora+timedelta(seconds=delta)
            with self.assertRaises(ValueError): self.avaliar()

    def test_registro_nao_libera_edicao_ambigua_ou_do_alvo(self):
        prova = self.avaliar()
        for descricao in ('Quantidade alterada na nota: Alvo fictício. De 1 UN para 2 UN.',
                          'Sem identidade'):
            evento = dict(pk=21, tipo_evento='quantidade_item_alterada', descricao=descricao)
            self.assertTrue(conflito_eventos([self.evento, evento], {'ovo fictício'},
                remocoes_comprovadas={p['evento'] for p in prova}))
