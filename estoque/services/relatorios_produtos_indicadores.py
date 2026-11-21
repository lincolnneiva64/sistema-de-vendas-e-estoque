"""Read-only evidence for quantities and frozen-cost estimates, not stock writes."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction

from django.db.models import CharField, Subquery
from django.db.models.functions import Cast

from inventario.models import DetalheMovimentoEstoque
from inventario.saldo_documental import provar_atribuicao, unidade, numero

ZERO = Decimal('0')
CENTAVO = Decimal('.01')
EMBALAGENS = {'DZ', 'PCT', 'CX', 'FD', 'SC', 'CB', 'MIL'}


def dinheiro_exato(valor):
    """HALF_EVEN in cents without rounding an intermediate conversion ratio."""
    centavos = valor * 100
    inteiro, resto = divmod(centavos.numerator, centavos.denominator)
    if resto * 2 > centavos.denominator or resto * 2 == centavos.denominator and inteiro % 2:
        inteiro += 1
    return Decimal(inteiro) / 100


def prova_item(item, detalhes):
    # Several events, edits, changed operands or a different stock unit are not
    # permission to attach an old factor to today's commercial line.
    if len(detalhes) != 1:
        return None
    d = detalhes[0]
    m = d.movimento
    if (d.documento_id != str(item['venda_id']) or d.item_referencia != str(item['pk'])
            or m.produto_id != item['produto_id'] or m.origem != 'venda' or d.acao != 'saida'
            or d.quantidade_entrada != item['quantidade']
            or unidade(d.unidade_entrada) != item['unidade_comercial']
            or unidade(d.unidade_base) != unidade(item['estoque_unidade_snapshot'])
            or m.antes != item['estoque_antes'] or m.depois != item['estoque_depois']):
        return None
    p = provar_atribuicao(d)
    if p is None:
        return None
    return dict(detalhe=d, prova=p)


def quantidade_fisica(itens, provas):
    unidades = {i['unidade_comercial'] for i in itens}
    pacotes = unidades - {'UN'}
    if len(pacotes) != 1 or not pacotes.issubset(EMBALAGENS):
        return None
    embalagem = next(iter(pacotes))
    fator = None
    total = Fraction(0)
    for item in itens:
        q = numero(item['quantidade'])
        if q is None or q < 0:
            return None
        if item['unidade_comercial'] == 'UN':
            total += Fraction(q)
            continue
        p = provas.get(item['pk'])
        if p is None:
            return None
        d = p['detalhe']
        # Require a frozen direct relation to physical UN on every packaging
        # item. A stock debit in CX/DZ/PCT alone does not prove pack capacity.
        if (unidade(d.unidade_base) != 'UN' or d.conversao != 'multiplicacao'
                or d.fator is None or d.fator <= 1 or d.fator != d.fator.to_integral_value()):
            return None
        if fator is not None and fator != int(d.fator):
            return None
        fator = int(d.fator)
        total += Fraction(q) * fator
    if fator is None or total.denominator != 1:
        return None
    inteiras, avulsas = divmod(total.numerator, fator)
    # Only the displayed decimal equivalent is rounded; integer physical
    # quantities and their total never pass through a rounded decimal ratio.
    equivalente = (Decimal(total.numerator) / Decimal(fator)).quantize(CENTAVO, rounding=ROUND_HALF_EVEN)
    return dict(embalagens=inteiras, unidade=embalagem, avulsas=avulsas,
                total_un=total.numerator, equivalente=equivalente, fator=fator)


def custo_item(item, prova):
    q = numero(item['quantidade'])
    unitario, total = numero(item['custo_unitario_snapshot']), numero(item['custo_total_snapshot'])
    custo_unidade = unidade(item['custo_unidade_snapshot'])
    if (q is None or q <= 0 or unitario is None or unitario <= 0 or total is None or total < 0
            or not custo_unidade or not item['unidade_comercial']
            or item['custo_origem_snapshot'] != 'preco_compra_atual'):
        return None, 'Snapshot de custo ausente, incompleto ou sem origem reconhecida.'
    if custo_unidade == item['unidade_comercial']:
        quantidade_custeada = Fraction(q)
    elif prova is not None and unidade(prova['detalhe'].unidade_base) == custo_unidade:
        quantidade_custeada = abs(prova['prova']['exato'])
    else:
        return None, 'Unidade de custo diferente da comercial, sem conversão histórica comprovada no item.'
    if dinheiro_exato(quantidade_custeada * Fraction(unitario)) != total:
        return None, 'Custo congelado incompatível com a quantidade atual e a evidência disponível.'
    return total, ''


def indicadores_produto(itens_periodo, *, unidade_recorte=None):
    """One product/period only; lean rows and one bulk immutable-evidence query."""
    itens = list(itens_periodo.values('pk', 'produto_id', 'venda_id', 'unidade_comercial',
        'quantidade', 'valor_total', 'estoque_antes', 'estoque_depois', 'estoque_unidade_snapshot',
        'custo_unitario_snapshot', 'custo_total_snapshot', 'custo_unidade_snapshot', 'custo_origem_snapshot'))
    por_item = defaultdict(list)
    if itens:
        ids = itens_periodo.annotate(_item_texto=Cast('pk', CharField())).values('_item_texto')
        detalhes = DetalheMovimentoEstoque.objects.filter(documento_tipo='venda',
            item_referencia__in=Subquery(ids), movimento__produto_id=itens[0]['produto_id']).select_related('movimento')
        for d in detalhes:
            por_item[d.item_referencia].append(d)
    provas = {i['pk']: prova_item(i, por_item[str(i['pk'])]) for i in itens}
    grupos = {}
    motivos = defaultdict(int)
    for i in itens:
        u = i['unidade_comercial']
        g = grupos.setdefault(u, dict(unidade=u, quantidade=ZERO, faturamento=ZERO,
            custo_conhecido=ZERO, itens=0, com_custo=0))
        g['quantidade'] += i['quantidade']
        g['faturamento'] += i['valor_total']
        g['itens'] += 1
        custo, motivo = custo_item(i, provas[i['pk']])
        if custo is not None:
            g['custo_conhecido'] += custo
            g['com_custo'] += 1
        elif unidade_recorte is None or unidade_recorte == u:
            motivos[motivo] += 1
    quantidades = [dict(unidade_comercial=u, quantidade=g['quantidade'] if u else None)
                   for u, g in sorted(grupos.items())]
    linhas = []
    for u, g in sorted(grupos.items()):
        if unidade_recorte is not None and u != unidade_recorte:
            continue
        completa = g['com_custo'] == g['itens']
        comparavel = bool(u) and g['quantidade'] > 0
        g.update(preco_medio=(g['faturamento']/g['quantidade']).quantize(CENTAVO) if comparavel else None,
            custo_medio=(g['custo_conhecido']/g['quantidade']).quantize(CENTAVO) if completa and comparavel else None)
        linhas.append(g)
    total_itens = sum(g['itens'] for g in linhas)
    cobertos = sum(g['com_custo'] for g in linhas)
    receita = sum((g['faturamento'] for g in linhas), ZERO)
    conhecido = sum((g['custo_conhecido'] for g in linhas), ZERO)
    disponivel = total_itens > 0 and total_itens == cobertos
    lucro = receita-conhecido if disponivel else None
    return dict(quantidades_vendidas=quantidades,
        fisica=quantidade_fisica(itens, provas), linhas=linhas,
        itens=total_itens, com_custo=cobertos, motivos=dict(motivos),
        cmv=conhecido if disponivel else None, custo_conhecido=conhecido,
        lucro=lucro, margem=(lucro/receita*100).quantize(CENTAVO) if lucro is not None and receita > 0 else None,
        unidade_recorte=unidade_recorte)
