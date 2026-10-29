"""Display-only unit evidence. Never supplies balances or adjustment approval.

ItemVenda freezes the base amount/unit, not a conversion rule. No current factor,
ratio, product name or later inventory can turn that snapshot into a conversion.
"""
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from estoque.models import EventoVenda, ItemVenda, ItemVendaRemovido
from .evidencia_saldos import conflito_eventos, unidade
from .models import MovimentoObservado

LIMITE = 2000


def numero(valor):
    try:
        n = Decimal(str(valor))
        return n if n.is_finite() and abs(n) < Decimal('1000000000') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def snapshot_base(item):
    """Validate only what is actually stored, independently of document quantity.

    A valid result is a recorded base debit, NOT proof of the documentary
    conversion, historical continuity, original edited quantity or stock balance.
    """
    antes, baixa, depois = (numero(item.get(k)) for k in (
        'estoque_antes', 'estoque_movimentado', 'estoque_depois'))
    base = unidade(item.get('estoque_unidade_snapshot'))
    if not base or any(n is None for n in (antes, baixa, depois)):
        return None, 'Snapshot incompleto; baixa e unidade histórica não comprovadas.'
    if baixa <= 0 or min(antes, depois) < 0 or antes - baixa != depois:
        return None, 'Snapshot inconsistente; baixa histórica não comprovada.'
    return dict(quantidade=baixa, unidade=base), ''


def bruto(linha, motivo=''):
    antes, depois = numero(linha.get('antes')), numero(linha.get('depois'))
    return dict(tipo='bruto', variacao=depois-antes if antes is not None and depois is not None else None,
        aviso='Unidade histórica não comprovada', motivo=motivo)


def apresentar_unidades(pagina, produto_id):
    """Bounded bulk projection, without changing any balance/flow/evidence rules."""
    ids = {r['documento'] for r in pagina if r['fonte'] == 'venda'}
    for r in pagina:
        if r['fonte'] == 'digital' and not r.get('unidade_comprovada'):
            r['unidades_evidencia'] = bruto(r)
            match = re.fullmatch(r'Venda #(\d+)', r['referencia'])
            if match and r['origem'] == 'venda':
                ids.add(int(match[1]))
    if not ids:
        return
    itens = list(ItemVenda.objects.filter(venda_id__in=ids).order_by('pk').values(
        'pk', 'venda_id', 'produto_id', 'produto__nome', 'quantidade', 'unidade',
        'estoque_antes', 'estoque_movimentado', 'estoque_depois', 'estoque_unidade_snapshot',
        'venda__cancelada', 'venda__estoque_devolvido_cancelamento')[:LIMITE + 1])
    eventos = list(EventoVenda.objects.filter(venda_id__in=ids).order_by('pk').values(
        'venda_id', 'tipo_evento', 'descricao', 'criado_em')[:LIMITE + 1])
    removidos = list(ItemVendaRemovido.objects.filter(venda_id__in=ids).order_by('pk').values(
        'venda_id', 'produto_id', 'produto_nome_snapshot')[:LIMITE + 1])
    referencias = {f'Venda #{i}': i for i in ids}
    observados = list(MovimentoObservado.objects.filter(produto_id=produto_id,
        referencia__in=referencias, origem__in=('venda', 'devolucao_venda')).order_by('pk').values(
            'pk', 'referencia', 'antes', 'depois', 'origem')[:LIMITE + 1])
    truncado = any(len(lista) > LIMITE for lista in (itens, eventos, removidos, observados))
    por_venda, por_evento, por_movimento = defaultdict(list), defaultdict(list), defaultdict(list)
    outros, nomes_alvo = defaultdict(set), defaultdict(set)
    removidas = set()
    for i in itens:
        if i['produto_id'] == produto_id:
            por_venda[i['venda_id']].append(i)
            nomes_alvo[i['venda_id']].add((i['produto__nome'] or '').strip().casefold())
        else:
            outros[i['venda_id']].add((i['produto__nome'] or '').strip().casefold())
    for i in removidos:
        if i['produto_id'] == produto_id:
            removidas.add(i['venda_id'])
        else:
            outros[i['venda_id']].add(i['produto_nome_snapshot'].strip().casefold())
    for e in eventos:
        por_evento[e['venda_id']].append(e)
    for m in observados:
        por_movimento[referencias[m['referencia']]].append(m)
    for r in pagina:
        if r['fonte'] == 'venda':
            # Phase B's current-note row and proven edited movements are untouched.
            if r.get('documento_atual'):
                docs = por_venda[r['documento']]
                if len(docs) == 1 and unidade(docs[0]['unidade']) != unidade(docs[0]['estoque_unidade_snapshot']):
                    r['conversao_aviso'] = 'Conversão histórica não comprovada; snapshot atual não aplicado aos movimentos anteriores.'
                continue
            venda_id = r['documento']
        elif r['fonte'] == 'digital' and not r.get('unidade_comprovada'):
            match = re.fullmatch(r'Venda #(\d+)', r['referencia'])
            if not match or r['origem'] != 'venda':
                continue
            venda_id = int(match[1])
        else:
            continue
        docs, movimentos = por_venda[venda_id], por_movimento[venda_id]
        if truncado or len(docs) != 1 or venda_id in removidas:
            if r['fonte'] == 'digital':
                r['unidades_evidencia']['motivo'] = 'Evidência incompleta, limitada ou ambígua.'
            else:
                r['unidades_evidencia'] = dict(bruto(r), nota=r['quantidade'],
                    unidade_nota=r['unidade'], motivo='Evidência incompleta, limitada ou ambígua; baixa não comprovada.')
            continue
        item = docs[0]
        base, motivo = snapshot_base(item)
        conflito = conflito_eventos(por_evento[venda_id], outros[venda_id]-nomes_alvo[venda_id],
            aceitar_nota_impressa=True)
        seguro = not (conflito or item['venda__cancelada'] or
            item['venda__estoque_devolvido_cancelamento'] or len(movimentos) > 1)
        correspondente = bool(len(movimentos) == 1 and movimentos[0]['origem'] == 'venda'
            and movimentos[0]['antes'] == item['estoque_antes']
            and movimentos[0]['depois'] == item['estoque_depois'])
        if r['fonte'] == 'digital' and not correspondente:
            continue
        # Never attach an edited/current snapshot to an older observer by amount
        # or timestamp. Missing observers permit snapshot display on the document
        # only; they do not prove an observer identity or create a movement.
        if not seguro or (movimentos and not correspondente):
            base, motivo = None, 'Snapshot sem vínculo histórico seguro; não aplicado a este movimento.'
        diferente = (unidade(item['unidade']) != unidade(item['estoque_unidade_snapshot'])
            or item['quantidade'] != item['estoque_movimentado'])
        if not diferente and base:
            continue
        if base and unidade(item['unidade']) == unidade(item['estoque_unidade_snapshot']):
            motivo = 'O snapshot registra uma quantidade diferente da nota; nenhum dos valores foi alterado.'
        evidencia = dict(tipo='snapshot_base' if base else 'bruto', nota=item['quantidade'],
            unidade_nota=item['unidade'], baixa=base,
            conversao_comprovada=False,
            aviso='Conversão histórica não comprovada' if unidade(item['unidade']) !=
                unidade(item['estoque_unidade_snapshot']) else 'Quantidade documental e baixa devem ser conferidas separadamente',
            motivo=motivo or 'O snapshot registra a baixa na unidade-base, mas não grava o fator histórico.',
            variacao=bruto(r)['variacao'])
        r['unidades_evidencia'] = evidencia
