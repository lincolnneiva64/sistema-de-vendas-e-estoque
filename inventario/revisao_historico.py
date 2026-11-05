"""Read-only evidence for the management table; never authorizes stock writes."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from . import services
from .ajustes import assinatura
from .conciliacao_documental import conciliar
from .models import AplicacaoAjuste, EventoAjuste, EventoInventario


def carregar(lote):
    """Fixed query count for all applications in this request, only if needed."""
    lote.aplicacoes_revisao = {}
    lote.eventos_aplicacao = defaultdict(list)
    lote.eventos_inventario_aplicacao = defaultdict(list)
    if not any(m.origem == 'ajuste_manual' for ms in lote.movimentos.values() for m in ms):
        return
    aplicacoes = list(AplicacaoAjuste.objects.filter(
        decisao__item_id__in=lote.incluidos).select_related(
            'decisao__movimentacao', 'proposta__confirmacao'))
    for a in aplicacoes:
        lote.aplicacoes_revisao[f'Movimentação manual #{a.decisao.movimentacao_id}'] = a
    for e in EventoAjuste.objects.filter(proposta_id__in=[a.proposta_id for a in aplicacoes], acao='aplicado'):
        lote.eventos_aplicacao[e.proposta_id].append(e)
    for e in EventoInventario.objects.filter(rodada_id__in=lote.rodadas, acao='ajuste_aplicado'):
        lote.eventos_inventario_aplicacao[e.rodada_id].append(e)


def validar_aplicacao(rodada, movimento, aplicacao, lote):
    """Cross-check the signed preparation, immutable receipt and both journals."""
    erro = 'Ajuste da conferência com auditoria incompleta ou incompatível. Solicite revisão técnica.'
    a, r, m = aplicacao, rodada, movimento
    p, d = a.proposta, a.decisao
    h, c, e = d.movimentacao, p.confirmacao, p.evidencia
    try:
        valido = (
            d.item_id == r.item_id and d.rodada_id == r.pk == p.rodada_id == c.rodada_id
            and p.estado == 'aplicado' and assinatura(e) == p.assinatura
            and a.evidencia['preparacao'] == e
            and a.evidencia['autorizado_por'] == p.autorizado_por_id
            and a.evidencia['justificativa'] == p.justificativa == d.justificativa == h.motivo
            and e['inventario'] == r.item.inventario_id and e['item'] == r.item_id
            and e['produto'] == h.produto_id == m.produto_id == r.item.produto_id
            and e['rodada'] == r.pk and e['confirmacao'] == c.pk
            and e['ok_em'] == c.ok_em.isoformat()
            and Decimal(e['esperado']) == c.esperado
            and Decimal(e['contado']) == c.contado_base
            and all(getattr(r, campo) == getattr(c, campo) for campo in (
                'ok_em', 'esperado', 'contado_base', 'contado', 'sequencia_ok', 'embalagens', 'avulsos'))
            and e['cadastro'] == r.item.cadastro
            and a.antes == h.estoque_antes == m.antes == Decimal(e['atual'])
            and a.depois == h.estoque_depois == m.depois == Decimal(e['resultante'])
            and a.diferenca == h.diferenca == d.diferenca == Decimal(e['diferenca'])
            and a.diferenca == c.contado_base - c.esperado
            and a.antes + a.diferenca == a.depois
            and a.unidade == r.item.cadastro['base'] and h.tipo == h.TIPO_AJUSTE
            and a.usuario_id == d.aprovado_por_id == h.usuario_id
            and m.origem == 'ajuste_manual' and m.referencia == f'Movimentação manual #{h.pk}'
            and m.sequencia == e['sequencia'] + 1
            and m.registrado_em >= c.ok_em
            and h.observacao.endswith(f'Conferência {r.item.inventario_id}; proposta {p.pk}; lote {a.lote}')
        )
        eventos = lote.eventos_aplicacao[p.pk]
        eventos_inv = [x for x in lote.eventos_inventario_aplicacao[r.pk]
                       if x.dados.get('operacao') == str(a.pk)]
        esperado = dict(operacao=str(a.pk), lote=str(a.lote), antes=str(a.antes),
                        diferenca=str(a.diferenca), depois=str(a.depois))
        valido = valido and len(eventos) == 1 and eventos[0].dados == esperado
        valido = valido and eventos[0].usuario_id == a.usuario_id
        valido = valido and len(eventos_inv) == 1 and eventos_inv[0].inventario_id == r.item.inventario_id
        valido = valido and eventos_inv[0].usuario_id == a.usuario_id
        valido = valido and eventos_inv[0].dados == dict(decisao=d.pk, operacao=str(a.pk),
                                                       lote=str(a.lote), diferenca=str(a.diferenca))
    except (KeyError, TypeError, ValueError, InvalidOperation, IndexError, AttributeError):
        valido = False
    if not valido:
        raise ValueError(erro)


def validar(rodada, produto, lote):
    lote.conferir(rodada)
    movimentos = lote.movimentos[rodada.item.produto_id]
    vendas = [m for m in movimentos if m.origem == 'venda']
    documentos = conciliar(rodada, vendas, travar=lote.travar, _lote=lote) if vendas else []
    comprovados = {d['observador'] for d in documentos}
    # An audit record alone never skips subsequent movements or chain checks.
    for m in movimentos:
        if m.origem != 'ajuste_manual':
            continue
        aplicacao = lote.aplicacoes_revisao.get(m.referencia)
        if aplicacao:
            validar_aplicacao(rodada, m, aplicacao, lote)
            comprovados.add(m.pk)
    sequencia = services.validar_cadeia(rodada, produto, documentos=comprovados, _lote=lote)
    return sequencia, comprovados
