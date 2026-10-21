"""Read-only corroboration of observer rows with historical sale snapshots.

No current conversion is used. Unsupported origins remain blocked per product.
"""
import re
from datetime import timedelta

from django.db import DatabaseError, transaction
from estoque.models import (Venda, ItemVenda, EventoVenda, ItemVendaRemovido,
                            AjusteItemVendaQuitada)
from .models import MovimentoObservado
from .evidencia_saldos import conflito_eventos, snapshot, unidade


def conciliar(rodada, movimentos, travar=False, *, _lote=None):
    if _lote is not None:
        _lote.conferir(rodada, travar)
    evidencias = []
    for m in movimentos:
        erro = 'Movimento sem documento e unidade histórica corroborados; conciliação textual não basta.'
        match = re.fullmatch(r'Venda #(\d+)', m.referencia or '')
        if (m.origem != 'venda' or not match or m.sequencia <= rodada.sequencia_ok
                or m.registrado_em < rodada.ok_em):
            raise ValueError(erro)
        if _lote is not None:
            if _lote.erro_documentos:
                raise ValueError(_lote.erro_documentos)
            venda = _lote.vendas.get(int(match[1]))
            if not venda:
                raise ValueError(erro)
            itens = _lote.itens[venda.pk]
        else:
            vendas = Venda.objects.filter(pk=int(match[1]))
            if travar:
                vendas = vendas.select_for_update(nowait=True)
            try:
                with transaction.atomic():
                    venda = vendas.first()
                    if not venda:
                        raise ValueError(erro)
                    itens = ItemVenda.objects.filter(venda=venda).select_related('produto').order_by('pk')
                    if travar:
                        itens = itens.select_for_update(of=('self',), nowait=True)
                    itens = list(itens)
            except DatabaseError:
                raise ValueError('Documento em alteração concorrente; revise novamente antes de aprovar ou aplicar.')
        alvos = [i for i in itens if i.produto_id == m.produto_id]
        if len(alvos) != 1:
            raise ValueError(erro)
        i = alvos[0]
        if (((venda.pk, m.produto_id) in _lote.removidos or (venda.pk, m.produto_id) in _lote.ajustes)
                if _lote is not None else (ItemVendaRemovido.objects.filter(venda=venda, produto_id=m.produto_id).exists()
                or AjusteItemVendaQuitada.objects.filter(venda=venda, produto_id=m.produto_id).exists())):
            raise ValueError(erro)
        # Require the same observer identity to occur exactly once for this SKU.
        total_observador = (_lote.contagens.get((m.produto_id, m.referencia), 0) if _lote is not None
            else MovimentoObservado.objects.filter(produto_id=m.produto_id, origem='venda', referencia=m.referencia).count())
        if total_observador != 1:
            raise ValueError(erro)
        nomes_alvo = {str(rodada.item.cadastro.get('nome', '')).strip().casefold(),
                      str(rodada.item.produto.nome).strip().casefold()}
        outros = {str(x.produto.nome).strip().casefold() for x in itens
                  if x.produto_id and x.produto_id != m.produto_id} - nomes_alvo
        eventos = (_lote.eventos[venda.pk] if _lote is not None else list(EventoVenda.objects.filter(venda=venda).order_by('pk').values(
            'pk', 'tipo_evento', 'descricao', 'criado_em')[:501]))
        if len(eventos) > 500:
            raise ValueError(erro)
        conflito = conflito_eventos(eventos, outros)
        validado, motivo = snapshot(dict(cancelada=venda.cancelada,
            devolvido=venda.estoque_devolvido_cancelamento,
            antes=i.estoque_antes, depois=i.estoque_depois,
            movimentado=i.estoque_movimentado, base=i.estoque_unidade_snapshot,
            unidade=i.unidade, quantidade=i.quantidade), conflito)
        if (not validado or unidade(i.estoque_unidade_snapshot) != unidade(rodada.item.cadastro['base'])
                or venda.cancelada_em or venda.estoque_devolvido_cancelamento_em
                or (m.antes, m.depois) != (i.estoque_antes, i.estoque_depois)
                or abs(venda.criado_em - m.registrado_em) > timedelta(seconds=5)
                or venda.criado_em < rodada.ok_em):
            raise ValueError(motivo or erro)
        # Persist only material evidence: viewing/WhatsApp do not expire a proposal.
        from .evidencia_saldos import BENIGNOS
        evidencias.append(dict(observador=m.pk, sequencia=m.sequencia,
            registrado_em=m.registrado_em.isoformat(), origem=m.origem, referencia=m.referencia,
            venda=venda.pk, item=i.pk, criado_em=venda.criado_em.isoformat(),
            produto=m.produto_id, unidade=unidade(i.estoque_unidade_snapshot),
            quantidade=str(i.quantidade), antes=str(m.antes), depois=str(m.depois),
            movimentado=str(i.estoque_movimentado),
            eventos=[dict(pk=e['pk'], tipo=e['tipo_evento'], descricao=e['descricao'],
                         criado_em=e['criado_em'].isoformat())
                     for e in eventos if e['tipo_evento'] not in BENIGNOS]))
    return evidencias
