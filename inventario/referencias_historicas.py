"""Read-only physical receipt anchors, loaded in batches for chronology pages."""
from decimal import InvalidOperation
from uuid import UUID

from django.urls import reverse

from .marco_fisico import normalizar_recibo
from .models import AplicacaoAjuste, EventoAjuste


def limite_anterior(produto_id, instante):
    """Bound a prefix by its latest persisted application, using two bounded queries.

    The actual receipt still has to pass validation. An invalid candidate is a
    gap, never permission to carry a still older balance through it.
    """
    ajuste = AplicacaoAjuste.objects.filter(proposta__rodada__item__produto_id=produto_id,
        proposta__estado='aplicado', aplicado_em__lt=instante).order_by('-aplicado_em').values_list(
            'aplicado_em', flat=True).first()
    zero = EventoAjuste.objects.filter(proposta__rodada__item__produto_id=produto_id,
        proposta__estado='aplicado', acao='marco_sem_ajuste', criado_em__lt=instante).order_by(
            '-criado_em').values_list('criado_em', flat=True).first()
    return max((t for t in (ajuste, zero) if t is not None), default=None)


def preparar(linhas, produto_id):
    linhas = [r for r in linhas if r['fonte'] in ('regularizacao', 'referencia_fisica')]
    if not linhas:
        return
    aplicacoes = {a.decisao.movimentacao_id: a for a in AplicacaoAjuste.objects.filter(
        decisao__movimentacao_id__in=[r['pk'] for r in linhas if r['fonte'] == 'regularizacao'],
        proposta__rodada__item__produto_id=produto_id).select_related(
            'proposta__confirmacao', 'proposta__rodada__item', 'decisao__movimentacao')}
    eventos = {str(e.proposta_id): e for e in EventoAjuste.objects.filter(
        proposta_id__in=[r['referencia'] for r in linhas if r['fonte'] == 'referencia_fisica'],
        proposta__rodada__item__produto_id=produto_id, acao='marco_sem_ajuste').select_related(
            'proposta__confirmacao', 'proposta__rodada__item', 'usuario')}
    for r in linhas:
        a = aplicacoes.get(r['pk']) if r['fonte'] == 'regularizacao' else None
        # UUID SQL casts differ on SQLite and PostgreSQL; normalize both forms.
        e = eventos.get(str(UUID(r['referencia']))) if r['fonte'] == 'referencia_fisica' else None
        try:
            p = a.proposta if a else e.proposta if e else None
            recibo = normalizar_recibo(p, a, e) if p else None
            if (not recibo or recibo.aplicado_em != r['instante']
                    or not recibo.unidade.strip() or not recibo.depois.is_finite()
                    or recibo.depois < 0):
                raise ValueError('Recibo incompatível com a cronologia.')
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            r.update(referencia_fisica_invalida=True, saldo_certificado=None,
                     saldo=None, saldo_unidade='', saldo_tipo='Saldo indisponível',
                     saldo_aviso='Referência aplicada sem recibo compatível; continuidade indisponível.')
            continue
        inv = p.rodada.item.inventario_id
        r.update(saldo_certificado=dict(valor=recibo.depois, unidade=recibo.unidade,
            origem=f'Referência física aplicada · Conferência nº {inv}',
            instante=recibo.aplicado_em, proposta=str(p.pk)),
            titulo=f'Referência física aplicada — Conferência nº {inv}',
            classe='manual', saldo=recibo.depois, saldo_antes=recibo.antes,
            saldo_unidade=recibo.unidade, saldo_tipo='Saldo certificado por referência física',
            saldo_evidencia=f'Recibo {recibo.pk} · Proposta {p.pk}',
            saldo_aviso='Saldo certificado neste instante; movimentações posteriores alteram o estoque.',
            url=reverse('inventario:historico_produto', args=[inv, p.rodada_id]),
            diferenca_aplicada=recibo.diferenca, unidade=recibo.unidade)
        if a:
            r.update(aplicacao_registrada=a, movimentacao_em=a.decisao.movimentacao.criado_em,
                nota=f'Ajuste manual #{a.decisao.movimentacao_id} · Aplicação {a.pk} · Lote {a.lote}',
                url=reverse('inventario:movimento_manual', args=[inv, p.rodada_id, a.decisao.movimentacao_id]))
