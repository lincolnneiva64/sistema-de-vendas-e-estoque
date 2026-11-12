"""Read-only corroboration of observer rows with historical sale snapshots.

No current conversion is used. Unsupported origins remain blocked per product.
"""
import re
from datetime import timedelta

from django.db import DatabaseError, transaction
from estoque.models import (Venda, ItemVenda, EventoVenda, ItemVendaRemovido,
                            AjusteItemVendaQuitada, Compra, ItemCompra, EdicaoCompra)
from .models import MovimentoObservado, DetalheMovimentoEstoque
from .evidencia_saldos import conflito_eventos, snapshot, unidade
from .evidencia_remocoes import CAMPOS, comprovar
from .saldo_documental import quantidade_historica


def comprovar_cadeia(rodada, produto, movimentos, travar=False, *, _lote=None):
    """Same documentary criteria for review and application; never write stock."""
    from .marco_fisico import comprovar
    return comprovar(rodada, produto, movimentos, travar=travar, lote=_lote)


def comprovar_compra(rodada, m, compra_id, travar=False, *, _lote=None):
    if _lote is not None:
        if _lote.erro_documentos:
            raise ValueError(_lote.erro_documentos)
        compra = _lote.compras.get(compra_id)
        itens = _lote.itens_compra[compra_id]
        editada = compra_id in _lote.compras_editadas
        detalhe = _lote.detalhes_compra.get(m.pk)
        total = _lote.contagens.get((m.produto_id, m.referencia), 0)
    else:
        compras = Compra.objects.filter(pk=compra_id)
        recebidos = ItemCompra.objects.filter(compra_id=compra_id).order_by('pk')
        if travar:
            compras = compras.select_for_update(nowait=True)
            recebidos = recebidos.select_for_update(nowait=True)
        try:
            with transaction.atomic():
                compra, itens = compras.first(), list(recebidos)
        except DatabaseError:
            raise ValueError('Compra em alteração concorrente. Aguarde e prepare novamente.') from None
        editada = EdicaoCompra.objects.filter(compra_id=compra_id).exists()
        detalhe = DetalheMovimentoEstoque.objects.filter(movimento=m).first()
        total = MovimentoObservado.objects.filter(produto_id=m.produto_id,
            origem='compra', referencia=m.referencia).count()
    alvos = [i for i in itens if i.produto_id == m.produto_id]
    if (not compra or compra.cancelada or compra.cancelada_em or editada
            or compra.status == Compra.STATUS_CANCELADA
            or not compra.estoque_entrada_realizada or not compra.estoque_entrada_realizada_em):
        raise ValueError('Recebimento ausente, cancelado ou editado; confira o documento antes de ajustar.')
    if (len(alvos) != 1 or total != 1 or m.antes is None or m.depois is None
            or m.sequencia <= rodada.sequencia_ok or m.registrado_em < rodada.ok_em
            or compra.estoque_entrada_realizada_em < rodada.ok_em):
        raise ValueError('Recebimento sem vínculo histórico único após o OK; revise o movimento ou solicite recontagem.')
    item = alvos[0]
    base = unidade(rodada.item.cadastro['base'])
    quantidade = item.quantidade if unidade(item.unidade) == base else None
    if detalhe:
        if (detalhe.documento_tipo != 'compra' or detalhe.documento_id != str(compra.pk)
                or detalhe.item_referencia != str(item.pk) or detalhe.acao != 'entrada'
                or detalhe.quantidade_entrada != item.quantidade
                or unidade(detalhe.unidade_base) != base):
            raise ValueError('Atribuição da entrada diverge da compra; revise item, quantidade e unidade.')
        quantidade = quantidade_historica(detalhe, item.quantidade, item.unidade)
    if quantidade is None or quantidade <= 0 or m.depois - m.antes != quantidade:
        raise ValueError('Quantidade ou conversão histórica não comprovada; confirme a entrada documental ou solicite recontagem.')
    return dict(observador=m.pk, sequencia=m.sequencia, registrado_em=m.registrado_em.isoformat(),
        origem=m.origem, referencia=m.referencia, compra=compra.pk, item=item.pk,
        criado_em=compra.estoque_entrada_realizada_em.isoformat(), produto=m.produto_id,
        unidade=base, quantidade=str(item.quantidade), antes=str(m.antes), depois=str(m.depois),
        movimentado=str(quantidade),
        atribuicao=(dict(id=detalhe.pk, operacao=str(detalhe.operacao_id),
                        unidade_entrada=detalhe.unidade_entrada, conversao=detalhe.conversao,
                        fator=str(detalhe.fator)) if detalhe else None))


def conciliar(rodada, movimentos, travar=False, *, _lote=None):
    if _lote is not None:
        _lote.conferir(rodada, travar)
    evidencias = []
    for m in movimentos:
        try:
            compra = re.fullmatch(r'Compra #(\d+)', m.referencia or '')
            if m.origem == 'compra' and compra:
                evidencias.append(comprovar_compra(rodada, m, int(compra[1]), travar, _lote=_lote))
                continue
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
                remocoes = _lote.registros_removidos[venda.pk]
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
                        removidos = ItemVendaRemovido.objects.filter(venda=venda).order_by('pk')
                        if travar:
                            removidos = removidos.select_for_update(nowait=True)
                        remocoes = list(removidos.values(*CAMPOS))
                except DatabaseError:
                    raise ValueError('Documento em alteração concorrente; revise novamente antes de aprovar ou aplicar.')
            alvos = [i for i in itens if i.produto_id == m.produto_id]
            if len(alvos) != 1:
                raise ValueError(erro)
            i = alvos[0]
            if (((venda.pk, m.produto_id) in _lote.removidos or (venda.pk, m.produto_id) in _lote.ajustes)
                    if _lote is not None else (any(r['produto_id'] == m.produto_id for r in remocoes)
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
            from .remocoes_legadas import ACAO, LEGADO, mapa
            from .models import EventoInventario
            reconhecimentos = (_lote.reconhecimentos_legados[rodada.pk] if _lote is not None else
                list(EventoInventario.objects.filter(rodada=rodada, acao=ACAO))
                if any(LEGADO.fullmatch(e.get('descricao', '')) for e in eventos) else [])
            legados = mapa(rodada, reconhecimentos, eventos, remocoes, venda.pk)
            comprovadas = comprovar(venda.pk, m.produto_id, nomes_alvo, itens, eventos, remocoes, legados=legados)
            for prova in comprovadas:
                if prova['evento'] in legados:
                    prova['reconhecimento_administrativo'] = next(x.pk for x in reconhecimentos
                        if x.dados['reconhecimento']['evento']['pk'] == prova['evento'])
            conflito = conflito_eventos(eventos, outros,
                remocoes_comprovadas={r['evento'] for r in comprovadas})
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
            if comprovadas:
                evidencias[-1]['remocoes_outros_produtos'] = comprovadas
        except ValueError as exc:
            nome = rodada.item.cadastro.get('nome', '')
            raise ValueError(f'{nome} (produto #{m.produto_id}), movimento #{m.pk}, '
                             f'{m.referencia or m.origem}: {exc}') from exc
    return evidencias
