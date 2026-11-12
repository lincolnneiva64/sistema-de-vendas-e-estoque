"""Request-local evidence loaded in batches. No stock writes or shared cache."""
from collections import defaultdict
from contextlib import contextmanager, nullcontext
import re

from django.db import DatabaseError, connection, transaction
from django.db.models import Count, F, Q, Window
from django.db.models.functions import RowNumber

from estoque.models import (Venda, ItemVenda, EventoVenda, ItemVendaRemovido, AjusteItemVendaQuitada,
                            Compra, ItemCompra, EdicaoCompra)
from .models import (ConfirmacaoContagem, MovimentoObservado, RodadaContagem,
                     DecisaoAjuste, PropostaAjuste, ConciliacaoMovimento, SequenciaEstoque,
                     DetalheMovimentoEstoque, EventoInventario)
from .selecao import itens_incluidos
from .evidencia_remocoes import CAMPOS


@contextmanager
def carregar(rodadas, *, travar=False, ate=None):
    dados = _Lote(rodadas, travar, ate=ate)
    try:
        yield dados
    finally:
        dados.ativo = False


class _Lote:
    def __init__(self, rodadas, travar, ate=None):
        if travar and not connection.in_atomic_block:
            raise ValueError('Validação do lote exige a transação e os locks existentes.')
        self.ativo, self.travar = True, travar
        self.rodadas = {r.pk: r for r in rodadas}
        ids = list(self.rodadas)
        itens = [r.item_id for r in rodadas]
        produtos = [r.item.produto_id for r in rodadas]
        self.confirmacoes = defaultdict(list)
        for c in ConfirmacaoContagem.objects.filter(rodada_id__in=ids).order_by('versao_entrada'):
            self.confirmacoes[c.rodada_id].append(c)
        administrativas = {c.rodada_id for confs in self.confirmacoes.values() for c in confs if c.acesso_id is None}
        self.confirmacoes_admin = {e.dados.get('confirmacao_id'): e for e in EventoInventario.objects.filter(
            rodada_id__in=administrativas, acao='contagem_admin_confirmada')}
        filtro = Q(pk__in=[])
        for r in rodadas:
            confs = self.confirmacoes[r.pk]
            corte = r.sequencia_ok
            if corte is not None:
                filtro |= Q(produto_id=r.item.produto_id, sequencia__gt=corte)
        self.movimentos = defaultdict(list)
        movimentos = MovimentoObservado.objects.filter(filtro)
        if ate is not None:
            movimentos = movimentos.filter(registrado_em__lte=ate)
        for m in movimentos.order_by('sequencia'):
            self.movimentos[m.produto_id].append(m)
        self.incluidos = set(itens_incluidos().filter(pk__in=itens).values_list('pk', flat=True))
        self.decisoes = set(DecisaoAjuste.objects.filter(item_id__in=itens).values_list('item_id', flat=True))
        self.propostas = {(p.rodada_id, p.assinatura): p for p in PropostaAjuste.objects.filter(rodada_id__in=ids)}
        self.sinais, self.maior_numero, self.pendentes = defaultdict(list), {}, set()
        for r in RodadaContagem.objects.filter(item_id__in=itens).order_by('numero', 'sinalizacoes__pk').values(
                'item_id', 'numero', 'sinalizacoes__pk', 'sinalizacoes__evidencia', 'sinalizacoes__resolvida_em'):
            item = r['item_id']
            self.maior_numero[item] = max(self.maior_numero.get(item, 0), r['numero'])
            self.sinais[item].append((r['sinalizacoes__pk'], r['sinalizacoes__evidencia']))
            if r['sinalizacoes__pk'] is not None and r['sinalizacoes__resolvida_em'] is None:
                self.pendentes.add(item)
        self.conciliados = defaultdict(set)
        for c in ConciliacaoMovimento.objects.filter(rodada_id__in=ids).values('rodada_id','movimento_id','conciliado_em'):
            r = self.rodadas[c['rodada_id']]
            if r.ok_em and c['conciliado_em'] >= r.ok_em:
                self.conciliados[r.pk].add(c['movimento_id'])
        self.sequencias = dict(SequenciaEstoque.objects.filter(produto_id__in=produtos).values_list('produto_id','sequencia'))
        from .services import verificar_observador
        self.erro_observador = ''
        try:
            verificar_observador()
        except ValueError as exc:
            self.erro_observador = str(exc)
        self._documentos(produtos)

    def conferir(self, rodada, travar=None):
        if (not self.ativo or self.rodadas.get(rodada.pk) is not rodada
                or (travar is not None and travar != self.travar)
                or (self.travar and not connection.in_atomic_block)):
            raise ValueError('Contexto do lote indisponível para esta validação.')

    def _documentos(self, produtos):
        referencias = set()
        vendas_ids = set()
        compras_ids = set()
        for ms in self.movimentos.values():
            for m in ms:
                match = re.fullmatch(r'Venda #(\d+)', m.referencia or '')
                if m.origem == 'venda' and match:
                    vendas_ids.add(int(match[1])); referencias.add(m.referencia)
                compra = re.fullmatch(r'Compra #(\d+)', m.referencia or '')
                if m.origem == 'compra' and compra:
                    compras_ids.add(int(compra[1])); referencias.add(m.referencia)
        vendas = Venda.objects.filter(pk__in=vendas_ids).order_by('pk')
        itens = ItemVenda.objects.filter(venda_id__in=vendas_ids).select_related('produto').order_by('pk')
        if self.travar:
            vendas = vendas.select_for_update(nowait=True)
            itens = itens.select_for_update(of=('self',), nowait=True)
        self.itens = defaultdict(list)
        self.registros_removidos = defaultdict(list)
        self.erro_documentos = ''
        self.compras, self.itens_compra, self.detalhes_compra = {}, defaultdict(list), {}
        self.compras_editadas = set()
        try:
            # Keep the savepoint so a NOWAIT error cannot poison the caller's transaction.
            with transaction.atomic() if self.travar else nullcontext():
                self.vendas = {v.pk: v for v in vendas}
                if compras_ids:
                    compras = Compra.objects.filter(pk__in=compras_ids).order_by('pk')
                    recebidos = ItemCompra.objects.filter(compra_id__in=compras_ids).order_by('pk')
                    if self.travar:
                        compras = compras.select_for_update(nowait=True)
                        recebidos = recebidos.select_for_update(nowait=True)
                    self.compras = {c.pk: c for c in compras}
                    for recebido in recebidos:
                        self.itens_compra[recebido.compra_id].append(recebido)
                    self.compras_editadas = set(EdicaoCompra.objects.filter(
                        compra_id__in=compras_ids).values_list('compra_id', flat=True))
                    self.detalhes_compra = {d.movimento_id: d for d in DetalheMovimentoEstoque.objects.filter(
                        movimento__produto_id__in=produtos, movimento__origem='compra',
                        movimento__referencia__in=referencias)}
                for item in itens:
                    self.itens[item.venda_id].append(item)
                removidos = ItemVendaRemovido.objects.filter(venda_id__in=vendas_ids).order_by('pk')
                if self.travar:
                    removidos = removidos.select_for_update(nowait=True)
                for registro in removidos.values(*CAMPOS):
                    self.registros_removidos[registro['venda_id']].append(registro)
        except DatabaseError:
            self.erro_documentos = 'Documento em alteração concorrente; revise novamente antes de aprovar ou aplicar.'
            self.vendas = {}
        self.removidos = {(venda, r['produto_id']) for venda, registros in self.registros_removidos.items()
                         for r in registros}
        self.ajustes = set(AjusteItemVendaQuitada.objects.filter(venda_id__in=vendas_ids,
            produto_id__in=produtos).values_list('venda_id','produto_id'))
        self.contagens = {(r['produto_id'],r['referencia']):r['total'] for r in MovimentoObservado.objects.filter(
            produto_id__in=produtos, origem__in=('venda', 'compra'), referencia__in=referencias).values('produto_id','referencia').annotate(total=Count('pk'))}
        self.eventos = defaultdict(list)
        # Same 501-row cutoff PER sale as the individual validator, not a global limit.
        eventos = EventoVenda.objects.filter(venda_id__in=vendas_ids).annotate(
            posicao=Window(expression=RowNumber(), partition_by=[F('venda_id')], order_by=F('pk').asc())
        ).filter(posicao__lte=501).order_by('pk').values('venda_id','pk','tipo_evento','descricao','criado_em')
        for evento in eventos:
            venda = evento.pop('venda_id')
            self.eventos[venda].append(evento)
        from .remocoes_legadas import carregar as carregar_reconhecimentos
        carregar_reconhecimentos(self)
